"""编译器：AST 结构校验、列引用解析、类型推导、计划序列化。

输出的计划是自描述 JSON（可真实保存到磁盘），执行器只认计划文件而不
重新接触 AST。计划内嵌 schema 指纹，执行时与实际批次的 schema 摘要
不一致即拒绝（换 schema 不能直接执行旧计划）。
"""

import hashlib
import json
from dataclasses import dataclass, field

from .decimals import parse_decimal_text, fits
from .errors import CompileError
from .types import DecimalType, derive_binary, MAX_PRECISION

PLAN_VERSION = 1
CMP_OPS = {"lt", "le", "eq", "ne", "ge", "gt"}
ARITH_OPS = {"add", "sub", "mul", "div"}


@dataclass
class PlanNode:
    id: int
    op: str
    type: object                      # DecimalType 或字符串 "boolean"
    children: list = field(default_factory=list)
    name: str = None                  # column: 列名
    value: str = None                 # const: 十进制文本（None 表示 NULL）
    cmp: str = None                   # cmp: 比较算子
    to: DecimalType = None            # cast: 目标类型
    when_children: list = field(default_factory=list)   # case: [[cond,then],...]
    else_child: int = None            # case: else 节点 id（可空）

    def to_dict(self):
        d = {"id": self.id, "op": self.op,
             "type": ("boolean" if self.type == "boolean"
                      else [self.type.precision, self.type.scale]),
             "children": self.children}
        if self.name is not None:
            d["name"] = self.name
        if self.value is not None:
            d["value"] = self.value
        if self.cmp is not None:
            d["cmp"] = self.cmp
        if self.to is not None:
            d["to"] = [self.to.precision, self.to.scale]
        if self.when_children:
            d["when"] = self.when_children
        if self.else_child is not None:
            d["else"] = self.else_child
        return d

    @classmethod
    def from_dict(cls, d):
        t = ("boolean" if d["type"] == "boolean"
             else DecimalType(d["type"][0], d["type"][1]))
        return cls(
            id=d["id"], op=d["op"], type=t, children=list(d["children"]),
            name=d.get("name"), value=d.get("value"), cmp=d.get("cmp"),
            to=(DecimalType(d["to"][0], d["to"][1]) if "to" in d else None),
            when_children=[list(pair) for pair in d.get("when", [])],
            else_child=d.get("else"),
        )


@dataclass
class Plan:
    nodes: dict                       # id -> PlanNode
    root_id: int
    schema: dict                      # name -> [p, s]
    schema_fingerprint: str
    result_type: object

    def to_dict(self):
        return {
            "version": PLAN_VERSION,
            "schema": self.schema,
            "schema_fingerprint": self.schema_fingerprint,
            "root_id": self.root_id,
            "result_type": ("boolean" if self.result_type == "boolean"
                            else [self.result_type.precision,
                                  self.result_type.scale]),
            "nodes": [self.nodes[i].to_dict()
                      for i in sorted(self.nodes)],
        }

    def save(self, path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") != PLAN_VERSION:
            raise CompileError(
                f"计划版本 {data.get('version')} 与当前版本 {PLAN_VERSION} 不兼容")
        nodes = {d["id"]: PlanNode.from_dict(d) for d in data["nodes"]}
        rt = ("boolean" if data["result_type"] == "boolean"
              else DecimalType(*data["result_type"]))
        return cls(nodes=nodes, root_id=data["root_id"],
                   schema=data["schema"],
                   schema_fingerprint=data["schema_fingerprint"],
                   result_type=rt)


def fingerprint_schema(schema: dict) -> str:
    """schema 摘要：列名与 decimal(p,s) 声明的规范化哈希。

    声明以 (p,s) 或 [p,s] 给出都归一为 [p,s]，保证编译期与批次期
    （计划内保存为 list）算出同一摘要。
    """
    canonical = json.dumps(
        {name: [int(decl[0]), int(decl[1])]
         for name, decl in sorted(schema.items())},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _as_type(decl, node_desc):
    if (not isinstance(decl, list) or len(decl) != 2
            or not all(isinstance(x, int) for x in decl)):
        raise CompileError(f"{node_desc} 的类型必须是 [precision, scale]")
    return DecimalType(decl[0], decl[1])


class _Compiler:
    def __init__(self, schema_types):
        self.schema_types = schema_types
        self.nodes = {}
        self._seq = 0

    def _new_id(self):
        nid = self._seq
        self._seq += 1
        return nid

    def compile(self, ast):
        root_type, root_id = self._visit(ast)
        return Plan(
            nodes=self.nodes, root_id=root_id,
            schema={name: [t.precision, t.scale]
                    for name, t in self.schema_types.items()},
            schema_fingerprint=fingerprint_schema(
                {name: (t.precision, t.scale)
                 for name, t in self.schema_types.items()}),
            result_type=root_type,
        )

    def _visit(self, ast):
        if not isinstance(ast, dict) or "op" not in ast:
            raise CompileError("AST 节点必须是带 op 的对象")
        op = ast["op"]
        nid = self._new_id()

        if op == "column":
            name = ast.get("name")
            if name not in self.schema_types:
                raise CompileError(f"未知列引用: {name!r}", node_id=nid)
            t = self.schema_types[name]
            self.nodes[nid] = PlanNode(nid, op, t, name=name)
            return t, nid

        if op == "const":
            t = _as_type(ast.get("type"), "常量")
            raw = ast.get("value", None)
            if raw is None:
                self.nodes[nid] = PlanNode(nid, op, t, value=None)
                return t, nid
            if not isinstance(raw, str):
                raise CompileError("常量值必须是十进制文本或 null", node_id=nid)
            value = parse_decimal_text(raw, node_id=nid)
            if not fits(value, t):
                # 常量是计划的一部分，不可表示属于类型/计划错误，整计划拒绝。
                raise CompileError(
                    f"常量 {raw!r} 不能由 {t} 精确表示（不做隐式舍入）",
                    node_id=nid)
            self.nodes[nid] = PlanNode(nid, op, t, value=raw)
            return t, nid

        if op in ARITH_OPS:
            lt, left = self._visit(ast["left"])
            rt, right = self._visit(ast["right"])
            if not (isinstance(lt, DecimalType) and isinstance(rt, DecimalType)):
                raise CompileError(f"{op} 的操作数必须是 decimal 类型",
                                   node_id=nid)
            out = derive_binary(op, lt, rt, node_id=nid)
            self.nodes[nid] = PlanNode(nid, op, out, children=[left, right])
            return out, nid

        if op == "cmp":
            cmp_op = ast.get("cmp")
            if cmp_op not in CMP_OPS:
                raise CompileError(f"非法比较算子: {cmp_op!r}", node_id=nid)
            lt, left = self._visit(ast["left"])
            rt, right = self._visit(ast["right"])
            if not (isinstance(lt, DecimalType) and isinstance(rt, DecimalType)):
                raise CompileError("比较的操作数必须是 decimal 类型",
                                   node_id=nid)
            self.nodes[nid] = PlanNode(nid, op, "boolean",
                                       children=[left, right], cmp=cmp_op)
            return "boolean", nid

        if op == "cast":
            target = _as_type(ast.get("to"), "CAST 目标")
            arg_t, arg = self._visit(ast["arg"])
            if not isinstance(arg_t, DecimalType):
                raise CompileError("CAST 只支持 decimal 间转换", node_id=nid)
            self.nodes[nid] = PlanNode(nid, op, target, children=[arg],
                                       to=target)
            return target, nid

        if op == "case":
            return self._visit_case(ast, nid)

        raise CompileError(f"未知 AST 算子: {op!r}", node_id=nid)

    def _visit_case(self, ast, nid):
        whens = ast.get("when")
        if not isinstance(whens, list) or len(whens) == 0:
            raise CompileError("CASE 至少需要一个 WHEN 分支", node_id=nid)

        cond_ids, then_ids, then_types = [], [], []
        for branch in whens:
            if not isinstance(branch, dict) or "cond" not in branch \
                    or "then" not in branch:
                raise CompileError("WHEN 分支必须含 cond 与 then",
                                   node_id=nid)
            cond_t, cond_id = self._visit(branch["cond"])
            if cond_t != "boolean":
                raise CompileError("CASE 条件必须是比较表达式（boolean）",
                                   node_id=nid)
            then_ast = branch["then"]
            if not isinstance(then_ast, dict) or then_ast.get("op") != "cast":
                raise CompileError(
                    "CASE 各 THEN 分支必须显式 CAST 到统一结果类型",
                    node_id=nid)
            then_t, then_id = self._visit(then_ast)
            cond_ids.append(cond_id)
            then_ids.append(then_id)
            then_types.append(then_t)

        result_type = then_types[0]
        for t in then_types[1:]:
            if t != result_type:
                raise CompileError(
                    "CASE 各分支 CAST 的目标类型必须完全相同", node_id=nid)

        else_id = None
        if ast.get("else") is not None:
            else_ast = ast["else"]
            if not isinstance(else_ast, dict) or else_ast.get("op") != "cast":
                raise CompileError(
                    "CASE 的 ELSE 分支必须显式 CAST 到与 THEN 相同的类型",
                    node_id=nid)
            else_t, else_id = self._visit(else_ast)
            if else_t != result_type:
                raise CompileError(
                    "CASE 的 ELSE 类型与 THEN 结果类型不一致", node_id=nid)

        self.nodes[nid] = PlanNode(
            nid, "case", result_type,
            when_children=[[c, t] for c, t in zip(cond_ids, then_ids)],
            else_child=else_id)
        return result_type, nid


def compile_plan(ast, schema):
    """编译入口。``schema`` 为 {列名: (p, s)} 或 {列名: [p, s]}。"""
    if not isinstance(schema, dict):
        raise CompileError("schema 必须是列声明映射（常量-only 计划可为空）")
    schema_types = {}
    for name, decl in schema.items():
        if isinstance(decl, tuple):
            decl = list(decl)
        schema_types[name] = _as_type(decl, f"列 {name}")
        if not (1 <= schema_types[name].precision <= MAX_PRECISION):
            raise CompileError(f"列 {name} precision 超出 1..38")
        if not (0 <= schema_types[name].scale
                <= schema_types[name].precision):
            raise CompileError(f"列 {name} scale 超出 0..precision")
    return _Compiler(schema_types).compile(ast)
