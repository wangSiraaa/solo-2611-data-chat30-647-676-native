"""合成模型工厂：题目要求的六类案例 + 错误隔离案例。

所有数值都是手工可核对的精确分数。函数返回 put-model 所需的 JSON 负载。
"""


def two_cell_unique_cycle(model_id="M_UNIQUE"):
    """两格唯一循环：x = 1/2 + (1/4)y；y = 1/3 + (1/2)x。

    手算：4x - y = 2；-3x + 6y = 2。
    代入：x = 1/2 + (1/4)(1/3 + x/2) = 7/12 + x/8，解得 x = y = 2/3。
    """
    return {
        "model_id": model_id,
        "name": "两格唯一循环",
        "cells": {
            "x": {
                "kind": "formula",
                "constant": "1/2",
                "terms": [{"coef": "1/4", "cell_id": "y"}],
            },
            "y": {
                "kind": "formula",
                "constant": "1/3",
                "terms": [{"coef": "1/2", "cell_id": "x"}],
            },
        },
        "outputs": ["x", "y"],
    }


def redundant_consistent_cycle(model_id="M_REDUNDANT"):
    """冗余一致循环：三个格三条方程，秩只有 2，留一个自由变量，输出 z 仍固定。

    方程（编译后形式）：
      a - b = 0
      b - c = 0
      a - c = 0            （第三条是前两条之和，冗余）
    手算：a=b=c=p 自由；但 r = a - b + 5 = 5 被唯一确定。
    """
    return {
        "model_id": model_id,
        "name": "冗余一致循环",
        "cells": {
            "a": {"kind": "formula", "constant": "0", "terms": [{"coef": "1", "cell_id": "b"}]},
            "b": {"kind": "formula", "constant": "0", "terms": [{"coef": "1", "cell_id": "c"}]},
            "c": {"kind": "formula", "constant": "0", "terms": [{"coef": "1", "cell_id": "a"}]},
            "r": {
                "kind": "formula",
                "constant": "5",
                "terms": [
                    {"coef": "1", "cell_id": "a"},
                    {"coef": "-1", "cell_id": "b"},
                ],
            },
        },
        "outputs": ["a", "r"],
    }


def contradictory_cycle(model_id="M_CONTRA"):
    """矛盾循环：a = b + 1；b = a + 1。代回得 0 = 2，无解。"""
    return {
        "model_id": model_id,
        "name": "矛盾循环",
        "cells": {
            "a": {"kind": "formula", "constant": "1", "terms": [{"coef": "1", "cell_id": "b"}]},
            "b": {"kind": "formula", "constant": "1", "terms": [{"coef": "1", "cell_id": "a"}]},
        },
        "outputs": ["a", "b"],
    }


def free_but_output_fixed(model_id="M_FREEFIX"):
    """自由变量存在但某输出固定：

    u, v 互相等（u=v，自由变量 p）；t = u - v + 2/7 恒为 2/7（唯一确定）。
    """
    return {
        "model_id": model_id,
        "name": "自由变量但输出固定",
        "cells": {
            "u": {"kind": "formula", "constant": "0", "terms": [{"coef": "1", "cell_id": "v"}]},
            "v": {"kind": "formula", "constant": "0", "terms": [{"coef": "1", "cell_id": "u"}]},
            "t": {
                "kind": "formula",
                "constant": "2/7",
                "terms": [
                    {"coef": "1", "cell_id": "u"},
                    {"coef": "-1", "cell_id": "v"},
                ],
            },
        },
        "outputs": ["u", "t"],
    }


def model_a_simple(model_id="A_FIN"):
    """跨模型引用的上游：输入 d 与公式 e。"""
    return {
        "model_id": model_id,
        "name": "上游财务模型",
        "cells": {
            "d": {"kind": "input", "value": "1000/3"},
            "e": {
                "kind": "formula",
                "constant": "1/6",
                "terms": [{"coef": "3/2", "cell_id": "d"}],
            },
        },
        "outputs": ["e"],
    }


def model_b_referencing_a(a_id="A_FIN", model_id="B_BUD"):
    """下游预算模型：f = 1/4 + (2/3)·A.e，g = f - A.e + A.d/4。

    手算（A.d=1000/3, A.e=500+1/6=3001/6）：
      f = 1/4 + (2/3)(3001/6) = 1/4 + 3001/9 = (9 + 12004)/36 = 12013/36
      g = f - A.e + A.d/4 = 12013/36 - 3001/6 + 1000/12
        = 12013/36 - 18006/36 + 3000/36 = -2993/36
    """
    return {
        "model_id": model_id,
        "name": "下游预算模型",
        "cells": {
            "f": {
                "kind": "formula",
                "constant": "1/4",
                "terms": [{"coef": "2/3", "model_id": a_id, "cell_id": "e"}],
            },
            "g": {
                "kind": "formula",
                "constant": "0",
                "terms": [
                    {"coef": "1", "cell_id": "f"},
                    {"coef": "-1", "model_id": a_id, "cell_id": "e"},
                    {"coef": "1/4", "model_id": a_id, "cell_id": "d"},
                ],
            },
        },
        "outputs": ["f", "g"],
    }


def input_cycle_model(model_id="M_INPUT_CYCLE"):
    """带外部输入的两格循环：p = in + (1/2)q；q = in - (1/4)p。

    手算（in = k）：
      p - q/2 = k；q + p/4 = k。
      由第二式 q = k - p/4 代入第一式：p - k/2 + p/8 = k
      (9/8)p = 3k/2 => p = 4k/3，q = k - k/3 = 2k/3。
    修订把 in 从 6/1 改成 12/1：旧快照 p=8, q=4；新快照 p=16, q=8。
    """
    return {
        "model_id": model_id,
        "name": "带输入更正的循环模型",
        "cells": {
            "inp": {"kind": "input", "value": "6"},
            "p": {
                "kind": "formula",
                "constant": "0",
                "terms": [
                    {"coef": "1", "cell_id": "inp"},
                    {"coef": "1/2", "cell_id": "q"},
                ],
            },
            "q": {
                "kind": "formula",
                "constant": "0",
                "terms": [
                    {"coef": "1", "cell_id": "inp"},
                    {"coef": "-1/4", "cell_id": "p"},
                ],
            },
        },
        "outputs": ["p", "q"],
    }


def bad_model_good_neighbor(model_id="M_BAD"):
    """结构非法的坏模型（引用不存在的格），用于目录隔离测试。"""
    return {
        "model_id": model_id,
        "name": "坏模型",
        "cells": {
            "h": {
                "kind": "formula",
                "constant": "1",
                "terms": [{"coef": "1", "cell_id": "ghost"}],
            }
        },
        "outputs": ["h"],
    }
