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
| Treadmill / indoor run | Speed at grade 0 (Garmin stores no incline) | m/min | `run_indoor` |
| Walking, hiking, mountaineering, snowshoeing (outdoor, GPS + altitude) | Metabolic power, Minetti | W/kg | `walk_outdoor` |
| Ski touring / splitboarding (`backcountry_skiing`, `backcountry_snowboarding`) | Metabolic power, Minetti, **climbs only** (gradient > +5 %) | W/kg | `walk_outdoor` |
| Treadmill walking | *excluded*: incline isn't recorded, and walking cost depends on it heavily | — | — |
| Running power | *not used*: usually Garmin's wrist estimate rather than a measurement; pace covers runs | — | — |

For walking-type activities without a GPS distance or altitude stream
(< 50 % of the time), we skip the activity as `no_gps_altitude`. Without
terrain, metabolic power is only speed.

Ski touring: every second not climbing steeper than +5 % is blanked, so a
chunk that contains descent, or a flat stretch, fails the 95 % coverage
rule. Only sustained climbs become points. Ski tours have no Durability,
because half their moving time is descending. Skinning on snow costs more
than Minetti's walking model predicts, so ski-tour points read as more
output-per-beat than they are. They're kept in `walk_outdoor`, as asked;
treat mixed hike/tour windows with care.

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
     when that's lower. **Walking/hiking/ski touring** start lower, at
     `HRrest + 0.30 · (HRmax − HRrest)` (30 % of HR reserve; 50 % of HRmax if
     resting HR is unknown). Slow walking on gentle grades is 80–125 bpm, well
     under 65 % of HRmax, and HR still tracks VO₂ linearly above ~30 % of
     reserve.
   - **HR not trending:** the least-squares slope of the 30 s-smoothed HR
     across the chunk is within **±2.0 bpm/min**. A chunk still catching a
     transition (HR settling after a change, or drifting) is rejected. HR that
     *oscillates* around a flat level is kept, e.g. ±10 bpm over rollers. An
     HR-spread rule (< 5 bpm) used to reject nearly every outdoor chunk for
     exactly that.
   - low variability: **power** VI = NP ÷ average ≤ 1.05 indoors, **≤ 1.10
     for outdoor rides**, where rollers move power around at a steady effort
     (NP from 30 s rolling power); **speed-based** CV of the 30 s rolling
     output < 5 % for runs, **< 15 % for walking/hiking/ski touring**: trail
     pace changes constantly with steps, roots and gradient, even at a steady
     effort.
6. Only runs of **≥ 2 consecutive valid chunks** (≥ 10 min steady) count.
   Each valid chunk becomes one point: (mean output, mean HR, date,
   temperature for outdoor activities).

HRmax and LTHR are **per sport**, from Garmin's own HR-zone settings (cycling,
running, and the default set for walking/hiking). If a sport has none, the
default set is used. With no zones at all, the highest HR seen in that sport
over the last 120 days stands in for HRmax.

## 3. Aerobic Efficiency

- **Model: shared slope, per-window intercept.** HR = a + b · output.
  - **One slope b per user and sport:**
    - Sports: cycling (indoor + outdoor pooled), running, walking.
    - Fitted with **Theil–Sen** (median of pairwise slopes) over every valid
      chunk of the last **180 days**.
    - Pairs only between chunks of the **same series** ≤ 28 days apart, whose
      outputs differ by ≥ 5 % of the median. The indoor/outdoor HR offset
      and fitness changes over months move the intercept, not the slope, and
      would bias pairs across them. Near-equal outputs make the pair's ratio
      pure noise.
    - The slope must be **physiologically plausible**:

      | Output | Plausible b | Population default |
      |---|---|---|
      | Power | 0.2–0.6 bpm/W | **0.30 bpm/W** |
      | Running speed / GAS | 0.15–0.75 bpm per m/min (≈ 2.5–12.5 bpm per km/h) | **0.40 bpm per m/min** |
      | Walking metabolic power | 2.5–12 bpm per W/kg | **6 bpm per W/kg** |

      The run and hike ranges are the bike range carried over through
      metabolic cost: about 23 % cycling efficiency, riders of 55–85 kg, and
      Minetti's 3.6 J/kg/m flat running cost.
    - `source`:
      - `fitted`: plausible, from ≥ 40 chunks.
      - `shrunk`: plausible but fewer chunks, blended
        `b = w · fitted + (1 − w) · default` with `w = n / 40`.
      - `default`: fewer than 8 chunks or 2 sessions, under 20 pairs, or an
        implausible fit.
  - **Per series (indoor/outdoor) and rolling 28-day window** (weekly: every
    Sunday, plus today): only the intercept,
    `a_w = median(HR_i − b · output_i)`. It's heat-weighted (§ Heat).
  - Why: a slope fitted inside one 28-day window rests on a few sessions. On
    a narrow output range it swung freely, and so did the headline (outdoor
    bike jumped between ~155 and ~210 W). Fitness moves the line up and down,
    not its steepness, so that's what each window estimates now.
- **Reference HR, running: your own typical easy-run HR.** It's the median of
  each run's median steady-chunk HR over the last 180 days, rounded. Every
  run counts once, so one long run can't dominate. It needs ≥ 3 runs; until
  then the formula below applies. Once set it only moves when the measured
  value shifts by ≥ 3 bpm. Every week of history is re-read at the same
  reference, so a move only relabels the line, but it shouldn't flicker.
  Why: zones compress very differently between people. A fit runner's easy
  pace sits far below threshold, while an untrained runner is in Z3 as soon as
  they run at all. A fixed 65 % of reserve could sit 10+ bpm below every run
  someone does, which turned the headline into an extrapolation (Anna:
  152 bpm reference, runs at 157–169).
- **Reference HR, cycling and walking:** `HRref = HRrest + 0.65 · (HRmax − HRrest)`,
  rounded, or **0.40 for walking/hiking**, so it sits inside the HR range
  hikes actually produce. The API's `hr_ref_source` says which applies
  (`easy_sessions` or `hr_reserve`). HRrest is Garmin's configured resting HR if you set
  one. Otherwise it's the median of your daily resting HR over 28 days,
  measured once and then **frozen**, so the whole history stays comparable.
- **Headline:** `Output@HRref = (HRref − a_w) / b`, e.g. "Power at 137 bpm:
  177 W" or "Pace at 145 bpm: 5:12/km". Higher means fitter.
- **Inverse:** `HR at reference output = a_w + b · ref`. Lower means fitter.
  The reference output defaults to the median of your last 56 days of points,
  rounded to 5 W, 0.1 W/kg or a 5 s/km pace. It stays fixed after that, and
  you can pin your own in the expanded panel.
- **Confidence** comes from the window's own data: points n, and the residual
  MAD, the median |HR − a_w − b · output| in bpm.
  - **high:** n ≥ 15 and MAD ≤ 3 bpm.
  - **medium:** n ≥ 8 and MAD ≤ 5 bpm.
  - **low:** otherwise.

  There is no ratio fallback any more: with a fixed slope every window has a
  well-defined value.
- **API:** each series carries `slope` {b, unit, source, b_fitted, default,
  points, activities, pairs, reason}. Each week carries `intercept_bpm`,
  `slope_bpm_per_unit` and `residual_mad_bpm`. The snapshot has `slope_b`,
  `slope_source` and `residual_mad_bpm`.
- **Trend:** change vs the week ending ~6 weeks earlier (absolute and %), plus
  a weekly sparkline. For pace, the change is also shown in s/km.

### Heat

- Outdoor points carry the activity's temperature (Garmin's activity weather,
  converted from °F). Points **above 25 °C count half** in the fit.
- Optional HR correction, **off by default**: subtract 1 bpm per °C above
  20 °C before fitting. Turn it on with `AEROBIC_HEAT_CORRECTION=1` in `.env`.
- Indoor sessions have no temperature, which the UI notes.

## 4. Durability (aerobic decoupling)

- **Eligible:** ≥ **90 min of moving time**, for rides, runs and hikes alike.
  Coasting at speed counts as moving; stops and pauses don't.
- **No intervals**, checked two ways:
  - The dashboard's own interval detection: lap power for rides with power,
    pace with HR following it for runs. It catches long steady reps like
    2×20 min, which have a low VI.
  - Whole-session variability after the first 10 min: VI (NP ÷ average)
    **≤ 1.10 indoors**, **≤ 1.20 for outdoor rides**. Terrain moves power
    around even on a steady outdoor ride, and the regression below compares
    like with like. Runs and hikes use ≤ 1.10, with the same formula on GAS
    or metabolic power.
- Missing HR/output on more than 10 % of the time also disqualifies a session.
- After dropping the first 10 min, the remaining moving time is split into
  **two equal halves**, and the decoupling is worked out one of three ways:

| Method | When | How |
|---|---|---|
| `regression` (primary) | ≥ **4 valid 5-min chunks** in each half (the same filters as Aerobic Efficiency: aerobic HR band, HR not trending, VI ≤ 1.05/1.10 / CV < 5 % (15 % walking), ≥ 2 in a row), and the halves' chunk output ranges overlap (within 2 % of the median) | Fit HR = a + b·output on each half's chunks. Read both fits at a **common reference output**, the middle of the overlap. Decoupling % = (HR₂ − HR₁) / HR₂ × 100, the drop in output per heartbeat at the same output. Within one session the output range can be tiny (ERG), so each half's slope is clamped to 0…the plausible maximum and re-centred on that half's mean. The reading then always stays inside the data. |
| `np_ef` (fallback) | too few chunks or no overlap, the halves' **NP differ by ≤ 10 %**, and their **VIs (NP ÷ avg per half) differ by ≤ 0.05**. NP rewards surges, so a surgier second half inflates its NP and hides HR drift. | EF = NP ÷ mean HR per half. Runs and hikes use mean GAS / metabolic power, which already account for terrain. Decoupling % = (EF₁ − EF₂) / EF₁ × 100. |
| `excluded` | neither | The halves can't be compared. Stored with no score, with `exclude_reason` `np_differs` or `vi_differs`, and listed separately (`durability_excluded`). |

- **< 5 %** strong · **5–8 %** moderate · **> 8 %** low.
- Why regression first: a plain first-half vs second-half output/HR compares
  different efforts when the second half is harder, easier or hillier, and
  HR isn't proportional to output (it has an offset), so a lower output alone
  lowers output/HR. Reading both halves at the same output removes that.
- The card shows the last scored session and the last 8 as bars. Indoor and
  outdoor are kept separate, and outdoor sessions show the temperature (heat
  raises late-session HR). Each session carries `method`, `chunks_per_half`,
  `reference_output` with `hr_at_reference_output` (regression), and
  `normalized_output` per half.

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
  next sync re-analyses each account's full 120 days.

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
