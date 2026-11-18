"""精确有理数解算器与结果发布。

算法（题面允许二选一，这里采用依赖分解 + 循环块求解 + 缓存）：

1. 编译器已把变量切成强连通分量并给出拓扑序；
2. 对一个输出的依赖闭包，按拓扑序逐块求解。块内是小规模精确分数 RREF，
   增广部分为 ``[常数右端 | 上游自由参数列]``，并对*常数右端*维护行溯源；
3. 上游唯一确定的格在代入时直接换成常数；上游未确定的格成为跨块传播的参数，
   因此自由变量可以沿 DAG 向下游表达式传播；
4. 块内 0 = 非零常数 → 无解，溯源给出原方程 λ 组合（含参数残差，恒等部分为零）；
5. 闭包按 (变量, 格哈希) 集合做内容寻址缓存并随服务落盘：输入修订/系数变更改变
   格哈希 → 指纹变化 → 受影响闭包重算，其余命中；旧报告永不重写。

全程 :class:`fractions.Fraction`，无浮点、无 Excel 缓存值参与。
"""

import os
from fractions import Fraction

from .errors import BudgetError, NotFoundError
from .rationals import qstr
from .util import (
    content_hash,
    ensure_dir,
    read_json,
    utc_now_iso,
    write_json_atomic,
)

REPORT_SCHEMA_VERSION = 1
CACHE_SCHEMA_VERSION = 2
PARAM_PREFIX = "p_"


# ============================================================ 精确线性代数
def rref_augmented(rows, const_rhs, param_matrix, origins):
    """对增广系统做精确 RREF。

    每行结构为 ``系数列 | 常数右端 | 各参数列``；``origins[r]`` 记录该行由哪些
    原始块内方程按什么 λ 组合而成（只追踪常数右端的来源，参数列是线性变换）。
    返回消元后的全部列与矛盾行下标。
    """
    rows = [list(row) for row in rows]
    const_rhs = list(const_rhs)
    param_matrix = [list(row) for row in param_matrix]
    origins = [dict(origin) for origin in origins]
    n_rows = len(rows)
    n_cols = n_rows and len(rows[0]) or 0
    pivot_cols = []
    pivot_row = 0
    for col in range(n_cols):
        candidates = [r for r in range(pivot_row, n_rows) if rows[r][col] != 0]
        if not candidates:
            continue
        swap = candidates[0]
        if swap != pivot_row:
            rows[pivot_row], rows[swap] = rows[swap], rows[pivot_row]
            const_rhs[pivot_row], const_rhs[swap] = const_rhs[swap], const_rhs[pivot_row]
            param_matrix[pivot_row], param_matrix[swap] = param_matrix[swap], param_matrix[pivot_row]
            origins[pivot_row], origins[swap] = origins[swap], origins[pivot_row]
        pivot = rows[pivot_row][col]
        rows[pivot_row] = [v / pivot for v in rows[pivot_row]]
        const_rhs[pivot_row] = const_rhs[pivot_row] / pivot
        param_matrix[pivot_row] = [v / pivot for v in param_matrix[pivot_row]]
        origins[pivot_row] = {k: Fraction(v) / pivot for k, v in origins[pivot_row].items()}
        for r in range(n_rows):
            if r == pivot_row or rows[r][col] == 0:
                continue
            factor = rows[r][col]
            rows[r] = [a - factor * b for a, b in zip(rows[r], rows[pivot_row])]
            const_rhs[r] = const_rhs[r] - factor * const_rhs[pivot_row]
            param_matrix[r] = [
                a - factor * b for a, b in zip(param_matrix[r], param_matrix[pivot_row])
            ]
            merged = dict(origins[r])
            for k, v in origins[pivot_row].items():
                merged[k] = merged.get(k, Fraction(0)) - factor * v
            origins[r] = {k: v for k, v in merged.items() if v != 0}
        pivot_cols.append(col)
        pivot_row += 1
        if pivot_row == n_rows:
            break
    bad_rows = [
        r for r in range(n_rows)
        if all(v == 0 for v in rows[r])
        and all(v == 0 for v in param_matrix[r])
        and const_rhs[r] != 0
    ]
    return rows, const_rhs, param_matrix, origins, pivot_cols, bad_rows


# ============================================================ 闭包
def dependency_closure(artifact, target_variable):
    """输出格的实际依赖：沿引用边 BFS 得到的传递闭包（含自身，排序）。"""
    edges = {}
    for edge in artifact["dependency_edges"]:
        edges.setdefault(edge["from"], []).append(edge["to"])
    seen = {target_variable}
    queue = [target_variable]
    head = 0
    while head < len(queue):
        node = queue[head]
        head += 1
        for nxt in edges.get(node, []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return sorted(seen)


def _closure_fingerprint(artifact, closure):
    material = [(v, artifact["variables"][v]["cell_hash"]) for v in closure]
    return content_hash({"schema": CACHE_SCHEMA_VERSION, "cells": material})[7:]


def _expr_json(constant, param_parts, all_params):
    """param_parts: {参数变量名(不带 p_ 前缀的格变量): Fraction 系数}。"""
    terms = [
        {"parameter": PARAM_PREFIX + pv, "of_variable": pv, "coef": qstr(coef)}
        for pv, coef in sorted(param_parts.items()) if coef != 0
    ]
    return {
        "constant": qstr(constant),
        "param_terms": terms,
        "parameters": sorted(all_params),
    }


def solve_closure(artifact, closure):
    """按 SCC 拓扑序对闭包分块精确求解。"""
    closure_set = set(closure)
    comp_of = artifact["var_component"]
    components = {c["scc_id"]: c for c in artifact["components"]}
    # 闭包内涉及的块，保持产物中的拓扑顺序
    block_ids = []
    seen_blocks = set()
    for v in closure:
        cid = comp_of[v]
        if cid not in seen_blocks:
            seen_blocks.add(cid)
            block_ids.append(cid)
    block_ids.sort(key=lambda cid: components[cid]["topo_index"])

    # known[v] = ("fixed", Fraction) 或 ("expr", constant, {param_var: coef})
    known = {}
    free_params = set()
    contradictions = []

    for cid in block_ids:
        members = [v for v in components[cid]["variables"] if v in closure_set]
        local_index = {v: i for i, v in enumerate(members)}
        # 收集上游参数（出现在本块方程里、已被分类为表达式/自由的格）
        upstream_params = set()
        for v in members:
            for ref_var in artifact["equations"][v]["coeffs"]:
                if ref_var in local_index:
                    continue
                info = known[ref_var]
                if info[0] == "expr":
                    upstream_params.add(ref_var)
                    free_params.update(info[2].keys())
        upstream_params = sorted(upstream_params)
        free_params.update(upstream_params)

        n = len(members)
        matrix = [[Fraction(0)] * n for _ in range(n)]
        const_rhs = []
        param_rows = []
        for r, v in enumerate(members):
            eq = artifact["equations"][v]
            rhs_const = Fraction(eq["rhs"])
            prow = {p: Fraction(0) for p in upstream_params}
            for ref_var, coef_str in eq["coeffs"].items():
                coef = Fraction(coef_str)
                if ref_var in local_index:
                    matrix[r][local_index[ref_var]] = coef
                    continue
                kind, *payload = known[ref_var]
                if kind == "fixed":
                    rhs_const -= coef * payload[0]
                else:
                    _, c0, parts = known[ref_var]
                    rhs_const -= coef * c0
                    for pv, pc in parts.items():
                        prow[pv] = prow.get(pv, Fraction(0)) - coef * pc
            const_rhs.append(rhs_const)
            param_rows.append([prow[p] for p in upstream_params])

        origins = [{v: Fraction(1)} for v in members]
        rows, rrhs, pmat, rorig, pivot_cols, bad = rref_augmented(
            matrix, const_rhs, param_rows, origins
        )
        if bad:
            for r in bad:
                combo = [
                    {"equation_variable": ev, "lambda": qstr(lam)}
                    for ev, lam in sorted(rorig[r].items()) if lam != 0
                ]
                param_residual = {
                    pv: qstr(pmat[r][j]) for j, pv in enumerate(upstream_params)
                    if pmat[r][j] != 0
                }
                contradictions.append(
                    {
                        "residual_lhs": "0",
                        "residual_rhs": qstr(rrhs[r]),
                        "param_residual_coeffs": param_residual,
                        "scc_id": cid,
                        "equation_combination": combo,
                    }
                )
            return {
                "status": "inconsistent",
                "closure": closure,
                "free_variables": sorted(free_params),
                "contradictions": contradictions,
                "classified": {v: {"status": "inconsistent"} for v in closure},
            }

        pivot_set = set(pivot_cols)
        free_cols = [c for c in range(n) if c not in pivot_set]
        pivot_row_of = {col: r for r, col in enumerate(pivot_cols)}
        block_free_vars = [members[c] for c in free_cols]
        free_params.update(block_free_vars)

        for col, v in enumerate(members):
            if col in free_cols:
                # 块内自由变量：自身就是参数
                known[v] = ("expr", Fraction(0), {v: Fraction(1)})
                continue
            pr = pivot_row_of[col]
            parts = {}
            for fc in free_cols:
                c = -rows[pr][fc]
                if c != 0:
                    parts[members[fc]] = c
            for j, pv in enumerate(upstream_params):
                c = pmat[pr][j]
                if c != 0:
                    parts[pv] = parts.get(pv, Fraction(0)) + c
            if not parts:
                known[v] = ("fixed", rrhs[pr])
            else:
                known[v] = ("expr", rrhs[pr], parts)

    classified = {}
    for v in closure:
        kind, *payload = known[v]
        if kind == "fixed":
            classified[v] = {"status": "determined", "value": qstr(payload[0])}
        else:
            c0, parts = payload
            classified[v] = {
                "status": "parametric",
                "expression": _expr_json(c0, parts, free_params),
            }
    return {
        "status": "consistent",
        "closure": closure,
        "free_variables": sorted(free_params),
        "classified": classified,
    }


class SolverService:
    """可重开的解算数据服务：目录 + 编译产物 + 磁盘缓存 + 报告发布。"""

    def __init__(self, catalog, artifact_dir, report_dir, cache_dir):
        self.catalog = catalog
        self.artifact_dir = os.path.abspath(artifact_dir)
        self.report_dir = os.path.abspath(report_dir)
        self.cache_dir = os.path.abspath(cache_dir)
        for path in (self.artifact_dir, self.report_dir, self.cache_dir):
            ensure_dir(path)
        self._cache_index = self._load_cache_index()

    # ------------------------------------------------------------ 缓存管理
    def _cache_index_path(self):
        return os.path.join(self.cache_dir, "index.json")

    def _load_cache_index(self):
        path = self._cache_index_path()
        if os.path.exists(path):
            return read_json(path)
        return {"schema": CACHE_SCHEMA_VERSION, "entries": {}}

    def _flush_cache_index(self):
        write_json_atomic(self._cache_index_path(), self._cache_index)

    def _get_or_solve(self, artifact, closure):
        fingerprint = _closure_fingerprint(artifact, closure)
        entry = self._cache_index["entries"].get(fingerprint)
        if entry is not None:
            path = os.path.join(self.cache_dir, entry["file"])
            if os.path.exists(path):
                return read_json(path), fingerprint, True
        result = solve_closure(artifact, closure)
        file_name = f"closure-{fingerprint}.json"
        payload = {
            "cache_schema": CACHE_SCHEMA_VERSION,
            "fingerprint": fingerprint,
            "result": result,
            "computed_at": utc_now_iso(),
        }
        write_json_atomic(os.path.join(self.cache_dir, file_name), payload)
        self._cache_index["entries"][fingerprint] = {"file": file_name}
        self._flush_cache_index()
        return payload, fingerprint, False

    # ------------------------------------------------------------ solve 入口
    def solve(self, artifact, solve_config=None):
        config = solve_config or {"mode": "scc_block_rref_exact", "use_cache": True}
        outputs = artifact["root_outputs"]
        output_results = {}
        used_fingerprints = []
        cache_hits = []
        for target in outputs:
            if target not in artifact["variables"]:
                raise BudgetError(f"输出 {target} 不在编译产物中")
            closure = dependency_closure(artifact, target)
            payload, fingerprint, hit = self._get_or_solve(artifact, closure)
            used_fingerprints.append(fingerprint)
            if hit:
                cache_hits.append(fingerprint)
            result = payload["result"]
            info = result["classified"][target]
            comp_id = artifact["var_component"][target]
            comp = next(c for c in artifact["components"] if c["scc_id"] == comp_id)
            output_results[target] = {
                "variable": target,
                "model_id": artifact["variables"][target]["model_id"],
                "cell_id": artifact["variables"][target]["cell_id"],
                "status": info["status"],
                "value": info.get("value"),
                "expression": info.get("expression"),
                "actual_dependencies": closure,
                "in_cyclic_scc": comp["cyclic"],
                "scc_id": comp_id,
                "closure_fingerprint": fingerprint,
            }
            if info["status"] == "inconsistent":
                output_results[target]["contradictions"] = result["contradictions"]
                output_results[target]["failure_source"] = {
                    "artifact_hash": artifact["artifact_hash"],
                    "closure": closure,
                    "equations": [
                        {
                            "variable": v,
                            "coeffs": artifact["equations"][v]["coeffs"],
                            "rhs": artifact["equations"][v]["rhs"],
                            "cell_hash": artifact["equations"][v]["cell_hash"],
                        }
                        for v in closure
                    ],
                }
        report = self._publish_report(
            artifact, config, output_results, used_fingerprints, cache_hits
        )
        return report

    def _publish_report(self, artifact, config, output_results, fingerprints, hits):
        determined, parametric, inconsistent = [], [], []
        for target in sorted(output_results):
            r = output_results[target]
            record = {
                "variable": r["variable"],
                "model_id": r["model_id"],
                "cell_id": r["cell_id"],
                "actual_dependencies": r["actual_dependencies"],
                "scc_id": r["scc_id"],
                "in_cyclic_scc": r["in_cyclic_scc"],
                "closure_fingerprint": r["closure_fingerprint"],
            }
            if r["status"] == "determined":
                determined.append({**record, "value": r["value"]})
            elif r["status"] == "parametric":
                parametric.append({**record, "expression": r["expression"]})
            else:
                inconsistent.append(
                    {
                        **record,
                        "contradictions": r["contradictions"],
                        "failure_source": r["failure_source"],
                    }
                )
        seq_path = os.path.join(self.report_dir, "seq.json")
        seq = read_json(seq_path) if os.path.exists(seq_path) else {"next": 1}
        solve_id = f"solve{seq['next']}"
        seq["next"] += 1
        write_json_atomic(seq_path, seq)
        report = {
            "report_kind": "solve_report",
            "schema_version": REPORT_SCHEMA_VERSION,
            "solve_id": solve_id,
            "created_at": utc_now_iso(),
            "artifact_hash": artifact["artifact_hash"],
            "root_model_id": artifact["root_model_id"],
            "root_snap_id": artifact["root_snap_id"],
            "solve_config": config,
            "catalog_snapshot": self.catalog.catalog_snapshot_descriptor(),
            "scope_snapshots": artifact["scope_snapshots"],
            "outputs": {k: output_results[k] for k in sorted(output_results)},
            "sections": {
                "determined": determined,
                "undetermined_parameters": parametric,
                "contradictions": inconsistent,
            },
            "cache": {
                "fingerprints_used": sorted(set(fingerprints)),
                "fingerprints_hit": sorted(set(hits)),
            },
        }
        report["report_hash"] = content_hash(
            {k: v for k, v in report.items() if k != "report_hash"}
        )
        write_json_atomic(os.path.join(self.report_dir, f"{solve_id}.json"), report)
        return report

    # ------------------------------------------------------------ 查询/解释
    def get_report(self, solve_id):
        path = os.path.join(self.report_dir, f"{solve_id}.json")
        if not os.path.exists(path):
            raise NotFoundError(f"解算报告 {solve_id} 不存在")
        return read_json(path)

    def query(self, solve_id, variable):
        report = self.get_report(solve_id)
        result = report["outputs"].get(variable)
        if result is None:
            raise NotFoundError(f"报告 {solve_id} 中没有输出 {variable}")
        return {
            "solve_id": solve_id,
            "variable": variable,
            "status": result["status"],
            "value": result.get("value"),
            "expression": result.get("expression"),
            "actual_dependencies": result["actual_dependencies"],
            "in_cyclic_scc": result["in_cyclic_scc"],
        }

    def explain(self, solve_id, variable):
        from .compiler import Compiler

        report = self.get_report(solve_id)
        result = report["outputs"][variable]
        artifact = Compiler.load_artifact(self._artifact_path_for(report))
        closure = result["actual_dependencies"]
        equations = [
            {
                "variable": v,
                "coeffs": artifact["equations"][v]["coeffs"],
                "rhs": artifact["equations"][v]["rhs"],
                "kind": artifact["variables"][v]["kind"],
            }
            for v in closure
        ]
        out = {
            **self.query(solve_id, variable),
            "scc_ids": sorted({artifact["var_component"][v] for v in closure}),
            "closure_equations": equations,
            "dependency_edges_within_closure": [
                edge
                for edge in artifact["dependency_edges"]
                if edge["from"] in closure and edge["to"] in closure
            ],
        }
        if result["status"] == "inconsistent":
            out["contradictions"] = result["contradictions"]
            out["failure_source"] = result["failure_source"]
        return out

    def _artifact_path_for(self, report):
        digest = report["artifact_hash"].split(":", 1)[1]
        path = os.path.join(self.artifact_dir, f"{digest[:12]}.json")
        if not os.path.exists(path):
            raise NotFoundError(f"报告引用的编译产物 {digest[:12]} 已不在本地")
        return path
