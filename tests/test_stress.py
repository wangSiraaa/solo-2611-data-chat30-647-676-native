"""把随机暴力交叉验证接入 unittest discover。"""

import unittest

from tests import stress_crosscheck


class StressCrosscheckTests(unittest.TestCase):
    def test_random_dags_match_brute_force(self):
        # 40 个随机 DAG：引擎逐格结果必须等于独立路径枚举参考实现
        stress_crosscheck.main()


if __name__ == "__main__":
    unittest.main()
