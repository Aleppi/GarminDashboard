"""Synthetic-stream tests for aerobic_metrics. Run: .venv/bin/python -m unittest discover tests"""

from __future__ import annotations

import math
import os
import random
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aerobic_metrics as am  # noqa: E402

T0 = 1_763_000_000  # arbitrary epoch start
ZONES = {"CYCLING": {"hr_max": 186, "lthr": 166}, "RUNNING": {"hr_max": 199, "lthr": 175},
         "DEFAULT": {"hr_max": 199, "lthr": 174}}


def details(samples: list[dict]) -> dict:
    """Build a Garmin-details-shaped response from {t, hr, power, speed, dist, alt} samples."""
    keys = ["directTimestamp", "directHeartRate", "directPower", "directSpeed", "sumDistance", "directElevation"]
    names = ["t", "hr", "power", "speed", "dist", "alt"]
    rows = []
    for s in samples:
        vals = [s.get(n) for n in names]
        vals[0] = s["t"] * 1000
        rows.append({"metrics": vals})
    return {"metricDescriptors": [{"key": k, "metricsIndex": i} for i, k in enumerate(keys)],
            "activityDetailMetrics": rows}


def ride(seconds: int, power_fn, hr_fn, start: int = T0, pause_at: int = None, pause_len: int = 0) -> list[dict]:
    out, t = [], start
    for i in range(seconds):
        if pause_at is not None and i == pause_at:
            t += pause_len
        out.append({"t": t, "hr": hr_fn(i), "power": power_fn(i), "speed": 8.0, "dist": 8.0 * i, "alt": 100.0})
        t += 1
    return out


def run(seconds: int, speed_fn, hr_fn, grade: float = 0.0) -> list[dict]:
    out, d = [], 0.0
    for i in range(seconds):
        v = speed_fn(i)
        out.append({"t": T0 + i, "hr": hr_fn(i), "power": None, "speed": v, "dist": d, "alt": 100.0 + grade * d})
        d += v
    return out


class Minetti(unittest.TestCase):
    def test_flat_costs(self):
        self.assertAlmostEqual(am.minetti_run_cost(0), 3.6)
        self.assertAlmostEqual(am.minetti_walk_cost(0), 2.5)

    def test_clamped_outside_valid_range(self):
        self.assertEqual(am.minetti_run_cost(0.9), am.minetti_run_cost(0.45))
        self.assertEqual(am.minetti_walk_cost(-0.9), am.minetti_walk_cost(-0.45))

    def test_uphill_costs_more(self):
        self.assertGreater(am.minetti_run_cost(0.1), am.minetti_run_cost(0))
        self.assertGreater(am.minetti_walk_cost(0.2), am.minetti_walk_cost(0))


class SteadyChunks(unittest.TestCase):
    def test_constant_output_and_hr_gives_exact_ef(self):
        res = am.analyze_activity(details(ride(3600, lambda i: 175, lambda i: 137)), "virtual_ride", ZONES)
        pts = res["series"]["bike_indoor"]["points"]
        # 60 min - 10 min warm-up = 50 min = 10 chunks
        self.assertEqual(len(pts), 10)
        for p in pts:
            self.assertAlmostEqual(p["output"], 175)
            self.assertAlmostEqual(p["hr"], 137)
            self.assertAlmostEqual(p["output"] / p["hr"], 175 / 137)

    def test_first_ten_minutes_dropped(self):
        # Garbage HR in the first 10 min must not matter.
        res = am.analyze_activity(details(ride(1800, lambda i: 175, lambda i: 90 if i < 600 else 137)),
                                  "virtual_ride", ZONES)
        pts = res["series"]["bike_indoor"]["points"]
        self.assertEqual(len(pts), 4)
        self.assertEqual(pts[0]["t_start"], T0 + 600)

    def test_pause_removed(self):
        # A 5-min timer pause: no samples at all for that span.
        samples = ride(2400, lambda i: 175, lambda i: 137, pause_at=1200, pause_len=300)
        grid = am.resample_1hz(samples)
        self.assertEqual(len(grid), 2400)
        pts, _ = am.steady_chunks(grid, "bike_indoor", 186, 166)
        # 600 s left before the pause (2 chunks), 1200 s after (4 chunks).
        self.assertEqual(len(pts), 6)
        self.assertEqual(pts[2]["t_start"], T0 + 1200 + 300)

    def test_zero_power_stop_removed(self):
        # 60 s of zero power mid-ride: removed, and splits the segment.
        samples = ride(2400, lambda i: 0 if 1500 <= i < 1560 else 175, lambda i: 137)
        grid = am.resample_1hz(samples)
        idx, breaks = am.active_seconds(grid, "bike_indoor")
        self.assertEqual(len(idx), 2400 - 60)
        self.assertEqual(sum(breaks), 1)

    def test_short_coast_kept(self):
        samples = ride(1200, lambda i: 0 if 700 <= i < 703 else 175, lambda i: 137)
        idx, _ = am.active_seconds(am.resample_1hz(samples), "bike_indoor")
        self.assertEqual(len(idx), 1200)

    def test_intervals_rejected(self):
        # 3 min at 300 W / 2 min at 120 W, HR following.
        samples = ride(3600, lambda i: 300 if (i % 300) < 180 else 120, lambda i: 150 if (i % 300) < 180 else 130)
        res = am.analyze_activity(details(samples), "virtual_ride", ZONES)
        s = res["series"]["bike_indoor"]
        self.assertEqual(s["points"], [])
        self.assertIsNone(s["durability"])

    def test_short_surges_rejected_by_vi(self):
        # 60 s at 350 W every 5 min (chunk VI ~1.13), otherwise 170 W — HR barely moves.
        samples = ride(3600, lambda i: 350 if (i % 300) < 60 else 170, lambda i: 138)
        pts = am.analyze_activity(details(samples), "virtual_ride", ZONES)["series"]["bike_indoor"]["points"]
        self.assertEqual(pts, [])

    def test_hr_outside_band_rejected(self):
        pts = am.analyze_activity(details(ride(3600, lambda i: 120, lambda i: 105)),
                                  "virtual_ride", ZONES)["series"]["bike_indoor"]["points"]
        self.assertEqual(pts, [])  # 105 < 0.65 * 186

    def test_unstable_hr_rejected(self):
        pts = am.analyze_activity(details(ride(3600, lambda i: 175, lambda i: 130 + (i % 300) / 15)),
                                  "virtual_ride", ZONES)["series"]["bike_indoor"]["points"]
        self.assertEqual(pts, [])

    def test_isolated_chunk_dropped(self):
        # Only one 5-min steady block between variable stretches.
        def p(i):
            return 175 if 900 <= i < 1200 else (300 if (i % 60) < 30 else 100)
        pts = am.analyze_activity(details(ride(2400, p, lambda i: 137)), "virtual_ride", ZONES)["series"]["bike_indoor"]["points"]
        self.assertEqual(pts, [])

    def test_hr_oscillation_ok_but_trend_rejected(self):
        # Rollers: HR swings +-8 bpm around a flat level (spread ~6 bpm) — steady.
        osc = ride(3600, lambda i: 175, lambda i: 140 + 8 * math.sin(2 * math.pi * i / 90))
        self.assertTrue(am.analyze_activity(details(osc), "virtual_ride", ZONES)["series"]["bike_indoor"]["points"])
        # HR climbing 3 bpm/min at constant power: mid-transition, rejected.
        ramp = ride(3600, lambda i: 175, lambda i: 125 + 3.0 * (i % 300) / 60)
        res = am.analyze_activity(details(ramp), "virtual_ride", ZONES)["series"]["bike_indoor"]
        self.assertEqual(res["points"], [])
        self.assertGreater(res["reasons"].get("hr_drifting", 0), 0)
        self.assertAlmostEqual(am.hr_trend_bpm_per_min([125 + 1.5 * i / 60 for i in range(300)]), 1.5, places=6)

    def test_walking_hr_floor_is_30pct_of_reserve(self):
        lo, hi = am.hr_band(199, 174, hr_rest=46, sport="walking")
        self.assertAlmostEqual(lo, 46 + 0.30 * 153)
        self.assertEqual(hi, 169.15)
        self.assertEqual(am.hr_band(199, 174, hr_rest=46, sport="running")[0], 0.65 * 199)
        self.assertEqual(am.hr_band(199, 174, sport="walking")[0], 0.50 * 199)  # no resting HR known
        zones = dict(ZONES, DEFAULT={"hr_max": 199, "lthr": 174, "hr_rest": 46})
        res = am.analyze_activity(details(run(3600, lambda i: 1.2, lambda i: 105, grade=0.08)), "hiking", zones)
        self.assertTrue(res["series"]["walk_outdoor"]["points"])  # 105 bpm: below 65% HRmax, above the walking floor

    def test_outdoor_bike_chunk_vi_limit_1_10(self):
        # Rollers: 290/150 W in 50 s swings, chunk VI ~1.09 — steady enough
        # outdoors, not on the trainer.
        def p(i):
            return 290 if (i % 100) < 50 else 150
        self.assertTrue(1.05 < am.variability_index([p(i) for i in range(600, 900)]) <= 1.10)
        samples = ride(3600, p, lambda i: 140)
        self.assertTrue(am.analyze_activity(details(samples), "road_biking", ZONES)["series"]["bike_outdoor"]["points"])
        self.assertEqual(am.analyze_activity(details(samples), "virtual_ride", ZONES)["series"]["bike_indoor"]["points"], [])

    def test_missing_hr_rejected(self):
        pts = am.analyze_activity(details(ride(3600, lambda i: 175, lambda i: None if i % 10 == 0 and i > 600 else 137)),
                                  "virtual_ride", ZONES)["series"]["bike_indoor"]["points"]
        self.assertEqual(pts, [])

    def test_bike_without_power_excluded(self):
        res = am.analyze_activity(details(ride(3600, lambda i: None, lambda i: 137)), "road_biking", ZONES)
        self.assertEqual(res["series"], {})
        self.assertEqual(res["skip_reason"], "bike_without_power")

    def test_no_hr_skipped(self):
        res = am.analyze_activity(details(ride(3600, lambda i: 175, lambda i: None)), "virtual_ride", ZONES)
        self.assertEqual(res["skip_reason"], "no_hr")

    def test_treadmill_walk_excluded(self):
        res = am.analyze_activity(details(run(3600, lambda i: 1.5, lambda i: 135)), "treadmill_walking", ZONES)
        self.assertEqual(res["skip_reason"], "treadmill_walk_no_incline")


class RunAndWalk(unittest.TestCase):
    def test_flat_run_gas_equals_speed(self):
        res = am.analyze_activity(details(run(3600, lambda i: 3.0, lambda i: 145)), "running", ZONES)
        pts = res["series"]["run_outdoor"]["points"]
        self.assertEqual(len(pts), 10)
        self.assertAlmostEqual(pts[0]["output"], 180.0, places=3)  # 3 m/s = 180 m/min

    def test_uphill_run_gas_faster_than_speed(self):
        res = am.analyze_activity(details(run(3600, lambda i: 2.5, lambda i: 150, grade=0.05)), "running", ZONES)
        pts = res["series"]["run_outdoor"]["points"]
        self.assertTrue(pts)
        expected = 150 * am.minetti_run_cost(0.05) / 3.6
        self.assertAlmostEqual(pts[0]["output"], expected, delta=0.5)

    def test_noisy_altitude_smoothed(self):
        rnd = random.Random(1)
        samples = run(3600, lambda i: 3.0, lambda i: 145)
        for s in samples:
            s["alt"] += rnd.uniform(-2, 2)  # baro/GPS jitter on a flat road
        pts = am.analyze_activity(details(samples), "running", ZONES)["series"]["run_outdoor"]["points"]
        self.assertEqual(len(pts), 10)
        for p in pts:
            self.assertAlmostEqual(p["output"], 180.0, delta=180 * 0.02)

    def test_walking_tolerates_more_pace_variation_than_running(self):
        # Pace swinging +-15% over 2 min (smoothed CV ~10%): steady for a hike, not for a run.
        def v(base):
            return lambda i: base * (1 + 0.15 * math.sin(2 * math.pi * i / 120))
        zones = dict(ZONES, DEFAULT={"hr_max": 199, "lthr": 174, "hr_rest": 46})
        hike = am.analyze_activity(details(run(3600, v(1.2), lambda i: 110, grade=0.08)), "hiking", zones)
        self.assertTrue(hike["series"]["walk_outdoor"]["points"])
        runr = am.analyze_activity(details(run(3600, v(3.0), lambda i: 150)), "running", zones)
        self.assertEqual(runr["series"]["run_outdoor"]["points"], [])
        self.assertGreater(runr["series"]["run_outdoor"]["reasons"].get("variable_output", 0), 0)

    def test_walk_metabolic_power(self):
        res = am.analyze_activity(details(run(3600, lambda i: 1.2, lambda i: 135, grade=0.10)), "hiking", ZONES)
        pts = res["series"]["walk_outdoor"]["points"]
        self.assertTrue(pts)
        self.assertAlmostEqual(pts[0]["output"], am.minetti_walk_cost(0.10) * 1.2, delta=0.05)

    def test_snowshoe_and_touring_types_map_to_walk(self):
        for t in ("hiking", "mountaineering", "snow_shoe_ws", "backcountry_skiing", "backcountry_snowboarding",
                  "backcountry_skiing_snowboarding_ws"):
            self.assertEqual(am.classify_activity(t, False), (["walk_outdoor"], None), t)

    def test_walk_needs_gps_and_altitude(self):
        samples = run(3600, lambda i: 1.2, lambda i: 140, grade=0.10)
        for x in samples:
            x["alt"] = None
        res = am.analyze_activity(details(samples), "hiking", ZONES)
        self.assertEqual(res["skip_reason"], "no_gps_altitude")

    def _tour(self, climb_grade):
        # 50 min climbing at 1.0 m/s, then 25 min descending at 4 m/s, then 30 more min climbing.
        out, d, alt = [], 0.0, 1000.0
        for i in range(6300):
            up = i < 3000 or i >= 4500
            v = 1.0 if up else 4.0
            out.append({"t": T0 + i, "hr": 140 if up else 125, "power": None, "speed": v, "dist": d, "alt": alt})
            d += v
            alt += v * (climb_grade if up else -0.25)
        return out

    def test_ski_touring_counts_only_climbs(self):
        res = am.analyze_activity(details(self._tour(0.12)), "backcountry_skiing", ZONES)
        s = res["series"]["walk_outdoor"]
        self.assertEqual(s["durability_reason"], "uphill_only")
        self.assertTrue(s["points"])
        for p in s["points"]:
            self.assertTrue(T0 + 600 <= p["t_start"] < T0 + 3000 - 299 or p["t_start"] >= T0 + 4500, p["t_start"])
            self.assertAlmostEqual(p["output"], am.minetti_walk_cost(0.12) * 1.0, delta=0.1)

    def test_ski_touring_gentle_climb_not_uphill_enough(self):
        res = am.analyze_activity(details(self._tour(0.03)), "backcountry_skiing", ZONES)
        self.assertEqual(res["series"]["walk_outdoor"]["points"], [])
        # The same terrain on foot isn't restricted to climbs.
        res = am.analyze_activity(details(self._tour(0.03)), "hiking", ZONES)
        self.assertTrue(res["series"]["walk_outdoor"]["points"])

    def test_treadmill_run_uses_raw_speed(self):
        res = am.analyze_activity(details(run(3600, lambda i: 3.0, lambda i: 145, grade=0.05)), "treadmill_running", ZONES)
        self.assertAlmostEqual(res["series"]["run_indoor"]["points"][0]["output"], 180.0)

    def test_run_stops_removed(self):
        samples = run(3000, lambda i: 0.0 if 1500 <= i < 1560 else 3.0, lambda i: 145)
        idx, breaks = am.active_seconds(am.resample_1hz(samples), "run_outdoor")
        self.assertEqual(len(idx), 3000 - 60)
        self.assertEqual(sum(breaks), 1)


class Model(unittest.TestCase):
    """Shared-slope model: one slope per sport (Theil-Sen, 180 days), one
    intercept per series and 28-day window."""

    def _points(self, relation, outputs, start=date(2026, 11, 1), series="bike_indoor", sessions=5):
        return [{"date": start + timedelta(days=i % 20), "output": o, "hr": relation(o), "temp_c": None,
                 "series": series, "activity_id": f"{series}-{i % sessions}", "t_start": i}
                for i, o in enumerate(outputs)]

    def test_theil_sen_recovers_slope(self):
        pts = self._points(lambda w: 60 + 0.44 * w, [150, 160, 165, 170, 175, 180, 190, 195, 200, 155] * 5)
        fit = am.sport_slope(pts, "W", date(2026, 11, 24))
        self.assertEqual(fit["source"], "fitted")
        self.assertAlmostEqual(fit["b"], 0.44, places=6)

    def test_theil_sen_robust_to_outliers(self):
        pts = self._points(lambda w: 60 + 0.3 * w, [150, 160, 170, 180, 190, 200, 210, 220] * 6)
        for p in pts[::7]:
            p["hr"] += 25
        self.assertAlmostEqual(am.sport_slope(pts, "W", date(2026, 11, 24))["b"], 0.3, delta=0.02)

    def test_indoor_outdoor_offset_does_not_bias_slope(self):
        # Outdoor HR runs 8 bpm higher at the same power: pairs stay within a
        # series, so the pooled cycling slope is still 0.3.
        indoor = self._points(lambda w: 80 + 0.3 * w, [150, 160, 170, 180, 190, 200] * 4, series="bike_indoor")
        outdoor = self._points(lambda w: 88 + 0.3 * w, [210, 220, 230, 240, 250, 260] * 4, series="bike_outdoor")
        self.assertAlmostEqual(am.sport_slope(indoor + outdoor, "W", date(2026, 11, 24))["b"], 0.3, places=6)

    def test_little_data_shrinks_toward_default(self):
        pts = self._points(lambda w: 60 + 0.5 * w, [150, 160, 170, 180, 190, 200, 210, 220, 165, 205])
        fit = am.sport_slope(pts, "W", date(2026, 11, 24))
        self.assertEqual(fit["source"], "shrunk")
        w = 10 / am.SLOPE_FULL_POINTS
        self.assertAlmostEqual(fit["b"], w * 0.5 + (1 - w) * am.DEFAULT_SLOPE["W"], places=6)

    def test_implausible_slope_uses_default(self):
        # b = 0.15 bpm/W (Nov 2026 outdoor rides alone): population default instead.
        pts = self._points(lambda w: 112 + 0.15 * w, [165, 175, 185, 195, 205, 215, 225, 235, 245, 250] * 5)
        fit = am.sport_slope(pts, "W", date(2026, 11, 24))
        self.assertEqual((fit["source"], fit["reason"]), ("default", "fitted_slope_out_of_range"))
        self.assertAlmostEqual(fit["b_fitted"], 0.15, places=6)
        self.assertEqual(fit["b"], 0.30)

    def test_too_little_data_uses_default(self):
        pts = self._points(lambda w: 60 + 0.44 * w, [150, 170, 190])
        fit = am.sport_slope(pts, "m/min", date(2026, 11, 24))
        self.assertEqual((fit["b"], fit["source"]), (0.40, "default"))

    def test_old_chunks_outside_180_days_ignored(self):
        old = self._points(lambda w: 60 + 0.55 * w, [150, 170, 190, 210] * 20, start=date(2026, 1, 1))
        new = self._points(lambda w: 60 + 0.25 * w, [150, 170, 190, 210] * 20)
        self.assertAlmostEqual(am.sport_slope(old + new, "W", date(2026, 11, 24))["b"], 0.25, places=6)

    def test_window_intercept_and_headline(self):
        pts = self._points(lambda w: 60 + 0.44 * w, [150, 160, 165, 170, 175, 180, 190, 195, 200, 155] * 2)
        w = am.fit_window(pts, 0.44)
        self.assertAlmostEqual(w["a"], 60, places=6)
        self.assertAlmostEqual(w["mad"], 0, places=6)
        head = am.window_headline(w, hr_ref=140, ref_output=175)
        self.assertAlmostEqual(head["output_at_hr_ref"], (140 - 60) / 0.44, places=3)
        self.assertAlmostEqual(head["hr_at_ref_output"], 60 + 0.44 * 175, places=3)
        self.assertEqual(head["confidence"], "high")
        self.assertEqual(head["method"], "shared_slope")

    def test_narrow_window_still_well_defined(self):
        # All chunks at ~175 W: no per-window slope possible, but the
        # intercept under the shared slope is exact and the value sane.
        pts = self._points(lambda w: 137, [174, 175, 176, 175, 174, 176, 175, 175])
        head = am.window_headline(am.fit_window(pts, 0.3), 140, 175)
        self.assertAlmostEqual(head["output_at_hr_ref"], 175 + 3 / 0.3, delta=0.5)
        self.assertEqual(head["confidence"], "medium")  # 8 points

    def test_confidence_from_n_and_mad(self):
        self.assertEqual(am.window_confidence(20, 2.0), "high")
        self.assertEqual(am.window_confidence(20, am.CONF_HIGH[1] + 0.5), "medium")
        self.assertEqual(am.window_confidence(10, 1.0), "medium")
        self.assertEqual(am.window_confidence(20, am.CONF_MEDIUM[1] + 0.5), "low")
        self.assertEqual(am.window_confidence(5, 1.0), "low")

    def test_heat_weighted_intercept(self):
        cool = self._points(lambda w: 60 + 0.3 * w, [200] * 10)
        hot = self._points(lambda w: 70 + 0.3 * w, [200] * 8)
        for p in hot:
            p["temp_c"] = 30
        # 10 cool at weight 1 vs 8 hot at 0.5: the median stays with the cool points.
        self.assertAlmostEqual(am.fit_window(cool + hot, 0.3)["a"], 60, places=6)

    def test_heat_downweight_and_correction(self):
        self.assertEqual(am.heat_weight(30), 0.5)
        self.assertEqual(am.heat_weight(20), 1.0)
        self.assertEqual(am.heat_weight(None), 1.0)
        self.assertEqual(am.heat_corrected_hr(150, 28), 142)
        self.assertEqual(am.heat_corrected_hr(150, 15), 150)

    def test_reference_hr(self):
        self.assertEqual(am.reference_hr(50, 186), 138)
        self.assertEqual(am.reference_hr(46, 199, "walking"), 107)  # 40% of HR reserve

    def test_weekly_windows_and_change(self):
        start = date(2026, 9, 1)
        pts = []
        for d in range(84):
            day = start + timedelta(days=d)
            gain = d * 0.3  # getting fitter: same HR, more watts
            for w in (150, 175, 200):
                pts.append({"date": day, "output": w + gain, "hr": 60 + 0.44 * w, "temp_c": None})
        weeks = am.weekly_windows(pts, start + timedelta(days=83), 0.44)
        for w in weeks:
            w.update(am.window_headline(w, 140, 175))
        ch = am.change_vs(weeks, "output_at_hr_ref")
        self.assertIsNotNone(ch)
        self.assertAlmostEqual(ch["abs"], 0.3 * 42, delta=1)

    def test_default_ref_output_rounding(self):
        self.assertEqual(am.default_ref_output([171, 176, 178], "W"), 175.0)
        self.assertAlmostEqual(60000 / am.default_ref_output([190], "m/min"), 315)
        self.assertEqual(am.pace_str(60000 / 312), "5:12/km")


class Durability(unittest.TestCase):
    def test_constant_session_zero_decoupling(self):
        dur = am.analyze_activity(details(ride(7200, lambda i: 163, lambda i: 130)), "virtual_ride", ZONES)
        d = dur["series"]["bike_indoor"]["durability"]
        self.assertAlmostEqual(d["decoupling_pct"], 0.0, places=6)
        self.assertEqual(d["label"], "strong")

    def test_hr_drift_gives_expected_decoupling(self):
        # Constant power, HR 130 in the first half after warm-up, 140 in the second.
        def hr(i):
            return 130 if i < 600 + (7200 - 600) // 2 else 140
        d = am.analyze_activity(details(ride(7200, lambda i: 163, hr)), "virtual_ride", ZONES)["series"]["bike_indoor"]["durability"]
        self.assertAlmostEqual(d["decoupling_pct"], (1 - 130 / 140) * 100, places=3)
        self.assertEqual(d["label"], "moderate")
        self.assertEqual(d["method"], "regression")
        self.assertEqual((d["n_chunks1"], d["n_chunks2"]), (11, 11))

    def test_regression_compares_halves_at_common_output(self):
        # Power alternates 150/200 W in 10-min blocks; HR = 60 + 0.44 P, plus
        # 5 bpm in the second half. Mean-based EF would also see the power
        # mix; the per-half fits read at 175 W isolate the 5 bpm drift.
        half_i = 600 + (7200 - 600) // 2
        def p(i):
            return 150 if (i // 600) % 2 == 0 else 200
        def hr(i):
            return 60 + 0.44 * p(i) + (5 if i >= half_i else 0)
        d = am.analyze_activity(details(ride(7200, p, hr)), "virtual_ride", ZONES)["series"]["bike_indoor"]["durability"]
        self.assertEqual(d["method"], "regression")
        self.assertAlmostEqual(d["ref_output"], 175, places=6)
        self.assertAlmostEqual(d["hr1_at_ref"], 137, places=3)
        self.assertAlmostEqual(d["hr2_at_ref"], 142, places=3)
        self.assertAlmostEqual(d["decoupling_pct"], (1 - 137 / 142) * 100, places=3)

    def test_np_ef_fallback_without_chunks(self):
        # HR above LTHR the whole way: no aerobic-band chunks, so NP / HR.
        def hr(i):
            return 168 if i < 600 + (7200 - 600) // 2 else 172
        d = am.analyze_activity(details(ride(7200, lambda i: 230, hr)), "virtual_ride", ZONES)["series"]["bike_indoor"]["durability"]
        self.assertEqual(d["method"], "np_ef")
        self.assertEqual(d["n_chunks1"], 0)
        self.assertAlmostEqual(d["decoupling_pct"], (1 - 168 / 172) * 100, places=2)

    def test_np_fallback_needs_similar_variability(self):
        # Steady 230 W, then 300/150 W surges: NP within 10% (230 -> 248) but
        # VI 1.00 vs 1.09, so NP-EF would compare unlike halves.
        half_i = 600 + (7200 - 600) // 2
        def p(i):
            if i < half_i:
                return 230
            return 300 if (i % 120) < 60 else 150
        res = am.analyze_activity(details(ride(7200, p, lambda i: 168)), "virtual_ride", ZONES)
        d = res["series"]["bike_indoor"]["durability"]
        self.assertEqual(res["series"]["bike_indoor"]["durability_reason"], "not_comparable")
        self.assertEqual(d["exclude_reason"], "vi_differs")
        self.assertLess(abs(d["np2"] - d["np1"]) / d["np1"], 0.10)
        self.assertGreater(d["vi2"] - d["vi1"], 0.05)

    def test_halves_not_comparable_excluded(self):
        # 150 W then 200 W: chunk ranges don't overlap and NP differs by 33%.
        half_i = 600 + (7200 - 600) // 2
        res = am.analyze_activity(details(ride(7200, lambda i: 150 if i < half_i else 200,
                                               lambda i: 126 if i < half_i else 150)), "virtual_ride", ZONES)
        s = res["series"]["bike_indoor"]
        self.assertEqual(s["durability_reason"], "not_comparable")
        self.assertEqual(s["durability"]["method"], "excluded")
        self.assertEqual(s["durability"]["exclude_reason"], "np_differs")
        self.assertIsNone(s["durability"]["decoupling_pct"])

    def test_short_session_not_eligible(self):
        res = am.analyze_activity(details(ride(5000, lambda i: 163, lambda i: 130)), "virtual_ride", ZONES)
        self.assertEqual(res["series"]["bike_indoor"]["durability_reason"], "too_short")

    def test_outdoor_bike_allows_more_variability(self):
        # Terrain-driven VI ~1.12: steady outdoors, too variable for the trainer.
        def p(i):
            return 260 if (i % 240) < 120 else 130
        samples = ride(7200, p, lambda i: 135)
        g = am.resample_1hz(samples)
        vi = am.variability_index([s["power"] for s in samples[600:]])
        self.assertTrue(1.10 < vi < 1.15, vi)
        self.assertIsNotNone(am.durability(g, "bike_outdoor")[0])
        self.assertEqual(am.durability(g, "bike_indoor")[1], "variable_output")

    def test_outdoor_bike_vi_limit_is_1_20(self):
        def vi_ride(hi, lo):
            return am.resample_1hz(ride(7200, lambda i: hi if (i % 240) < 120 else lo, lambda i: 135))
        self.assertEqual(am.durability(vi_ride(285, 115), "bike_outdoor")[1], "ok")              # VI ~1.18
        self.assertEqual(am.durability(vi_ride(300, 100), "bike_outdoor")[1], "variable_output")  # VI ~1.23

    def test_coasting_counts_as_moving_time(self):
        # 95 min, with 10 min of coasting in 60 s stretches: pedalling time
        # is under 90 min but moving time isn't.
        samples = ride(95 * 60, lambda i: 0 if (i % 570) < 60 else 170, lambda i: 132)
        d, reason = am.durability(am.resample_1hz(samples), "bike_outdoor")
        self.assertEqual(reason, "ok")

    def test_interval_session_excluded(self):
        res = am.analyze_activity(details(ride(7200, lambda i: 163, lambda i: 130)), "virtual_ride", ZONES,
                                  interval_session=True)
        self.assertEqual(res["series"]["bike_indoor"]["durability_reason"], "intervals")
        self.assertTrue(res["series"]["bike_indoor"]["points"])  # steady chunks still count

    def test_labels(self):
        self.assertEqual(am.durability_label(4.9), "strong")
        self.assertEqual(am.durability_label(6), "moderate")
        self.assertEqual(am.durability_label(8.1), "low")


if __name__ == "__main__":
    unittest.main()
