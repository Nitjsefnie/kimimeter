# Active Dashboard Issues Design

## Scope

Resolve the two issues open on `Nitjsefnie/kimimeter` as of 2026-08-09:

- #21: backend-aggregated rows render into frontend bins that can be finer
  than the server bucket, producing spikes and misleading axis labels.
- #22: the no-backend preview emits Claude model names and prices instead of
  the Kimi models and rates used by kimimeter.

Unrelated defects discovered during implementation will be filed as new
GitHub issues and will not be fixed in these commits.

## #21: Respect Backend Aggregation

Extract the dashboard's display-bin selection into a pure JavaScript helper.
It will retain the existing nice-bin algorithm based on the visible data span.
When the dashboard payload provides a positive `bucket_s`, the selected display
bin will be clamped to at least that server bucket width. Offline synthetic and
drag-drop data do not provide this metadata and will retain the current picker.

The dashboard will pass the helper's result to every shared `TimeSeriesPanel`,
including token, cost, and churn panels. This keeps bar widths, cumulative
steps, tooltips, and the `per <width>` axis caption aligned with the actual
aggregation grain without changing backend queries, cache keys, or rollups.

Node-backed pytest coverage will execute the real helper extracted from
`src/app.jsx`. It will cover a narrow data span under 24-hour, 7-day, and
30-day server ranges, as well as the unchanged no-metadata behavior. A source
assertion will pin the Dashboard wiring to the helper.

## #22: Use Canonical Preview Models and Pricing

Replace the preview's Claude-only pricing table and model assignments with the
three canonical models kimimeter reports: `kimi-k2-6`, `kimi-k2-7-code`, and
`kimi-k3`. Keep the existing zero-cost `<synthetic>` marker because downstream
panels intentionally discard it where model identity is meaningful.

The generator will calculate each real-model event's cost through the
already-loaded `window.rateForModel` function from `src/parser.js`, while the
`<synthetic>` marker remains explicitly zero-cost. Rates therefore remain owned
by the frontend mirror of `backend/pricing.py`; the preview will not add another
pricing copy or expose a new global pricing catalog.

Node-backed pytest coverage will run the real parser pricing code and synthetic
generator together. It will assert that only canonical Kimi model names (plus
the marker) are generated, all canonical models appear in the deterministic
dataset, Claude names are absent, and every event cost matches the shared rate
resolver.

## Delivery and Verification

The design document is a standalone commit. Each issue is then implemented in
its own focused commit with `Closes #21` or `Closes #22`. Every commit includes
`Co-authored-by: GPT-5.6 Sol <noreply@openai.com>`.

Verification consists of the focused tests first, the complete pytest suite,
frontend ESLint, and the repository's Python lint and type gates. The completed
commits will be pushed to `origin/master`; GitHub will close the corresponding
issues from the closing trailers.
