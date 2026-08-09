# Synthetic Kimi Models Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the no-backend preview use kimimeter's canonical model names and shared frontend pricing.

**Architecture:** Keep the deterministic preview generator and volume shape intact while replacing Claude assignments with the three canonical Kimi labels. Remove its private pricing table; real-model event cost resolves through `window.rateForModel`, while the existing `<synthetic>` marker stays zero-cost.

**Tech Stack:** Browser JavaScript, shared `src/parser.js` pricing resolver, Node.js probes driven by pytest.

## Global Constraints

- Generated real-model names are exactly `kimi-k2-6`, `kimi-k2-7-code`, and `kimi-k3`.
- `<synthetic>` remains a zero-cost marker and is not treated as a billable model.
- Do not add another pricing table or expose a new global pricing catalog.
- File unrelated defects as GitHub issues; do not fix them in this task.
- The closing commit must include `Closes #22` and `Co-authored-by: GPT-5.6 Sol <noreply@openai.com>`.

---

### Task 1: Test and implement canonical synthetic models

**Files:**
- Create: `tests/test_synthetic_data.py`
- Modify: `src/synthetic-data.js:15-85`

**Interfaces:**
- Consumes: `window.rateForModel(model)`, loaded from `src/parser.js` before `src/synthetic-data.js` by `public/index.html`.
- Produces: unchanged `window.generateSyntheticData() -> {events, limitHits, range}` shape with canonical models and correctly priced `cost_usd`.

- [ ] **Step 1: Write the failing integration tests**

Create a Node-backed pytest probe that sets `global.window = {}`, evaluates
the real `src/parser.js`, evaluates `src/synthetic-data.js`, calls
`window.generateSyntheticData()`, and returns each generated model plus a
representative event for Python to price against `backend.pricing`.

Then replace `window.rateForModel` with a resolver that returns the distinctive
rates `{fresh: 7, output: 11, read: 13}` and regenerate the deterministic data.
Return whether every real-model event has the following cost and every marker
event remains zero-cost:

```javascript
const generated = [...new Set(data.events.map((event) => event.model))].sort();
window.rateForModel = () => ({ fresh: 7, output: 11, read: 13 });
const injected = window.generateSyntheticData();
const usesInjectedRates = injected.events.every((event) => {
  if (event.model === '<synthetic>') return event.cost_usd === 0;
  const expectedCost = (
    event.input_tokens * 7
    + event.output_tokens * 11
    + event.cache_read * 13
  ) / 1e6;
  return Math.abs(event.cost_usd - expectedCost) < 1e-12;
});
```

Assert:

```python
def test_preview_generates_every_canonical_kimi_model(js):
    expected = {"kimi-k2-6", "kimi-k2-7-code", "kimi-k3"}
    assert set(js["generated"]) == expected | {"<synthetic>"}


def test_preview_costs_match_backend_pricing(js):
    for model, event in js["representatives"].items():
        assert event["cost_usd"] == pytest.approx(
            pricing.compute_cost(
                model,
                fresh=event["input_tokens"],
                create=0,
                read=event["cache_read"],
                output=event["output_tokens"],
            )
        )


def test_preview_uses_the_shared_rate_resolver(js):
    assert js["usesInjectedRates"]
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m pytest tests/test_synthetic_data.py -q`

Expected: FAIL because the generated set contains Claude models and replacing
`window.rateForModel` does not affect costs from the private pricing table.

- [ ] **Step 3: Replace model assignments and pricing**

Delete the private `PRICING` object. Replace the three time-phase assignments
with:

```javascript
if (frac < 0.3) {
  model = r < 0.8 ? 'kimi-k2-6' : 'kimi-k2-7-code';
} else if (frac < 0.7) {
  model = r < 0.35 ? 'kimi-k2-6' : r < 0.9 ? 'kimi-k2-7-code' : 'kimi-k3';
} else {
  model = r < 0.55 ? 'kimi-k3' : r < 0.85 ? 'kimi-k2-7-code' : r < 0.92 ? 'kimi-k2-6' : '<synthetic>';
}
```

Replace the cost lookup with:

```javascript
let cost = 0;
if (model !== '<synthetic>') {
  const rates = window.rateForModel(model);
  cost = (
    inputT * rates.fresh + outputT * rates.output + crT * rates.read
  ) / 1e6;
}
```

- [ ] **Step 4: Run focused tests and frontend lint**

Run: `python -m pytest tests/test_synthetic_data.py -q`

Expected: all tests PASS.

Run: `npx --no-install eslint 'src/**/*.js' 'src/**/*.jsx'`

Expected: exit 0.

- [ ] **Step 5: Commit the completed issue**

```bash
git add src/synthetic-data.js tests/test_synthetic_data.py
git commit -m "Use Kimi models and rates in dashboard preview" \
  -m "Closes #22." \
  -m "Co-authored-by: GPT-5.6 Sol <noreply@openai.com>"
```

### Task 2: Verify the combined change before publishing

**Files:**
- Verify only; no planned modifications.

**Interfaces:**
- Consumes: both issue-closing commits.
- Produces: evidence that repository test, lint, and type gates pass together.

- [ ] **Step 1: Run the complete pytest suite**

Run: `python -m pytest tests/ -q --tb=short -ra`

Expected: all available tests PASS; environment-dependent skips are reported.

- [ ] **Step 2: Run repository lint and type gates**

Run: `git ls-files '*.py' | xargs pylint`

Expected: exit 0.

Run: `git ls-files '*.py' | xargs pycodestyle`

Expected: exit 0.

Run: `pyright`

Expected: zero errors.

Run: `npx --no-install eslint 'src/**/*.js' 'src/**/*.jsx'`

Expected: exit 0.

- [ ] **Step 3: Inspect scope and closing trailers**

Run: `git status --short && git log -3 --format=fuller`

Expected: clean worktree; the two implementation commits each contain the
required co-author and matching `Closes` trailer.

- [ ] **Step 4: Push and confirm remote issue state**

Run: `git push origin master`

Expected: push succeeds.

Run: `gh issue list --state open --limit 100 --json number,title,url`

Expected: neither #21 nor #22 remains open after GitHub processes the push.
