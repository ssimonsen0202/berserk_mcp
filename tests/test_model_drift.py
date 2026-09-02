import json, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import model_drift


def series(*scores, version="v1", role="all", discovery_mode="0", repeats=3):
    return [{"tool_accuracy": s, "case_set_version": version, "status": "ok",
             "role": role, "discovery_mode": discovery_mode, "repeats": repeats}
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


class EnoughHistoryAfterCaseSetEditTest(unittest.TestCase):
    """A prior version required every row in the query window to share one
    case_set_version, so a deliberate case-set edit permanently blocked
    classification until every older-version row aged out of the 30-day
    window -- even once enough new-version runs already existed. Found by
    Codex review, 2026-09-02."""

    def test_four_new_version_rows_after_old_ones_classify_normally(self):
        mixed = series(0.88, 0.88, version="v1") + series(0.60, 0.60, 0.60, 0.60, version="v2")
        out = model_drift.classify(mixed, noise_band=0.05)
        # 4 rows at v2, all 0.60 -- flat, so stable, not insufficient-data.
        self.assertEqual(out["verdict"], "stable")

    def test_a_real_regression_still_fires_after_a_case_set_edit(self):
        mixed = series(0.88, 0.88, version="v1") + series(0.88, 0.88, 0.60, 0.59, version="v2")
        out = model_drift.classify(mixed, noise_band=0.05)
        self.assertIn(out["verdict"], ("degrading", "step-change"))


class RoleAndDiscoveryModeGateClassificationTest(unittest.TestCase):
    """Role and discovery mode change the tool schema a canary run actually
    sees (run_eval.py's MCP subprocess inherits BERSERK_MCP_ROLE /
    BERSERK_MCP_DISCOVERY from the environment). Comparing runs across a
    role change would look identical to a model regression under the same
    case_set_version. Found by Codex review, 2026-09-02."""

    def test_role_change_is_not_compared_across(self):
        mixed = (series(0.88, 0.88, role="all")
                + series(0.60, 0.60, 0.60, 0.60, role="claude"))
        out = model_drift.classify(mixed, noise_band=0.05)
        # Only 4 rows share the latest (role="claude") combination -- flat
        # among themselves, so stable, not a false "regression" against
        # the role="all" baseline.
        self.assertEqual(out["verdict"], "stable")


class RepeatsConfidenceTest(unittest.TestCase):
    """The noise band was calibrated at repeats=3. At repeats=1, a single
    case flipping (1/48 = 0.0208) already exceeds the 0.02 band on its
    own -- a sampling regime the calibration never covered. Found by
    Codex review, 2026-09-02."""

    def test_low_repeats_caps_confidence_at_low(self):
        s = series(1.0, 1.0, 0.98, 0.98, repeats=1)
        out = model_drift.classify(s)
        self.assertEqual(out["confidence"], "low")
        self.assertIn("repeats=1", out["reason"])

    def test_calibrated_repeats_does_not_cap_confidence(self):
        s = series(0.9, 0.9, 0.6, 0.59, repeats=3)
        out = model_drift.classify(s, noise_band=0.05)
        self.assertNotEqual(out["confidence"], "low")


class SeriesKqlTableTest(unittest.TestCase):
    """series_kql() hardcoded the literal table name "default" instead of
    reusing the project's own configurable BERSERK_TABLE, silently
    breaking on any deployment that configures a different table. Found
    by Codex review, 2026-09-02. model_drift.py reads the env var itself
    (not by importing berserk_mcp, which would be circular), so this test
    reimports the module fresh after setting the variable."""

    def test_honors_berserk_table_env_var(self):
        import importlib
        import os
        old = os.environ.get("BERSERK_TABLE")
        os.environ["BERSERK_TABLE"] = "custom_table"
        try:
            importlib.reload(model_drift)
            kql = model_drift.series_kql("vendor/model")
            self.assertTrue(kql.startswith("custom_table |"))
        finally:
            if old is None:
                os.environ.pop("BERSERK_TABLE", None)
            else:
                os.environ["BERSERK_TABLE"] = old
            importlib.reload(model_drift)  # restore the default for later tests

    def test_defaults_to_default_table_when_unset(self):
        self.assertTrue(model_drift.series_kql("vendor/model").startswith("default |"))


class GroupByModelJsonFallbackTest(unittest.TestCase):
    """bzrk_search_json() documents a plain aligned-table text fallback for
    bzrk builds that reject --json (berserk_mcp.py:1536-1546). A prior
    version called json.loads() unconditionally, raising JSONDecodeError
    on that fallback text instead of failing distinctly from "genuinely no
    rows". Found by Codex review, 2026-09-02."""

    def test_non_json_text_does_not_raise(self):
        aligned_table_text = "model    accuracy\nvendor/x    0.95\n"
        result = model_drift.group_by_model(aligned_table_text)  # must not raise
        self.assertEqual(result, {})


class FingerprintChangedAutoDerivationTest(unittest.TestCase):
    """classify()'s fingerprint_changed parameter defaults to None, not
    False -- None means "derive it from the series", so a real caller
    that just calls classify(series) gets correct behavior. Neither of
    berserk_mcp.py's two dispatcher call sites ever passed this argument
    explicitly, so it was always False in practice regardless of what the
    fingerprints actually showed. Found by Codex review, 2026-09-02."""

    def _row(self, score, fp):
        return {"tool_accuracy": score, "case_set_version": "v1", "status": "ok",
                "role": "all", "discovery_mode": "0", "repeats": 3,
                "behavioral_fingerprint": fp, "provider_metadata_fingerprint": fp}

    def test_fingerprint_change_on_the_last_transition_raises_confidence(self):
        rows = [self._row(0.9, "a"), self._row(0.9, "a"),
                self._row(0.6, "a"), self._row(0.6, "b")]
        without = model_drift.classify(rows, noise_band=0.05, fingerprint_changed=False)
        auto = model_drift.classify(rows, noise_band=0.05)  # fingerprint_changed=None (default)
        self.assertEqual(auto["verdict"], "step-change")
        self.assertGreater(
            model_drift.CONFIDENCE_RANK[auto["confidence"]],
            model_drift.CONFIDENCE_RANK[without["confidence"]],
        )

    def test_no_fingerprint_change_does_not_raise_confidence(self):
        rows = [self._row(0.9, "a"), self._row(0.9, "a"),
                self._row(0.6, "a"), self._row(0.6, "a")]
        auto = model_drift.classify(rows, noise_band=0.05)
        self.assertNotIn("fingerprint", auto["reason"])
