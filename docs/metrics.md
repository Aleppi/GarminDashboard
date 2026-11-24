# Aerobic Efficiency and Durability

Most numbers on TrainState come from Garmin (readiness, training load, VO2 max,
endurance score). These two don't. We compute them from each activity's raw
second-by-second stream (heart rate, power, speed, distance, altitude), so you
can see exactly what goes in. Code: `aerobic_metrics.py` (the method),
`aerobic_sync.py` (fetching and caching), tests in `tests/test_aerobic_metrics.py`.

- **Aerobic Efficiency:** *am I getting fitter?* It's your output at a fixed,
  easy heart rate, tracked week by week.
- **Durability:** *can I hold output late in long sessions?* It measures how
  much output per heartbeat drops from the first half of a long session to the
  second half.

Both need heart rate. Activities without HR are skipped.

## 1. Output signal per sport

| Activity | Output | Unit | Series |
|---|---|---|---|
| Cycling with a power meter (outdoor) | Power | W | `bike_outdoor` |
| Cycling with power, indoor/virtual | Power | W | `bike_indoor` |
| Cycling **without** power | *excluded*: bike speed depends on wind, drafting and terrain | — | — |
| Running, outdoor | Grade-adjusted speed (GAS), Minetti | m/min, shown as pace | `run_outdoor` |
| Running power (watch or pod) | Power, kept separate from GAS | W | `run_power_outdoor` / `_indoor` |
| Treadmill / indoor run | Speed at grade 0 (Garmin stores no incline) | m/min | `run_indoor` |
| Walking, hiking, ski touring (outdoor) | Metabolic power, Minetti | W/kg | `walk_outdoor` |
| Treadmill walking | *excluded*: incline isn't recorded, and walking cost depends on it heavily | — | — |

Indoor and outdoor are always **separate series**. Indoors there's no wind or
weather heat, and trainer/treadmill speed is measured differently from GPS.

### Minetti et al. (2002) energy cost

`i` is the gradient as a fraction, clamped to −0.45…+0.45 (the range the
polynomials were fitted on).

- Running: `Cr(i) = 155.4 i⁵ − 30.4 i⁴ − 43.3 i³ + 46.3 i² + 19.5 i + 3.6` J·kg⁻¹·m⁻¹
- Walking: `Cw(i) = 280.5 i⁵ − 58.7 i⁴ − 76.8 i³ + 51.9 i² + 19.6 i + 2.5` J·kg⁻¹·m⁻¹
- GAS = `v · Cr(i) / Cr(0)`: the flat-ground speed that would cost the same energy
- Walking metabolic power = `Cw(i) · v` (W/kg)

### Gradient

GPS and barometric altitude are noisy, so the gradient is never computed point
to point:

1. Altitude: 5 s rolling median (removes spikes), then a 30 s centred mean.
2. Gradient at each second = altitude change ÷ horizontal distance over a
   window of ≥ 25 m centred on that point. Below 5 m of movement the gradient
   is 0.

## 2. Steady chunks

For each activity and series:

1. Resample to 1 Hz. Gaps over 10 s between samples count as timer pauses.
2. **Remove stops:** zero power (power series) or speed under 1.0 m/s for runs,
   0.3 m/s for walking, lasting **more than 5 s**. Shorter blips, like a
   3-second coast, stay in.
3. **Drop the first 10 minutes** of moving time (HR settling).
4. Split the rest into segments at any stop or pause of ≥ 30 s, then each
   segment into **5-minute chunks**.
5. A chunk is **valid** if all of these hold:
   - ≥ 95 % of its seconds have both HR and output (no missing data)
   - mean HR is in the **aerobic band**: 65 % to 85 % of HRmax, capped at LTHR
     when that's lower
   - HR standard deviation < 5 bpm (not catching a transition)
   - low variability: **power** VI = NP ÷ average < 1.05 (NP from 30 s
     rolling power); **speed-based** CV of the 30 s rolling output < 5 %
6. Only runs of **≥ 2 consecutive valid chunks** (≥ 10 min steady) count.
   Each valid chunk becomes one point: (mean output, mean HR, date,
   temperature for outdoor activities).

HRmax and LTHR are **per sport**, from Garmin's own HR-zone settings (cycling,
running, and the default set for walking/hiking). If a sport has none, the
default set is used. With no zones at all, the highest HR seen in that sport
over the last 120 days stands in for HRmax.

## 3. Aerobic Efficiency

- **Model:** for each series, a rolling **28-day window**, evaluated weekly
  (every Sunday, plus today for the week in progress). Within the window we fit
  **HR = a + b · output** with a robust **Huber** regression (iteratively
  reweighted least squares, scale from the median absolute deviation). We use
  Huber rather than Theil–Sen because it accepts per-point weights, which the
  heat handling needs.
- **Reference HR** per sport: `HRref = HRrest + 0.65 · (HRmax − HRrest)`,
  rounded. HRrest is Garmin's configured resting HR if you set one. Otherwise
  it's the median of your daily resting HR over 28 days, measured once and then
  **frozen**, so the whole history stays comparable. HRref only changes when
  HRmax or a configured resting HR changes.
- **Headline:** `Output@HRref = (HRref − a) / b`, e.g. "Power at 137 bpm:
  177 W" or "Pace at 145 bpm: 5:12/km". Higher means fitter.
- **Inverse:** `HR at reference output = a + b · ref`, e.g. "HR at 175 W:
  136 bpm". Lower means fitter. The reference output defaults to the median of
  your last 56 days of points, rounded to 5 W, 0.1 W/kg or a 5 s/km pace. It
  stays fixed after that, and you can pin your own in the expanded panel.
- **Confidence**
  - **high:** ≥ 8 points from ≥ 2 sessions, output range (max − min) ≥ 10 %
    of the median, slope b > 0, HR responds to output (elasticity
    `b · median output / median HR ≥ 0.10`), and HRref within 5 bpm of the HR
    range actually seen.
  - **medium:** the same fit, but HRref is outside that HR range, i.e. the fit
    is extrapolated.
  - **low:** no trustworthy fit, or it would land more than 25 % beyond the
    outputs actually seen. The fallback is the median output/HR ratio × HRref
    (or ref ÷ ratio for the inverse).

  The single-session rule exists because within one ride, HR drift at
  constant power isn't an HR–output relationship. The elasticity rule exists
  because a nearly flat slope makes `(HRref − a) / b` swing wildly.
- **Trend:** change vs the week ending ~6 weeks earlier (absolute and %), plus
  a weekly sparkline. For pace, the change is also shown in s/km.

### Heat

- Outdoor points carry the activity's temperature (Garmin's activity weather,
  converted from °F). Points **above 25 °C count half** in the fit.
- Optional HR correction, **off by default**: subtract 1 bpm per °C above
  20 °C before fitting. Turn it on with `AEROBIC_HEAT_CORRECTION=1` in `.env`.
- Indoor sessions have no temperature, which the UI notes.

## 4. Durability (aerobic decoupling)

- **Eligible:** ≥ 90 min of moving time, same sport adapters, and **no
  intervals**: the whole session's VI (NP ÷ average, after the first 10 min)
  must be < 1.10. Speed-based sports use the same formula on GAS or metabolic
  power, which is the equivalent limit. Missing HR/output on more than 10 % of
  the time also disqualifies a session.
- After dropping the first 10 min, the remaining moving time is split into
  two equal halves. `EF = mean output ÷ mean HR` for each half (seconds where
  both are present).
- **Decoupling % = (EF₁ − EF₂) / EF₁ × 100**
- **< 5 %** strong · **5–8 %** moderate · **> 8 %** low.
- The card shows the last eligible session and the last 8 as bars. Indoor and
  outdoor are kept separate, and outdoor sessions show the temperature (heat
  raises late-session HR).

## 5. Caching and compute

- `trainstate-aerobic.timer` runs `aerobic_sync.py` every 30 minutes. The
  first run for an account looks back **120 days**. After that it re-lists
  the last 10 days and only processes activities it hasn't seen before.
- Each activity costs one Garmin request (the 1 Hz stream via the activity
  details endpoint, about 1 s), plus a weather request for outdoor ones, and
  0.1–1 s of Python on a Raspberry Pi. Only results are stored, in
  `data/aerobic.db`: one row per valid chunk and one per durability session.
  Activities that produce nothing (swims, no HR, bike without power) are
  remembered so they're never fetched again.
- The weekly fits are rebuilt from those rows on each sync, which takes
  milliseconds. The dashboard, `/api/aerobic` and the MCP tools only read the
  cache.
- Changing the method? Bump `ALGO_VERSION` in `aerobic_metrics.py`, and the
  next sync re-analyses everything.

## 6. Where it shows up

- Dashboard: **Aerobic Efficiency** and **Durability** tiles under VO2 max /
  Endurance Score. Click either to expand weekly history, sport tabs and an
  indoor/outdoor toggle.
- `get_training_snapshot` → `aerobic`: per series headline, reference HR and
  output, 6-week change, confidence, sparkline, last durability session.
- MCP `get_aerobic_efficiency(series, weeks, include_chunks)`: full weekly
  series, durability per session, and optionally every chunk point.

## 7. Known limits

- Walking and hiking often sit below 65 % of HRmax, and real trail terrain
  rarely keeps metabolic power within 5 % CV. So hike data is sparse by design:
  the filters favour trustworthy points over many points.
- Outdoor riding with frequent coasting or surges produces few steady chunks.
  Indoor ERG rides give the cleanest signal.
- Treadmill speed comes from the watch or foot pod and isn't GPS-checked.
