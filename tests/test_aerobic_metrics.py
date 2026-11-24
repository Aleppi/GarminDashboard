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
    def _points(self, relation, outputs, start=date(2026, 11, 1)):
        return [{"date": start + timedelta(days=i % 20), "output": o, "hr": relation(o), "temp_c": None}
                for i, o in enumerate(outputs)]

    def test_linear_relation_recovers_output_at_hr_ref(self):
        rel = lambda w: 60 + 0.44 * w  # noqa: E731
        pts = self._points(rel, [150, 160, 165, 170, 175, 180, 190, 195, 200, 155] * 2)
        w = am.fit_window(pts, unit="W")
        self.assertTrue(w["fit_ok"])
        head = am.window_headline(w, hr_ref=140, ref_output=175)
        self.assertAlmostEqual(head["output_at_hr_ref"], (140 - 60) / 0.44, places=3)
        self.assertAlmostEqual(head["hr_at_ref_output"], rel(175), places=3)
        self.assertEqual(head["confidence"], "high")
        self.assertEqual(head["method"], "regression")

    def test_8_to_14_points_is_medium(self):
        pts = self._points(lambda w: 60 + 0.44 * w, [150, 160, 165, 170, 175, 180, 190, 195, 200, 155])
        w = am.fit_window(pts, unit="W")
        self.assertTrue(w["fit_ok"])
        self.assertEqual(am.window_headline(w, 140, 175)["confidence"], "medium")

    def test_implausible_bike_slope_falls_back_to_ratio(self):
        # b = 0.15 bpm/W (the Nov 2026 outdoor-bike case): HRref - a over b
        # turns small HR errors into tens of watts. Low + median ratio.
        pts = self._points(lambda w: 112 + 0.15 * w, [165, 175, 185, 195, 205, 215, 225, 235, 245, 250] * 2)
        w = am.fit_window(pts, unit="W")
        self.assertFalse(w["fit_ok"])
        self.assertEqual(w["fit_issue"], "slope_out_of_range")
        self.assertAlmostEqual(w["raw_b"], 0.15, places=6)
        head = am.window_headline(w, 137, 200)
        self.assertEqual(head["confidence"], "low")
        self.assertEqual(head["method"], "ratio")
        self.assertAlmostEqual(head["output_at_hr_ref"], w["ratio"] * 137)
        # Too steep is just as implausible.
        steep = self._points(lambda w: 20 + 0.8 * w, [150, 160, 170, 180, 190, 200, 210, 220] * 2)
        self.assertEqual(am.fit_window(steep, unit="W")["fit_issue"], "slope_out_of_range")

    def test_slope_bounds_per_unit(self):
        # Running: ~8 bpm per km/h = 0.48 bpm per m/min is plausible...
        run = self._points(lambda v: 50 + 0.48 * v, [160, 170, 180, 190, 200, 210, 165, 185] * 2)
        self.assertTrue(am.fit_window(run, unit="m/min")["fit_ok"])
        # ...but the same numbers read as bpm/W would be fine too, while a
        # 0.1 bpm per m/min slope isn't.
        flat = self._points(lambda v: 120 + 0.1 * v, [160, 170, 180, 190, 200, 210, 165, 185] * 2)
        self.assertEqual(am.fit_window(flat, unit="m/min")["fit_issue"], "slope_out_of_range")
        hike = self._points(lambda m: 60 + 8 * m, [5.0, 5.5, 6.0, 6.5, 7.0, 7.5, 5.2, 6.8] * 2)
        self.assertTrue(am.fit_window(hike, unit="W/kg")["fit_ok"])

    def test_output_range_under_15_pct_is_low(self):
        outs = [190, 195, 200, 205, 210, 192, 198, 202, 208, 196, 204, 200, 194, 206, 199, 201]  # 10% range
        w = am.fit_window(self._points(lambda w: 60 + 0.4 * w, outs), unit="W")
        self.assertEqual(w["fit_issue"], "narrow_output_range")
        self.assertEqual(am.window_headline(w, 140, 200)["confidence"], "low")

    def test_robust_to_outlier(self):
        rel = lambda w: 60 + 0.44 * w  # noqa: E731
        pts = self._points(rel, [150, 160, 165, 170, 175, 180, 190, 195, 200, 155])
        pts[3]["hr"] += 25
        head = am.window_headline(am.fit_window(pts), 140, 175)
        self.assertAlmostEqual(head["output_at_hr_ref"], (140 - 60) / 0.44, delta=2)

    def test_narrow_range_falls_back_to_ratio(self):
        pts = self._points(lambda w: 137, [174, 175, 176, 175, 174, 176, 175, 175])
        w = am.fit_window(pts)
        self.assertFalse(w["fit_ok"])
        head = am.window_headline(w, 140, 175)
        self.assertEqual(head["confidence"], "low")
        self.assertAlmostEqual(head["hr_at_ref_output"], 137, delta=0.5)

    def test_too_few_points_low_confidence(self):
        pts = self._points(lambda w: 60 + 0.44 * w, [150, 170, 190])
        self.assertEqual(am.window_headline(am.fit_window(pts), 140, 175)["confidence"], "low")

    def test_single_session_never_fits(self):
        # Cardiac drift inside one ride is not an HR-output relationship.
        pts = self._points(lambda w: 60 + 0.44 * w, [150, 160, 165, 170, 175, 180, 190, 195, 200, 155])
        for p in pts:
            p["activity_id"] = "one"
        w = am.fit_window(pts)
        self.assertFalse(w["fit_ok"])
        self.assertEqual(am.window_headline(w, 140, 175)["confidence"], "low")

    def test_wild_extrapolation_falls_back(self):
        w = {"n": 10, "fit_ok": True, "a": 140.0, "b": 0.06, "hr_min": 150, "hr_max": 165,
             "out_min": 280, "out_max": 330, "ratio": 2.0}
        head = am.window_headline(w, 120, 300)  # fit would say -333 W
        self.assertEqual(head["confidence"], "low")
        self.assertAlmostEqual(head["output_at_hr_ref"], 240)

    def test_flat_slope_rejected(self):
        # HR barely responds to output: solving for HRref would be unstable.
        pts = self._points(lambda w: 144 + 0.011 * w, [280, 290, 300, 310, 320, 330, 340, 350, 285, 345])
        w = am.fit_window(pts)
        self.assertFalse(w["fit_ok"])
        self.assertEqual(am.window_headline(w, 145, 300)["confidence"], "low")

    def test_heat_downweight_and_correction(self):
        self.assertEqual(am.heat_weight(30), 0.5)
        self.assertEqual(am.heat_weight(20), 1.0)
        self.assertEqual(am.heat_weight(None), 1.0)
        self.assertEqual(am.heat_corrected_hr(150, 28), 142)
        self.assertEqual(am.heat_corrected_hr(150, 15), 150)

    def test_reference_hr(self):
        self.assertEqual(am.reference_hr(50, 186), 138)

    def test_weekly_windows_and_change(self):
        start = date(2026, 9, 1)
        pts = []
        for d in range(84):
            day = start + timedelta(days=d)
            gain = d * 0.3  # getting fitter: same HR, more watts
            for w in (150, 175, 200):
                pts.append({"date": day, "output": w + gain, "hr": 60 + 0.44 * w, "temp_c": None})
        weeks = am.weekly_windows(pts, start + timedelta(days=83))
        for w in weeks:
            w.update(am.window_headline(w, 140, 175))
        ch = am.change_vs(weeks, "output_at_hr_ref")
        self.assertIsNotNone(ch)
        self.assertGreater(ch["abs"], 10)  # ~0.3 W/day * 42 days

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
