# Dashboard Bucket Floor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent dashboard charts from rendering backend aggregates into display bins finer than `bucket_s`.

**Architecture:** Move the existing nice-bin picker into a pure `dashboardBinMs(range, bucketS)` helper in `src/app.jsx`. The helper preserves offline behavior and floors backend-driven bins at the server aggregation width; Dashboard computes it once and continues passing it to every shared time-series panel.

**Tech Stack:** Browser JavaScript/React JSX, Node.js probes driven by pytest.

## Global Constraints

- Do not change backend queries, response shapes, rollups, or cache keys.
- Offline synthetic and drag-drop data without `bucketS` retain the current data-span picker.
- File unrelated defects as GitHub issues; do not fix them in this task.
- The closing commit must include `Closes #21` and `Co-authored-by: GPT-5.6 Sol <noreply@openai.com>`.

---

### Task 1: Test and implement the server bucket floor

**Files:**
- Create: `tests/test_dashboard_bin_size.py`
- Modify: `src/app.jsx:687-752`

**Interfaces:**
- Consumes: `range` with numeric millisecond `start` and `end`; optional positive `bucketS` in seconds from `/api/dashboard`.
- Produces: `dashboardBinMs(range, bucketS) -> number`, a positive display width in milliseconds.

- [ ] **Step 1: Write the failing helper tests**

Create a Node-backed pytest probe that reads `src/app.jsx`, extracts from
`function dashboardBinMs` up to `function Dashboard`, evaluates the helper,
and emits these results:

```python
def test_data_span_picker_is_unchanged_without_server_metadata(js):
    assert js["sixDaysOffline"] == 60 * 60 * 1000
    assert js["invalidBucket"] == 60 * 60 * 1000


def test_display_bin_never_falls_below_server_bucket(js):
    assert js["sevenDayServer"] == 60 * 60 * 1000
    assert js["thirtyDayServer"] == 6 * 60 * 60 * 1000
    assert js["shortTwentyFourHourServer"] == 5 * 60 * 1000
```

The JavaScript probe inputs are:

```javascript
const sixDays = { start: 0, end: 6 * 24 * 60 * 60 * 1000 };
const ninetyMinutes = { start: 0, end: 90 * 60 * 1000 };
const out = {
  sixDaysOffline: dashboardBinMs(sixDays),
  invalidBucket: dashboardBinMs(sixDays, 0),
  sevenDayServer: dashboardBinMs(sixDays, 3600),
  thirtyDayServer: dashboardBinMs(sixDays, 21600),
  shortTwentyFourHourServer: dashboardBinMs(ninetyMinutes, 300),
};
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m pytest tests/test_dashboard_bin_size.py -q`

Expected: FAIL because `function dashboardBinMs` does not exist yet.

- [ ] **Step 3: Implement the pure helper and wire Dashboard**

Insert before `function Dashboard`:

```javascript
function dashboardBinMs(range, bucketS) {
  const span = range.end - range.start;
  const MIN_BINS = 100;
  const MAX_BIN_MS = 24 * 3600 * 1000;
  const niceBins = [
    60_000, 5*60_000, 15*60_000, 30*60_000,
    3600_000, 6*3600_000, 12*3600_000, 24*3600_000,
  ];
  let binMs = niceBins[0];
  for (const b of niceBins) {
    if (b > MAX_BIN_MS) break;
    if (span / b < MIN_BINS) break;
    binMs = b;
  }

  const serverBinMs = Number(bucketS) * 1000;
  if (Number.isFinite(serverBinMs) && serverBinMs > 0) {
    binMs = Math.max(binMs, serverBinMs);
  }
  return binMs;
}
```

Delete the inline picker from Dashboard and replace it with:

```javascript
const binMs = dashboardBinMs(range, bucketS);
```

- [ ] **Step 4: Run focused tests and frontend lint**

Run: `python -m pytest tests/test_dashboard_bin_size.py -q`

Expected: all tests PASS.

Run: `npx --no-install eslint 'src/**/*.js' 'src/**/*.jsx'`

Expected: exit 0.

- [ ] **Step 5: Commit the completed issue**

```bash
git add src/app.jsx tests/test_dashboard_bin_size.py
git commit -m "Fix dashboard bins finer than backend aggregates" \
  -m "Closes #21." \
  -m "Co-authored-by: GPT-5.6 Sol <noreply@openai.com>"
```
