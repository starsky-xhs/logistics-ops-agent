"""评测集完整性与指标回归测试。"""

from __future__ import annotations

import unittest
import uuid

from app.config import PROJECT_ROOT
from app.eval import load_cases, run_eval


class EvalTests(unittest.TestCase):
    def test_load_40_cases(self) -> None:
        cases = load_cases()
        self.assertEqual(len(cases), 40)
        self.assertEqual(len({case.case_id for case in cases}), 40)
        self.assertTrue(any(case.diagnosis for case in cases))

    def test_full_eval_runs_and_report_contains_failure_details(self) -> None:
        tmp_dir = PROJECT_ROOT / "data" / ".test_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        report_path = tmp_dir / f"eval_{uuid.uuid4().hex}.md"
        try:
            report = run_eval(report_path=report_path)
            self.assertEqual(report.total, 40)
            self.assertGreaterEqual(report.tool_accuracy, 0.85)
            self.assertLessEqual(report.tool_accuracy, 0.95)
            self.assertGreaterEqual(report.completion_accuracy, 0.80)
            self.assertLessEqual(report.completion_accuracy, 0.95)
            self.assertTrue(report_path.is_file())
            markdown = report_path.read_text(encoding="utf-8")
            self.assertIn("失败 / 降级明细", markdown)
            self.assertIn("可能原因", markdown)
            self.assertIn("MockPlanner 的规则是通用意图", markdown)
            self.assertTrue(any(not item.passed for item in report.results))
        finally:
            report_path.unlink(missing_ok=True)

    def test_metric_calculation_consistency(self) -> None:
        report = run_eval(limit=3, report_path=None)
        self.assertEqual(report.total, 3)
        self.assertEqual(report.tool_correct, 3)
        self.assertEqual(report.approval_correct, 3)
        self.assertEqual(report.completion_accuracy, 1.0)


if __name__ == "__main__":
    unittest.main()
