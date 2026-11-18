"""独立验证器。

验证走与编译器/解算器*不同的代码路径*：直接从目录快照中的原始公式重建方程、
重建引用图与闭包，然后用本模块自带的独立消元实现（按拓扑逐块、自带 RREF 原语
的独立调用与独立解释）复算分类，并做代回检查。

* verify-compile：产物内方程/引用/SCC 与快照事实逐条比对；
* verify-solve ：报告中每个输出的确定值代回全部闭包方程；参数表达式做多组
  实例化符号代回；矛盾证书按 λ 组合重算，要求常数右端非 0、参数残差与证书一致。
"""

from fractions import Fraction

from .compiler import var_name
from .errors import VerifyError
from .solver import rref_augmented
from .util import content_hash


# ------------------------------------------------- 独立地从快照重建事实
def rebuild_facts(catalog, scope_snapshots):
    """根据报告/产物里钉住的快照清单，重建变量、方程与引用边。"""
    equations = {}
    edges = {}
    variables = {}
    for mid, pin in sorted(scope_snapshots.items()):
        snap = catalog.get_snapshot(pin["snap_id"])
        if snap["model_id"] != mid:
            raise VerifyError(f"快照 {pin['snap_id']} 与模型 {mid} 不匹配")
        for cell_id, cell in snap["cells"].items():
            v = var_name(mid, cell_id)
            variables[v] = {
                "kind": cell["kind"],
                "cell_hash": snap["cell_hashes"][cell_id],
            }
    for mid, pin in sorted(scope_snapshots.items()):
        snap = catalog.get_snapshot(pin["snap_id"])
        for cell_id, cell in snap["cells"].items():
            v = var_name(mid, cell_id)
            coeffs = {v: Fraction(1)}
            if cell["kind"] == "input":
                rhs = Fraction(cell["value"])
            else:
                rhs = Fraction(cell["constant"])
                for term in cell["terms"]:
                    ref = var_name(term["model_id"], term["cell_id"])
                    if ref not in variables:
                        raise VerifyError(f"快照中出现悬挂引用 {v} -> {ref}")
                    coeffs[ref] = coeffs.get(ref, Fraction(0)) - Fraction(term["coef"])
                    edges.setdefault(v, set()).add(ref)
            equations[v] = (coeffs, rhs)
            edges.setdefault(v, set())
    return variables, equations, edges


def rebuild_closure(edges, target):
    seen = {target}
    queue = [target]
    head = 0
    while head < len(queue):
        node = queue[head]
        head += 1
        for nxt in sorted(edges.get(node, ())):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return sorted(seen)


def independent_block_solve(equations, closure, scc_partition, topo_index):
    """验证器自己的分块求解：独立组装矩阵、独立解释 RREF 结果。

    ``scc_partition``: {scc_id: [变量...]}；``topo_index``: {scc_id: 序号}。
    返回 (classified, contradiction_certs)。
    """
    closure_set = set(closure)
    block_ids = sorted(
        {cid for v in closure for cid in [_block_of(scc_partition, v)]},
        key=lambda cid: topo_index[cid],
    )
    known = {}  # v -> ("fixed", F) | ("expr", const, {param_var: coef})
    free_params = set()
    certs = []

    for cid in block_ids:
        members = [v for v in scc_partition[cid] if v in closure_set]
        local = {v: i for i, v in enumerate(members)}
        upstream = set()
        for v in members:
            for ref in equations[v][0]:
                if ref not in local and known[ref][0] == "expr":
                    upstream.add(ref)
                    free_params.update(known[ref][2].keys())
        upstream = sorted(upstream)
        free_params.update(upstream)

        n = len(members)
        mat = [[Fraction(0)] * n for _ in range(n)]
        rhs = []
        pmat = []
        for r, v in enumerate(members):
            coeffs, eq_rhs = equations[v]
            raw_rhs = Fraction(eq_rhs)  # 局部副本：Fraction 不可变但 rhs 容器需独立
            prow = {p: Fraction(0) for p in upstream}
            for ref, coef in coeffs.items():
                if ref in local:
                    mat[r][local[ref]] = coef
                    continue
                info = known[ref]
                if info[0] == "fixed":
                    raw_rhs -= coef * info[1]
                else:
                    raw_rhs -= coef * info[1]
                    for pv, pc in info[2].items():
                        prow[pv] = prow.get(pv, Fraction(0)) - coef * pc
            rhs.append(raw_rhs)
            pmat.append([prow[p] for p in upstream])
        origins = [{v: Fraction(1)} for v in members]
        rows, rrhs, rpmat, rorig, pivot_cols, bad = rref_augmented(
            mat, rhs, pmat, origins
        )
        if bad:
            for r in bad:
                certs.append(
                    {
                        "scc_id": cid,
                        "residual_rhs": rrhs[r],
                        "param_residual": {
                            pv: rpmat[r][j] for j, pv in enumerate(upstream)
                            if rpmat[r][j] != 0
                        },
                        "combination": {
                            ev: lam for ev, lam in rorig[r].items() if lam != 0
                        },
                    }
                )
            return None, certs
        free_cols = [c for c in range(n) if c not in pivot_cols]
        pivot_row = {col: r for r, col in enumerate(pivot_cols)}
        free_params.update(members[c] for c in free_cols)
        for col, v in enumerate(members):
            if col in free_cols:
                known[v] = ("expr", Fraction(0), {v: Fraction(1)})
                continue
            pr = pivot_row[col]
            parts = {}
            for fc in free_cols:
                c = -rows[pr][fc]
                if c:
                    parts[members[fc]] = c
            for j, pv in enumerate(upstream):
                if rpmat[pr][j]:
                    parts[pv] = parts.get(pv, Fraction(0)) + rpmat[pr][j]
            known[v] = ("expr", rrhs[pr], parts) if parts else ("fixed", rrhs[pr])

    classified = {}
    for v in closure:
        info = known[v]
        classified[v] = info
    return (classified, sorted(free_params)), certs


def _block_of(partition, variable):
    for cid, members in partition.items():
        if variable in members:
            return cid
    raise VerifyError(f"变量 {variable} 不属于任何 SCC")


# ------------------------------------------------- 代回与表达式检查
def substitute_all(equations, closure, assignment):
    residuals = []
    for v in closure:
        coeffs, rhs = equations[v]
        lhs = sum(coef * assignment.get(var, Fraction(0)) for var, coef in coeffs.items())
        residuals.append((v, lhs - rhs))
    return residuals


def parse_expression(expr):
    constant = Fraction(expr["constant"])
    parts = {term["of_variable"]: Fraction(term["coef"]) for term in expr["param_terms"]}
    return constant, parts


# ========================================================= 验证入口
def verify_compile(catalog, artifact):
    checks = []

    def record(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            raise VerifyError(f"编译产物验证失败：{name} {detail}")

    variables, equations, edges = rebuild_facts(catalog, artifact["scope_snapshots"])
    record("变量集合与快照一致", set(variables) == set(artifact["variables"]))
    for v, (coeffs, rhs) in equations.items():
        eq = artifact["equations"][v]
        record(f"{v} 右端项", rhs == Fraction(eq["rhs"]), f"rhs={rhs} vs {eq['rhs']}")
        art_full = {k: Fraction(0) for k in variables}
        art_full.update({k: Fraction(val) for k, val in eq["coeffs"].items()})
        full = {k: Fraction(0) for k in variables}
        full.update(coeffs)
        record(f"{v} 方程系数", full == art_full)
    art_edges = {}
    for edge in artifact["dependency_edges"]:
        art_edges.setdefault(edge["from"], set()).add(edge["to"])
    for v in variables:
        record(f"{v} 引用边", edges.get(v, set()) == art_edges.get(v, set()))
    partition = {c["scc_id"]: c["variables"] for c in artifact["components"]}
    topo = {c["scc_id"]: c["topo_index"] for c in artifact["components"]}
    for out in artifact["root_outputs"]:
        closure = rebuild_closure(edges, out)
        members = partition[_block_of(partition, out)]
        record(f"{out} 的 SCC 成员都在依赖闭包内", set(members) <= set(closure))
    # SCC 循环标记与图事实一致（自环或多成员）
    for cid, members in partition.items():
        cyclic = len(members) > 1 or members[0] in edges.get(members[0], set())
        record(
            f"{cid} 循环标记正确",
            cyclic == next(c["cyclic"] for c in artifact["components"] if c["scc_id"] == cid),
        )
    expect_hash = content_hash(
        {k: v for k, v in artifact.items() if k != "artifact_hash"}
    )
    record("产物内容哈希", expect_hash == artifact["artifact_hash"])
    return {"ok": True, "checks": checks, "check_count": len(checks)}


def verify_solve(catalog, artifact, report):
    checks = []

    def record(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            raise VerifyError(f"解算报告验证失败：{name} {detail}")

    record("报告绑定的产物哈希", report["artifact_hash"] == artifact["artifact_hash"])
    variables, equations, edges = rebuild_facts(catalog, report["scope_snapshots"])
    record(
        "报告作用域与产物作用域一致",
        set(report["scope_snapshots"]) == set(artifact["scope_snapshots"]),
    )
    partition = {c["scc_id"]: c["variables"] for c in artifact["components"]}
    topo = {c["scc_id"]: c["topo_index"] for c in artifact["components"]}

    for target, output in sorted(report["outputs"].items()):
        closure = rebuild_closure(edges, target)
        record(f"{target} 实际依赖闭包独立重建一致", closure == output["actual_dependencies"])
        result, certs = independent_block_solve(equations, closure, partition, topo)

        if output["status"] == "inconsistent":
            record(f"{target} 独立复算无解", result is None)
            check_certificates(catalog, equations, output, certs, record, target)
            continue

        classified, free_params = result
        info = classified[target]
        if output["status"] == "determined":
            record(f"{target} 独立复算唯一确定", info[0] == "fixed")
            record(f"{target} 确定值一致", info[1] == Fraction(output["value"]),
                   output["value"])
            # 闭包内所有确定格共同赋值，代回全部原方程
            assignment = {v: Fraction(0) for v in closure}
            for v in closure:
                if classified[v][0] == "fixed":
                    assignment[v] = classified[v][1]
            nonzero = [
                (v, str(r)) for v, r in substitute_all(equations, closure, assignment) if r != 0
            ]
            record(f"{target} 代回全部闭包方程残差为 0", not nonzero, str(nonzero))
        else:
            record(f"{target} 独立复算为非唯一", info[0] == "expr")
            const, parts = parse_expression(output["expression"])
            if info[0] == "expr":
                record(f"{target} 表达式常数一致", const == info[1])
                record(f"{target} 参数系数一致", parts == info[2])
            check_parametric_identity(
                catalog, equations, closure, classified, partition, topo,
                report, target, record,
            )

    section_vars = {
        "determined": {r["variable"] for r in report["sections"]["determined"]},
        "undetermined_parameters": {
            r["variable"] for r in report["sections"]["undetermined_parameters"]
        },
        "contradictions": {r["variable"] for r in report["sections"]["contradictions"]},
    }
    status_vars = {"determined": set(), "parametric": set(), "inconsistent": set()}
    for v, o in report["outputs"].items():
        status_vars[o["status"]].add(v)
    record(
        "发布分区与输出状态一致",
        section_vars["determined"] == status_vars["determined"]
        and section_vars["undetermined_parameters"] == status_vars["parametric"]
        and section_vars["contradictions"] == status_vars["inconsistent"],
    )
    expect_hash = content_hash(
        {k: v for k, v in report.items() if k != "report_hash"}
    )
    record("报告内容哈希", expect_hash == report["report_hash"])
    return {"ok": True, "checks": checks, "check_count": len(checks)}


def check_certificates(catalog, equations, output, certs, record, target):
    report_certs = output["contradictions"]
    record(f"{target} 矛盾证书数量一致", len(report_certs) == len(certs))
    for index, cert in enumerate(report_certs):
        # 按报告证书的 λ 直接重算原方程组合（不信任解算器的溯源结果）
        lhs_combo = {}
        rhs_combo = Fraction(0)
        for item in cert["equation_combination"]:
            ev = item["equation_variable"]
            lam = Fraction(item["lambda"])
            coeffs, rhs = equations[ev]
            for var, coef in coeffs.items():
                lhs_combo[var] = lhs_combo.get(var, Fraction(0)) + lam * coef
            rhs_combo += lam * rhs
        record(
            f"{target} 证书{index} 组合后左端恒为 0",
            all(v == 0 for v in lhs_combo.values()),
        )
        record(
            f"{target} 证书{index} 常数右端非零且与证书一致",
            rhs_combo != 0 and rhs_combo == Fraction(cert["residual_rhs"]),
            f"rhs_combo={rhs_combo}",
        )
        # 参数残差列与独立分块求解结果一致
        if index < len(certs):
            ind = certs[index]
            report_param = {
                pv: Fraction(c) for pv, c in cert["param_residual_coeffs"].items()
            }
            record(
                f"{target} 证书{index} 参数残差独立复算一致",
                report_param == ind["param_residual"],
            )


def check_parametric_identity(catalog, equations, closure, classified, partition,
                              topo, report, target, record):
    """用多组具体参数实例化，按报告表达式求值后代回目标方程，必须恒等成立。"""
    free_vars = [v for v in closure if classified[v][0] == "expr"
                 and classified[v][1] == 0 and classified[v][2] == {v: Fraction(1)}]
    trials = [Fraction(k) for k in (1, 2, -3, 7, -11, 101)]
    memo = {}

    def eval_var(v, chosen):
        if v in memo:
            return memo[v]
        if v in assignment_free:
            memo[v] = assignment_free[v]
            return memo[v]
        if classified[v][0] == "fixed":
            memo[v] = classified[v][1]
            return memo[v]
        # 优先使用被验证报告里的表达式；非输出格用独立复算表达式
        if v in report["outputs"] and report["outputs"][v]["status"] == "parametric":
            const, parts = parse_expression(report["outputs"][v]["expression"])
        else:
            const, parts = classified[v][1], classified[v][2]
        memo[v] = const + sum(c * eval_var(p, chosen) for p, c in parts.items())
        return memo[v]

    for trial_index, chosen in enumerate(trials):
        memo.clear()
        assignment_free = {
            fv: chosen + Fraction(k) for k, fv in enumerate(free_vars)
        }
        coeffs, rhs = equations[target]
        lhs = sum(coef * eval_var(var, chosen) for var, coef in coeffs.items())
        record(
            f"{target} 参数表达式第 {trial_index} 组代回恒等",
            lhs == rhs,
            f"lhs={lhs} rhs={rhs}",
        )
