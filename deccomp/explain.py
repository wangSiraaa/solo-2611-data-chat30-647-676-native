"""计划解释：把保存的计划还原为带类型与节点编号的可读树。"""

from .planner import Plan


def _type_name(t):
    return "boolean" if t == "boolean" else f"decimal({t.precision},{t.scale})"


def explain_plan(plan: Plan) -> str:
    lines = []
    lines.append("=== Decimal Expression Plan ===")
    lines.append(f"plan version : 1")
    lines.append(f"schema fp    : {plan.schema_fingerprint[:16]}...")
    lines.append("schema       :")
    for name, decl in sorted(plan.schema.items()):
        lines.append(f"  - {name}: decimal({decl[0]},{decl[1]})")
    lines.append(f"result type  : {_type_name(plan.result_type)}")
    lines.append(f"nodes        : {len(plan.nodes)}  root=# {plan.root_id}")
    lines.append("")
    lines.append("node tree (id | op | result type | detail):")

    def render(nid, indent):
        node = plan.nodes[nid]
        detail = ""
        if node.op == "column":
            detail = node.name
        elif node.op == "const":
            detail = "NULL" if node.value is None else node.value
        elif node.op == "cmp":
            detail = node.cmp
        elif node.op == "cast":
            detail = f"-> {_type_name(node.to)}"
        prefix = "  " * indent
        lines.append(
            f"{prefix}#{nid:<3} {node.op:<6} {_type_name(node.type):<16} {detail}")
        if node.op == "case":
            for k, (cid, tid) in enumerate(node.when_children):
                lines.append(f"{prefix}  WHEN[{k}] cond:")
                render(cid, indent + 3)
                lines.append(f"{prefix}  WHEN[{k}] then:")
                render(tid, indent + 3)
            if node.else_child is not None:
                lines.append(f"{prefix}  ELSE:")
                render(node.else_child, indent + 2)
        else:
            for cid in node.children:
                render(cid, indent + 1)

    render(plan.root_id, 0)
    lines.append("")
    lines.append("note: 每个算术/cast 节点在运行时按自身 scale 做 "
                 "ROUND_HALF_EVEN，随后检查 precision 溢出。")
    return "\n".join(lines)
