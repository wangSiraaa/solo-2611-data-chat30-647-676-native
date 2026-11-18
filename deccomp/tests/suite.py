"""测试套件聚合，供 ``deccomp.verify()`` 与 CLI verify 子命令使用。"""

import unittest

from .test_core import (TypeDerivationTests, RoundingTests, CaseTests,
                        NullSemanticsTests, InputStrictnessTests,
                        SchemaBindingTests, PersistenceAndReadbackTests)
from .test_fuzz import OracleEquivalenceTests
from .test_scale import ScaleTests
from .test_no_float import NoFloatTests


def suite():
    loader = unittest.TestLoader()
    s = unittest.TestSuite()
    for cls in (TypeDerivationTests, RoundingTests, CaseTests,
                NullSemanticsTests, InputStrictnessTests,
                SchemaBindingTests, PersistenceAndReadbackTests,
                OracleEquivalenceTests, ScaleTests, NoFloatTests):
        s.addTests(loader.loadTestsFromTestCase(cls))
    return s
