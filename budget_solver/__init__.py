"""精确预算循环引用解算系统（纯 Python 3.11 标准库，无浮点、无 eval）。"""

import sys as _sys

# 消元会产生远超 4300 位十进制的中间整数；Python 3.11 的字符串转换安全限制
# 仅影响 str/int 转换而非运算，这里显式放开（0 = 不限制），保证精确分数可序列化。
if hasattr(_sys, "set_int_max_str_digits"):
    _sys.set_int_max_str_digits(0)

__version__ = "1.0.0"
