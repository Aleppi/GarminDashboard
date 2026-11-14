(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const setText = (id, value) => { const el = $(id); if (el) el.textContent = value; };
  const NBSP = ' ';

  function fmtOrDash(v) {
    return (v === null || v === undefined || v === '') ? '—' : v;
  }

  // No "N to NextTier" once a score is missing (nothing to classify) or
  // already at the top published tier (nowhere higher to point at).
  function fmtCategoryLabel(score, category, nextCategory, pointsToNext, fallback) {
    if (score === null || score === undefined || !category) return fallback;
    if (!nextCategory || pointsToNext === null || pointsToNext === undefined) return category;
    return category + ' · ' + pointsToNext + ' to ' + nextCategory;
  }

  // --------------------------------------------------------- help tooltip --
  // JS-positioned via getBoundingClientRect + position:fixed, not a CSS
  // ::after — a pure-CSS tooltip anchored to its trigger has no way to
  // clamp itself inside the viewport or stay clear of a neighboring panel,
  // so a trigger near an edge (ACWR, the last of 4 stat tiles) either
  // overflowed into the panel next to it or ran off the screen on mobile.
  function showHelpTooltip(trigger) {
    const text = trigger.getAttribute('data-tooltip');
    if (!text) return;
    const tip = $('helpTooltip');
    if (!tip) return;
    tip.textContent = text;
    const tipWidth = Math.min(230, window.innerWidth - 24);
    tip.style.width = tipWidth + 'px';

    const rect = trigger.getBoundingClientRect();
    let left = rect.left + rect.width / 2 - tipWidth / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - tipWidth - 8));
    tip.style.left = left + 'px';

    // Prefer above the trigger; flip below only if there's not enough room
    // (e.g. the trigger sits near the very top of the viewport).
    if (rect.top > 120) {
      tip.style.bottom = (window.innerHeight - rect.top + 8) + 'px';
      tip.style.top = 'auto';
    } else {
      tip.style.top = (rect.bottom + 8) + 'px';
      tip.style.bottom = 'auto';
    }
    tip.classList.add('visible');
  }

  function hideHelpTooltip() {
    const tip = $('helpTooltip');
    if (tip) tip.classList.remove('visible');
  }

  function bindHelpTooltips() {
    document.querySelectorAll('.help-mark').forEach((el) => {
      el.addEventListener('mouseenter', () => showHelpTooltip(el));
      el.addEventListener('mouseleave', hideHelpTooltip);
      el.addEventListener('focus', () => showHelpTooltip(el));
      el.addEventListener('blur', hideHelpTooltip);
    });
  }

  // ---------------------------------------------------------- error banner --
  function renderError(message, errorType) {
    const banner = $('errorBanner');
    if (!message) { banner.style.display = 'none'; return; }
    banner.style.display = 'block';
    banner.innerHTML = '<b>Could not load live Garmin data.</b> ' + message;
    if (errorType === 'auth') {
      banner.innerHTML += ' <a href="/reauth" style="color:inherit;text-decoration:underline;">Sign in again &rarr;</a>';
    }
  }

  // -------------------------------------------------------------- hero ------
  function animateHero(target) {
    const el = $('heroFigure');
    if (target === null || target === undefined) { el.textContent = '—'; return; }
    const start = performance.now();
    const duration = 700;
    function tick(now) {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      el.innerHTML = Math.round(target * eased) + '<sup>/100</sup>';
      if (t < 1) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }

  function renderHero(data) {
    const hero = data.hero || {};
    animateHero(hero.readiness_now);
    setText('heroDate', data.date_label || '');
    setText('heroContext', hero.context || '');

    const wake = hero.wake_reading, latest = hero.latest_reading;
    setText('wakeScore', wake ? fmtOrDash(wake.score) : '—');
    setText('wakeTag', wake ? (wake.label || '') : 'No data');
    setText('wakeTime', wake && wake.time ? ('at wake, ' + wake.time) : '');
    setText('latestScore', latest ? fmtOrDash(latest.score) : '—');
    setText('latestTag', latest ? (latest.label || '') : 'No data');
    setText('latestTime', latest && latest.time ? ('now, ' + latest.time) : '');
  }

  // ------------------------------------------------------------ ridge chart -
  function smoothPath(points) {
    if (points.length < 3) return 'M' + points.map((p) => p[0] + ',' + p[1]).join('L');
    let d = 'M' + points[0][0] + ',' + points[0][1];
    for (let i = 0; i < points.length - 1; i++) {
      const p0 = points[i === 0 ? 0 : i - 1];
      const p1 = points[i];
      const p2 = points[i + 1];
      const p3 = points[i + 2 < points.length ? i + 2 : i + 1];
      const c1x = p1[0] + (p2[0] - p0[0]) / 6, c1y = p1[1] + (p2[1] - p0[1]) / 6;
      const c2x = p2[0] - (p3[0] - p1[0]) / 6, c2y = p2[1] - (p3[1] - p1[1]) / 6;
      d += ' C' + c1x + ',' + c1y + ' ' + c2x + ',' + c2y + ' ' + p2[0] + ',' + p2[1];
    }
    return d;
  }

  // Older-history chunks fetched on demand (scrolled-to-edge) live here,
  // keyed by date, separate from the regularly-polled recent window — so a
  // 60s auto-refresh (which only re-fetches the recent window) can't wipe
  // out history the user scrolled back to load. lastRidgeMainData is the
  // raw last poll result, kept so zooming can re-render without re-fetching.
  const ridgeExtraTrend = {};
  let lastRidgeMainData = null;
  let ridgePxPerDay = 13;
  const RIDGE_PX_PER_DAY_MIN = 4, RIDGE_PX_PER_DAY_MAX = 32;
  let ridgeLoadingMore = false;
  let ridgeNoMoreHistory = false;
  let ridgeListenersBound = false;
  let ridgeInitialRenderDone = false;
  const ridgeRaceMarkers = new Set(); // accumulated across the main poll + any lazy-loaded older chunks
  let ridgeFlagPx = []; // x position (CSS px, current zoom) of each drawn flag, for the legend visibility check
  const RIDGE_CHUNK_DAYS = 90;
  // Layout facts that only change on a full re-render (zoom, new data) —
  // panning alone doesn't touch these, only the Y-scale redrawVisibleWindow
  // derives from them each time.
  let ridgeLayout = null; // {allTrend, xAt, n, W, H, padTop, padBottom, chartWidth}
  let ridgeRedrawScheduled = false;

  function mergedRidgeTrend(ridge) {
    const byDate = {};
    for (const d of Object.values(ridgeExtraTrend)) byDate[d.date] = d;
    for (const d of (ridge && ridge.trend) || []) byDate[d.date] = d; // fresh poll wins on overlap
    return Object.values(byDate).sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
  }

  function loadOlderTrend() {
    const allDates = Object.keys(ridgeExtraTrend).concat(((lastRidgeMainData && lastRidgeMainData.trend) || []).map((d) => d.date));
    if (!allDates.length || ridgeLoadingMore || ridgeNoMoreHistory) return;
    const earliest = allDates.reduce((a, b) => (a < b ? a : b));
    const end = new Date(earliest + 'T00:00:00');
    end.setDate(end.getDate() - 1);
    const start = new Date(end);
    start.setDate(start.getDate() - (RIDGE_CHUNK_DAYS - 1));
    const endStr = isoDate(end), startStr = isoDate(start);

    ridgeLoadingMore = true;
    const ridgeScroll = $('ridgeScroll');
    const beforeScrollWidth = ridgeScroll.scrollWidth;
    fetch('/api/trend?start=' + startStr + '&end=' + endStr)
      .then((r) => r.json())
      .then((data) => {
        const got = (data && data.trend) || [];
        if (!got.length) { ridgeNoMoreHistory = true; return; }
        got.forEach((d) => { ridgeExtraTrend[d.date] = d; });
        ((data && data.race_markers) || []).forEach((d) => ridgeRaceMarkers.add(d));
        renderRidgeChart(lastRidgeMainData);
        // Keep the view anchored on what was visible — the chart just got
        // wider on the left, so scrollLeft needs to grow by the same amount.
        const ridgeScroll2 = $('ridgeScroll');
        ridgeScroll2.scrollLeft += (ridgeScroll2.scrollWidth - beforeScrollWidth);
      })
      .catch(() => {})
      .finally(() => { ridgeLoadingMore = false; });
  }

  function bindRidgeInteractions() {
    if (ridgeListenersBound) return;
    ridgeListenersBound = true;
    const ridgeScroll = $('ridgeScroll');

    ridgeScroll.addEventListener('wheel', function (e) {
      if (!e.deltaY) return;
      e.preventDefault();
      const beforeWidth = ridgeScroll.scrollWidth;
      const beforeLeft = ridgeScroll.scrollLeft;
      const anchorFrac = beforeWidth > ridgeScroll.clientWidth ? beforeLeft / (beforeWidth - ridgeScroll.clientWidth) : 1;
      const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
      ridgePxPerDay = Math.max(RIDGE_PX_PER_DAY_MIN, Math.min(RIDGE_PX_PER_DAY_MAX, ridgePxPerDay * factor));
      renderRidgeChart(lastRidgeMainData);
      const afterWidth = ridgeScroll.scrollWidth;
      ridgeScroll.scrollLeft = afterWidth > ridgeScroll.clientWidth ? anchorFrac * (afterWidth - ridgeScroll.clientWidth) : 0;
    }, { passive: false });

    ridgeScroll.addEventListener('scroll', function () {
      if (ridgeScroll.scrollLeft < 60) loadOlderTrend();
      scheduleRedrawVisibleWindow(); // also updates the race legend visibility
    });
  }

  // Shows the "race day" legend entry only while a flag is actually
  // scrolled into view — with lazy-loaded history the chart can span
  // months a flag isn't currently visible within.
  function updateRaceLegendVisibility() {
    const ridgeScroll = $('ridgeScroll');
    const left = ridgeScroll.scrollLeft, right = left + ridgeScroll.clientWidth;
    const anyVisible = ridgeFlagPx.some((x) => x >= left && x <= right);
    $('raceLegend').style.display = anyVisible ? '' : 'none';
  }

  // Rescales the Y axis to whatever's currently panned into view — the
  // highest point in the visible window keeps the same margin from the
  // top regardless of where you've scrolled, rather than one scale fixed
  // to the whole loaded range (which would leave most of the chart's
  // height unused whenever you're looking at a lower-lying stretch).
  // X positions never change here, only Y — panning doesn't touch layout.
  function redrawVisibleWindow() {
    if (!ridgeLayout) return;
    const { allTrend, xAt, n, W, H, padTop, padBottom, chartWidth } = ridgeLayout;
    const ridgeScroll = $('ridgeScroll');
    const left = ridgeScroll.scrollLeft, right = left + ridgeScroll.clientWidth;
    // W === chartWidth and the viewBox height === the SVG's actual CSS
    // height (both set in renderRidgeChart) — viewBox units ARE CSS
    // pixels, 1:1, no stretch in either axis. That's what keeps stroke
    // width, the flag emoji, and the today dot a constant on-screen size
    // regardless of zoom or scroll, with no compensation math needed here.
    const visLeftVB = left, visRightVB = right;

    const visible = allTrend.filter((d, i) => {
      if (d.ctl === null || d.atl === null) return false;
      const x = xAt(i);
      return x >= visLeftVB && x <= visRightVB;
    });
    const scaleSource = visible.length ? visible : allTrend.filter((d) => d.ctl !== null && d.atl !== null);

    const ctlVals = scaleSource.map((d) => d.ctl);
    const atlVals = scaleSource.map((d) => d.atl);
    const ctlMin = Math.min(...ctlVals) * 0.92, ctlMax = Math.max(...ctlVals) * 1.06 || 1;
    const atlMin = Math.min(...atlVals) * 0.85, atlMax = Math.max(...atlVals) * 1.08 || 1;
    const yCtl = (v) => H - padBottom - ((v - ctlMin) / (ctlMax - ctlMin || 1)) * (H - padTop - padBottom);
    const yAtl = (v) => H - padBottom - ((v - atlMin) / (atlMax - atlMin || 1)) * (H - padTop - padBottom);

    const ctlPts = [], atlPts = [];
    let lastRealIndex = -1;
    allTrend.forEach((d, i) => {
      if (d.ctl !== null) { ctlPts.push([xAt(i), yCtl(d.ctl)]); lastRealIndex = i; }
      if (d.atl !== null) atlPts.push([xAt(i), yAtl(d.atl)]);
    });
    const ctlLineD = smoothPath(ctlPts);
    const atlLineD = smoothPath(atlPts);
    const ctlAreaD = ctlLineD + ' L' + xAt(lastRealIndex) + ',' + H + ' L' + xAt(0) + ',' + H + ' Z';
    $('ctlLine').setAttribute('d', ctlLineD);
    $('atlLine').setAttribute('d', atlLineD);
    $('ctlArea').setAttribute('d', ctlAreaD);

    const todayX = xAt(lastRealIndex), todayY = yCtl(allTrend[lastRealIndex].ctl);
    const todayDot = $('todayDot');
    todayDot.setAttribute('cx', todayX);
    todayDot.setAttribute('cy', todayY);

    const flagsGroup = $('raceFlags');
    flagsGroup.innerHTML = '';
    ridgeFlagPx = [];
    allTrend.forEach((d, i) => {
      if (!ridgeRaceMarkers.has(d.date)) return;
      const x = xAt(i);
      const y = d.ctl !== null ? yCtl(d.ctl) : todayY; // future day: no line to anchor to, level with today instead
      flagsGroup.innerHTML +=
        '<line x1="' + x + '" y1="' + (y - 2) + '" x2="' + x + '" y2="' + (y - 38) + '" stroke="var(--gold)" stroke-width="2"/>' +
        '<text x="' + x + '" y="' + (y - 42) + '" font-size="16" text-anchor="middle">🚩</text>';
      ridgeFlagPx.push(x); // already CSS px — see the 1:1 note above
    });
    updateRaceLegendVisibility();

    const last = allTrend[lastRealIndex];
    $('ridgeCaption').textContent = 'Independent scales per line — hover for exact CTL/ATL/TSB. Current TSB ' +
      (last.tsb > 0 ? '+' : '') + fmtOrDash(last.tsb) + '.';
  }

  function scheduleRedrawVisibleWindow() {
    if (ridgeRedrawScheduled) return;
    ridgeRedrawScheduled = true;
    requestAnimationFrame(function () { ridgeRedrawScheduled = false; redrawVisibleWindow(); });
  }

  function renderRidgeChart(ridge) {
    if (ridge) {
      lastRidgeMainData = ridge;
      (ridge.race_markers || []).forEach((d) => ridgeRaceMarkers.add(d));
    }
    bindRidgeInteractions();
    // allTrend is gapless — one entry per requested day, including future
    // days that only exist to give a marked upcoming race somewhere to
    // place its flag (ctl/atl/tsb null there, and any rare real gap).
    // Index into allTrend is what xAt() uses, so a future day still gets a
    // well-defined x position even though it has no line drawn through it.
    const allTrend = mergedRidgeTrend(ridge);
    const realTrend = allTrend.filter((d) => d.ctl !== null && d.atl !== null);
    const caption = $('ridgeCaption');

    if (realTrend.length < 2) {
      caption.textContent = 'Not enough training-status history from Garmin yet to draw a trend.';
      ['ctlLine', 'atlLine', 'ctlArea'].forEach((id) => $(id).setAttribute('d', ''));
      $('raceFlags').innerHTML = '';
      ridgeFlagPx = [];
      updateRaceLegendVisibility();
      ridgeLayout = null;
      return;
    }

    const n = allTrend.length;
    let lastRealIndex = -1;
    allTrend.forEach((d, i) => { if (d.ctl !== null) lastRealIndex = i; });

    // Chart widens with day count/zoom level instead of always stretching
    // to the container — once it's wider than the panel, .ridge-scroll
    // scrolls horizontally to reveal older history, and vertical wheel
    // scroll over it zooms (see bindRidgeInteractions). scrollLeft is only
    // force-set on the very first render (today visible by default) —
    // every render after that (a 60s poll, a zoom, older history loading
    // in) leaves the user's current scroll position alone. This is a
    // fitness chart, not a general calendar — the window never extends
    // past today (see build_ridge_chart), so there's no empty space to
    // scroll into beyond it.
    const svg = $('ridgeSvg');
    const ridgeScroll = $('ridgeScroll');
    const chartWidth = Math.max(ridgeScroll.clientWidth, n * ridgePxPerDay);

    // viewBox now matches the actual rendered pixel size exactly (W ===
    // chartWidth, H === the CSS height for this breakpoint) — 1:1, no
    // scaling on either axis. That's what makes stroke width, the flag
    // emoji, and the today dot render at a constant on-screen size no
    // matter the zoom level or which part of the range is scrolled into
    // view, with no compensation math needed anywhere downstream. padTop/
    // padBottom keep the original design's proportions (they were tuned
    // against a 280-unit-tall chart) rather than being arbitrary at the
    // new height.
    const H = window.innerWidth <= CAL_MOBILE_BREAKPOINT ? 150 : 230;
    const W = chartWidth;
    const padTop = H * (55 / 280), padBottom = H * (25 / 280);
    const xAt = (i) => (i / (n - 1)) * W;
    svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
    svg.style.width = W + 'px';
    svg.style.height = H + 'px';

    $('ctlArea').style.transition = 'opacity 1s ease';
    $('ctlArea').style.opacity = 1;
    $('atlLine').style.opacity = 1;

    ridgeLayout = { allTrend, xAt, n, W, H, padTop, padBottom, chartWidth };
    redrawVisibleWindow(); // Y-scale + curves/dot/flags, fitted to whatever's visible right now

    const scrollHint = $('ridgeScrollHint');
    scrollHint.classList.toggle('visible', chartWidth > ridgeScroll.clientWidth);
    if (!ridgeInitialRenderDone) {
      ridgeInitialRenderDone = true;
      // Anchor on today, not the chart's rightmost edge — that edge can
      // now be a future race day, and today should still be what's front
      // and center by default.
      const todayPx = xAt(lastRealIndex);
      ridgeScroll.scrollLeft = Math.max(0, Math.min(ridgeScroll.scrollWidth - ridgeScroll.clientWidth, todayPx - ridgeScroll.clientWidth + 30));
      redrawVisibleWindow(); // re-fit now that scrollLeft (and so the visible window) actually landed
    }

    const tip = $('chartTip');
    const wrap = $('ridgeWrap');
    function fmtDate(iso) {
      const d = new Date(iso + 'T00:00:00');
      return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
    }
    svg.onmousemove = function (e) {
      const rect = svg.getBoundingClientRect();
      const wrapRect = wrap.getBoundingClientRect();
      const relX = (e.clientX - rect.left) / rect.width;
      const idx = Math.max(0, Math.min(n - 1, Math.round(relX * (n - 1))));
      const d = allTrend[idx];
      tip.style.display = 'block';
      let left = e.clientX - wrapRect.left + 14;
      if (left > wrapRect.width - 140) left = e.clientX - wrapRect.left - 150;
      tip.style.left = left + 'px';
      tip.style.top = (e.clientY - wrapRect.top - 44) + 'px';
      tip.innerHTML = '<b>' + fmtDate(d.date) + '</b> · CTL ' + fmtOrDash(d.ctl) + ' · ATL ' + fmtOrDash(d.atl) +
        ' · TSB ' + (d.tsb > 0 ? '+' : '') + fmtOrDash(d.tsb);
    };
    svg.onmouseleave = function () { tip.style.display = 'none'; };
  }

  // ------------------------------------------------------------ training ----
  function renderBalanceRows(rows) {
    const container = $('balanceRows');
    container.innerHTML = '';
    if (!rows || !rows.length) {
      container.innerHTML = '<div class="empty-note">No training-load balance data from Garmin.</div>';
      return;
    }
    const scale = Math.max(...rows.flatMap((r) => [r.actual || 0, r.target_max || 0])) * 1.05 || 1;
    rows.forEach((row) => {
      const targetLeft = ((row.target_min || 0) / scale) * 100;
      const targetWidth = (((row.target_max || 0) - (row.target_min || 0)) / scale) * 100;
      const actualLeft = ((row.actual || 0) / scale) * 100;
      const div = document.createElement('div');
      div.className = 'balance-row';
      div.innerHTML =
        '<div class="balance-top"><span class="lab">' + row.label + ' <span class="stat-tag ' + row.status + '">' + row.status + '</span></span>' +
        '<span class="val">' + Math.round(row.actual) + ' / ' + Math.round(row.target_min || 0) + '–' + Math.round(row.target_max || 0) + '</span></div>' +
        '<div class="bar-track"><div class="bar-target" style="left:' + targetLeft + '%;width:' + targetWidth + '%;"></div>' +
        '<div class="bar-actual ' + row.status + '" style="left:' + actualLeft + '%;"></div></div>';
      container.appendChild(div);
    });
  }

  function renderTrainingLoad(tl) {
    tl = tl || {};
    setText('ctlVal', fmtOrDash(tl.ctl));
    setText('atlVal', fmtOrDash(tl.atl));
    setText('tsbVal', tl.tsb !== null && tl.tsb !== undefined ? ((tl.tsb > 0 ? '+' : '') + tl.tsb) : '—');
    setText('tsbLabel', tl.tsb !== null && tl.tsb !== undefined ? (tl.tsb >= 0 ? 'fresh' : 'fatigued') : NBSP);
    setText('acwrVal', fmtOrDash(tl.acwr));
    setText('acwrLabel', tl.acwr !== null && tl.acwr !== undefined ? (tl.acwr < 0.8 ? 'low' : tl.acwr > 1.3 ? 'high' : 'optimal') : NBSP);
    if (tl.status_phrase) {
      // Garmin's numbered sub-variants (e.g. "Recovery 2") aren't documented
      // anywhere, including in Garmin's own apps — just the 9 named
      // categories are. Show the category, not the undocumented number.
      setText('heroStatusValue', tl.status_phrase.replace(/\s+\d+$/, ''));
      $('heroStatusBlock').style.display = 'block';
    } else {
      $('heroStatusBlock').style.display = 'none';
    }

    renderBalanceRows(tl.balance);

    // Show whichever of run/bike VO2 max Garmin actually has. Run is the
    // fallback slot when NEITHER is available (so the row never goes fully
    // empty) — Bike only shows at all when it has real data, so a
    // run-only account doesn't carry a permanently dashed-out Bike tile.
    const hasVo2Run = tl.vo2max_run !== null && tl.vo2max_run !== undefined;
    const hasVo2Bike = tl.vo2max_bike !== null && tl.vo2max_bike !== undefined;
    setText('vo2RunVal', fmtOrDash(tl.vo2max_run));
    setText('vo2RunLabel', fmtCategoryLabel(tl.vo2max_run, tl.vo2max_run_category, tl.vo2max_run_next_category, tl.vo2max_run_points_to_next, 'current'));
    setText('vo2BikeVal', fmtOrDash(tl.vo2max_bike));
    setText('vo2BikeLabel', fmtCategoryLabel(tl.vo2max_bike, tl.vo2max_bike_category, tl.vo2max_bike_next_category, tl.vo2max_bike_points_to_next, 'current'));
    $('vo2RunTile').style.display = (hasVo2Run || !hasVo2Bike) ? '' : 'none';
    $('vo2BikeTile').style.display = hasVo2Bike ? '' : 'none';
    $('vo2StatGrid').style.gridTemplateColumns = (hasVo2Run && hasVo2Bike) ? 'repeat(3,1fr)' : 'repeat(2,1fr)';
    setText('enduranceVal', fmtOrDash(tl.endurance_score));
    setText('enduranceLabel', fmtCategoryLabel(tl.endurance_score, tl.endurance_category, tl.endurance_next_category, tl.endurance_points_to_next, ''));

    const rp = tl.race_predictions || {};
    setText('predict5k', fmtOrDash(rp['5k']));
    setText('predict10k', fmtOrDash(rp['10k']));
    setText('predictHalf', fmtOrDash(rp.half_marathon));
    setText('predictMarathon', fmtOrDash(rp.marathon));

    let hillNote = '';
    if (tl.hill_score !== null && tl.hill_score !== undefined) {
      hillNote = 'Hill Score ' + tl.hill_score + ' (strength ' + fmtOrDash(tl.hill_strength) + ' / endurance ' + fmtOrDash(tl.hill_endurance) + ').';
    }
    setText('hillScoreNote', hillNote);

    // A tile that just got hidden (e.g. no bike VO2 on this account) must
    // not leave its chart expanded underneath.
    if (openMetric === 'vo2_bike' && !hasVo2Bike) closeMetricChart();
    if (openMetric === 'vo2_run' && !(hasVo2Run || !hasVo2Bike)) closeMetricChart();
  }

  // ------------------------------------------------- expandable metric chart -
  // Garmin's own gauge colouring, lowest tier to highest: red → orange →
  // green → blue → purple. Keyed by name so VO2 max (5 tiers) and endurance
  // score (7) both land on the right hue, with an index-based fallback for
  // any tier name Garmin adds later.
  // Endurance score stacks Superior and Elite adjacently, so those two must
  // not share a hue — Elite is pink, above Superior's purple.
  const TIER_COLORS = {
    'Poor': '#C0564E', 'Fair': '#D98A3C', 'Good': '#6BA85E',
    'Excellent': '#4A7BC8', 'Superior': '#8A62B8',
    'Beginner': '#C0564E', 'Intermediate': '#D98A3C', 'Trained': '#D4B544',
    'Well Trained': '#6BA85E', 'Expert': '#3E9AA8', 'Elite': '#D4568E',
  };
  const TIER_FALLBACK = ['#C0564E', '#D98A3C', '#D4B544', '#6BA85E', '#3E9AA8', '#4A7BC8', '#8A62B8'];

  function tierColor(name, idx, total) {
    if (TIER_COLORS[name]) return TIER_COLORS[name];
    const span = Math.max(1, total - 1);
    return TIER_FALLBACK[Math.round((idx / span) * (TIER_FALLBACK.length - 1))];
  }

  let openMetric = null;              // which tile is expanded, or null
  const metricHistoryCache = {};      // metric -> payload, so re-opening is instant

  function metricTileFor(metric) {
    return document.querySelector('.stat-tile.expandable[data-metric="' + metric + '"]');
  }

  function closeMetricChart() {
    openMetric = null;
    const panel = $('metricChartPanel');
    if (panel) panel.style.display = 'none';
    document.querySelectorAll('.stat-tile.expandable').forEach((el) => {
      el.classList.remove('open');
      el.setAttribute('aria-expanded', 'false');
    });
  }

  function toggleMetricChart(metric) {
    // Same tile again closes it; a different tile replaces what's open —
    // only ever one chart visible.
    if (openMetric === metric) { closeMetricChart(); return; }

    closeMetricChart();
    openMetric = metric;
    const tile = metricTileFor(metric);
    if (tile) {
      tile.classList.add('open');
      tile.setAttribute('aria-expanded', 'true');
    }
    const panel = $('metricChartPanel');
    panel.style.display = 'block';

    const cached = metricHistoryCache[metric];
    if (cached) { renderMetricChart(cached); return; }

    setText('metricChartTitle', 'Loading…');
    setText('metricChartMeta', '');
    $('metricChartPlot').innerHTML = '';
    fetch('/api/metric-history?metric=' + encodeURIComponent(metric))
      .then((r) => r.json())
      .then((data) => {
        metricHistoryCache[metric] = data;
        if (openMetric === metric) renderMetricChart(data);   // ignore a stale response
      })
      .catch(() => {
        if (openMetric !== metric) return;
        setText('metricChartTitle', 'Could not load history');
        $('metricChartPlot').innerHTML = '<div class="mc-empty">Try again in a moment.</div>';
      });
  }

  function renderMetricChart(data) {
    const body = $('metricChartPlot');
    setText('metricChartTitle', data.label || '');
    const points = (data.points || []).filter((p) => typeof p.value === 'number');

    if (data.error || points.length === 0) {
      setText('metricChartMeta', '');
      body.innerHTML = '<div class="mc-empty">' +
        (data.error ? escapeHtml(data.error) : 'No history recorded for this metric yet.') + '</div>';
      return;
    }

    const dec = data.decimals === 0 ? 0 : 1;
    const fmtVal = (v) => v.toFixed(dec);
    const latest = points[points.length - 1];
    const first = points[0];
    const delta = latest.value - first.value;
    const sign = delta > 0 ? '+' : '';
    setText('metricChartMeta',
      points.length + ' readings · ' + fmtShortDate(first.date) + ' – ' + fmtShortDate(latest.date) +
      ' · ' + sign + fmtVal(delta) + (data.unit ? ' ' + data.unit : ''));

    // 1:1 pixel viewBox (same reasoning as the ridge chart) so nothing
    // stretches: text and strokes stay true at any container width.
    const W = Math.max(320, body.clientWidth || 600);
    const H = 210;
    const padL = 42, padT = 12, padB = 24;
    const stripW = 76;                       // tier colour strip + labels, right side
    const plotR = W - stripW - 8;
    const plotW = plotR - padL;
    const plotH = H - padT - padB;

    const tiers = (data.tiers || []).slice().sort((a, b) => a.min - b.min);
    const values = points.map((p) => p.value);
    let lo = Math.min.apply(null, values);
    let hi = Math.max.apply(null, values);

    // Always keep the next tier up in frame — the whole point is seeing how
    // far away crossing over is — plus the floor of the tier you're in.
    // Anchored on the LATEST reading, not the window maximum: "which tier
    // am I in, what's next" is about where you are now. Anchoring on the
    // peak would call someone Superior because they touched it months ago
    // and then show no target to chase.
    if (tiers.length) {
      let currentFloor = tiers[0].min, nextUp = null;
      for (let i = 0; i < tiers.length; i++) {
        if (latest.value >= tiers[i].min) currentFloor = tiers[i].min;
        else if (nextUp === null) nextUp = tiers[i].min;
      }
      lo = Math.min(lo, currentFloor);
      if (nextUp !== null) hi = Math.max(hi, nextUp);
    }
    const pad = (hi - lo) * 0.12 || Math.max(1, hi * 0.05);
    lo -= pad; hi += pad;
    const span = (hi - lo) || 1;

    const yAt = (v) => padT + plotH - ((v - lo) / span) * plotH;
    const xAt = points.length === 1
      ? () => padL + plotW / 2
      : (i) => padL + (i / (points.length - 1)) * plotW;

    const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    let svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="' + W + '" height="' + H + '" role="img" aria-label="' + esc(data.label) + ' history">';

    // Tier bands: a colour strip down the right edge, plus a faint boundary
    // line across the plot at each threshold that falls in view.
    tiers.forEach((t, i) => {
      const nextMin = i + 1 < tiers.length ? tiers[i + 1].min : Infinity;
      const top = Math.max(padT, yAt(Math.min(nextMin, hi)));
      const bottom = Math.min(padT + plotH, yAt(Math.max(t.min, lo)));
      if (bottom - top < 0.5) return;                      // tier not visible in this window
      const color = tierColor(t.name, i, tiers.length);
      svg += '<rect x="' + (plotR + 8) + '" y="' + top + '" width="14" height="' + (bottom - top) +
             '" fill="' + color + '" opacity="0.8"/>';
      // Multi-word tier names ("Well Trained") wrap onto two lines rather
      // than widening the strip and stealing width from the plot.
      const words = t.name.split(' ');
      const mid = (top + bottom) / 2;
      if (words.length > 1 && bottom - top > 24) {
        svg += '<text class="mc-tier-label" x="' + (plotR + 26) + '" y="' + (mid - 2) + '" fill="' + color + '">' +
               esc(words[0]) + '<tspan x="' + (plotR + 26) + '" dy="10">' + esc(words.slice(1).join(' ')) + '</tspan></text>';
      } else if (bottom - top > 11) {
        svg += '<text class="mc-tier-label" x="' + (plotR + 26) + '" y="' + (mid + 3) +
               '" fill="' + color + '">' + esc(t.name) + '</text>';
      }
      if (t.min > lo && t.min < hi) {
        svg += '<line class="mc-gridline" x1="' + padL + '" x2="' + plotR + '" y1="' + yAt(t.min) +
               '" y2="' + yAt(t.min) + '" stroke="' + color + '" stroke-opacity="0.35" stroke-dasharray="4 4"/>';
        svg += '<text class="mc-axis" x="' + (padL - 6) + '" y="' + (yAt(t.min) + 3) + '" text-anchor="end">' +
               fmtVal(t.min) + '</text>';
      }
    });

    // Value axis: just the extremes, so it never fights the tier labels.
    svg += '<text class="mc-axis" x="' + (padL - 6) + '" y="' + (padT + 8) + '" text-anchor="end">' + fmtVal(hi) + '</text>';
    svg += '<text class="mc-axis" x="' + (padL - 6) + '" y="' + (padT + plotH) + '" text-anchor="end">' + fmtVal(lo) + '</text>';

    const coords = points.map((p, i) => [xAt(i), yAt(p.value)]);
    svg += '<path class="mc-line" d="M' + coords.map((c) => c[0].toFixed(1) + ',' + c[1].toFixed(1)).join(' L') + '"/>';
    coords.forEach((c, i) => {
      const isLast = i === coords.length - 1;
      svg += '<circle class="' + (isLast ? 'mc-dot-last' : 'mc-dot') + '" cx="' + c[0].toFixed(1) +
             '" cy="' + c[1].toFixed(1) + '" r="' + (isLast ? 4 : 2.2) + '"/>';
    });
    // Transparent oversized hit targets on top — a 2.2px dot is far too
    // small to hover reliably, especially on a dense series.
    coords.forEach((c, i) => {
      svg += '<circle class="mc-hit" cx="' + c[0].toFixed(1) + '" cy="' + c[1].toFixed(1) +
             '" r="11" fill="transparent" data-i="' + i + '"/>';
    });

    svg += '</svg>';
    body.innerHTML = svg;
    bindMetricHover(body, points, coords, data);
  }

  function bindMetricHover(body, points, coords, data) {
    const tip = $('metricChartTip');
    if (!tip) return;
    const hide = () => { tip.style.display = 'none'; };
    hide();

    body.querySelectorAll('.mc-hit').forEach((hit) => {
      const i = parseInt(hit.getAttribute('data-i'), 10);
      hit.addEventListener('mouseenter', () => {
        const p = points[i];
        const dec = data.decimals === 0 ? 0 : 1;
        tip.innerHTML = '<b>' + p.value.toFixed(dec) + '</b>' +
          (data.unit ? ' ' + escapeHtml(data.unit) : '') +
          '<span class="mc-tip-date">' + escapeHtml(fmtShortDate(p.date)) + '</span>';
        tip.style.display = 'block';

        // Position within the chart panel, flipping to the left of the dot
        // near the right edge so it never spills past the panel.
        const svgEl = body.querySelector('svg');
        const scale = svgEl.clientWidth / svgEl.viewBox.baseVal.width || 1;
        const x = coords[i][0] * scale, y = coords[i][1] * scale;
        const tw = tip.offsetWidth, th = tip.offsetHeight;
        let left = x + 12;
        if (left + tw > body.clientWidth) left = x - tw - 12;
        tip.style.left = Math.max(0, left) + 'px';
        tip.style.top = Math.max(0, y - th - 8) + 'px';
      });
      hit.addEventListener('mouseleave', hide);
    });
  }

  function metricPointLabel(point, data) {
    const dec = data.decimals === 0 ? 0 : 1;
    return fmtShortDate(point.date) + ': ' + point.value.toFixed(dec) + (data.unit ? ' ' + data.unit : '');
  }

  function bindMetricTiles() {
    document.querySelectorAll('.stat-tile.expandable').forEach((el) => {
      const metric = el.getAttribute('data-metric');
      el.addEventListener('click', () => toggleMetricChart(metric));
      el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleMetricChart(metric); }
      });
    });
    // Re-render on resize so the 1:1 viewBox keeps matching the container.
    let t = null;
    window.addEventListener('resize', () => {
      if (!openMetric) return;
      clearTimeout(t);
      t = setTimeout(() => {
        const d = metricHistoryCache[openMetric];
        if (d) renderMetricChart(d);
      }, 180);
    });
  }

  // ----------------------------------------------------------------- vitals -
  function renderFactors(factors) {
    const container = $('factorRows');
    container.innerHTML = '';
    (factors || []).forEach((f) => {
      const row = document.createElement('div');
      row.className = 'factor-row';
      const fillClass = f.no_data ? '' : (f.status === 'good' ? 'good' : 'mod');
      const trackClass = f.no_data ? 'factor-track nodata' : 'factor-track';
      const width = f.no_data ? 0 : Math.max(0, Math.min(100, f.pct));
      row.innerHTML =
        '<div class="factor-label">' + f.label + '</div>' +
        '<div class="' + trackClass + '"><div class="factor-fill ' + fillClass + '" style="width:' + width + '%;"></div></div>' +
        '<div class="factor-val">' + f.display + '</div>';
      container.appendChild(row);
    });
    if (!factors || !factors.length) {
      container.innerHTML = '<div class="empty-note">No training-readiness factor breakdown from Garmin today.</div>';
    }
  }

  function fmtHm(seconds) {
    const mins = Math.round((seconds || 0) / 60);
    const h = Math.floor(mins / 60), m = mins % 60;
    return h ? (h + 'h ' + String(m).padStart(2, '0') + 'm') : (m + 'm');
  }

  function renderSleepStages(sleep) {
    const panel = $('sleepStages');
    if (!panel) return;
    const stages = (sleep && sleep.stages) || [];
    if (!stages.length) {
      panel.innerHTML = '<div class="empty-note">No sleep stage breakdown from Garmin for this night.</div>';
      return;
    }
    const bar = stages.filter((s) => s.seconds > 0).map((s) =>
      '<span class="stage-' + s.key + '" style="flex:' + s.seconds + ' 0 0;" title="' +
      s.label + ' ' + fmtHm(s.seconds) + '"></span>').join('');
    const rows = stages.map((s) =>
      '<div class="sleep-stage-row"><span class="sw stage-' + s.key + '"></span>' + s.label +
      '<span class="t">' + fmtHm(s.seconds) + '</span>' +
      '<span class="p">' + (s.pct != null ? s.pct + '%' : '—') + '</span></div>').join('');
    panel.innerHTML = '<div class="sleep-bar">' + bar + '</div><div class="sleep-stage-rows">' + rows + '</div>';
  }

  function bindSleepBox() {
    const box = $('sleepBox');
    if (!box || !$('sleepStages')) return;
    const toggle = () => {
      const open = !box.classList.contains('open');
      box.classList.toggle('open', open);
      box.setAttribute('aria-expanded', String(open));
      $('sleepStages').hidden = !open;
    };
    box.addEventListener('click', toggle);
    box.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    });
  }

  function renderVitals(vitals) {
    vitals = vitals || {};
    renderFactors(vitals.factors);

    const fa = vitals.fitness_age;
    setText('fitnessAgeVal', fa ? (fa.fitness_age + ' yr') : '—');
    setText('fitnessAgeDetail', fa ? ('vs chrono. ' + fmtOrDash(fa.chronological_age) + ' · achievable ' + fmtOrDash(fa.achievable_age)) : 'No data');

    const rhr = vitals.resting_hr;
    setText('rhrVal', rhr ? (fmtOrDash(rhr.fresh) + ' bpm') : '—');
    setText('rhrDetail', rhr ? ('7d avg ' + fmtOrDash(rhr.week_avg)) : 'No data');

    const sleep = vitals.last_sleep;
    setText('sleepVal', sleep ? sleep.duration + (sleep.score ? (' · ' + sleep.score) : '') : '—');
    setText('sleepDetail', sleep ? ('night of ' + sleep.date + (sleep.quality ? (' · ' + sleep.quality) : '')) : 'No recent night logged');
    renderSleepStages(sleep);

    const bb = vitals.body_battery;
    setText('batteryVal', bb ? (fmtOrDash(bb.label) + ' · ' + fmtOrDash(bb.current)) : '—');
    setText('batteryDetail', bb ? ('range today ' + fmtOrDash(bb.low) + '–' + fmtOrDash(bb.high)) : 'No data');

    const gap = $('gapCallout');
    if (vitals.gap_callout) {
      gap.style.display = 'block';
      gap.innerHTML = '<b>Wearable gap:</b> ' + vitals.gap_callout.replace(/^Wearable gap:\s*/, '');
    } else {
      gap.style.display = 'none';
    }
  }

  // ------------------------------------------------------------- waypoints --
  let currentActivityId = null;

  function renderLastWaypoint(activity) {
    if (!activity) {
      setText('lastActivityTitle', 'No recent activity');
      setText('lastActivityWindow', '');
      setText('lastActivityBody', 'Garmin has no activities in range.');
      currentActivityId = null;
      $('activityReview').value = '';
      $('activityReview').disabled = true;
      return;
    }
    currentActivityId = activity.activity_id;
    $('activityReview').disabled = false;
    setText('lastActivityTitle', activity.name + (activity.distance !== '—' ? (' — ' + activity.distance) : ''));
    setText('lastActivityWindow', [activity.date, activity.duration, activity.avg_hr !== '—' ? ('avg HR ' + activity.avg_hr) : null]
      .filter(Boolean).join(' · '));
    const body = activity.zone_breakdown && activity.zone_breakdown.length
      ? activity.zone_breakdown.join(', ') + '.'
      : (activity.description || 'No further detail from Garmin for this activity.');
    setText('lastActivityBody', body);

    loadReview(currentActivityId);
  }

  function loadReview(activityId) {
    if (!activityId) return;
    fetch('/api/review/' + encodeURIComponent(activityId))
      .then((r) => r.json())
      .then((data) => { $('activityReview').value = data.note || ''; })
      .catch(() => {});
  }

  function saveReview() {
    if (!currentActivityId) return;
    const btn = $('saveReviewBtn');
    const status = $('reviewStatus');
    btn.disabled = true;
    fetch('/api/review/' + encodeURIComponent(currentActivityId), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note: $('activityReview').value }),
    })
      .then((r) => r.json())
      .then((data) => { status.textContent = data.saved ? 'Saved' : 'Save failed — try again'; })
      .catch(() => { status.textContent = 'Save failed — try again'; })
      .finally(() => {
        btn.disabled = false;
        setTimeout(() => { status.textContent = ''; }, 2500);
      });
  }

  function fillList(id, items, emptyText) {
    const el = $(id);
    el.innerHTML = '';
    (items || []).forEach((text) => {
      const li = document.createElement('li');
      li.textContent = text;
      el.appendChild(li);
    });
    if ((!items || !items.length) && emptyText) {
      el.innerHTML = '<li>' + emptyText + '</li>';
    }
  }

  function fmtAge(hours) {
    if (hours < 1) return 'just now';
    if (hours < 24) return Math.round(hours) + 'h ago';
    return Math.round(hours / 24) + 'd ago';
  }

  function escapeHtml(s) {
    const div = document.createElement('div');
    div.textContent = s == null ? '' : s;
    return div.innerHTML;
  }

  function renderNextWaypoint(nw, coachNote) {
    nw = nw || {};
    const altLabel = $('nextSessionAltLabel');
    const notesWrap = $('nextWaypointNotesWrap');
    const primary = $('nextSessionPrimary');
    const reasoning = $('nextWaypointReasoning');
    const windowEl = $('nextWaypointWindow');

    if (coachNote && coachNote.session) {
      windowEl.style.display = 'none';
      const sourceTag = 'coach’s note' + (coachNote.age_hours != null ? ' · ' + fmtAge(coachNote.age_hours) : '');
      primary.innerHTML = escapeHtml(coachNote.session) + ' <span class="wp-source-tag">(' + sourceTag + ')</span>';
      if (coachNote.reasoning) {
        setText('nextWaypointReasoning', coachNote.reasoning);
        reasoning.style.display = 'block';
      } else {
        reasoning.style.display = 'none';
      }
      fillList('nextSessionAlt', [], '');
      altLabel.style.display = 'none';
      notesWrap.style.display = 'none';
      return;
    }

    reasoning.style.display = 'none';

    const isTrainingPlan = nw.source === 'training_plan';
    if (isTrainingPlan && nw.when) {
      setText('nextWaypointWindow', nw.when);
      windowEl.style.display = 'block';
    } else {
      windowEl.style.display = 'none';
    }

    const sessions = nw.sessions || [];
    const headline = sessions[0];
    const sourceTag = isTrainingPlan ? 'from training plan' : 'auto-suggested';
    primary.innerHTML = headline
      ? escapeHtml(headline) + ' <span class="wp-source-tag">(' + sourceTag + ')</span>'
      : 'No live recommendation available right now.';
    const alts = sessions.slice(1);
    fillList('nextSessionAlt', alts, '');
    altLabel.style.display = alts.length ? 'block' : 'none';

    if (isTrainingPlan) {
      notesWrap.style.display = 'none';
    } else {
      notesWrap.style.display = 'block';
      fillList('nextWaypointNotesList', nw.notes, 'Nothing flagged right now — load balance and recovery look on track.');
    }
  }

  // ------------------------------------------------------------ race plan --
  function fmtRaceCountdown(days) {
    if (days === null || days === undefined) return '';
    if (days < 0) return 'race day passed';
    if (days === 0) return 'race day';
    if (days === 1) return '1 day to go';
    return days + ' days to go';
  }

  function fmtShortDate(iso) {
    const d = new Date(iso + 'T00:00:00');
    return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
  }

  function renderRacePlan(plan) {
    const panel = $('racePlanPanel');
    if (!plan || !plan.sessions || !plan.sessions.length) {
      panel.style.display = 'none';
      return;
    }
    panel.style.display = 'block';

    const bits = [plan.race_name, plan.goal_time ? ('goal ' + plan.goal_time) : null, fmtRaceCountdown(plan.days_to_race)]
      .filter(Boolean);
    setText('racePlanTag', bits.join(' · '));

    const weekNote = $('racePlanWeekNote');
    if (plan.is_current_week === false) {
      setText(
        'racePlanWeekNote',
        'Nothing scheduled this week — showing the plan’s nearest block: ' +
          fmtShortDate(plan.week_start) + ' – ' + fmtShortDate(plan.week_end) + '.'
      );
      weekNote.style.display = 'block';
    } else {
      weekNote.style.display = 'none';
    }

    const week = $('racePlanWeek');
    week.innerHTML = '';
    plan.sessions.forEach((s) => {
      const card = document.createElement('div');
      card.className = 'rp-day ' + s.status;

      const STATUS_LABELS = { completed: 'Done', missed: 'Missed', unplanned: 'Extra', rest: 'Done', upcoming: 'Upcoming' };
      const statusLabel = STATUS_LABELS[s.status] || 'Upcoming';
      const icon = ACTIVITY_ICONS[s.activity_type] || ACTIVITY_ICONS.other;
      const head = document.createElement('div');
      head.className = 'rp-day-head';
      head.innerHTML = '<span class="rp-day-head-left">' + icon + '<span>' + s.weekday + ' · ' + s.date.slice(5) + '</span></span><span class="rp-day-status">' + statusLabel + '</span>';
      card.appendChild(head);

      const body = document.createElement('div');
      body.className = 'rp-day-body';
      if (s.completed_activity && (s.status === 'completed' || s.status === 'unplanned')) {
        const a = s.completed_activity;
        const pace = a.pace && a.pace !== '—' ? ' · ' + escapeHtml(a.pace) : '';
        body.innerHTML = '<b>' + escapeHtml(a.name) + '</b><br>' + escapeHtml(a.distance || '—') + ' · ' + escapeHtml(a.duration || '—') + pace;
        card.appendChild(body);
        const sub = document.createElement('div');
        sub.className = 'rp-day-planned';
        // An unplanned session has no planned text to show against it —
        // say why it's here instead of rendering "Planned: ".
        sub.textContent = s.status === 'unplanned' ? 'Not in the plan' : 'Planned: ' + s.planned_session;
        card.appendChild(sub);
      } else {
        body.textContent = s.planned_session;
        card.appendChild(body);
      }
      week.appendChild(card);
    });
  }

  // ----------------------------------------------------------- calendar ----
  const monthCache = {}; // 'YYYY-MM' -> Promise<{'YYYY-MM-DD': [activity, ...]}>
  let calMonth = (function () { const d = new Date(); d.setDate(1); return d; })();
  let selectedDate = null;

  function pad2(n) { return String(n).padStart(2, '0'); }
  function monthKey(d) { return d.getFullYear() + '-' + pad2(d.getMonth() + 1); }
  function isoDate(d) { return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()); }

  // Garmin's own activity-type glyphs aren't exposed anywhere in the
  // activities/activity-types API (checked — just typeId/typeKey/flags, no
  // icon or asset reference), so these are small custom line-glyphs of our
  // own standing in for them, matched by typeKey.
  const CAL_SVG_OPEN = '<svg viewBox="0 0 16 16" class="cal-icon" aria-hidden="true">';
  const ACTIVITY_ICONS = {
    // Lucide "sneaker" (@lucide/lab), real path data, tilted 45deg clockwise.
    running: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="cal-icon" aria-hidden="true"><g transform="rotate(45 12 12)"><path d="M14.1 7.9 12.5 10" /><path d="M17.4 10.1 16 12" /><path d="M2 16a2 2 0 0 0 2 2h13c2.8 0 5-2.2 5-5a2 2 0 0 0-2-2c-.8 0-1.6-.2-2.2-.7l-6.2-4.2c-.4-.3-.9-.2-1.3.1 0 0-.6.8-1.2 1.1a3.5 3.5 0 0 1-4.2.1C4.4 7 3.7 6.3 3.7 6.3A.92.92 0 0 0 2 7Z" /><path d="M2 11c0 1.7 1.3 3 3 3h7" /></g></svg>',
    cycling: CAL_SVG_OPEN + '<g transform="translate(0,-1.3)"><circle cx="4" cy="11" r="2.6" fill="none" stroke="currentColor" stroke-width="1.2"/><circle cx="12" cy="11" r="2.6" fill="none" stroke="currentColor" stroke-width="1.2"/><path d="M4 11 L7 5 L10 5 M7 5 L9 11 M9 11 L12 11 M9 11 L6.5 8.5" stroke="currentColor" stroke-width="1.2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></g></svg>',
    strength: CAL_SVG_OPEN + '<path d="M2 6v4M4 5v6M12 5v6M14 6v4M4.5 8h7" stroke="currentColor" stroke-width="1.2" fill="none" stroke-linecap="round"/></svg>',
    swimming: CAL_SVG_OPEN + '<path d="M2 6 Q4 4.5 6 6 T10 6 T14 6" stroke="currentColor" stroke-width="1.2" fill="none" stroke-linecap="round"/><path d="M2 10 Q4 8.5 6 10 T10 10 T14 10" stroke="currentColor" stroke-width="1.2" fill="none" stroke-linecap="round"/></svg>',
    walking: CAL_SVG_OPEN + '<ellipse cx="5" cy="5" rx="1.4" ry="2.2" fill="currentColor"/><ellipse cx="10" cy="11" rx="1.4" ry="2.2" fill="currentColor"/></svg>',
    hiking: CAL_SVG_OPEN + '<path d="M2 13 L6 6 L8.5 10 L10 7.5 L14 13 Z" fill="currentColor"/></svg>',
    skiing: CAL_SVG_OPEN + '<path d="M3 13 L6 5 Q6.6 3.3 8 4" stroke="currentColor" stroke-width="1.3" fill="none" stroke-linecap="round"/><path d="M9 13 L12 5 Q12.6 3.3 14 4" stroke="currentColor" stroke-width="1.3" fill="none" stroke-linecap="round"/></svg>',
    multisport: CAL_SVG_OPEN + '<path d="M8 2 L14 13 L2 13 Z" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/></svg>',
    other: CAL_SVG_OPEN + '<circle cx="8" cy="8" r="2.5" fill="currentColor"/></svg>',
  };
  const ICON_MORE = CAL_SVG_OPEN + '<path d="M8 3 V13 M3 8 H13" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>';
  const CAL_MOBILE_BREAKPOINT = 860; // matches the CSS media query breakpoint

  function calIconSlots() {
    // Last slot becomes "+" once there's more than this many that day —
    // mobile cells are much smaller, so fewer icons fit before crowding.
    return window.innerWidth <= CAL_MOBILE_BREAKPOINT ? 2 : 3;
  }

  function iconKeyForType(typeKey) {
    const t = (typeKey || '').toLowerCase();
    if (t.includes('run')) return 'running';
    if (t.includes('cycl') || t.includes('bik') || t.includes('ride')) return 'cycling';
    if (t.includes('strength')) return 'strength';
    if (t.includes('swim')) return 'swimming';
    if (t.includes('walk')) return 'walking';
    if (t.includes('hik') || t.includes('mountaineer')) return 'hiking';
    if (t.includes('ski')) return 'skiing';
    if (t === 'multi_sport') return 'multisport';
    return 'other';
  }

  function renderCellIcons(rows) {
    if (!rows || !rows.length) return '';
    const slots = calIconSlots();
    const visibleCount = rows.length <= slots ? rows.length : slots - 1;
    let html = '<div class="cal-cell-icons">';
    for (let i = 0; i < visibleCount; i++) {
      html += ACTIVITY_ICONS[iconKeyForType(rows[i].type_key)] || ACTIVITY_ICONS.other;
    }
    if (rows.length > visibleCount) html += ICON_MORE;
    html += '</div>';
    return html;
  }

  function fetchMonth(d) {
    const key = monthKey(d);
    if (monthCache[key]) return monthCache[key];
    const start = isoDate(new Date(d.getFullYear(), d.getMonth(), 1));
    const end = isoDate(new Date(d.getFullYear(), d.getMonth() + 1, 0));
    const p = fetch('/api/activities?start=' + start + '&end=' + end)
      .then((r) => r.json())
      .then((data) => data.days || {})
      .catch(() => ({}));
    monthCache[key] = p;
    return p;
  }

  const SESSION_TYPE_LABEL = { easy: 'Easy', tempo: 'Tempo', interval: 'Interval', long: 'Long', race: 'Race' };
  // Load-point split is a rough time-proportional estimate, not something
  // Garmin reports directly (see EPOC discussion) — button hidden until
  // that's tightened up. Backend/endpoint stay in place for later.
  const SHOW_TRAINING_LOAD_TOGGLE = false;

  function fmtLoadSource(source) {
    if (source === 'power') return 'power zones';
    if (source === 'heart_rate') return 'HR zones';
    return null;
  }

  function renderDayDetail(dateStr, rows) {
    const wrap = $('calDayDetail');
    const tbody = $('calDayDetailBody');
    tbody.innerHTML = '';
    if (!rows || !rows.length) {
      wrap.style.display = 'none';
      return;
    }
    const d = new Date(dateStr + 'T00:00:00');
    setText('calDayDetailLabel', d.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' }));
    wrap.style.display = 'block';

    rows.forEach((row, idx) => {
      const tr = document.createElement('tr');
      const hasLegs = row.legs && row.legs.length;
      if (hasLegs) {
        tr.className = 'kalmar-row';
        tr.id = 'calMultisportToggle' + idx;
      }
      const typeLabel = SESSION_TYPE_LABEL[row.session_type];
      const typeBadge = typeLabel ? '<span class="type-badge ' + row.session_type + '">' + typeLabel + '</span>' : '';
      const loadsToggle = SHOW_TRAINING_LOAD_TOGGLE ? '<span class="loads-toggle" id="calLoadsToggle' + idx + '">loads ▾</span>' : '';
      tr.innerHTML =
        '<td>' + escapeHtml(row.name) + typeBadge +
        (hasLegs ? '<span class="hint" id="calMultisportHint' + idx + '">▸ ' + row.legs.length + ' legs</span>' : '') +
        loadsToggle + '</td>' +
        '<td data-label="Distance">' + row.distance + '</td><td data-label="Duration">' + row.duration + '</td><td data-label="' + row.pace_label + '">' + row.pace + '</td><td data-label="Avg HR">' + row.avg_hr + '</td>' +
        '<td data-label="Elev+">' + row.elevation_gain + '</td><td data-label="Cal">' + row.calories + '</td>';
      tbody.appendChild(tr);

      if (hasLegs) {
        const legRows = row.legs.map((leg) => {
          const legTr = document.createElement('tr');
          legTr.className = 'legs-group hidden';
          legTr.innerHTML =
            '<td class="leg">' + escapeHtml(leg.name) + '</td><td data-label="Distance">' + leg.distance + '</td><td data-label="Duration">' + leg.duration + '</td>' +
            '<td data-label="' + leg.pace_label + '">' + leg.pace + '</td><td data-label="Avg HR">' + leg.avg_hr + '</td><td data-label="Elev+">' + leg.elevation_gain + '</td><td data-label="Cal">' + leg.calories + '</td>';
          tbody.appendChild(legTr);
          return legTr;
        });
        tr.addEventListener('click', function () {
          const nowHidden = legRows[0].classList.contains('hidden');
          legRows.forEach((r) => r.classList.toggle('hidden', !nowHidden));
          $('calMultisportHint' + idx).textContent = (!nowHidden ? '▸ ' : '▾ ') + row.legs.length + ' legs';
        });
      }

      if (SHOW_TRAINING_LOAD_TOGGLE) {
        const loadsTr = document.createElement('tr');
        loadsTr.className = 'load-row hidden';
        loadsTr.innerHTML = '<td colspan="6" class="load-cell"></td>';
        tbody.appendChild(loadsTr);
        let loadsFetched = false;
        $('calLoadsToggle' + idx).addEventListener('click', function (ev) {
          ev.stopPropagation();
          const nowHidden = loadsTr.classList.contains('hidden');
          if (nowHidden && !loadsFetched) {
            loadsFetched = true;
            const cell = loadsTr.querySelector('.load-cell');
            cell.textContent = 'Loading training load…';
            const params = new URLSearchParams({ type_key: row.type_key || '' });
            if (row.training_load != null) params.set('total_load', row.training_load);
            fetch('/api/activity/' + row.activity_id + '/training-load?' + params.toString())
              .then((r) => r.json())
              .then((data) => {
                if (!data.source) {
                  cell.textContent = 'No zone/load data available for this activity.';
                  return;
                }
                const src = fmtLoadSource(data.source);
                cell.innerHTML =
                  '<span class="load-stat"><b>Aerobic Low</b> ' + data.aerobic_low + '</span>' +
                  '<span class="load-stat"><b>Aerobic High</b> ' + data.aerobic_high + '</span>' +
                  '<span class="load-stat"><b>Anaerobic</b> ' + data.anaerobic + '</span>' +
                  (src ? '<span class="load-source">load pts, split by ' + src + '</span>' : '');
              })
              .catch(() => { cell.textContent = 'Could not load training load data.'; });
          }
          loadsTr.classList.toggle('hidden', !nowHidden);
          $('calLoadsToggle' + idx).textContent = nowHidden ? 'loads ▴' : 'loads ▾';
        });
      }
    });
  }

  function renderCalendarGrid(days) {
    const grid = $('calGrid');
    grid.innerHTML = '';
    setText('calMonthLabel', calMonth.toLocaleDateString(undefined, { month: 'long', year: 'numeric' }));

    const year = calMonth.getFullYear();
    const month = calMonth.getMonth();
    const firstOfMonth = new Date(year, month, 1);
    const startOffset = (firstOfMonth.getDay() + 6) % 7; // grid starts Monday
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const todayStr = isoDate(new Date());

    for (let i = 0; i < startOffset; i++) {
      const filler = document.createElement('div');
      filler.className = 'cal-cell empty';
      grid.appendChild(filler);
    }

    for (let day = 1; day <= daysInMonth; day++) {
      const dateStr = year + '-' + pad2(month + 1) + '-' + pad2(day);
      const rows = days[dateStr];
      const isToday = dateStr === todayStr;
      const isPast = dateStr < todayStr;
      const cell = document.createElement('div');
      cell.className = 'cal-cell' +
        (isToday ? ' is-today' : (isPast && rows && rows.length ? ' has-activity' : '')) +
        (rows && rows.length ? ' clickable' : '') +
        (dateStr === selectedDate ? ' is-selected' : '');
      cell.innerHTML = '<span class="cal-day-num">' + day + '</span>' + renderCellIcons(rows);
      if (rows && rows.length) {
        cell.title = rows.length + ' activit' + (rows.length === 1 ? 'y' : 'ies');
        cell.addEventListener('click', function () {
          selectedDate = dateStr;
          renderCalendarGrid(days);
          renderDayDetail(dateStr, rows);
        });
      }
      grid.appendChild(cell);
    }
  }

  let lastCalDays = null;

  function loadCalendarMonth(d) {
    calMonth = new Date(d.getFullYear(), d.getMonth(), 1);
    fetchMonth(calMonth).then((days) => { lastCalDays = days; renderCalendarGrid(days); });
  }

  // Re-render on resize (debounced) so crossing the mobile breakpoint
  // updates the per-day icon cap without needing a month change/reload.
  let calResizeTimer = null;
  window.addEventListener('resize', function () {
    if (!lastCalDays) return;
    clearTimeout(calResizeTimer);
    calResizeTimer = setTimeout(function () { renderCalendarGrid(lastCalDays); }, 150);
  });

  // ------------------------------------------------------------------ load --
  function updateLastUpdatedLabel(data) {
    if (data.error) { setText('lastUpdatedLabel', 'Last attempt failed'); return; }
    if (data.cache_age_seconds === null || data.cache_age_seconds === undefined) {
      setText('lastUpdatedLabel', 'Updated just now');
      return;
    }
    const mins = Math.floor(data.cache_age_seconds / 60);
    setText('lastUpdatedLabel', mins < 1 ? 'Updated just now' : ('Updated ' + mins + 'm ago (cached)'));
  }

  // Female Garmin accounts default to the Bloom theme on first-ever load —
  // but only until the person picks anything themselves. Gender isn't known
  // until this payload arrives, so it can't happen in the pre-paint <head>
  // script; skipped entirely once trainstate-theme has ever been set
  // (including explicitly picking "Panel"), so it never fights a real choice.
  function applyGenderDefaultTheme(trainingLoad) {
    let hasExplicitTheme = false;
    try { hasExplicitTheme = localStorage.getItem('trainstate-theme') !== null; } catch (e) {}
    if (hasExplicitTheme) return;
    if (trainingLoad && trainingLoad.gender === 'FEMALE') {
      document.documentElement.setAttribute('data-theme', 'bloom');
      const sw = $('themeSwitcher');
      if (sw) sw.value = 'bloom';
      // Persist it (not just apply in-memory) so next load's pre-paint
      // <head> script already knows — only the very first load flashes
      // default before this runs; every load after is flash-free. Picking
      // anything from the dropdown overwrites this the same as any other
      // saved theme, so it never fights a real choice.
      try { localStorage.setItem('trainstate-theme', 'bloom'); } catch (e) {}
    }
  }

  function render(data) {
    renderError(data.error || null, data.error_type);
    $('liveBadge').textContent = data.error ? 'Error' : 'Live · Garmin Connect';
    applyGenderDefaultTheme(data.training_load);
    renderHero(data);
    renderRidgeChart(data.ridge_chart);
    renderTrainingLoad(data.training_load);
    renderVitals(data.vitals);
    renderLastWaypoint(data.last_activity);
    renderNextWaypoint(data.next_waypoint, data.coach_note);
    renderRacePlan(data.training_plan);
    updateLastUpdatedLabel(data);
  }

  let loadInFlight = false;

  function loadDashboard(force) {
    if (loadInFlight) return Promise.resolve();
    loadInFlight = true;
    const btn = $('refreshBtn');
    btn.disabled = true;
    btn.classList.add('spinning');
    return fetch('/api/dashboard' + (force ? '?force=true' : ''))
      .then((r) => r.json())
      .then((data) => { render(data); })
      .catch((err) => { renderError('Network error talking to the dashboard server: ' + err); })
      .finally(() => {
        btn.disabled = false;
        btn.classList.remove('spinning');
        loadInFlight = false;
      });
  }

  // Poll the (cheap, server-cached) endpoint every minute so the page picks
  // up new data on its own once the server-side cache goes stale, with no
  // reload needed. Each tick is a plain, un-forced fetch — most of them just
  // return the same cached payload until DASHBOARD_CACHE_TTL elapses, so
  // this doesn't cause extra Garmin API calls.
  const AUTO_REFRESH_MS = 60 * 1000;

  document.addEventListener('DOMContentLoaded', function () {
    bindHelpTooltips();
    bindMetricTiles();
    bindSleepBox();

    const themeSwitcher = $('themeSwitcher');
    let savedTheme = 'default';
    try { savedTheme = localStorage.getItem('trainstate-theme') || 'default'; } catch (e) {}
    themeSwitcher.value = savedTheme;
    themeSwitcher.addEventListener('change', function () {
      const theme = themeSwitcher.value;
      if (theme === 'default') {
        document.documentElement.removeAttribute('data-theme');
      } else {
        document.documentElement.setAttribute('data-theme', theme);
      }
      try { localStorage.setItem('trainstate-theme', theme); } catch (e) {}
    });

    $('refreshBtn').addEventListener('click', function () { loadDashboard(true); });
    $('saveReviewBtn').addEventListener('click', saveReview);
    loadDashboard(false);

    $('calPrevBtn').addEventListener('click', function () {
      selectedDate = null;
      $('calDayDetail').style.display = 'none';
      loadCalendarMonth(new Date(calMonth.getFullYear(), calMonth.getMonth() - 1, 1));
    });
    $('calNextBtn').addEventListener('click', function () {
      selectedDate = null;
      $('calDayDetail').style.display = 'none';
      loadCalendarMonth(new Date(calMonth.getFullYear(), calMonth.getMonth() + 1, 1));
    });
    loadCalendarMonth(calMonth);

    setInterval(function () { loadDashboard(false); }, AUTO_REFRESH_MS);

    // Catch up right away if the tab was backgrounded/asleep past a tick.
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible') loadDashboard(false);
    });
  });
})();
