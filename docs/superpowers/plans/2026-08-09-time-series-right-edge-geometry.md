# Time-series Right-edge Geometry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep every time-series bar, cumulative point, and hover interval inside the plot while faithfully representing backend aggregate timestamps as bucket centers.

**Architecture:** `backendDashToShape` reconstructs the complete server-bucket coverage from the first and last center timestamps. `TimeSeriesPanel` builds bounded intervals, derives bar rectangles from each interval, and uses those same intervals for cumulative and hover geometry. Pure JavaScript helpers make the temporal contract executable through Node without mocking React or SVG.

**Tech Stack:** React 18 JSX served through in-browser Babel, SVG, Node-based JavaScript probes under pytest, pytest, Daedalus rendered-DOM measurement.

## Global Constraints

- Preserve the display-bin floor at `bucket_s`; never split a backend aggregate across finer visual bins.
- Do not use SVG clipping as the primary fix and do not hide or truncate the final aggregate.
- Offline drag-and-drop point-event range behavior remains unchanged.
- The existing palette, typography, gutters, and 90% bar fill ratio remain unchanged.
- Do not edit or invoke `~/.kimi-code/scripts/parse_wire.py`.
- Every commit includes `Co-authored-by: GPT-5.6 Sol <noreply@openai.com>`.
- The completing implementation commit includes `Closes #24`.
- Unrelated defects become separate issues and are not fixed here.

## File Structure

- Create `tests/test_time_series_geometry.py`: execute the real pure helpers from shipped JSX and assert bucket coverage, bounded bins, bar bounds, and hover selection.
- Modify `src/app.jsx`: add `backendAggregateRange(events, bucketS)` and use it in `backendDashToShape`.
- Modify `src/dashboard-charts.jsx`: add bounded interval/bar/hit-test helpers and route `TimeSeriesPanel` through them.

---

### Task 1: Reconstruct backend aggregate coverage

**Files:**
- Create: `tests/test_time_series_geometry.py`
- Modify: `src/app.jsx` near `backendDashToShape`

**Interfaces:**
- Consumes: backend event objects with numeric millisecond `ts`; dashboard `bucket_s` in seconds.
- Produces: `backendAggregateRange(events, bucketS) -> {start: number, end: number}`.

- [ ] **Step 1: Write the failing range test**

Create a Node probe that extracts `backendAggregateRange` from `src/app.jsx` and emits:

```javascript
const HOUR = 3600_000;
const centers = [{ts: 3 * HOUR}, {ts: 9 * HOUR}, {ts: 15 * HOUR}];
const covered = backendAggregateRange(centers, 21600);
const fallback = backendAggregateRange(centers, null);
```

Assert literal results:

```python
assert result["covered"] == {"start": 0, "end": 18 * 3_600_000}
assert result["fallback"] == {
    "start": 3 * 3_600_000,
    "end": 15 * 3_600_000 + 1,
}
```

- [ ] **Step 2: Run the test and verify RED**

Run: `pytest -q tests/test_time_series_geometry.py`

Expected: FAIL because `backendAggregateRange` is absent.

- [ ] **Step 3: Implement aggregate coverage**

Add before `backendDashToShape`:

```javascript
function backendAggregateRange(events, bucketS) {
  const first = events[0].ts;
  const last = events[events.length - 1].ts;
  const bucketMs = Number(bucketS) * 1000;
  if (Number.isFinite(bucketMs) && bucketMs > 0) {
    return { start: first - bucketMs / 2, end: last + bucketMs / 2 };
  }
  return { start: first, end: last + 1 };
}
```

Replace the adapter's first-center/last-center range construction with:

```javascript
const range = backendAggregateRange(events, b.bucket_s);
```

and return that `range` object unchanged.

- [ ] **Step 4: Run the focused test and existing bin test**

Run: `pytest -q tests/test_time_series_geometry.py tests/test_dashboard_bin_size.py`

Expected: PASS; the server bucket still floors display bin width.

- [ ] **Step 5: Commit the independently useful adapter correction**

```bash
git add src/app.jsx tests/test_time_series_geometry.py
git commit -m "fix: reconstruct aggregate bucket coverage" \
  -m "Co-authored-by: GPT-5.6 Sol <noreply@openai.com>"
```

### Task 2: Bound rendered and interactive bin geometry

**Files:**
- Modify: `tests/test_time_series_geometry.py`
- Modify: `src/dashboard-charts.jsx` inside and immediately before `TimeSeriesPanel`

**Interfaces:**
- Produces: `boundedTimeIntervals(range, binMs) -> Array<{start, end}>`.
- Produces: `timeBarRect(bin, range, padL, plotW) -> {x, width}`.
- Produces: `timeBinIndexAtX(bins, range, padL, plotW, x) -> number`, returning `-1` outside the plot.
- `TimeSeriesPanel` uses all three helpers for the existing SVG and tooltip behavior.

- [ ] **Step 1: Extend the Node probe with failing geometry assertions**

Extract the three helpers from `src/dashboard-charts.jsx`. Probe a deliberately partial final interval:

```javascript
const range = {start: 0, end: 10};
const bins = boundedTimeIntervals(range, 6);
const rects = bins.map(bin => timeBarRect(bin, range, 60, 400));
const indices = [59, 60, 300, 459, 460, 461]
  .map(x => timeBinIndexAtX(bins, range, 60, 400, x));
```

Assert:

```python
assert result["bins"] == [{"start": 0, "end": 6}, {"start": 6, "end": 10}]
assert result["rects"] == [{"x": 60, "width": 216}, {"x": 300, "width": 144}]
assert all(rect["x"] + rect["width"] <= 460 for rect in result["rects"])
assert result["indices"] == [-1, 0, 1, 1, 1, -1]
```

- [ ] **Step 2: Run the test and verify RED**

Run: `pytest -q tests/test_time_series_geometry.py`

Expected: FAIL because the three chart helpers are absent.

- [ ] **Step 3: Implement bounded interval and bar helpers**

Add pure helpers before `TimeSeriesPanel`:

```javascript
function boundedTimeIntervals(range, binMs) {
  if (!Number.isFinite(range.start) || !Number.isFinite(range.end)
      || range.end <= range.start || !Number.isFinite(binMs) || binMs <= 0) {
    throw new TypeError('Invalid time-series range or bin width');
  }
  const bins = [];
  for (let start = range.start; start < range.end; start += binMs) {
    bins.push({ start, end: Math.min(start + binMs, range.end) });
  }
  return bins;
}

function timeBarRect(bin, range, padL, plotW) {
  const span = range.end - range.start;
  const x = padL + ((bin.start - range.start) / span) * plotW;
  const endX = padL + ((bin.end - range.start) / span) * plotW;
  return { x, width: Math.max(0, (endX - x) * 0.9) };
}

function timeBinIndexAtX(bins, range, padL, plotW, x) {
  if (!bins.length || x < padL || x > padL + plotW) return -1;
  const ts = range.start + ((x - padL) / plotW) * (range.end - range.start);
  return bins.findIndex((bin, index) =>
    ts >= bin.start && (ts < bin.end
      || (index === bins.length - 1 && ts === bin.end)));
}
```

- [ ] **Step 4: Route the panel through the bounded helpers**

Build the existing summed bins by iterating `boundedTimeIntervals(range, binMs)`.
Keep the current value reader (`event[valueKey] || 0`) and event ordering. Use
each bounded `bin.end` for cumulative points and tooltip text. In the SVG bar
loop, replace the global `barW` with:

```javascript
const { x, width } = timeBarRect(b, range, padL, plotW);
```

and render `width={width}`. Restrict hover to `padL <= mx <= w - padR`, obtain
the selected index with `timeBinIndexAtX`, and remove the old extended-gutter
hover band and pitch/rounding calculation.

- [ ] **Step 5: Run focused tests and JavaScript lint**

Run:

```bash
pytest -q tests/test_time_series_geometry.py tests/test_dashboard_bin_size.py
npm install --no-save --no-package-lock --no-audit --no-fund eslint@8.57.1 eslint-plugin-react@7.37.5
npx --no-install eslint 'src/**/*.js' 'src/**/*.jsx'
```

Expected: all tests pass and ESLint exits 0.

- [ ] **Step 6: Commit the completing geometry fix**

```bash
git add src/dashboard-charts.jsx tests/test_time_series_geometry.py
git commit -m "fix: keep time-series bars inside plot bounds" \
  -m "Closes #24" \
  -m "Co-authored-by: GPT-5.6 Sol <noreply@openai.com>"
```

### Task 3: Verify, publish, restart, and measure

**Files:** none beyond Tasks 1–2.

- [ ] **Step 1: Run every local gate**

```bash
pytest -q
git ls-files '*.py' | xargs pylint
git ls-files '*.py' | xargs pycodestyle
pyright
npx --no-install eslint 'src/**/*.js' 'src/**/*.jsx'
git diff --check origin/master..HEAD
```

Expected: the complete existing suite plus the new geometry tests passes; every lint/type command exits 0.

- [ ] **Step 2: Audit every new commit trailer**

Verify every commit after `origin/master` contains the exact co-author trailer
and the completing implementation commit contains `Closes #24`.

- [ ] **Step 3: Fast-forward master and push**

Rebase the feature branch onto current `origin/master`, fast-forward local
`master`, rerun `pytest -q`, and push `master` without force.

- [ ] **Step 4: Restart only the existing kimimeter service**

Resolve the installed unit with:

```bash
systemctl list-unit-files --type=service | rg -i 'kimimeter'
```

Restart the exact matching unit, wait until `systemctl is-active` reports
`active`, then require `curl -fsS https://kimi.nitjsefni.eu/` to succeed.

- [ ] **Step 5: Repeat rendered-DOM measurements**

Through Daedalus, inspect every `svg[data-panel]` containing
`text[data-yr-label]`. Measure the plot boundary from grid-line `x2`, each
rightmost bar's `x + width`, and its intersections with right-axis label
bounding boxes. Require every `overflow <= 0` and every intersection list to
be empty.

- [ ] **Step 6: Monitor remote pipelines and issue closure**

Wait for the push's tests, lint, types, and ESLint workflows. Require all to
complete successfully and confirm issue #24 is closed.
