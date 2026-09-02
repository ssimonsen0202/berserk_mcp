import json, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import model_drift


def series(*scores, version="v1"):
    return [{"tool_accuracy": s, "case_set_version": version, "status": "ok"}
            for s in scores]


class VerdictTest(unittest.TestCase):
    BAND = 0.05

    def test_insufficient_history_is_explicit_not_stable(self):
        out = model_drift.classify(series(0.88), self.BAND, False)
        self.assertEqual(out["verdict"], "insufficient-data")

    def test_flat_series_is_stable(self):
        out = model_drift.classify(series(0.88, 0.87, 0.88, 0.89), self.BAND, False)
        self.assertEqual(out["verdict"], "stable")

    def test_drop_inside_the_noise_band_does_not_fire(self):
        out = model_drift.classify(series(0.88, 0.88, 0.86, 0.85), self.BAND, False)
        self.assertEqual(out["verdict"], "stable")

    def test_single_degraded_run_does_not_fire(self):
        out = model_drift.classify(series(0.88, 0.88, 0.88, 0.60), self.BAND, False)
        self.assertNotIn(out["verdict"], ("degrading", "step-change"))

    def test_two_consecutive_degraded_runs_fire_as_step_change(self):
        out = model_drift.classify(series(0.88, 0.88, 0.60, 0.59), self.BAND, False)
        self.assertEqual(out["verdict"], "step-change")

    def test_mixed_case_set_versions_are_not_compared(self):
        mixed = series(0.88, 0.88, version="v1") + series(0.60, 0.59, version="v2")
        out = model_drift.classify(mixed, self.BAND, False)
        self.assertEqual(out["verdict"], "insufficient-data")

    def test_failed_runs_are_excluded_not_scored_zero(self):
        s = series(0.88, 0.88, 0.88, 0.88)
        s.append({"status": "failed", "case_set_version": "v1"})
        out = model_drift.classify(s, self.BAND, False)
        self.assertEqual(out["verdict"], "stable")

    def test_fingerprint_change_raises_confidence_on_step_change(self):
        without = model_drift.classify(series(0.88, 0.88, 0.60, 0.59), self.BAND, False)
        with_fp = model_drift.classify(series(0.88, 0.88, 0.60, 0.59), self.BAND, True)
        self.assertEqual(with_fp["verdict"], "step-change")
        self.assertGreater(
            model_drift.CONFIDENCE_RANK[with_fp["confidence"]],
            model_drift.CONFIDENCE_RANK[without["confidence"]],
        )


class GroupByModelTest(unittest.TestCase):
    def test_groups_rows_by_model_column(self):
        payload = json.dumps({"Tables": [{
            "schema": {"columns": [{"name": "model"}, {"name": "tool_accuracy"}]},
            "rows": [["m1", 0.9], ["m2", 0.5], ["m1", 0.85]],
        }]})
        grouped = model_drift.group_by_model(payload)
        self.assertEqual(len(grouped["m1"]), 2)
        self.assertEqual(len(grouped["m2"]), 1)

    def test_empty_text_returns_empty_dict(self):
        self.assertEqual(model_drift.group_by_model(""), {})


class SeriesKqlTest(unittest.TestCase):
    def test_projects_columns_without_eval_prefix(self):
        kql = model_drift.series_kql("deepseek/deepseek-v4-flash")
        self.assertIn("tool_accuracy=toreal(attributes['eval.tool_accuracy'])", kql)
        self.assertIn("model=tostring(attributes['eval.model'])", kql)
        self.assertNotIn("project eval.tool_accuracy", kql)


class NoiseBandCalibrationTest(unittest.TestCase):
    """Locks the real, measured calibration (2026-09-01) against the noise
    it was actually calibrated from -- not a hypothetical. If this ever
    fails, either the calibration data changed (re-baseline deliberately)
    or DEFAULT_NOISE_BAND was edited without re-measuring (a regression)."""

    MEASURED_SCORES = [0.9513888888888888, 0.9583333333333334,
                       0.9513888888888888, 0.9513888888888888, 0.9444444444444444]

    def test_default_noise_band_is_the_measured_value(self):
        self.assertEqual(model_drift.DEFAULT_NOISE_BAND, 0.02)

    def test_default_noise_band_clears_the_real_measured_spread(self):
        """The band must exceed the actual observed run-to-run spread,
        or the 5 calibration runs themselves would have false-positived
        against each other."""
        mean = sum(self.MEASURED_SCORES) / len(self.MEASURED_SCORES)
        max_deviation = max(abs(s - mean) for s in self.MEASURED_SCORES)
        self.assertGreater(model_drift.DEFAULT_NOISE_BAND, max_deviation)

    def test_real_calibration_series_classifies_as_stable(self):
        """The 5 live runs used to derive the band must themselves read as
        stable under that band -- otherwise the calibration disagrees with
        the classifier it calibrated."""
        out = model_drift.classify(series(*self.MEASURED_SCORES))
        self.assertEqual(out["verdict"], "stable")
