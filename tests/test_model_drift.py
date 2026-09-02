import json, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import model_drift


def series(*scores, version="v1", role="all", discovery_mode="0", tier="", repeats=3):
    return [{"tool_accuracy": s, "case_set_version": version, "status": "ok",
             "role": role, "discovery_mode": discovery_mode, "tier": tier,
             "repeats": repeats}
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

    def test_tier_change_is_not_compared_across(self):
        """BERSERK_MCP_TIER hides tools the same way role/discovery mode
        do -- an identically-shaped gap to the role/discovery one above,
        missed in the first fix and found by Codex's backtest of that
        same fix, same day."""
        mixed = (series(0.88, 0.88, tier="small")
                + series(0.60, 0.60, 0.60, 0.60, tier="deep"))
        out = model_drift.classify(mixed, noise_band=0.05)
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
    bzrk builds that reject --json (berserk_mcp.py:1536-1546).

    This went through two contract changes:
    - Originally: json.loads() unconditionally, raising a bare
      JSONDecodeError on that fallback text. Found by Codex review,
      2026-09-02.
    - First fix: caught the JSONDecodeError and returned {} -- but that
      made a parse failure indistinguishable from a genuinely empty
      result, so --drift-report printed "All models stable" and exited 0
      on a totally broken read path. Found by Codex backtest of that same
      fix, same day -- the fix introduced a new failure mode instead of
      fixing the original one.
    - Current: raises BzrkResultParseError distinctly, so callers can
      (and must) tell "couldn't read this" apart from "read it, no rows".
      See RunDriftReportTest and the dispatcher tests in
      tests/test_berserk_mcp.py for the caller side of this contract."""

    def test_non_json_text_raises_a_distinct_typed_error(self):
        aligned_table_text = "model    accuracy\nvendor/x    0.95\n"
        with self.assertRaises(model_drift.BzrkResultParseError):
            model_drift.group_by_model(aligned_table_text)

    def test_error_is_not_a_bare_json_decode_error(self):
        """Callers must be able to catch this without importing json
        themselves, and must not accidentally catch it via a bare
        `except Exception` that also swallows real bugs."""
        try:
            model_drift.group_by_model("not json")
        except model_drift.BzrkResultParseError as exc:
            self.assertNotIsInstance(exc, json.JSONDecodeError)
        else:
            self.fail("expected BzrkResultParseError")


class FingerprintChangedAutoDerivationTest(unittest.TestCase):
    """classify()'s fingerprint_changed parameter defaults to None, not
    False -- None means "derive it from the series", so a real caller
    that just calls classify(series) gets correct behavior. Neither of
    berserk_mcp.py's two dispatcher call sites ever passed this argument
    explicitly, so it was always False in practice regardless of what the
    fingerprints actually showed. Found by Codex review, 2026-09-02."""

    def _row(self, score, fp):
        return {"tool_accuracy": score, "case_set_version": "v1", "status": "ok",
                "role": "all", "discovery_mode": "0", "tier": "", "repeats": 3,
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

    def test_fingerprint_change_at_the_baseline_recent_boundary_is_still_detected(self):
        """The regression test for the actual bug: comparing only the two
        most recent rows (an earlier version) misses a transition that
        lands exactly at the baseline/recent boundary. Here the
        fingerprint and the score both change between row[1] and row[2],
        so the two most recent rows (row[2], row[3]) are already both
        "b" -- a last-two-rows comparison finds no change at all. Found
        by Codex backtest, 2026-09-02."""
        rows = [self._row(0.9, "a"), self._row(0.9, "a"),
                self._row(0.6, "b"), self._row(0.6, "b")]
        self.assertFalse(
            rows[-2]["behavioral_fingerprint"] != rows[-1]["behavioral_fingerprint"],
            "test setup sanity check: the last two rows must share a fingerprint "
            "for this to actually exercise the boundary case",
        )
        auto = model_drift.classify(rows, noise_band=0.05)
        self.assertTrue(auto["fingerprint_changed"])
        self.assertIn("fingerprint", auto["reason"])

    def test_no_fingerprint_change_does_not_raise_confidence(self):
        rows = [self._row(0.9, "a"), self._row(0.9, "a"),
                self._row(0.6, "a"), self._row(0.6, "a")]
        auto = model_drift.classify(rows, noise_band=0.05)
        self.assertNotIn("fingerprint", auto["reason"])

    def test_stable_verdict_still_reports_fingerprint_values(self):
        """classify()'s job is accuracy-trend classification, not
        provider-change detection (that's mechanism 2 in the design spec,
        kept deliberately separate) -- a fingerprint-only change with
        unchanged accuracy correctly stays "stable". But the two MCP
        tools' own descriptions promise reporting fingerprint status
        regardless of verdict, and nothing previously surfaced it when
        the verdict stayed stable. Found by Codex backtest, 2026-09-02."""
        rows = [self._row(0.9, "a"), self._row(0.9, "a"),
                self._row(0.9, "b"), self._row(0.9, "b")]
        out = model_drift.classify(rows, noise_band=0.05)
        self.assertEqual(out["verdict"], "stable")
        self.assertEqual(out["fingerprint_values"]["behavioral_fingerprint"], ["b"])
