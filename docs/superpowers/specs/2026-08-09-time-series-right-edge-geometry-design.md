# Time-series right-edge geometry design

## Scope

Fix the shared `TimeSeriesPanel` geometry defect in codexmeter and kimimeter.
The rightmost bar must remain inside the plot and must never cover the
cumulative-axis labels. Preserve the server-bucket floor introduced for wide
dashboard ranges; do not return to finer display bins or hide the defect with
SVG clipping.

This change does not redesign the dashboard, alter colors or typography, or
change API aggregation. It corrects the temporal interval represented by the
existing bars and makes rendering and interaction use the same bounds.

## Measured failure

The dashboard API timestamps aggregate rows at bucket centers. Both browser
adapters currently define the chart range as the first center through the last
center plus 1 ms. With a display bin equal to the server bucket, the final bin
therefore starts effectively at the plot's right boundary and retains a full
bar width.

Daedalus DOM measurements on the deployed dashboards established the failure:

- codexmeter rightmost bars overflow the plot by 50.6–52.8 px;
- kimimeter rightmost bars overflow the plot by 68.7–70.4 px;
- those bars intersect right-axis labels including `0`, `20M`, and `$0.00`.

## Correct temporal model

For backend aggregates, each event timestamp is a bucket center, not a point
event and not a range edge. Given server bucket width `bucket_s`, the visible
coverage is:

```text
start = first_center - bucket_s / 2
end   = last_center  + bucket_s / 2
```

Offline drag-and-drop events have no server bucket metadata and retain their
existing point-event range behavior.

Every visual bin is a half-open interval inside that coverage:

```text
bin.start = previous bin end
bin.end   = min(bin.start + display_bin_width, range.end)
```

The final bin may be partial when the data coverage is not an exact multiple of
the selected display width. That partial interval is real and must render at
its proportional width rather than as a full bar or a clipped full bar.

## Rendering and interaction

Bar geometry derives from each bounded bin:

- `x` is the scaled `bin.start`;
- available width is the distance from scaled `bin.start` to scaled `bin.end`;
- the bar consumes 90% of that available width, preserving the current gap;
- no artificial minimum width may push a bar beyond the plot boundary.

Cumulative points use the same bounded `bin.end`, so the final point cannot
escape the plot. Hover hit-testing is limited to the plot rectangle and selects
the bin whose bounded interval contains the pointer timestamp. Tooltip ranges
show the bounded start and end used by the rendered bar.

The plot gutter remains reserved exclusively for right-axis labels and its
rotated cumulative caption. No clipping mask is the primary fix: clipping would
conceal invalid geometry and could make the final aggregate invisible.

## Components

- `src/app.jsx`: convert server bucket centers into their full aggregate
  coverage while retaining the existing offline fallback.
- `src/dashboard-charts.jsx`: build bounded bins and derive bar, cumulative,
  and hover geometry from those intervals.
- Tests: execute the shipped JavaScript through Node and assert the public
  adapter/range behavior plus the chart's bounded interval geometry.

The two repositories receive equivalent changes adapted only where their
existing token readers differ.

## Validation

The regression suite must prove:

1. three server rows six hours apart with `bucket_s=21600` yield a range that
   begins three hours before the first center and ends three hours after the
   last center;
2. a display width that does not divide the range produces a bounded partial
   final bin;
3. every rendered bar ends at or before the plot boundary;
4. cumulative points and hover selection use the same bounded intervals;
5. offline range behavior is unchanged.

After deployment, repeat the Daedalus element measurement on every
`TimeSeriesPanel` in both apps. Success is `overflow <= 0` and an empty list of
right-label intersections for every panel. Run each repository's full tests,
lint, type, and JavaScript checks, then monitor all remote pipelines.

## Repository workflow

Open one issue in each repository containing the measured reproduction. Each
implementation commit closes its repository's issue and includes:

```text
Co-authored-by: GPT-5.6 Sol <noreply@openai.com>
```

Unrelated defects are reported separately and are not repaired in this work.
