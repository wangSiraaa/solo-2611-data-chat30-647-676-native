"""列式向量执行器：按列批处理 + 行掩码 + 按需扩张的节点 memo。

与行式引擎的等价性要点：

* 每个节点为 ``active`` 集合中的行整列求值一次并缓存；共享子表达式被
  多个父节点复用时，active 集合只扩张、不重算已覆盖行（复用开销由
  扩张行数决定，而不是按父节点倍数重复）。
* CASE 的掩码顺序传播严格模拟短路：条件为 False/NULL 的行才进入下一个
  WHEN；THEN 分支只对命中行物化，未选分支从不求值，故其中的除零/溢出
  物理上不可达。无 ELSE 且未命中的行得到 NULL。
* 节点首次服务某行时产生的数据错误才入账（扩张时对旧行屏蔽重复错误），
  错误节点来源即当前节点 id，与行式引擎完全一致。

trace（仅测试用）不在求值循环里顺手记录：memo 的 union 掩码无法表达
“某行在 CASE 语义下是否真正到达某节点”。求值结束后另做一次纯可达性
遍历（只读 memo 向量、不重算数值），重建与行式引擎同构的逐行节点轨迹。
"""

from .decimals import (
    Decimal, parse_decimal_text, div as dec_div,
    add as dec_add, sub as dec_sub, mul as dec_mul,
    round_arith_result, round_to_node,
)
from .errors import DataError
from .runtime import CMP_FUNCS


class VectorEngine:
    name = "vector"

    def __init__(self, plan, trace=False):
        self.plan = plan
        self.nodes = plan.nodes
        self.trace_enabled = trace
        self.traces = {}

    def execute(self, batch):
        n = batch.nrows
        input_bad = set(batch.input_errors)
        alive = {i for i in range(n) if i not in input_bad}
        errors = dict(batch.input_errors)
        memo = {}          # nid -> [Decimal|bool|None] * n（列向量）
        active = {}        # nid -> set(row) 该向量已覆盖的行

        def ensure(nid, wanted):
            """保证节点 nid 对 wanted 中的所有存活行已求值，返回其列向量。"""
            wanted = {r for r in wanted if r not in errors}
            if not wanted:
                return memo.get(nid, [None] * n)
            prev = active.get(nid)
            if prev is None:
                mask = set(wanted)
                vec = [None] * n
                memo[nid] = vec
                active[nid] = mask
                delta = mask
            else:
                delta = wanted - prev
                if not delta:
                    return memo[nid]
                prev.update(delta)
                mask = prev
                vec = memo[nid]
            self._eval_node(nid, mask, delta, vec, memo, active, ensure,
                            errors, batch)
            return vec

        root_vec = ensure(self.plan.root_id, alive)
        results = []
        for i in range(n):
            if i in errors:
                results.append(None)       # 错误行明确去向：结果 NULL + 错误清单
            else:
                results.append(root_vec[i])

        if self.trace_enabled:
            self.traces = self._build_reachability_trace(
                batch, memo, errors, alive)
        return results, errors

    def _eval_node(self, nid, mask, delta, vec, memo, active, ensure,
                   errors, batch):
        node = self.nodes[nid]
        op = node.op

        if op == "column":
            col = batch.columns[node.name]
            for r in mask:
                vec[r] = col[r]
            return

        if op == "const":
            val = None if node.value is None \
                else parse_decimal_text(node.value)
            for r in mask:
                vec[r] = val
            return

        if op in ("add", "sub", "mul", "div", "cast", "cmp"):
            child_vecs = [ensure(cid, set(mask)) for cid in node.children]
            for r in mask:
                if r in errors:
                    # 子节点已对该行报错：本节点不可达，不产生本节点错误。
                    vec[r] = None
                    continue
                self._eval_scalar_row(node, r, child_vecs, vec,
                                      errors, record=r in delta)
            return

        if op == "case":
            self._eval_case(node, mask, vec, ensure, errors)
            return

        raise ValueError(f"未知节点算子: {op}")  # pragma: no cover

    def _eval_scalar_row(self, node, r, child_vecs, vec, errors, record):
        op = node.op
        try:
            if op in ("add", "sub", "mul", "div"):
                a, b = child_vecs[0][r], child_vecs[1][r]
                if a is None or b is None:
                    vec[r] = None
                    return
                if op == "div":
                    vec[r] = dec_div(a, b, node.type, node.id)
                else:
                    fn = {"add": dec_add, "sub": dec_sub,
                          "mul": dec_mul}[op]
                    vec[r] = round_arith_result(fn(a, b), node.type, node.id)
                return
            if op == "cast":
                a = child_vecs[0][r]
                if a is None:
                    vec[r] = None
                    return
                vec[r] = round_to_node(a, node.to, node.id)
                return
            if op == "cmp":
                a, b = child_vecs[0][r], child_vecs[1][r]
                if a is None or b is None:
                    vec[r] = None
                else:
                    vec[r] = CMP_FUNCS[node.cmp](a, b)
                return
        except DataError as exc:
            vec[r] = None
            if record and r not in errors:
                errors[r] = exc

    def _eval_case(self, node, mask, vec, ensure, errors):
        remaining = set(mask)
        for cond_id, then_id in node.when_children:
            if not remaining:
                break
            cond_vec = ensure(cond_id, set(remaining))
            hit = {r for r in remaining if cond_vec[r] is True}
            # False/NULL 行继续向下走；命中行只物化对应 THEN 子树。
            if hit:
                then_vec = ensure(then_id, set(hit))
                for r in hit:
                    if r in errors:
                        continue
                    vec[r] = then_vec[r]
            remaining = {r for r in remaining if cond_vec[r] is not True}
        if remaining and node.else_child is not None:
            else_vec = ensure(node.else_child, set(remaining))
            for r in remaining:
                if r in errors:
                    continue
                vec[r] = else_vec[r]
        else:
            # 无 ELSE（或所有分支均未命中）的行：明确去向为 NULL。
            for r in remaining:
                vec[r] = None

    # ------------------------------------------------------------------
    # 可达性 trace：独立于求值循环，按行式短路语义遍历 DAG。
    # ------------------------------------------------------------------
    def _build_reachability_trace(self, batch, memo, errors, alive):
        n = batch.nrows
        traces = {i: {} for i in range(n)}
        error_node = {i: exc.node_id for i, exc in errors.items()}

        def walk(nid, reachable):
            """按行式引擎的精确顺序求值并记录 trace。

            返回对父节点仍存活的行集合：左子树出错的行不会进入右兄弟，
            也不会到达本节点输出与任何祖先。
            """
            node = self.nodes[nid]
            vec = memo.get(nid, [None] * n)

            if node.op in ("column", "const"):
                for r in reachable:
                    traces[r][nid] = (vec[r], None)
                return set(reachable)

            if node.op == "case":
                for r in reachable:
                    traces[r][nid] = (vec[r], None)
                remaining = set(reachable)   # 尚未决定去向的行
                completed = set()           # 已选中分支且正常结束的行
                for cond_id, then_id in node.when_children:
                    if not remaining:
                        break
                    cond_alive = walk(cond_id, set(remaining))
                    # 条件出错的行：整个 CASE 对该行终止（不进 completed）。
                    remaining &= cond_alive
                    cond_vec = memo.get(cond_id, [None] * n)
                    hit = {r for r in remaining if cond_vec[r] is True}
                    if hit:
                        then_alive = walk(then_id, set(hit))
                        completed |= (hit & then_alive)   # THEN 存活：完成
                        # THEN 出错的行留在 remaining 外且不进 completed。
                    remaining = {r for r in remaining
                                 if cond_vec[r] is not True}
                if remaining and node.else_child is not None:
                    else_alive = walk(node.else_child, set(remaining))
                    completed |= (remaining & else_alive)
                elif remaining:
                    # 无 ELSE 且未命中：合法 NULL 结果，正常完成。
                    completed |= remaining
                return completed

            # 普通算子：从左到右，任一子树对某行出错即短路后续兄弟。
            surviving = set(reachable)
            for cid in node.children:
                if not surviving:
                    break
                surviving = walk(cid, set(surviving))
            # 到达本节点输出的行：子树全部存活。自身出错的节点同样要
            # 记录（带错误码），只是不再向父节点传播。
            for r in surviving:
                if error_node.get(r) == nid:
                    traces[r][nid] = (vec[r], errors[r].code)
                else:
                    traces[r][nid] = (vec[r], None)
            return {r for r in surviving if error_node.get(r) != nid}

        # 入口包含所有非输入错误行；运行时错误行由 walk 内部按短路规则
        # 记录到具体出错节点，而不是在入口整体剔除。
        walk(self.plan.root_id, set(alive))
        return traces
