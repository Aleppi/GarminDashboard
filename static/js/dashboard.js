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

    const latest = hero.latest_reading;
    const score = latest ? latest.score : null;
    setText('latestScore', latest ? fmtOrDash(score) : '—');
    setText('latestTag', latest ? (latest.label || '') : 'No data');
    setText('latestTime', latest && latest.time ? ('as of ' + latest.time) : '');
    // Garmin's own bands: Poor <25, Low <50, Moderate <75, High/Prime 75+.
    $('latestScore').className = 'rf-score ' + (typeof score !== 'number' ? '' : score >= 75 ? 'high' : score >= 50 ? 'mod' : 'low');
  }

  // ------------------------------------------------------- recovery time --
  let lastRecovery = null;

  function fmtRecoveryLeft(minutes) {
    if (minutes <= 0) return '0h';
    const hours = Math.round(minutes / 60);
    if (hours < 1) return '<1h';
    if (hours < 24) return hours + 'h';
    const d = Math.floor(hours / 24), h = hours % 24;
    return h ? d + 'd ' + h + 'h' : d + 'd';
  }

  // Counts down live from the absolute clears_at the server sends, so the
  // box stays right between the (cached, 60s-polled) dashboard payloads.
  function renderRecovery(rec) {
    if (rec !== undefined) lastRecovery = rec;
    rec = lastRecovery;
    const val = $('recoveryVal');
    if (!rec) {
      val.textContent = '—';
      val.className = 'rf-score';
      setText('recoveryTag', 'No data');
      setText('recoveryClears', '');
      return;
    }
    const clears = new Date(rec.clears_at);
    const left = rec.recovered ? 0 : Math.max(0, (clears - new Date()) / 60000);
    val.textContent = fmtRecoveryLeft(left);
    val.className = 'rf-score ' + (left <= 0 ? 'high' : 'mod');
    if (left <= 0) {
      setText('recoveryTag', 'Fully recovered');
      setText('recoveryClears', '');
    } else {
      setText('recoveryTag', 'until fully recovered');
      const sameDay = clears.toDateString() === new Date().toDateString();
      const when = clears.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
      setText('recoveryClears', 'clears ' + (sameDay ? 'today ' : clears.toLocaleDateString('en-GB', { weekday: 'short' }) + ' ') + when);
    }
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
    const aePanel = $('aerobicChartPanel');
    if (aePanel) aePanel.style.display = 'none';
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
    if (AE_METRICS[metric]) { openAerobicPanel(metric); return; }
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
        if (e.target !== el) return;   // e.g. Enter on a help mark inside the tile
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleMetricChart(metric); }
      });
      // The "?" inside a tile explains it; tapping it shouldn't also expand the chart.
      el.querySelectorAll('.help-mark').forEach((h) => h.addEventListener('click', (e) => e.stopPropagation()));
    });
    // Re-render on resize so the 1:1 viewBox keeps matching the container.
    let t = null;
    window.addEventListener('resize', () => {
      if (!openMetric) return;
      clearTimeout(t);
      t = setTimeout(() => {
        if (AE_METRICS[openMetric]) { renderAerobicPanel(); return; }
        const d = metricHistoryCache[openMetric];
        if (d) renderMetricChart(d);
      }, 180);
    });
  }

  // --------------------------------------- aerobic efficiency / durability -
  // Our own metrics, from raw activity streams (docs/metrics.md). The two
  // tiles render from the payload's compact `aerobic` summary; expanding one
  // fetches the full weekly history from /api/aerobic (a cache read, no
  // Garmin call) and keeps it until the next sync changes it.
  const AE_METRICS = { aerobic: true, durability: true };
  const AE_LABEL_COLORS = { strong: '#6BA85E', moderate: '#D98A3C', low: '#C0564E' };
  let aerobicSummary = null;
  let aerobicHistory = null;
  let aerobicHistoryPromise = null;
  const aeSelected = { aerobic: null, durability: null };   // chosen series per panel

  function aeSportKey(series) { return series.replace(/_(indoor|outdoor)$/, ''); }

  function fmtAeOutput(v, unit) {
    if (typeof v !== 'number') return '—';
    if (unit === 'm/min') {
      const sec = Math.round(60000 / v);
      return Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0') + '/km';
    }
    if (unit === 'W/kg') return v.toFixed(2) + ' W/kg';
    return Math.round(v) + ' W';
  }

  // Each weekly point is a rolling 28-day window ending on that date.
  function aeWindowRange(endIso) {
    const end = new Date(endIso + 'T00:00:00');
    const start = new Date(end.getTime() - 27 * 86400000);
    const fmt = (d) => d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
    return fmt(start) + ' – ' + fmt(end);
  }

  function aeQuantity(unit) {
    return unit === 'W' ? 'Power' : (unit === 'm/min' ? 'Pace' : 'Metabolic power');
  }

  function aeBadge(conf) {
    return conf ? '<span class="ae-badge ' + conf + '">' + conf + '</span>' : '';
  }

  // Tiny trend line for a tile; null weeks are gaps in time, kept in the x scale.
  function aeSparkline(values) {
    const w = 64, h = 18;
    const pts = values.map((v, i) => [i, v]).filter((p) => typeof p[1] === 'number');
    if (pts.length < 2) return '';
    const vs = pts.map((p) => p[1]);
    let lo = Math.min.apply(null, vs), hi = Math.max.apply(null, vs);
    if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
    const n = Math.max(1, values.length - 1);
    const xy = pts.map((p) => [2 + (p[0] / n) * (w - 4), h - 2 - ((p[1] - lo) / (hi - lo)) * (h - 4)]);
    const last = xy[xy.length - 1];
    return '<svg class="ae-spark" viewBox="0 0 ' + w + ' ' + h + '" width="' + w + '" height="' + h + '" aria-hidden="true">' +
      '<path d="M' + xy.map((c) => c[0].toFixed(1) + ',' + c[1].toFixed(1)).join(' L') + '"/>' +
      '<circle cx="' + last[0].toFixed(1) + '" cy="' + last[1].toFixed(1) + '" r="2"/></svg>';
  }

  function renderAerobicTiles(ae) {
    if (!aerobicSummary || !ae || aerobicSummary.last_sync !== ae.last_sync) {
      aerobicHistory = null;          // a new sync landed: re-fetch detail on next open
      aerobicHistoryPromise = null;
    }
    aerobicSummary = ae;
    const grid = $('aerobicStatGrid');
    // Static JS is served fresh from disk but the page HTML is cached by the
    // running app until it restarts — a new script on an old page must not
    // take the whole dashboard down with it.
    if (!grid) return;
    if (!ae) { grid.style.display = 'none'; return; }
    grid.style.display = '';
    const pending = ae.status === 'pending';

    const eff = ae.efficiency || [];
    if (eff.length) {
      const p = eff[0];
      $('aerobicVal').innerHTML = escapeHtml(p.display) + '<span class="ae-unit"> @ ' + p.hr_ref + ' bpm</span>';
      let line1 = '';
      const ch = p.change_6w;
      if (ch && typeof ch.pct === 'number') {
        const up = ch.abs > 0;
        line1 = '<span class="ae-chg ' + (up ? 'up' : 'down') + '">' + (up ? '▲ ' : '▼ ') + escapeHtml(ch.display) +
                '</span> <span class="ae-dim">vs 6 wk ago</span>';
      } else {
        line1 = '<span class="ae-dim">' + (p.stale ? 'as of ' + escapeHtml(fmtShortDate(p.as_of)) : 'trend after 6 weeks of data') + '</span>';
      }
      $('aerobicLabel').innerHTML =
        '<div class="ae-line">' + line1 + aeSparkline(p.spark || []) + '</div>' +
        '<div class="ae-line">' + escapeHtml(p.label) + ' · ' + (p.indoor ? 'indoor' : 'outdoor') + ' ' +
        aeBadge(p.confidence) + ' <span class="ae-dim">' + p.n + ' pts</span></div>';
    } else {
      $('aerobicVal').textContent = '—';
      $('aerobicLabel').textContent = pending
        ? 'Computing from your recent activities — check back soon.'
        : 'Do steady efforts of 10+ min in your aerobic zone to unlock this.';
    }

    const dur = ae.durability || [];
    if (dur.length) {
      const last = dur[0].last;
      const color = AE_LABEL_COLORS[last.label] || 'inherit';
      $('durabilityVal').innerHTML = '<span style="color:' + color + '">' + last.decoupling_pct.toFixed(1) + '%</span>' +
        '<span class="ae-unit"> ' + escapeHtml(last.label) + '</span>';
      const bars = dur[0].sessions.slice().reverse().map((s) =>
        '<span class="ae-minibar" title="' + escapeHtml(fmtShortDate(s.date) + ': ' + s.decoupling_pct.toFixed(1) + '%') +
        '" style="height:' + Math.max(2, Math.min(18, Math.abs(s.decoupling_pct) * 1.5)).toFixed(0) +
        'px;background:' + (AE_LABEL_COLORS[s.label] || 'var(--ink-faint)') + '"></span>').join('');
      $('durabilityLabel').innerHTML =
        '<div class="ae-line"><span class="ae-dim">decoupling, last long session</span><span class="ae-minibars">' + bars + '</span></div>' +
        '<div class="ae-line">' + escapeHtml(last.name || dur[0].label) + ' · ' + escapeHtml(fmtShortDate(last.date)) + '</div>';
    } else {
      $('durabilityVal').textContent = '—';
      $('durabilityLabel').textContent = pending
        ? 'Computing from your recent activities — check back soon.'
        : 'Do a steady ride, run or hike of 90+ min (no intervals) to unlock this.';
    }

    if (AE_METRICS[openMetric] && aerobicHistory === null) openAerobicPanel(openMetric);
  }

  function ensureAerobicHistory() {
    if (aerobicHistory) return Promise.resolve(aerobicHistory);
    if (!aerobicHistoryPromise) {
      aerobicHistoryPromise = fetch('/api/aerobic')
        .then((r) => r.json())
        .then((d) => { aerobicHistory = d; return d; })
        .catch((e) => { aerobicHistoryPromise = null; throw e; });
    }
    return aerobicHistoryPromise;
  }

  function openAerobicPanel(metric) {
    $('aerobicChartPanel').style.display = 'block';
    if (!aerobicHistory) {
      setText('aerobicChartTitle', 'Loading…');
      setText('aerobicChartMeta', '');
      $('aerobicControls').innerHTML = '';
      $('aerobicChartPlot').innerHTML = '';
      $('aerobicFoot').innerHTML = '';
    }
    ensureAerobicHistory()
      .then(() => { if (openMetric === metric) renderAerobicPanel(); })
      .catch(() => {
        if (openMetric !== metric) return;
        setText('aerobicChartTitle', 'Could not load history');
        $('aerobicChartPlot').innerHTML = '<div class="mc-empty">Try again in a moment.</div>';
      });
  }

  function renderAerobicPanel() {
    if (!aerobicHistory || !AE_METRICS[openMetric]) return;
    $('aerobicChartTip').style.display = 'none';
    if (openMetric === 'aerobic') renderEfficiencyPanel(aerobicHistory);
    else renderDurabilityPanel(aerobicHistory);
  }

  // Sport tabs (only sports with data) + an indoor/outdoor toggle when the
  // chosen sport has both. `candidates` are series entries from /api/aerobic.
  function aeControls(candidates, panelKey) {
    const current = candidates.find((s) => s.series === aeSelected[panelKey]) || candidates[0];
    aeSelected[panelKey] = current.series;
    const sports = [];
    candidates.forEach((s) => { const k = aeSportKey(s.series); if (!sports.some((x) => x.key === k)) sports.push({ key: k, label: s.label }); });
    const curSport = aeSportKey(current.series);
    let html = '';
    if (sports.length > 1) {
      html += '<div class="ae-seg" role="tablist">' + sports.map((sp) =>
        '<button type="button" role="tab" data-sport="' + sp.key + '" class="' + (sp.key === curSport ? 'on' : '') +
        '" aria-selected="' + (sp.key === curSport) + '">' + escapeHtml(sp.label) + '</button>').join('') + '</div>';
    }
    const variants = candidates.filter((s) => aeSportKey(s.series) === curSport);
    if (variants.length > 1) {
      html += '<div class="ae-seg">' + variants.map((v) =>
        '<button type="button" data-series="' + v.series + '" class="' + (v.series === current.series ? 'on' : '') + '">' +
        (v.indoor ? 'Indoor' : 'Outdoor') + '</button>').join('') + '</div>';
    } else {
      html += '<span class="ae-dim ae-io-note">' + (current.indoor ? 'Indoor' : 'Outdoor') + '</span>';
    }
    const box = $('aerobicControls');
    box.innerHTML = html;
    box.querySelectorAll('button[data-sport]').forEach((b) => b.addEventListener('click', () => {
      const k = b.getAttribute('data-sport');
      const sameIo = candidates.find((s) => aeSportKey(s.series) === k && s.indoor === current.indoor);
      aeSelected[panelKey] = (sameIo || candidates.find((s) => aeSportKey(s.series) === k)).series;
      renderAerobicPanel();
    }));
    box.querySelectorAll('button[data-series]').forEach((b) => b.addEventListener('click', () => {
      aeSelected[panelKey] = b.getAttribute('data-series');
      renderAerobicPanel();
    }));
    return current;
  }

  function aeBindTips(body, tip, coords, htmlFor) {
    const hide = () => { tip.style.display = 'none'; };
    body.querySelectorAll('.mc-hit').forEach((hit) => {
      const i = parseInt(hit.getAttribute('data-i'), 10);
      hit.addEventListener('mouseenter', () => {
        tip.innerHTML = htmlFor(i);
        tip.style.display = 'block';
        const svgEl = body.querySelector('svg');
        const scale = svgEl.clientWidth / svgEl.viewBox.baseVal.width || 1;
        const x = coords[i][0] * scale, y = coords[i][1] * scale;
        let left = x + 12;
        if (left + tip.offsetWidth > body.clientWidth) left = x - tip.offsetWidth - 12;
        tip.style.left = Math.max(0, left) + 'px';
        tip.style.top = Math.max(0, y - tip.offsetHeight - 8) + 'px';
      });
      hit.addEventListener('mouseleave', hide);
    });
  }

  function aeEmpty(title, message) {
    setText('aerobicChartTitle', title);
    setText('aerobicChartMeta', '');
    $('aerobicControls').innerHTML = '';
    $('aerobicChartPlot').innerHTML = '<div class="mc-empty">' + escapeHtml(message) + '</div>';
    $('aerobicFoot').innerHTML = '';
  }

  function renderEfficiencyPanel(d) {
    const candidates = (d.series || []).filter((s) => (s.weeks || []).some((w) => typeof w.output_at_hr_ref === 'number'));
    if (!candidates.length) {
      aeEmpty('Aerobic Efficiency', d.status === 'pending'
        ? 'Your activities are still being analysed — check back soon.'
        : 'Do steady efforts of 10+ min in your aerobic zone to unlock this. Rides need a power meter; runs and hikes need GPS.');
      return;
    }
    if (!aeSelected.aerobic && aerobicSummary && aerobicSummary.efficiency && aerobicSummary.efficiency.length) {
      aeSelected.aerobic = aerobicSummary.efficiency[0].series;
    }
    const s = aeControls(candidates, 'aerobic');
    const unit = s.unit;
    setText('aerobicChartTitle', aeQuantity(unit) + ' at ' + s.hr_ref + ' bpm · ' + s.label + ' ' + (s.indoor ? 'indoor' : 'outdoor'));
    const ch = s.change_6w;
    setText('aerobicChartMeta', 'rolling 28 days, weekly' + (ch && ch.display ? ' · 6 wk: ' + ch.display : ''));

    const weeks = s.weeks.filter((w) => typeof w.output_at_hr_ref === 'number');
    const body = $('aerobicChartPlot');
    const W = Math.max(320, body.clientWidth || 600), H = 200;
    const padL = 58, padR = 14, padT = 12, padB = 24;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const vals = weeks.map((w) => w.output_at_hr_ref);
    let lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    const pad = (hi - lo) * 0.15 || Math.max(1, hi * 0.04);
    lo -= pad; hi += pad;
    // x by real date so a gap with no steady sessions reads as a gap.
    const t = (w) => new Date(w.week_end + 'T00:00:00').getTime();
    const t0 = t(weeks[0]), t1 = t(weeks[weeks.length - 1]);
    const xAt = (w) => (t1 === t0 ? padL + plotW / 2 : padL + ((t(w) - t0) / (t1 - t0)) * plotW);
    const yAt = (v) => padT + plotH - ((v - lo) / (hi - lo)) * plotH;

    let svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="' + W + '" height="' + H + '" role="img" aria-label="Aerobic efficiency history">';
    for (let k = 0; k <= 2; k++) {
      const v = lo + ((hi - lo) * k) / 2, y = yAt(v);
      svg += '<line class="mc-gridline" x1="' + padL + '" x2="' + (W - padR) + '" y1="' + y.toFixed(1) + '" y2="' + y.toFixed(1) + '" stroke-dasharray="3 4"/>';
      svg += '<text class="mc-axis" x="' + (padL - 6) + '" y="' + (y + 3).toFixed(1) + '" text-anchor="end">' + escapeHtml(fmtAeOutput(v, unit)) + '</text>';
    }
    svg += '<text class="mc-axis" x="' + padL + '" y="' + (H - 6) + '">' + escapeHtml(fmtShortDate(weeks[0].week_end)) + '</text>';
    if (weeks.length > 1) {
      svg += '<text class="mc-axis" x="' + (W - padR) + '" y="' + (H - 6) + '" text-anchor="end">' + escapeHtml(fmtShortDate(weeks[weeks.length - 1].week_end)) + '</text>';
    }
    const coords = weeks.map((w) => [xAt(w), yAt(w.output_at_hr_ref)]);
    if (coords.length > 1) {
      svg += '<path class="mc-line" d="M' + coords.map((c) => c[0].toFixed(1) + ',' + c[1].toFixed(1)).join(' L') + '"/>';
    }
    weeks.forEach((w, i) => {
      const c = coords[i], last = i === weeks.length - 1;
      svg += '<circle class="ae-dot ' + (w.confidence || 'low') + (last ? ' last' : '') + '" cx="' + c[0].toFixed(1) +
             '" cy="' + c[1].toFixed(1) + '" r="' + (last ? 4.5 : 3.2) + '"/>';
    });
    coords.forEach((c, i) => {
      svg += '<circle class="mc-hit" cx="' + c[0].toFixed(1) + '" cy="' + c[1].toFixed(1) + '" r="11" fill="transparent" data-i="' + i + '"/>';
    });
    body.innerHTML = svg + '</svg>';
    aeBindTips(body, $('aerobicChartTip'), coords, (i) => {
      const w = weeks[i];
      return '<b>' + escapeHtml(fmtAeOutput(w.output_at_hr_ref, unit)) + '</b> @ ' + s.hr_ref + ' bpm' +
        (typeof w.hr_at_ref_output === 'number' ? '<br>HR at ' + escapeHtml(fmtAeOutput(s.ref_output, unit)) + ': ' + w.hr_at_ref_output.toFixed(0) + ' bpm' : '') +
        '<br>' + w.n + ' pts · ' + escapeHtml(w.confidence || '') + ' confidence' +
        (typeof w.residual_mad_bpm === 'number' ? ' · scatter ±' + w.residual_mad_bpm.toFixed(1) + ' bpm' : '') +
        (typeof w.mean_temp_c === 'number' ? '<br>avg ' + w.mean_temp_c.toFixed(0) + ' °C' + (w.hot_points ? ' (' + w.hot_points + ' hot pts count half)' : '') : '') +
        '<span class="mc-tip-date">from sessions ' + escapeHtml(aeWindowRange(w.week_end)) + '</span>';
    });

    const latest = s.latest || {};
    const refVal = unit === 'm/min' ? fmtAeOutput(s.ref_output, unit).replace('/km', '') :
      (typeof s.ref_output === 'number' ? (unit === 'W/kg' ? s.ref_output.toFixed(2) : Math.round(s.ref_output)) : '');
    const unitHint = unit === 'm/min' ? 'min/km' : unit;
    // The same fit read the other way round: the HR this window predicts at
    // an output you choose (e.g. your usual Z2 watts). Editable in place.
    let foot = '<div class="ae-foot-row"><form class="ae-pin" id="aeRefForm"><label>Your HR at ' +
      '<input id="aeRefInput" type="text" inputmode="decimal" value="' + escapeHtml(String(refVal)) +
      '" aria-label="Output to check HR at, in ' + unitHint + '"> ' + escapeHtml(unitHint) + '</label>' +
      '<span>: <b>' + (typeof latest.hr_at_ref_output === 'number' ? latest.hr_at_ref_output.toFixed(0) + ' bpm' : '—') + '</b> ' +
      aeBadge(latest.confidence) + '</span>' +
      '<button type="submit">Update</button>' +
      (s.ref_output_pinned ? '<button type="button" id="aeRefReset" class="ae-icon-btn" title="Reset to your typical recent ' +
        (unit === 'm/min' ? 'pace' : 'output') + '" aria-label="Reset to your typical recent ' + (unit === 'm/min' ? 'pace' : 'output') + '">' +
        '<svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><path d="M3.2 6.2A5 5 0 1 1 3 9.6" fill="none" ' +
        'stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><path d="M2.4 2.6v3.9h3.9" fill="none" ' +
        'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg></button>' : '') +
      '</form></div>';
    foot += '<div class="ae-dim">Type any ' + (unit === 'm/min' ? 'pace' : 'output') + ' to see your HR at it' +
      (s.ref_output_pinned ? '' : ' (default: your typical recent ' + (unit === 'm/min' ? 'pace' : 'output') + ')') + '. ' +
      'Lower HR at the same output, or more output at ' + s.hr_ref + ' bpm, means fitter. ' +
      (s.hr_ref_source === 'easy_sessions' ? s.hr_ref + ' bpm is your typical easy-run HR (last 180 days, ' + (s.easy_sessions || '') + ' runs). ' : '') +
      (s.slope ? 'HR rises ' + s.slope.b.toFixed(unit === 'W' ? 2 : (unit === 'W/kg' ? 1 : 2)) + ' ' + escapeHtml(s.slope.unit) +
        (s.slope.source === 'fitted' ? ' (your ' + escapeHtml(s.sport) + ' data, 180 days)'
          : s.slope.source === 'shrunk' ? ' (your data, blended with a typical value — still little data)'
          : ' (typical value — not enough of your own data yet)') + '. ' : '') +
      (s.indoor ? 'Indoor sessions have no temperature, so heat isn\'t accounted for.'
                : 'Outdoor points above 25 °C count half (heat raises HR).') +
      ' <span class="ae-key"><i class="ae-dot-k high"></i>high <i class="ae-dot-k medium"></i>medium <i class="ae-dot-k low"></i>low confidence</span></div>';
    $('aerobicFoot').innerHTML = foot;
    const form = $('aeRefForm');
    const post = (value) => fetch('/api/aerobic/ref-output', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ series: s.series, value: value }),
    }).then((r) => r.json()).then((res) => {
      if (res.error) { alert(res.error); return; }
      aerobicHistory = null; aerobicHistoryPromise = null;
      openAerobicPanel('aerobic');
    });
    form.addEventListener('submit', (e) => { e.preventDefault(); post($('aeRefInput').value.trim()); });
    const reset = $('aeRefReset');
    if (reset) reset.addEventListener('click', () => post(null));
  }

  function renderDurabilityPanel(d) {
    const candidates = (d.series || []).filter((s) => (s.durability || []).length);
    if (!candidates.length) {
      aeEmpty('Durability', d.status === 'pending'
        ? 'Your activities are still being analysed — check back soon.'
        : 'Do a steady ride, run or hike of 90+ min (no intervals) to unlock this.');
      return;
    }
    if (!aeSelected.durability && aerobicSummary && aerobicSummary.durability && aerobicSummary.durability.length) {
      aeSelected.durability = aerobicSummary.durability[0].series;
    }
    const s = aeControls(candidates, 'durability');
    const sessions = s.durability.slice().reverse();     // oldest left
    setText('aerobicChartTitle', 'Durability · ' + s.label + ' ' + (s.indoor ? 'indoor' : 'outdoor'));
    setText('aerobicChartMeta', 'last ' + sessions.length + ' long session' + (sessions.length === 1 ? '' : 's'));

    const body = $('aerobicChartPlot');
    const W = Math.max(320, body.clientWidth || 600), H = 190;
    const padL = 36, padR = 14, padT = 12, padB = 24;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const vals = sessions.map((x) => x.decoupling_pct);
    const hi = Math.max(10, Math.max.apply(null, vals) + 1);
    const lo = Math.min(0, Math.min.apply(null, vals) - 1);
    const yAt = (v) => padT + plotH - ((v - lo) / (hi - lo)) * plotH;
    const slot = plotW / Math.max(sessions.length, 4);
    const bw = Math.min(34, slot * 0.6);

    let svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="' + W + '" height="' + H + '" role="img" aria-label="Durability per session">';
    [[0, 'mc-gridline', '0%'], [5, 'ae-band strong', '5%'], [8, 'ae-band low', '8%']].forEach((b) => {
      const y = yAt(b[0]);
      svg += '<line class="' + b[1] + '" x1="' + padL + '" x2="' + (W - padR) + '" y1="' + y.toFixed(1) + '" y2="' + y.toFixed(1) + '"' + (b[0] ? ' stroke-dasharray="4 4"' : '') + '/>';
      svg += '<text class="mc-axis" x="' + (padL - 6) + '" y="' + (y + 3).toFixed(1) + '" text-anchor="end">' + b[2] + '</text>';
    });
    const coords = [];
    sessions.forEach((x, i) => {
      const cx = padL + slot * i + slot / 2;
      const y0 = yAt(0), y1 = yAt(x.decoupling_pct);
      const top = Math.min(y0, y1), h = Math.max(1.5, Math.abs(y1 - y0));
      svg += '<rect x="' + (cx - bw / 2).toFixed(1) + '" y="' + top.toFixed(1) + '" width="' + bw.toFixed(1) + '" height="' + h.toFixed(1) +
             '" rx="2" fill="' + (AE_LABEL_COLORS[x.label] || 'var(--ink-faint)') + '" opacity="0.85"/>';
      svg += '<text class="mc-axis" x="' + cx.toFixed(1) + '" y="' + (H - 6) + '" text-anchor="middle">' + escapeHtml(fmtShortDate(x.date)) + '</text>';
      coords.push([cx, top]);
    });
    sessions.forEach((x, i) => {
      svg += '<rect class="mc-hit" x="' + (coords[i][0] - slot / 2).toFixed(1) + '" y="' + padT + '" width="' + slot.toFixed(1) +
             '" height="' + plotH + '" fill="transparent" data-i="' + i + '"/>';
    });
    body.innerHTML = svg + '</svg>';
    aeBindTips(body, $('aerobicChartTip'), coords, (i) => {
      const x = sessions[i];
      const hrs = x.hr_at_reference_output, np = x.normalized_output;
      const halves = x.method === 'regression' && hrs
        ? '<br>HR at ' + escapeHtml(x.reference_output || '—') + ': ' + hrs[0].toFixed(0) + ' → ' + hrs[1].toFixed(0) + ' bpm' +
          '<br><span class="ae-dim">fitted on ' + x.chunks_per_half[0] + ' + ' + x.chunks_per_half[1] + ' steady 5-min chunks</span>'
        : '<br>1st half: ' + escapeHtml((np && np[0]) || x.first_half.output || '—') + ' @ ' + (x.first_half.hr || 0).toFixed(0) + ' bpm' +
          '<br>2nd half: ' + escapeHtml((np && np[1]) || x.second_half.output || '—') + ' @ ' + (x.second_half.hr || 0).toFixed(0) + ' bpm' +
          (np && s.unit === 'W' ? '<br><span class="ae-dim">normalized power per half</span>' : '');
      return '<b>' + x.decoupling_pct.toFixed(1) + '%</b> ' + escapeHtml(x.label) +
        '<br>' + escapeHtml(x.name || '') + ' · ' + x.moving_min + ' min' + halves +
        (typeof x.temp_c === 'number' ? '<br>' + x.temp_c.toFixed(0) + ' °C' : '') +
        '<span class="mc-tip-date">' + escapeHtml(fmtShortDate(x.date)) + '</span>';
    });
    const nExcl = (s.durability_excluded || []).length;
    $('aerobicFoot').innerHTML = '<div class="ae-dim">Drop in output per heartbeat from the first to the second half, after the first 10 min, ' +
      'compared at the same output (HR fitted on each half\'s steady chunks; normalized ' + (s.unit === 'W' ? 'power' : 'output') +
      ' per half when there are too few). ' +
      (nExcl ? nExcl + ' long session' + (nExcl === 1 ? ' was' : 's were') + ' left out because the halves weren\'t comparable. ' : '') +
      '<span style="color:' + AE_LABEL_COLORS.strong + '">&lt; 5% strong</span> · <span style="color:' + AE_LABEL_COLORS.moderate +
      '">5–8% moderate</span> · <span style="color:' + AE_LABEL_COLORS.low + '">&gt; 8% low</span>. ' +
      (s.indoor ? 'Indoor: no temperature recorded.' : 'Heat raises HR late in a session, so hot days read worse.') + '</div>';
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

  // ---------------------------------------------------- vitals detail boxes --
  // Fitness Age / Resting HR / Last Sleep / Body Battery each expand in place
  // to full row width; opening one closes whichever else was open. Sleep's
  // detail is already in the dashboard payload; the three charts load their
  // history on first open from /api/metric-history (server-cached).
  const VITAL_CHART_TTL_MS = 10 * 60 * 1000;
  const vitalHistory = {}; // metric -> {data, fetchedAt} | {promise}
  let openVital = null;

  function bindVitalBoxes() {
    const boxes = Array.from(document.querySelectorAll('.ms-box'));
    const setOpen = (box, open) => {
      box.classList.toggle('open', open);
      box.setAttribute('aria-expanded', String(open));
      box.querySelector('.ms-detail').hidden = !open;
    };
    const toggle = (box) => {
      const opening = !box.classList.contains('open');
      boxes.forEach((b) => setOpen(b, b === box && opening));
      openVital = opening ? box.dataset.vital : null;
      if (opening) loadVitalChart(box.dataset.vital);
    };
    boxes.forEach((box) => {
      box.addEventListener('click', (e) => {
        // Interacting with an open chart (hover/tap for values) shouldn't
        // collapse it — only the box's own header area toggles.
        if (e.target.closest('.ms-detail')) return;
        toggle(box);
      });
      box.addEventListener('keydown', (e) => {
        if (e.target !== box) return;
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(box); }
      });
    });
    let t = null;
    window.addEventListener('resize', () => {
      if (!openVital) return;
      clearTimeout(t);
      t = setTimeout(() => {
        const cached = vitalHistory[openVital];
        if (cached && cached.data) renderVitalChart(openVital, cached.data);
      }, 180);
    });
  }

  function vitalChartEl(metric) {
    return $({ fitness_age: 'fitnessAgeChart', rhr: 'rhrChart', body_battery: 'batteryChart', hrv: 'hrvChart', sleep: 'sleepTrend' }[metric]);
  }

  function loadVitalChart(metric) {
    const el = vitalChartEl(metric);
    const cached = vitalHistory[metric];
    if (cached && cached.data && Date.now() - cached.fetchedAt < VITAL_CHART_TTL_MS) {
      renderVitalChart(metric, cached.data);
      return;
    }
    if (cached && cached.promise) return;
    if (!(cached && cached.data)) el.innerHTML = '<div class="mc-empty">Loading from Garmin…</div>';
    const promise = fetch('/api/metric-history?metric=' + encodeURIComponent(metric))
      .then((r) => r.json())
      .then((data) => {
        vitalHistory[metric] = { data, fetchedAt: Date.now() };
        if (openVital === metric) renderVitalChart(metric, data);
      })
      .catch((err) => {
        vitalHistory[metric] = null;
        if (openVital === metric) el.innerHTML = '<div class="mc-empty">Couldn’t load this from Garmin: ' + escapeHtml(String(err)) + '</div>';
      });
    vitalHistory[metric] = Object.assign({}, cached || {}, { promise });
  }

  function renderVitalChart(metric, data) {
    const el = vitalChartEl(metric);
    if (data.error) {
      el.innerHTML = '<div class="mc-empty">' + escapeHtml(data.error) + '</div>';
      return;
    }
    if (metric === 'body_battery') return renderBodyBatteryChart(el, data);
    if (metric === 'rhr') return renderRhrChart(el, data);
    if (metric === 'fitness_age') return renderFitnessAgeChart(el, data);
    if (metric === 'hrv') return renderHrvChart(el, data);
    if (metric === 'sleep') return renderSleepTrendChart(el, data);
  }

  function vitalHead(title, meta) {
    return '<div class="mc-head"><span class="mc-title">' + escapeHtml(title) + '</span>' +
      '<span class="mc-meta">' + escapeHtml(meta || '') + '</span></div>';
  }

  function fmtClock(ms) {
    return new Date(ms).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  }

  function renderBodyBatteryChart(el, data) {
    const pts = (data.points || []).filter((p) => typeof p.t === 'number');
    const measured = pts.filter((p) => typeof p.value === 'number');
    if (!measured.length) {
      el.innerHTML = vitalHead('Body Battery — today', '') + '<div class="mc-empty">No body battery recorded yet today.</div>';
      return;
    }
    const vals = measured.map((p) => p.value);
    const meta = [
      data.charged != null ? '+' + data.charged + ' charged' : null,
      data.drained != null ? '−' + data.drained + ' drained' : null,
      'low ' + Math.min.apply(null, vals) + ' · high ' + Math.max.apply(null, vals),
    ].filter(Boolean).join(' · ');
    el.innerHTML = vitalHead('Body Battery — today', meta) + '<div class="vc-plot"></div>';
    const plot = el.querySelector('.vc-plot');
    const x0 = pts[0].t, x1 = Math.max(pts[pts.length - 1].t, x0 + 3600000);
    const narrow = (plot.clientWidth || 600) < 420;
    const ticks = [];
    const d = new Date(x0); d.setMinutes(0, 0, 0);
    const step = narrow ? 6 : 3;
    d.setHours(Math.ceil(d.getHours() / step) * step);
    for (; d.getTime() <= x1; d.setHours(d.getHours() + step)) ticks.push({ x: d.getTime(), label: pad2(d.getHours()) + ':00' });
    drawVitalChart(plot, {
      height: 170,
      xDomain: [x0, x1], yDomain: [0, 100], yTicks: [0, 25, 50, 75, 100],
      xTicks: ticks,
      series: [{ cls: 'teal', area: true, points: pts.map((p) => ({ x: p.t, y: p.value })) }],
      tip: (i) => '<b>' + pts[i].value + '</b><span class="mc-tip-date">' + fmtClock(pts[i].t) + '</span>',
    });
  }

  function renderRhrChart(el, data) {
    const pts = data.points || [];
    if (!pts.length) {
      el.innerHTML = vitalHead('Resting HR — last 7 days', '') + '<div class="mc-empty">No resting HR recorded in the last 7 days.</div>';
      return;
    }
    const vals = pts.map((p) => p.value);
    const lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    const avg = data.avg;
    el.innerHTML = vitalHead('Resting HR — last 7 days',
      (avg != null ? '7-day avg ' + avg.toFixed(1) + ' bpm · ' : '') + 'range ' + lo + '–' + hi) + '<div class="vc-plot"></div>';
    const yLo = Math.floor(Math.min(lo, avg || lo) - 3), yHi = Math.ceil(Math.max(hi, avg || hi) + 3);
    drawVitalChart(el.querySelector('.vc-plot'), {
      height: 160,
      xDomain: [-0.5, pts.length - 0.5], yDomain: [yLo, yHi], yTicks: [yLo, yHi],
      xTicks: pts.map((p, i) => ({ x: i, label: parseLocalDate(p.date).toLocaleDateString('en-GB', { weekday: 'short' }) })),
      refLines: avg != null ? [{ y: avg, label: 'avg ' + avg.toFixed(1) }] : [],
      series: [{ cls: 'gold', dots: true, points: pts.map((p, i) => ({ x: i, y: p.value })) }],
      tip: (i) => '<b>' + pts[i].value + '</b> bpm<span class="mc-tip-date">' +
        parseLocalDate(pts[i].date).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' }) + '</span>',
    });
  }

  function renderFitnessAgeChart(el, data) {
    const pts = data.points || [];
    if (!pts.length) {
      el.innerHTML = vitalHead('Fitness Age', '') + '<div class="mc-empty">No fitness age history from Garmin yet.</div>';
      return;
    }
    const first = pts[0], last = pts[pts.length - 1];
    const change = last.fitness - first.fitness;
    el.innerHTML = vitalHead('Fitness Age',
      (change <= 0 ? '' : '+') + change.toFixed(1) + ' yr since ' + fmtShortDate(first.date)) +
      '<div class="vc-legend">' +
        '<span><i class="vc-swatch gold"></i>Fitness age</span>' +
        '<span><i class="vc-swatch teal dashed"></i>Achievable</span>' +
        '<span><i class="vc-swatch faint dashed"></i>Actual age</span>' +
      '</div><div class="vc-plot"></div>';
    const xs = pts.map((p) => parseLocalDate(p.date).getTime());
    const all = [];
    pts.forEach((p) => ['fitness', 'achievable', 'chrono'].forEach((k) => { if (typeof p[k] === 'number') all.push(p[k]); }));
    const yLo = Math.floor(Math.min.apply(null, all) - 1), yHi = Math.ceil(Math.max.apply(null, all) + 1);
    const ticks = [];
    const m = new Date(xs[0]); m.setDate(1); m.setMonth(m.getMonth() + 1);
    for (; m.getTime() <= xs[xs.length - 1]; m.setMonth(m.getMonth() + 1)) {
      ticks.push({ x: m.getTime(), label: m.toLocaleDateString('en-GB', { month: 'short' }) });
    }
    const line = (k) => pts.map((p, i) => ({ x: xs[i], y: typeof p[k] === 'number' ? p[k] : null }));
    const fmt = (v) => (typeof v === 'number' ? v.toFixed(1).replace(/\.0$/, '') : '—');
    drawVitalChart(el.querySelector('.vc-plot'), {
      height: 180,
      xDomain: [xs[0], Math.max(xs[xs.length - 1], xs[0] + 86400000)], yDomain: [yLo, yHi], yTicks: [yLo, yHi],
      xTicks: ticks,
      series: [
        { cls: 'faint', dashed: true, points: line('chrono') },
        { cls: 'teal', dashed: true, points: line('achievable') },
        { cls: 'gold', points: line('fitness'), lastDot: true },
      ],
      tipSeries: 2,
      tip: (i) => '<b>' + fmt(pts[i].fitness) + '</b> fitness age' +
        '<span class="mc-tip-date">achievable ' + fmt(pts[i].achievable) + ' · actual ' + fmt(pts[i].chrono) + '</span>' +
        '<span class="mc-tip-date">' + fmtShortDate(pts[i].date) + '</span>',
    });
  }

  function dateTicks(xs, everyDays) {
    // A label every `everyDays` days, anchored on the LAST point (today) so
    // the most recent tick is always a real, recent date.
    const ticks = [];
    for (let t = xs[xs.length - 1]; t >= xs[0]; t -= everyDays * 86400000) {
      ticks.push({ x: t, label: new Date(t).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) });
    }
    return ticks;
  }

  function renderHrvChart(el, data) {
    const pts = (data.points || []).filter((p) => typeof p.night === 'number' || typeof p.week === 'number');
    if (!pts.length) {
      el.innerHTML = vitalHead('HRV', '') + '<div class="mc-empty">No overnight HRV from Garmin yet — it needs a few nights of wear.</div>';
      return;
    }
    const last = pts[pts.length - 1];
    const meta = [
      typeof last.week === 'number' ? '7-day avg ' + last.week + ' ms' : null,
      typeof last.low === 'number' ? 'baseline ' + last.low + '–' + last.high : null,
      last.status,
    ].filter(Boolean).join(' · ');
    el.innerHTML = vitalHead(data.label || 'HRV', meta) +
      '<div class="vc-legend">' +
        '<span><i class="vc-swatch dot faint"></i>Nightly</span>' +
        '<span><i class="vc-swatch gold"></i>7-day average</span>' +
        '<span><i class="vc-swatch band"></i>Your balanced range</span>' +
      '</div><div class="vc-plot"></div>';
    const xs = pts.map((p) => parseLocalDate(p.date).getTime());
    const all = [];
    pts.forEach((p) => ['night', 'week', 'low', 'high'].forEach((k) => { if (typeof p[k] === 'number') all.push(p[k]); }));
    const yLo = Math.floor((Math.min.apply(null, all) - 4) / 5) * 5, yHi = Math.ceil((Math.max.apply(null, all) + 4) / 5) * 5;
    const narrow = (el.clientWidth || 600) < 420;
    drawVitalChart(el.querySelector('.vc-plot'), {
      height: 180,
      xDomain: [xs[0] - 43200000, xs[xs.length - 1] + 43200000], yDomain: [yLo, yHi], yTicks: [yLo, yHi],
      xTicks: dateTicks(xs, narrow ? 21 : 14),
      band: { cls: 'teal', points: pts.map((p, i) => ({ x: xs[i], lo: p.low, hi: p.high })) },
      series: [
        { cls: 'faint', noLine: true, dots: true, dotR: 2.4, points: pts.map((p, i) => ({ x: xs[i], y: typeof p.night === 'number' ? p.night : null })) },
        { cls: 'gold', points: pts.map((p, i) => ({ x: xs[i], y: typeof p.week === 'number' ? p.week : null })), lastDot: true },
      ],
      tipSeries: 0,
      tip: (i) => {
        const p = pts[i];
        return '<b>' + (p.night != null ? p.night : '—') + '</b> ms overnight' +
          '<span class="mc-tip-date">7-day avg ' + (p.week != null ? p.week : '—') +
          (p.low != null ? ' · range ' + p.low + '–' + p.high : '') + '</span>' +
          '<span class="mc-tip-date">' + fmtShortDate(p.date) + (p.status ? ' · ' + escapeHtml(p.status) : '') + '</span>';
      },
    });
  }

  function fmtHours(h) {
    const mins = Math.round(h * 60);
    return Math.floor(mins / 60) + 'h ' + pad2(mins % 60) + 'm';
  }

  function renderSleepTrendChart(el, data) {
    const pts = data.points || [];
    if (!pts.length) {
      el.innerHTML = '<div class="mc-empty">No sleep recorded in the last 4 weeks.</div>';
      return;
    }
    const meta = [
      data.avg_hours != null ? 'avg ' + fmtHours(data.avg_hours) : null,
      data.avg_score != null ? 'score ' + Math.round(data.avg_score) : null,
      data.need_hours != null ? 'need ' + fmtHours(data.need_hours) : null,
    ].filter(Boolean).join(' · ');
    el.innerHTML = vitalHead(data.label || 'Sleep — last 4 weeks', meta) +
      '<div class="vc-legend">' +
        '<span><i class="vc-swatch block teal"></i>Score 80+</span>' +
        '<span><i class="vc-swatch block gold"></i>60–79</span>' +
        '<span><i class="vc-swatch block rust"></i>Under 60</span>' +
      '</div><div class="vc-plot"></div>';
    const xs = pts.map((p) => parseLocalDate(p.date).getTime());
    const hi = Math.max(9, Math.ceil(Math.max.apply(null, pts.map((p) => p.hours)) + 0.5));
    const band = (sc) => (typeof sc !== 'number' ? 'faint' : sc >= 80 ? 'teal' : sc >= 60 ? 'gold' : 'rust');
    const narrow = (el.clientWidth || 600) < 420;
    drawVitalChart(el.querySelector('.vc-plot'), {
      height: 170,
      xDomain: [xs[0] - 43200000, xs[xs.length - 1] + 43200000], yDomain: [0, hi], yTicks: [0, 4, 8].filter((v) => v <= hi),
      yFmt: (v) => v + 'h',
      xTicks: dateTicks(xs, narrow ? 14 : 7),
      refLines: data.need_hours != null ? [{ y: data.need_hours, label: 'need ' + fmtHours(data.need_hours) }] : [],
      series: [{ bars: true, cls: 'teal', points: pts.map((p, i) => ({ x: xs[i], y: p.hours, cls: band(p.score) })) }],
      tip: (i) => {
        const p = pts[i];
        return '<b>' + fmtHours(p.hours) + '</b>' + (p.score != null ? ' · score ' + p.score : '') +
          '<span class="mc-tip-date">' + parseLocalDate(p.date).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' }) +
          (p.need_hours != null ? ' · need ' + fmtHours(p.need_hours) : '') + '</span>';
      },
    });
  }

  // Minimal shared line chart for the vitals boxes: 1:1 pixel viewBox (same
  // reasoning as the ridge/metric charts — text and strokes never stretch),
  // null y breaks the line, hover/tap shows the nearest point.
  function drawVitalChart(plot, cfg) {
    const W = Math.max(260, plot.clientWidth || 600), H = cfg.height || 170;
    const padL = 30, padR = 10, padT = 10, padB = 22;
    const [x0, x1] = cfg.xDomain, [y0, y1] = cfg.yDomain;
    const xAt = (x) => padL + ((x - x0) / ((x1 - x0) || 1)) * (W - padL - padR);
    const yAt = (y) => padT + (1 - (y - y0) / ((y1 - y0) || 1)) * (H - padT - padB);
    let svg = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="' + W + '" height="' + H + '" class="vc-svg">';
    (cfg.yTicks || []).forEach((v) => {
      svg += '<line class="mc-gridline" x1="' + padL + '" x2="' + (W - padR) + '" y1="' + yAt(v) + '" y2="' + yAt(v) + '"/>' +
        '<text class="mc-axis" x="' + (padL - 5) + '" y="' + (yAt(v) + 3) + '" text-anchor="end">' + (cfg.yFmt ? cfg.yFmt(v) : v) + '</text>';
    });
    (cfg.xTicks || []).forEach((t) => {
      const x = xAt(t.x);
      if (x < padL - 1 || x > W - padR + 1) return;
      svg += '<text class="mc-axis" x="' + x + '" y="' + (H - 6) + '" text-anchor="middle">' + escapeHtml(t.label) + '</text>';
    });
    (cfg.vLines || []).forEach((v, k) => {
      const x = xAt(v.x);
      svg += '<line class="vc-vline" x1="' + x + '" x2="' + x + '" y1="' + padT + '" y2="' + (H - padB) + '"/>' +
        '<text class="vc-vline-label" x="' + (x + 3) + '" y="' + (padT + 9 + (k % 2) * 11) + '">' + escapeHtml(v.label) + '</text>';
    });
    (cfg.refLines || []).forEach((r) => {
      svg += '<line class="vc-ref" x1="' + padL + '" x2="' + (W - padR) + '" y1="' + yAt(r.y) + '" y2="' + yAt(r.y) + '"/>' +
        '<text class="vc-ref-label" x="' + (W - padR - 2) + '" y="' + (yAt(r.y) - 4) + '" text-anchor="end">' + escapeHtml(r.label) + '</text>';
    });
    if (cfg.band) {
      // Shaded lo–hi range (e.g. HRV baseline), broken wherever either edge is missing.
      let run = [];
      const flush = () => {
        if (run.length > 1) {
          svg += '<path class="vc-band ' + cfg.band.cls + '" d="M' + run.map((p) => xAt(p.x).toFixed(1) + ',' + yAt(p.hi).toFixed(1)).join(' L') +
            ' L' + run.slice().reverse().map((p) => xAt(p.x).toFixed(1) + ',' + yAt(p.lo).toFixed(1)).join(' L') + ' Z"/>';
        }
        run = [];
      };
      cfg.band.points.forEach((p) => { if (typeof p.lo === 'number' && typeof p.hi === 'number') run.push(p); else flush(); });
      flush();
    }
    cfg.series.forEach((s) => {
      if (s.bars) {
        const n = s.points.length || 1;
        const bw = Math.max(3, Math.min(16, ((W - padL - padR) / n) * 0.62));
        s.points.forEach((p) => {
          if (p.y == null) return;
          const top = yAt(p.y), base = yAt(y0);
          svg += '<rect class="vc-bar ' + (p.cls || s.cls) + '" x="' + (xAt(p.x) - bw / 2).toFixed(1) + '" y="' + top.toFixed(1) +
            '" width="' + bw.toFixed(1) + '" height="' + Math.max(0, base - top).toFixed(1) + '" rx="1.5"/>';
        });
        return;
      }
      let d = '', pen = false, areaD = '', segStart = null, prevX = null;
      const closeArea = () => {
        if (s.area && segStart !== null) areaD += ' L' + prevX.toFixed(1) + ',' + yAt(y0) + ' L' + segStart.toFixed(1) + ',' + yAt(y0) + ' Z';
        segStart = null;
      };
      s.points.forEach((p) => {
        if (p.y === null || p.y === undefined) { pen = false; closeArea(); return; }
        const x = xAt(p.x), y = yAt(p.y);
        d += (pen ? ' L' : ' M') + x.toFixed(1) + ',' + y.toFixed(1);
        if (s.area) areaD += (segStart === null ? ' M' : ' L') + x.toFixed(1) + ',' + y.toFixed(1);
        if (segStart === null) segStart = x;
        prevX = x; pen = true;
      });
      closeArea();
      if (s.area && areaD) svg += '<path class="vc-area ' + s.cls + '" d="' + areaD + '"/>';
      if (!s.noLine) svg += '<path class="vc-line ' + s.cls + (s.dashed ? ' dashed' : '') + '" d="' + d + '"/>';
      if (s.dots) s.points.forEach((p) => { if (p.y != null) svg += '<circle class="vc-dot ' + s.cls + '" cx="' + xAt(p.x) + '" cy="' + yAt(p.y) + '" r="' + (s.dotR || 3) + '"/>'; });
      if (s.lastDot) {
        const lp = s.points.filter((p) => p.y != null).pop();
        if (lp) svg += '<circle class="mc-dot-last" cx="' + xAt(lp.x) + '" cy="' + yAt(lp.y) + '" r="4"/>';
      }
    });
    svg += '<line class="vc-guide" x1="0" x2="0" y1="' + padT + '" y2="' + (H - padB) + '" visibility="hidden"/>' +
      '<circle class="vc-focus" r="4.5" cx="0" cy="0" visibility="hidden"/></svg>';
    plot.innerHTML = svg + '<div class="mc-tip vc-tip" style="display:none;"></div>';

    // Hover/tap: nearest point (by x) on the series the tooltip describes.
    const svgEl = plot.querySelector('svg'), tip = plot.querySelector('.vc-tip');
    const guide = svgEl.querySelector('.vc-guide'), focus = svgEl.querySelector('.vc-focus');
    const ser = cfg.series[cfg.tipSeries || 0];
    const idx = ser.points.map((p, i) => (p.y != null ? i : -1)).filter((i) => i >= 0);
    const hide = () => { tip.style.display = 'none'; guide.setAttribute('visibility', 'hidden'); focus.setAttribute('visibility', 'hidden'); };
    const show = (e) => {
      if (!idx.length) return;
      const rect = svgEl.getBoundingClientRect();
      const scale = rect.width / W || 1;
      const mx = (e.clientX - rect.left) / scale;
      let best = idx[0], bestDist = Infinity;
      idx.forEach((i) => { const dist = Math.abs(xAt(ser.points[i].x) - mx); if (dist < bestDist) { bestDist = dist; best = i; } });
      const px = xAt(ser.points[best].x), py = yAt(ser.points[best].y);
      guide.setAttribute('x1', px); guide.setAttribute('x2', px); guide.setAttribute('visibility', 'visible');
      focus.setAttribute('cx', px); focus.setAttribute('cy', py); focus.setAttribute('class', 'vc-focus ' + (ser.points[best].cls || ser.cls)); focus.setAttribute('visibility', ser.bars ? 'hidden' : 'visible');
      tip.innerHTML = cfg.tip(best);
      tip.style.display = 'block';
      let left = px * scale + 12;
      if (left + tip.offsetWidth > plot.clientWidth) left = px * scale - tip.offsetWidth - 12;
      tip.style.left = Math.max(0, left) + 'px';
      tip.style.top = Math.max(0, py * scale - tip.offsetHeight - 10) + 'px';
    };
    svgEl.addEventListener('pointermove', show);
    svgEl.addEventListener('pointerdown', show);
    svgEl.addEventListener('pointerleave', hide);
  }

  function renderVitals(vitals) {
    vitals = vitals || {};
    renderFactors(vitals.factors);
    renderRecovery(vitals.recovery || null);

    const fa = vitals.fitness_age;
    setText('fitnessAgeVal', fa ? (fa.fitness_age + ' yr') : '—');
    setText('fitnessAgeDetail', fa ? ('vs chrono. ' + fmtOrDash(fa.chronological_age) + ' · achievable ' + fmtOrDash(fa.achievable_age)) : 'No data');

    const rhr = vitals.resting_hr;
    setText('rhrVal', rhr ? (fmtOrDash(rhr.fresh) + ' bpm') : '—');
    setText('rhrDetail', rhr ? ('7d avg ' + fmtOrDash(rhr.week_avg)) : 'No data');

    const hrv = vitals.hrv;
    setText('hrvVal', hrv && hrv.last_night != null ? hrv.last_night + ' ms' : '—');
    setText('hrvDetail', hrv ? [hrv.week_avg != null ? '7d avg ' + hrv.week_avg : null, hrv.status].filter(Boolean).join(' · ') : 'No data');

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
  function renderLastWaypoint(activity) {
    if (!activity) {
      setText('lastActivityTitle', 'No recent activity');
      setText('lastActivityWindow', '');
      setText('lastActivityBody', 'Garmin has no activities in range.');
      return;
    }
    setText('lastActivityTitle', activity.name + (activity.distance !== '—' ? (' — ' + activity.distance) : ''));
    setText('lastActivityWindow', [activity.date, activity.duration, activity.avg_hr !== '—' ? ('avg HR ' + activity.avg_hr) : null]
      .filter(Boolean).join(' · '));
    const body = activity.zone_breakdown && activity.zone_breakdown.length
      ? activity.zone_breakdown.join(', ') + '.'
      : (activity.description || 'No further detail from Garmin for this activity.');
    setText('lastActivityBody', body);
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

  // ------------------------------------------------------- weekly summary --
  function fmtDur(sec) {
    const mins = Math.round((sec || 0) / 60);
    if (mins < 60) return mins + 'm';
    return Math.floor(mins / 60) + 'h ' + pad2(mins % 60) + 'm';
  }
  function fmtKm(m) { return ((m || 0) / 1000).toFixed(m >= 100000 ? 0 : 1) + ' km'; }

  function renderWeeklySummary(w) {
    const panel = $('weekPanel');
    if (!w) { panel.style.display = 'none'; return; }
    panel.style.display = 'block';
    const range = fmtShortDate(w.week_start) + ' – ' + fmtShortDate(w.week_end);
    setText('weekTag', range + ' · day ' + w.days_in + ' of 7 · vs your ' + w.avg_weeks + '-week average');

    const t = w.totals;
    const tile = (label, cur, fmt, cls) => {
      const pct = cur.pct_of_avg;
      return '<div class="stat-tile"><div class="v ' + (cls || '') + '">' + fmt(cur.value) + '</div>' +
        '<div class="l">' + label + '</div>' +
        '<div class="d">avg week ' + fmt(cur.avg) + (pct != null ? ' · ' + pct + '%' : '') + '</div></div>';
    };
    $('weekTotals').innerHTML =
      tile('Time', t.seconds, fmtDur, 'gold') +
      tile('Distance', t.meters, fmtKm) +
      tile('Training load', t.load, (v) => String(Math.round(v))) +
      tile('Sessions', t.sessions, (v) => (Math.round(v * 10) / 10).toString());

    // One row per sport: bar = this week's time, tick = an average week.
    $('weekSports').innerHTML = (w.sports || []).map((sp) => {
      const cur = sp.seconds.value, avg = sp.seconds.avg;
      const max = Math.max(cur, avg, 1) * 1.08;
      const icon = ACTIVITY_ICONS[sp.key] || ACTIVITY_ICONS.other;
      const dist = sp.meters.value > 0 ? ' · ' + fmtKm(sp.meters.value) : '';
      return '<div class="week-sport">' +
        '<span class="week-sport-name">' + icon + escapeHtml(sp.label) + '</span>' +
        '<span class="week-track" title="Average week: ' + fmtDur(avg) + '">' +
          '<span class="week-fill ' + sp.key + '" style="width:' + (100 * cur / max).toFixed(1) + '%"></span>' +
          (avg > 0 ? '<span class="week-avg" style="left:' + (100 * avg / max).toFixed(1) + '%"></span>' : '') +
        '</span>' +
        '<span class="week-sport-val"><b>' + (cur > 0 ? fmtDur(cur) : '—') + '</b>' + dist +
          '<span class="week-sport-avg">avg ' + fmtDur(avg) + '</span></span>' +
      '</div>';
    }).join('') + '<div class="week-legend"><span><i class="week-legend-bar"></i>this week</span><span><i class="week-legend-avg"></i>average week</span></div>';

    const plan = w.plan;
    const planEl = $('weekPlan');
    if (plan && (plan.due || plan.remaining || plan.skipped)) {
      const bits = [plan.due ? '<b>' + plan.done + ' of ' + plan.due + '</b> sessions due so far done' : 'Nothing due yet'];
      if (plan.missed) bits.push(plan.missed + ' missed');
      if (plan.skipped) bits.push(plan.skipped + ' skipped');
      if (plan.remaining) bits.push(plan.remaining + ' still to come');
      if (plan.extra) bits.push(plan.extra + ' extra');
      planEl.innerHTML = '<span class="week-plan-label">Plan</span>' + bits.join(' · ');
      planEl.style.display = 'block';
    } else {
      planEl.style.display = 'none';
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

      const STATUS_LABELS = { completed: 'Done', missed: 'Missed', skipped: 'Skipped', unplanned: 'Extra', rest: 'Done', upcoming: 'Upcoming' };
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

  // ---------------------------------------------------------- view tabs --
  // Two views: the dashboard itself and Races. The tab bar stays hidden
  // until /api/races reports something upcoming, so for anyone without a
  // race planned the page is exactly the single dashboard it always was.
  const VIEWS = ['dashboard', 'races'];
  let currentView = 'dashboard';
  let racesAvailable = false;
  // The fitness chart measures its container on render, and a hidden view
  // measures 0 wide — so while Races is showing, dashboard polls park the
  // chart data here and it's drawn the moment the dashboard is shown again.
  let pendingRidge = null;

  function viewFromHash() {
    return location.hash === '#races' ? 'races' : 'dashboard';
  }

  function setView(view, opts) {
    opts = opts || {};
    if (!VIEWS.includes(view) || (view === 'races' && !racesAvailable)) view = 'dashboard';
    currentView = view;
    VIEWS.forEach((v) => {
      const panel = $(v === 'races' ? 'viewRaces' : 'viewDashboard');
      const tab = $(v === 'races' ? 'tabRaces' : 'tabDashboard');
      panel.hidden = v !== view;
      tab.setAttribute('aria-selected', v === view ? 'true' : 'false');
      tab.tabIndex = v === view ? 0 : -1;
    });
    if (opts.updateHash !== false) {
      const hash = view === 'races' ? '#races' : '';
      if (location.hash !== hash) history.replaceState(null, '', location.pathname + location.search + hash);
    }
    if (view === 'dashboard' && pendingRidge) {
      const r = pendingRidge;
      pendingRidge = null;
      renderRidgeChart(r);
    }
    if (view === 'races') startRaceClock(); else stopRaceClock();
  }

  function bindViewTabs() {
    const tabs = [$('tabDashboard'), $('tabRaces')];
    tabs.forEach((tab, i) => {
      tab.addEventListener('click', () => setView(tab.dataset.view));
      tab.addEventListener('keydown', (e) => {
        if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
        e.preventDefault();
        const next = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
        next.focus();
        setView(next.dataset.view);
      });
    });
    window.addEventListener('hashchange', () => setView(viewFromHash(), { updateHash: false }));
  }

  // -------------------------------------------------------------- races --
  let lastRaces = null;
  let featuredRace = null;
  let raceClockTimer = null;

  function parseLocalDate(iso) { return new Date(iso + 'T00:00:00'); }

  function daysUntil(iso) {
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    return Math.round((parseLocalDate(iso) - today) / 86400000);
  }

  function fmtRaceDate(r) {
    const d = parseLocalDate(r.date);
    if (r.date_approx) return '~' + d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
    return d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
  }

  function fmtWeeksDays(days) {
    if (days < 7) return days === 1 ? '1 day' : days + ' days';
    const w = Math.floor(days / 7), d = days % 7;
    return w + (w === 1 ? ' week' : ' weeks') + (d ? ', ' + d + (d === 1 ? ' day' : ' days') : '');
  }

  function raceIconKey(r) {
    const s = (r.sport || '').toLowerCase();
    if (s.includes('tri') || s.includes('multi')) return 'multisport';
    if (s.includes('run')) return 'running';
    if (s.includes('cycl') || s.includes('bik') || s.includes('ride')) return 'cycling';
    if (s.includes('swim')) return 'swimming';
    if (s.includes('ski')) return 'skiing';
    if (s.includes('hik')) return 'hiking';
    return 'other';
  }

  function safeUrl(u) {
    try {
      const url = new URL(u);
      return url.protocol === 'https:' || url.protocol === 'http:' ? url.href : null;
    } catch (e) { return null; }
  }

  function raceChips(r) {
    const chips = [];
    if (r.kind === 'goal') chips.push('<span class="race-chip goal">Goal</span>');
    if (r.status === 'tentative') chips.push('<span class="race-chip tentative">Not confirmed</span>');
    else if (r.kind !== 'goal') chips.push('<span class="race-chip confirmed">Confirmed</span>');
    if (r.date_approx) chips.push('<span class="race-chip tbc">Date TBC</span>');
    if (r.has_plan) chips.push('<button type="button" class="race-chip plan" data-goto-plan>Training plan ›</button>');
    return chips.join('');
  }

  function raceFacts(r) {
    const goal = r.goal || r.plan_goal;
    const facts = [
      ['Distance', r.distance],
      ['Climb', r.elevation],
      ['Goal', goal],
      ['Start', r.start_time],
    ].filter((f) => f[1]);
    if (!facts.length) return '';
    return '<dl class="race-facts">' + facts.map((f) =>
      '<div><dt>' + f[0] + '</dt><dd>' + escapeHtml(f[1]) + '</dd></div>').join('') + '</dl>';
  }

  function raceWhere(r) {
    return [fmtRaceDate(r), r.location]
      .filter(Boolean).map(escapeHtml).join(' · ');
  }

  function raceExtras(r) {
    let html = '';
    if (r.notes) html += '<p class="race-notes">' + escapeHtml(r.notes) + '</p>';
    const url = r.url && safeUrl(r.url);
    if (url) html += '<a class="race-link" href="' + escapeHtml(url) + '" target="_blank" rel="noopener noreferrer">Race website ↗</a>';
    return html;
  }

  function renderRaceFeature(r) {
    const el = $('raceFeature');
    const days = daysUntil(r.date);
    const icon = ACTIVITY_ICONS[raceIconKey(r)] || ACTIVITY_ICONS.other;
    const label = r.kind === 'goal' ? 'Next goal' : 'Next race';
    el.className = 'race-feature' + (r.status === 'tentative' ? ' tentative' : '');
    el.innerHTML =
      '<div class="race-feature-main">' +
        '<div class="race-eyebrow">' + icon + '<span>' + label + '</span>' + raceChips(r) + '</div>' +
        '<h2 class="race-feature-name">' + escapeHtml(r.name || 'Unnamed race') + '</h2>' +
        '<div class="race-when">' + raceWhere(r) + '</div>' +
        raceFacts(r) +
        raceExtras(r) +
        renderCourseBlock(r, { profile: true }) +
      '</div>' +
      '<div class="race-countdown" aria-live="off">' +
        '<div class="race-countdown-figure">' + (days === 0 ? 'Today' : (r.date_approx ? '~' : '') + days) + '</div>' +
        '<div class="race-countdown-label">' + (days === 0 ? (r.kind === 'goal' ? 'goal day' : 'race day') : (days === 1 ? 'day to go' : 'days to go')) + '</div>' +
        (days >= 7 ? '<div class="race-countdown-weeks">' + fmtWeeksDays(days) + '</div>' : '') +
        '<div class="race-clock" id="raceClock"></div>' +
      '</div>';
  }

  function renderRaceTimeline(upcoming) {
    const panel = $('raceSeasonPanel');
    if (upcoming.length < 2) { panel.hidden = true; return; }
    panel.hidden = false;
    const now = new Date();
    const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const lastDate = parseLocalDate(upcoming[upcoming.length - 1].date);
    // Pad the far end a little so the last marker isn't jammed on the edge.
    const spanDays = Math.max(30, Math.round((lastDate - start) / 86400000) * 1.04);
    const end = new Date(start.getTime() + spanDays * 86400000);
    const pct = (d) => Math.max(0, Math.min(100, ((d - start) / (end - start)) * 100));

    let ticks = '';
    const m = new Date(start.getFullYear(), start.getMonth() + 1, 1);
    let i = 0;
    while (m < end) {
      if (pct(m) < 7) { m.setMonth(m.getMonth() + 1); i++; continue; } // would collide with "Today"
      const isJan = m.getMonth() === 0;
      const lbl = isJan ? m.getFullYear() : m.toLocaleDateString('en-GB', { month: 'short' });
      ticks += '<span class="race-tick' + (i % 2 ? ' odd' : '') + (isJan ? ' year' : '') + '" style="left:' + pct(m).toFixed(2) + '%"><i></i>' + lbl + '</span>';
      m.setMonth(m.getMonth() + 1);
      i++;
    }

    // Labels go into lanes (above, below, then further above/below) so two
    // races a few weeks apart never print on top of each other — on a phone
    // that's most race pairs. Widths are estimated in % of the track, since
    // the view may be hidden (0 wide) when this runs.
    const mobile = window.innerWidth <= CAL_MOBILE_BREAKPOINT;
    const trackPx = Math.max(240, Math.min(window.innerWidth, 1180) - (mobile ? 90 : 100));
    const labelPct = ((mobile ? 122 : 176) / trackPx) * 100;
    const LANES = ['above', 'below', 'above2', 'below2'];
    const laneEnds = LANES.map(() => -Infinity);
    let maxAbove = 0, maxBelow = 0;
    const markers = upcoming.map((r) => {
      const p = pct(parseLocalDate(r.date));
      const align = p > 82 ? 'right' : (p < 18 ? 'left' : 'center');
      const from = align === 'left' ? p : align === 'right' ? p - labelPct : p - labelPct / 2;
      let lane = LANES.findIndex((_, li) => laneEnds[li] + 1.5 <= from);
      if (lane === -1) lane = laneEnds.indexOf(Math.min(...laneEnds)); // all full: least-crowded lane
      laneEnds[lane] = from + labelPct;
      const above = lane % 2 === 0, depth = lane < 2 ? 1 : 2;
      if (above) maxAbove = Math.max(maxAbove, depth); else maxBelow = Math.max(maxBelow, depth);
      const cls = ['race-marker', above ? 'above' : 'below', 'depth-' + depth, 'align-' + align];
      if (r.status === 'tentative') cls.push('tentative');
      if (r.kind === 'goal') cls.push('goal');
      return '<div class="' + cls.join(' ') + '" style="left:' + p.toFixed(2) + '%">' +
        '<span class="race-marker-dot"></span>' +
        '<span class="race-marker-label"><b>' + escapeHtml(r.name || 'Race') + '</b>' +
        escapeHtml(fmtRaceDate(r)) + '</span></div>';
    }).join('');
    const marginTop = [22, 76, 130][maxAbove], marginBottom = [34, 88, 142][maxBelow];

    $('raceTimeline').innerHTML =
      '<div class="race-track" style="margin:' + marginTop + 'px 0 ' + marginBottom + 'px"><span class="race-today" style="left:0%"><i></i>Today</span>' + ticks + markers + '</div>';
    const lastDays = daysUntil(upcoming[upcoming.length - 1].date);
    setText('raceSeasonTag', upcoming.length + ' ahead · next ' + Math.round(lastDays / 7) + ' weeks');
  }

  function renderRaceCard(r) {
    const days = daysUntil(r.date);
    const icon = ACTIVITY_ICONS[raceIconKey(r)] || ACTIVITY_ICONS.other;
    const cls = ['race-card'];
    if (r.status === 'tentative') cls.push('tentative');
    if (r.kind === 'goal') cls.push('goal');
    return '<article class="' + cls.join(' ') + '">' +
      '<div class="race-card-top">' +
        '<div class="race-card-days"><span>' + (r.date_approx ? '~' : '') + days + '</span>' + (days === 1 ? 'day' : 'days') + '</div>' +
        '<div class="race-card-chips">' + raceChips(r) + '</div>' +
      '</div>' +
      '<h3 class="race-card-name">' + icon + '<span>' + escapeHtml(r.name || 'Unnamed race') + '</span></h3>' +
      '<div class="race-when">' + raceWhere(r) + '</div>' +
      raceFacts(r) +
      raceExtras(r) +
      renderCourseBlock(r, { profile: false }) +
    '</article>';
  }

  let lastRacesSig = null;

  function renderRaces(data) {
    // The 60s poll would otherwise rebuild the tab every minute — wiping a
    // target time mid-typing, an open file picker or splits table.
    const sig = JSON.stringify(data);
    if (sig === lastRacesSig) return;
    const active = document.activeElement;
    if (lastRacesSig && active && $('viewRaces').contains(active) && /^(INPUT|SELECT)$/.test(active.tagName)) return;
    lastRacesSig = sig;
    lastRaces = data;
    const upcoming = (data && data.upcoming) || [];
    const past = (data && data.past) || [];
    racesAvailable = upcoming.length > 0;
    $('viewTabs').hidden = !racesAvailable;
    // Named and counted for what's in it: "Races" + number of upcoming races
    // as soon as there's one; "Goals" + number of goals while there are only
    // goals (e.g. a VO2 max target). Goals don't add to a "Races" count.
    const raceCount = upcoming.filter((r) => r.kind !== 'goal').length;
    setText('racesTabLabel', raceCount ? 'Races' : 'Goals');
    setText('racesTabCount', racesAvailable ? String(raceCount || upcoming.length) : '');
    if (!racesAvailable) {
      featuredRace = null;
      if (currentView === 'races') setView('dashboard');
      return;
    }

    // Feature the nearest actual race; a dated goal only takes the spot if
    // there's no race ahead at all.
    featuredRace = upcoming.find((r) => r.kind !== 'goal') || upcoming[0];
    renderRaceFeature(featuredRace);
    renderRaceTimeline(upcoming);

    const rest = upcoming.filter((r) => r !== featuredRace);
    $('raceListPanel').hidden = rest.length === 0;
    $('raceGrid').innerHTML = rest.map(renderRaceCard).join('');

    $('racePastPanel').hidden = past.length === 0;
    $('racePast').innerHTML = past.map((r) =>
      '<li><span class="race-past-date">' + escapeHtml(fmtRaceDate(r)) + '</span>' +
      '<span class="race-past-name">' + escapeHtml(r.name || 'Race') + '</span>' +
      '<span class="race-past-ago">' + fmtAgoDays(-daysUntil(r.date)) + '</span></li>').join('');

    hydrateCourses();
    tickRaceClock();
  }

  function fmtAgoDays(days) {
    if (days < 14) return days + ' days ago';
    if (days < 60) return Math.round(days / 7) + ' weeks ago';
    return Math.round(days / 30.4) + ' months ago';
  }

  // Live d/h/m/s to the featured race's start (its start_time if known,
  // else midnight). Skipped when the date itself is only an estimate —
  // ticking seconds toward a guessed day would be false precision.
  function tickRaceClock() {
    const el = $('raceClock');
    if (!el || !featuredRace) return;
    if (featuredRace.date_approx) { el.textContent = ''; return; }
    const target = parseLocalDate(featuredRace.date);
    const m = /^(\d{1,2}):(\d{2})/.exec(featuredRace.start_time || '');
    if (m) target.setHours(+m[1], +m[2], 0, 0);
    let s = Math.floor((target - new Date()) / 1000);
    if (s <= 0) { el.textContent = featuredRace.kind === 'goal' ? '' : 'Go!'; return; }
    const d = Math.floor(s / 86400); s -= d * 86400;
    const h = Math.floor(s / 3600); s -= h * 3600;
    const mi = Math.floor(s / 60); s -= mi * 60;
    el.textContent = d + 'd ' + pad2(h) + 'h ' + pad2(mi) + 'm ' + pad2(s) + 's' + (m ? ' to the gun' : '');
  }

  function startRaceClock() {
    tickRaceClock();
    if (!raceClockTimer) raceClockTimer = setInterval(tickRaceClock, 1000);
  }

  function stopRaceClock() {
    if (raceClockTimer) { clearInterval(raceClockTimer); raceClockTimer = null; }
  }

  // ------------------------------------------------------- race courses --
  // A race can carry a course (GPX upload or a saved Garmin course): an
  // elevation profile plus sections, with the target time spread across
  // sections by ITRA effort-km (km + 1 per 100 m climbed).
  const FUEL_CARBS_G_PER_H = 60, FUEL_FLUID_ML_PER_H = 500, FUEL_MIN_MINUTES = 75;

  function raceByDate(d) {
    return ((lastRaces && lastRaces.upcoming) || []).find((r) => r.date === d) || null;
  }

  // "4:30:00", "53:00", "Sub 4:30", "0:53:00" -> seconds. Two-part times are
  // ambiguous (h:mm or mm:ss); pick the reading that gives a believable
  // pace for this course.
  function parseTargetSeconds(text, course) {
    const m = /(\d{1,3})(?::(\d{2}))(?::(\d{2}))?/.exec(text || '');
    if (!m) return null;
    if (m[3] !== undefined) return (+m[1]) * 3600 + (+m[2]) * 60 + (+m[3]);
    const hm = (+m[1]) * 3600 + (+m[2]) * 60, ms = (+m[1]) * 60 + (+m[2]);
    const ekm = (course && course.effort_km) || 0;
    if (!ekm) return ms >= 600 ? ms : hm;
    const plausible = (secs) => secs / ekm >= 150 && secs / ekm <= 1200; // 2:30–20:00 per effort-km
    if (plausible(ms) && !plausible(hm)) return ms;
    if (plausible(hm) && !plausible(ms)) return hm;
    return ms / ekm >= 150 ? ms : hm;
  }

  function fmtHms(sec) {
    sec = Math.round(sec);
    const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s2 = sec % 60;
    return h ? h + ':' + pad2(m) + ':' + pad2(s2) : m + ':' + pad2(s2);
  }

  // The elevation profile is drawn only for the featured (next) race — the
  // smaller cards keep the course stats and splits without a cramped chart.
  // Goals aren't events, so they get no course section at all.
  function renderCourseBlock(r, opts) {
    if (r.kind === 'goal') return '';
    const c = r.course;
    if (!c) {
      return '<div class="race-course empty" data-date="' + escapeHtml(r.date) + '">' +
        '<span class="rc-label">Course</span>' +
        '<label class="rc-btn">Upload GPX<input type="file" accept=".gpx,application/gpx+xml" data-gpx hidden></label>' +
        '<button type="button" class="rc-btn" data-garmin-pick>Use a Garmin course</button>' +
        '<select class="rc-select" data-garmin-select hidden></select>' +
        '<span class="rc-status" data-status></span></div>';
    }
    const stats = [(c.distance_m / 1000).toFixed(1) + ' km', '↑' + c.gain_m.toLocaleString('en-GB') + ' m',
      '↓' + c.loss_m.toLocaleString('en-GB') + ' m', c.max_ele != null ? 'high point ' + Math.round(c.max_ele).toLocaleString('en-GB') + ' m' : null]
      .filter(Boolean).join(' · ');
    const targetText = r.target_time || '';
    const fromGoal = !targetText && parseTargetSeconds(r.goal || r.plan_goal, c);
    return '<div class="race-course" data-date="' + escapeHtml(r.date) + '">' +
      '<div class="rc-head"><span class="rc-label">Course</span><span class="rc-stats">' + escapeHtml(stats) + '</span>' +
        '<button type="button" class="rc-remove" data-course-remove title="Remove this course">Remove</button></div>' +
      (c.name ? '<div class="rc-name">' + escapeHtml(c.name) + '</div>' : '') +
      (opts && opts.profile ? '<div class="vc-plot rc-profile" data-profile></div>' : '') +
      '<details class="rc-splits"><summary>Splits &amp; fuel</summary>' +
        '<div class="rc-target"><label>Target time <input type="text" inputmode="numeric" placeholder="h:mm:ss" data-target value="' +
          escapeHtml(targetText || (fromGoal ? fmtHms(fromGoal) : '')) + '"></label>' +
          '<button type="button" class="rc-btn" data-target-save>Save</button><span class="rc-status" data-status></span></div>' +
        '<div class="log-scroll"><table class="log rc-table"><thead><tr><th>To</th><th>Section</th><th>↑ / ↓</th><th>Time</th><th>Pace</th><th>Arrive</th><th data-fuel-col>Fuel</th></tr></thead>' +
        '<tbody data-splits></tbody></table></div>' +
        '<p class="rc-note" data-note></p>' +
      '</details></div>';
  }

  function hydrateCourses() {
    document.querySelectorAll('#viewRaces .race-course[data-date]').forEach((el) => {
      const r = raceByDate(el.dataset.date);
      if (!r || !r.course) return;
      drawCourseProfile(el.querySelector('[data-profile]'), r.course);
      fillSplits(el, r);
    });
  }

  function drawCourseProfile(plot, c) {
    if (!plot || !c.profile || c.profile.length < 2) {
      if (plot) plot.innerHTML = '<div class="mc-empty">This course has no elevation data.</div>';
      return;
    }
    const km = c.distance_m / 1000;
    const step = [1, 2, 5, 10, 20, 25, 50].find((s) => km / s <= 8) || 50;
    const ticks = [];
    for (let k = 0; k <= km + 1e-9; k += step) ticks.push({ x: k * 1000, label: k + ' km' });
    const lo = Math.floor((c.min_ele - 20) / 50) * 50, hi = Math.ceil((c.max_ele + 20) / 50) * 50;
    drawVitalChart(plot, {
      height: plot.closest('.race-card') ? 120 : 150,
      xDomain: [0, c.distance_m], yDomain: [lo, hi], yTicks: [lo, hi], yFmt: (v) => v + 'm',
      xTicks: ticks,
      vLines: (c.checkpoints || []).map((cp) => ({ x: cp.dist, label: cp.name })),
      series: [{ cls: 'teal', area: true, points: c.profile.map((p) => ({ x: p[0], y: p[1] })) }],
      tip: (i) => '<b>' + Math.round(c.profile[i][1]).toLocaleString('en-GB') + ' m</b><span class="mc-tip-date">km ' + (c.profile[i][0] / 1000).toFixed(1) + '</span>',
    });
  }

  function fillSplits(el, r, override) {
    const c = r.course;
    const tbody = el.querySelector('[data-splits]');
    if (!tbody) return;
    const input = el.querySelector('[data-target]');
    const text = override !== undefined ? override : (input ? input.value : r.target_time);
    const total = parseTargetSeconds(text, c);
    const fuel = total && total / 60 >= FUEL_MIN_MINUTES;
    el.querySelectorAll('[data-fuel-col]').forEach((th) => { th.hidden = !fuel; });
    let start = null;
    const m = /^(\d{1,2}):(\d{2})/.exec(r.start_time || '');
    if (m && !r.date_approx) { start = parseLocalDate(r.date); start.setHours(+m[1], +m[2], 0, 0); }
    let cum = 0;
    tbody.innerHTML = c.sections.map((sec) => {
      const km = (sec.to_m - sec.from_m) / 1000;
      const t = total ? total * sec.effort_km / c.effort_km : null;
      if (t) cum += t;
      const arrive = t && start ? new Date(start.getTime() + cum * 1000).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }) : (t ? fmtHms(cum) : '—');
      const fuelCell = fuel ? '<td data-label="Fuel">' + Math.round(FUEL_CARBS_G_PER_H * t / 3600 / 5) * 5 + ' g · ' +
        Math.round(FUEL_FLUID_ML_PER_H * t / 3600 / 50) * 50 + ' ml</td>' : '';
      return '<tr><td data-label="To"><b>' + escapeHtml(sec.to) + '</b><span class="rc-at">km ' + (sec.to_m / 1000).toFixed(1) + '</span></td>' +
        '<td data-label="Section">' + km.toFixed(1) + ' km</td>' +
        '<td data-label="↑ / ↓">↑' + sec.gain + ' ↓' + sec.loss + '</td>' +
        '<td data-label="Time">' + (t ? fmtHms(t) : '—') + '</td>' +
        '<td data-label="Pace">' + (t ? fmtHms(t / km) + '/km' : '—') + '</td>' +
        '<td data-label="Arrive">' + arrive + '</td>' + fuelCell + '</tr>';
    }).join('');
    const note = el.querySelector('[data-note]');
    note.textContent = !total
      ? 'Enter a target time to split it across the sections.'
      : 'Time is spread by effort: each km counts once, plus 1 km per 100 m climbed (ITRA). ' +
        (start ? 'Arrive = clock time from the ' + r.start_time + ' start. ' : 'Arrive = elapsed time. ') +
        (fuel ? 'Fuel at ' + FUEL_CARBS_G_PER_H + ' g carbs + ' + FUEL_FLUID_ML_PER_H + ' ml per hour — adjust to what you’ve practised.' : '');
  }

  function bindCourseControls() {
    const view = $('viewRaces');
    const status = (el, msg, bad) => {
      const s2 = el.querySelector('[data-status]');
      if (s2) { s2.textContent = msg; s2.classList.toggle('bad', !!bad); }
    };
    const attach = (el, body) => {
      status(el, 'Loading course…');
      return fetch('/api/races/' + encodeURIComponent(el.dataset.date) + '/course', body)
        .then((r) => r.json().then((j) => ({ ok: r.ok, j })))
        .then(({ ok, j }) => {
          if (!ok || j.error) { status(el, j.error || 'Couldn’t attach that course.', true); return; }
          lastRacesSig = null;
          return loadRaces();
        })
        .catch((err) => status(el, 'Upload failed: ' + err, true));
    };
    view.addEventListener('change', (e) => {
      const el = e.target.closest('.race-course');
      if (!el) return;
      if (e.target.matches('[data-gpx]') && e.target.files[0]) {
        const fd = new FormData();
        fd.append('gpx', e.target.files[0]);
        attach(el, { method: 'POST', body: fd });
      } else if (e.target.matches('[data-garmin-select]') && e.target.value) {
        attach(el, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ garmin_course_id: +e.target.value }) });
      }
    });
    view.addEventListener('input', (e) => {
      if (!e.target.matches('[data-target]')) return;
      const el = e.target.closest('.race-course');
      const r = raceByDate(el.dataset.date);
      if (r && r.course) fillSplits(el, r, e.target.value);
    });
    view.addEventListener('click', (e) => {
      const el = e.target.closest('.race-course');
      if (!el) return;
      if (e.target.closest('[data-garmin-pick]')) {
        const sel = el.querySelector('[data-garmin-select]');
        status(el, 'Loading your Garmin courses…');
        fetch('/api/garmin-courses').then((r) => r.json()).then((j) => {
          if (j.error || !(j.courses || []).length) { status(el, j.error || 'No saved courses in Garmin Connect.', !!j.error); return; }
          sel.innerHTML = '<option value="">Pick a course…</option>' + j.courses.map((c) =>
            '<option value="' + c.id + '">' + escapeHtml(c.name) + ' — ' + c.km + ' km, ↑' + c.gain + ' m</option>').join('');
          sel.hidden = false;
          e.target.closest('[data-garmin-pick]').hidden = true;
          status(el, '');
          sel.focus();
        }).catch((err) => status(el, 'Couldn’t reach Garmin: ' + err, true));
      } else if (e.target.closest('[data-course-remove]')) {
        if (!confirm('Remove this course from the race?')) return;
        fetch('/api/races/' + encodeURIComponent(el.dataset.date) + '/course', { method: 'DELETE' })
          .then(() => { lastRacesSig = null; return loadRaces(); });
      } else if (e.target.closest('[data-target-save]')) {
        const value = el.querySelector('[data-target]').value.trim();
        fetch('/api/races/' + encodeURIComponent(el.dataset.date) + '/target', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ target_time: value }),
        }).then((r) => r.json()).then((j) => {
          if (j.error) { status(el, j.error, true); return; }
          status(el, 'Saved');
          el.querySelector('[data-target]').blur();
          lastRacesSig = null;
          setTimeout(loadRaces, 600);
        });
      }
    });
  }

  let raceResizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(raceResizeTimer);
    raceResizeTimer = setTimeout(() => {
      if (!lastRaces) return;
      renderRaceTimeline(lastRaces.upcoming || []);
      document.querySelectorAll('#viewRaces .race-course[data-date]').forEach((el) => {
        const r = raceByDate(el.dataset.date);
        if (r && r.course) drawCourseProfile(el.querySelector('[data-profile]'), r.course);
      });
    }, 150);
  });

  function loadRaces() {
    return fetch('/api/races')
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!data) return;
        renderRaces(data);
        // First load: honour a #races deep link now that we know the tab exists.
        if (viewFromHash() === 'races' && currentView !== 'races') setView('races', { updateHash: false });
      })
      .catch(() => { /* Races are an extra — a failed fetch just leaves the tab as it was */ });
  }

  function bindRacePlanLinks() {
    bindCourseControls();
    $('viewRaces').addEventListener('click', (e) => {
      if (!e.target.closest('[data-goto-plan]')) return;
      setView('dashboard');
      const panel = $('racePlanPanel');
      if (panel && panel.style.display !== 'none') panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }

  function render(data) {
    renderError(data.error || null, data.error_type);
    $('liveBadge').textContent = data.error ? 'Error' : 'Live · Garmin Connect';
    applyGenderDefaultTheme(data.training_load);
    renderHero(data);
    if (currentView === 'dashboard') renderRidgeChart(data.ridge_chart);
    else pendingRidge = data.ridge_chart;
    renderTrainingLoad(data.training_load);
    renderAerobicTiles(data.aerobic);
    renderVitals(data.vitals);
    renderLastWaypoint(data.last_activity);
    renderNextWaypoint(data.next_waypoint, data.coach_note);
    renderWeeklySummary(data.weekly_summary);
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
    bindVitalBoxes();

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

    bindViewTabs();
    bindRacePlanLinks();
    loadRaces();

    $('refreshBtn').addEventListener('click', function () { loadDashboard(true); loadRaces(); });
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

    setInterval(function () { loadDashboard(false); loadRaces(); }, AUTO_REFRESH_MS);
    setInterval(function () { renderRecovery(); }, 30 * 1000);

    // Catch up right away if the tab was backgrounded/asleep past a tick.
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible') { loadDashboard(false); loadRaces(); }
    });
  });
})();
