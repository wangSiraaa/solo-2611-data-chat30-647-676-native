"""预算按权重比例向下分解的纯函数。

规则（题面）：父预算按各直接孩子的正整数权重比例分解到整数分；
先下取整，再把下取整丢掉的分按小数余数从大到小补，余数并列时
按部门 ID 升序决定，直到补完。各孩子预算之和必须严格等于父预算。

本模块不含任何 I/O，便于独立测试与重算。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class WeightedChild:
    """一条直接父子关系上的分配权重。"""

    department_id: str
    weight: int


def allocate_children(parent_budget: int, children: list[WeightedChild]) -> dict[str, int]:
    """把 ``parent_budget`` 按权重分给直接孩子。

    返回 ``{department_id: 预算分}``，保证：

    * 每个孩子预算是非负整数，且总和严格等于 ``parent_budget``；
    * 权重必须为正整数；
    * 孩子 ID 不允许重复。
    """
    if parent_budget < 0:
        raise ValueError("parent_budget 不能为负")
    if not children:
        if parent_budget != 0:
            raise ValueError("没有孩子时父预算无法向下分解")
        return {}

    ids = [c.department_id for c in children]
    if len(set(ids)) != len(ids):
        raise ValueError(f"孩子部门 ID 重复: {ids}")
    total_weight = 0
    for c in children:
        if not isinstance(c.weight, int) or isinstance(c.weight, bool) or c.weight <= 0:
            raise ValueError(f"部门 {c.department_id} 的权重必须是正整数: {c.weight!r}")
        total_weight += c.weight

    # 第一步：下取整。
    floors: dict[str, int] = {}
    remainders: dict[str, int] = {}
    allocated = 0
    for c in children:
        scaled = parent_budget * c.weight
        floor = scaled // total_weight
        floors[c.department_id] = floor
        # 只保存分子余数（分母统一为 total_weight），比较时等价于小数余数。
        remainders[c.department_id] = scaled % total_weight
        allocated += floor

    # 第二步：余数降序补分；并列按部门 ID 升序。补分数恰好等于未整除的分。
    leftover = parent_budget - allocated
    assert 0 <= leftover < len(children), "下取整后余数分必少于孩子数"
    order = sorted(children, key=lambda c: (-remainders[c.department_id], c.department_id))
    result = dict(floors)
    for i in range(leftover):
        result[order[i].department_id] += 1

    assert sum(result.values()) == parent_budget
    return result


def decompose_tree(
    root_budget: int,
    root_id: str,
    children_of: dict[str, list[str]],
    weights: dict[tuple[str, str], int],
) -> dict[str, int]:
    """自根向下对整棵树重算每个部门的预算。

    ``children_of`` 给出每个内部节点的直接孩子（叶节点对应空列表或缺失）；
    ``weights`` 以 ``(parent_id, child_id)`` 为键给出正整数权重。
    调用方需保证权重对每个非叶节点的直接孩子恰好补齐。
    """
    allocations = {root_id: root_budget}
    queue: deque[str] = deque([root_id])
    while queue:
        parent = queue.popleft()
        kids = children_of.get(parent, [])
        if not kids:
            continue
        weighted = [WeightedChild(kid, weights[(parent, kid)]) for kid in kids]
        child_alloc = allocate_children(allocations[parent], weighted)
        for kid, amount in child_alloc.items():
            allocations[kid] = amount
            queue.append(kid)
    return allocations
