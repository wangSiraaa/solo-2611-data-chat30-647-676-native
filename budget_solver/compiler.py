"""线性公式编译器。

输入是目录里的一个根快照（含递归钉住的外部快照），产出一份自包含编译产物：

1. 引用核对：所有 term 必须落在快照作用域内，快照钉选与目录事实一致；
2. 每个格规范化为一条线性方程（输入格退化为 ``x = value``）；
3. 实际依赖图（只保留真实引用边）与强连通分量（迭代 Tarjan，无递归深度限制）；
4. 每个 SCC 的拓扑顺序与循环标记，供解算器/查询/独立检查使用。

产物整体带内容哈希并落盘，解算器只消费产物，不再直接读公式。
"""

import os
from fractions import Fraction

from .errors import CompileError
from .util import content_hash, ensure_dir, read_json, write_json_atomic

EQUATION_SCHEMA_VERSION = 1


def var_name(model_id, cell_id):
    return f"{model_id}::{cell_id}"


# ------------------------------------------------------------ 迭代 Tarjan SCC
def tarjan_scc(nodes, adjacency):
    """迭代版 Tarjan。``adjacency[node]`` 返回节点序列；返回 SCC 列表（逆拓扑序）。"""
    index_of = {}
    lowlink = {}
    on_stack = set()
    stack = []
    sccs = []
    counter = 0

    for root in sorted(nodes):
        if root in index_of:
            continue
        work = [(root, iter(sorted(adjacency.get(root, ()))))]
        index_of[root] = lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in index_of:
                    index_of[nxt] = lowlink[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(sorted(adjacency.get(nxt, ())))))
                    advanced = True
                    break
                if nxt in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index_of[node]:
                component = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                sccs.append(sorted(component))
    return sccs


class Compiler:
    def __init__(self, catalog, artifact_dir):
        self.catalog = catalog
        self.artifact_dir = os.path.abspath(artifact_dir)
        ensure_dir(self.artifact_dir)

    # ---------------------------------------------------------- 作用域展开
    def _resolve_scope(self, root_snap_id):
        """从根快照出发，按 external_refs 递归收集全部钉住快照。"""
        scope = {}  # model_id -> snapshot dict
        pending = [root_snap_id]
        root_model = None
        while pending:
            snap_id = pending.pop()
            snap = self.catalog.get_snapshot(snap_id)
            mid = snap["model_id"]
            if mid in scope:
                if scope[mid]["snap_id"] != snap_id:
                    raise CompileError(
                        f"模型 {mid} 在作用域内出现两个不同快照："
                        f"{scope[mid]['snap_id']} 与 {snap_id}"
                    )
                continue
            # 完整性：快照里的修订在目录中真实存在且内容哈希稳定
            self.catalog.get_revision(mid, snap["rev_id"])
            scope[mid] = snap
            if root_model is None:
                root_model = mid
            for ext_mid, pin in snap["external_refs"].items():
                ext_snap = self.catalog.get_snapshot(pin["snap_id"])
                if ext_snap["model_id"] != ext_mid:
                    raise CompileError(
                        f"快照 {snap_id} 对 {ext_mid} 的钉选指向了错误模型"
                    )
                ext_rev = self.catalog.get_revision(ext_mid, ext_snap["rev_id"])
                pin_rev = self.catalog.get_revision(ext_mid, pin["rev_id"])
                if ext_rev["content_hash"] != pin_rev["content_hash"]:
                    raise CompileError(
                        f"快照 {snap_id} 钉住的 {ext_mid} 修订与目录事实不符"
                    )
                if ext_mid not in scope:
                    pending.append(pin["snap_id"])
        return root_model, scope

    @staticmethod
    def _build_equations(scope):
        variables = {}
        equations = {}
        adjacency = {}
        for mid in sorted(scope):
            snap = scope[mid]
            for cell_id in sorted(snap["cells"]):
                cell = snap["cells"][cell_id]
                vname = var_name(mid, cell_id)
                variables[vname] = {
                    "model_id": mid,
                    "cell_id": cell_id,
                    "kind": cell["kind"],
                    "cell_hash": snap["cell_hashes"][cell_id],
                }
        for mid in sorted(scope):
            snap = scope[mid]
            for cell_id in sorted(snap["cells"]):
                cell = snap["cells"][cell_id]
                vname = var_name(mid, cell_id)
                coeffs = {vname: "1"}
                rhs = cell["value"] if cell["kind"] == "input" else cell["constant"]
                deps = []
                if cell["kind"] == "formula":
                    # cell = constant + Σ coef·ref  =>  cell - Σ coef·ref = constant
                    # 注意自引用：ref 可能就是本格，系数必须与单位项相加而非覆盖。
                    for term in cell["terms"]:
                        ref_var = var_name(term["model_id"], term["cell_id"])
                        if ref_var not in variables:
                            raise CompileError(
                                f"引用越出快照作用域：{vname} -> {ref_var}"
                            )
                        deps.append(
                            {
                                "model_id": term["model_id"],
                                "cell_id": term["cell_id"],
                                "variable": ref_var,
                                "coef": term["coef"],
                            }
                        )
                        neg = "-" + term["coef"] if not term["coef"].startswith("-") else term["coef"][1:]
                        coeffs[ref_var] = str(
                            Fraction(coeffs.get(ref_var, "0")) + Fraction(neg)
                        )
                equations[vname] = {
                    "lhs_variable": vname,
                    "coeffs": dict(sorted(coeffs.items())),
                    "rhs": rhs,
                    "cell_hash": snap["cell_hashes"][cell_id],
                }
                adjacency[vname] = sorted(d["variable"] for d in deps)
                variables[vname]["deps"] = deps
        return variables, equations, adjacency

    def compile_snapshot(self, root_snap_id, label=None):
        root_model, scope = self._resolve_scope(root_snap_id)
        variables, equations, adjacency = self._build_equations(scope)
        sccs = tarjan_scc(variables.keys(), adjacency)
        # Tarjan 的出栈顺序即拓扑序：被依赖的 SCC 先出栈（在前），依赖者在后，
        # 解算器可直接按该顺序逐块代入上游结果。
        components = []
        var_component = {}
        for topo_index, members in enumerate(sccs):
            comp_id = f"scc{topo_index}"
            is_cyclic = len(members) > 1 or members[0] in adjacency.get(members[0], ())
            for member in members:
                var_component[member] = comp_id
            components.append(
                {
                    "scc_id": comp_id,
                    "topo_index": topo_index,
                    "variables": members,
                    "cyclic": is_cyclic,
                }
            )
        root_snap = scope[root_model]
        artifact = {
            "artifact_kind": "compiled_equations",
            "schema_version": EQUATION_SCHEMA_VERSION,
            "label": label,
            "root_model_id": root_model,
            "root_snap_id": root_snap_id,
            "scope_snapshots": {
                mid: {
                    "snap_id": snap["snap_id"],
                    "rev_id": snap["rev_id"],
                    "name": snap["name"],
                }
                for mid, snap in sorted(scope.items())
            },
            "variables": {v: variables[v] for v in sorted(variables)},
            "equations": {v: equations[v] for v in sorted(equations)},
            "dependency_edges": [
                {"from": v, "to": to}
                for v in sorted(adjacency)
                for to in adjacency[v]
            ],
            "components": components,
            "var_component": var_component,
            "root_outputs": [
                var_name(root_model, cid) for cid in root_snap["outputs"]
            ],
        }
        artifact["artifact_hash"] = content_hash(
            {k: v for k, v in artifact.items() if k != "artifact_hash"}
        )
        path = os.path.join(self.artifact_dir, f"{artifact['artifact_hash'][7:19]}.json")
        write_json_atomic(path, artifact)
        return artifact, path

    @staticmethod
    def load_artifact(path):
        artifact = read_json(path)
        if artifact.get("artifact_kind") != "compiled_equations":
            raise CompileError(f"{path} 不是编译产物")
        return artifact
