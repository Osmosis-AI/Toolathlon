import importlib.util
import sys
import types
import unittest
from pathlib import Path


HERE = Path(__file__).parent
PACKAGE = "investment_decision_analysis_evaluation"

package = types.ModuleType(PACKAGE)
package.__path__ = [str(HERE)]

stub_modules = {
    PACKAGE: package,
    f"{PACKAGE}.realtime": types.SimpleNamespace(
        get_all_realtime_data=lambda: {},
        better=lambda *_args: None,
    ),
    "utils.general.helper": types.SimpleNamespace(
        normalize_str=lambda value: value.lower(),
    ),
    "utils.app_specific.google_oauth.ops": types.SimpleNamespace(
        get_credentials=lambda _path: None,
    ),
    "utils.evaluation.retry": types.SimpleNamespace(
        grade_with_retry=lambda check: check(),
    ),
}
missing = object()
previous_modules = {name: sys.modules.get(name, missing) for name in stub_modules}
sys.modules.update(stub_modules)
try:
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.main", HERE / "main.py")
    main = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(main)
finally:
    for name, previous in previous_modules.items():
        if previous is missing:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = previous


class CompareSheetsTests(unittest.TestCase):
    def test_unequal_non_numeric_cells_are_mismatches(self):
        for expected, actual in ((100, ""), ("Buy", "Sell")):
            with self.subTest(expected=expected, actual=actual):
                report = main.compare_sheets([[expected]], [[actual]], "Sheet")
                self.assertEqual(report["matched_cells"], 0)
                self.assertEqual(len(report["mismatches"]), 1)

    def test_numeric_cells_keep_three_percent_tolerance(self):
        report = main.compare_sheets([[100]], [[102.9]], "Sheet")
        self.assertEqual(report["matched_cells"], 1)
        self.assertEqual(report["mismatches"], [])


if __name__ == "__main__":
    unittest.main()
