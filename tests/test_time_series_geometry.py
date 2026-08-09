"""Behavioral geometry coverage for dashboard aggregate time series."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHARTS_JSX = ROOT / "src" / "dashboard-charts.jsx"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def _node(script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", script], cwd=ROOT, capture_output=True,
        text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_backend_bucket_centers_expand_to_their_full_coverage():
    result = _node(r"""
      const src = require('fs').readFileSync('src/app.jsx', 'utf8');
      const start = src.indexOf('function backendAggregateRange');
      if (start < 0) throw new Error('backendAggregateRange helper missing');
      const end = src.indexOf('\nfunction backendDashToShape', start);
      eval(src.slice(start, end));
      const hour = 3600000;
      const centers = [{ts: 3 * hour}, {ts: 9 * hour}, {ts: 15 * hour}];
      console.log(JSON.stringify({
        covered: backendAggregateRange(centers, 21600),
        fallback: backendAggregateRange(centers, null),
      }));
    """)
    assert result["covered"] == {"start": 0, "end": 18 * 3_600_000}
    assert result["fallback"] == {
        "start": 3 * 3_600_000,
        "end": 15 * 3_600_000 + 1,
    }


def test_backend_adapter_and_offline_path_keep_distinct_range_contracts():
    result = _node(r"""
      global.window = {
        shortModelName: model => model,
        dashboardCol: {},
        rateForModel: () => ({fresh: 1, create: 1, read: 1, out: 1}),
        asUsageRecord: meta => meta.type === 'usage' ? meta : null,
      };
      const src = require('fs').readFileSync('src/app.jsx', 'utf8');
      const optionalStart = src.indexOf('function optionalFiniteNumber');
      const adapterStart = optionalStart >= 0
        ? optionalStart : src.indexOf('function backendAggregateRange');
      const adapterEnd = src.indexOf('\nfunction TopBar', adapterStart);
      const adapter = eval(src.slice(adapterStart, adapterEnd)
        + '\n;({backendDashToShape})');
      const hour = 3600000;
      const hourly = [3, 9, 15].map((value, index) => ({
        hour: new Date(value * hour).toISOString(),
        model: 'model', input_tokens: index + 1, output_tokens: 0,
        cache_read_tokens: 0, cost_usd: 0,
      }));
      const backend = adapter.backendDashToShape({
        bucket_s: 21600, hourly, token_types: [],
      });

      const offlineStart = src.indexOf('function txToDashData');
      const offlineEnd = src.indexOf('\nfunction App', offlineStart);
      eval(src.slice(offlineStart, offlineEnd));
      const usage = tsMs => ({
        type: 'usage', sessionId: 'offline', line: tsMs,
        tsMs, ctx: 1, model: 'model', input: 1, create: 0,
        read: 0, output: 0,
      });
      const offline = txToDashData({
        meta: [usage(3 * hour), usage(15 * hour)], events: [],
      });
      console.log(JSON.stringify({
        backend: backend.range,
        offline: offline.range,
      }));
    """)
    assert result["backend"] == {"start": 0, "end": 18 * 3_600_000}
    offline_pad = int(12 * 3_600_000 * 0.02)
    assert result["offline"] == {
        "start": 3 * 3_600_000 - offline_pad,
        "end": 15 * 3_600_000 + offline_pad,
    }


@pytest.fixture(scope="module", name="bounded_geometry")
def _bounded_geometry_fixture() -> dict:
    return _node(r"""
      const src = require('fs').readFileSync(
        'src/dashboard-charts.jsx', 'utf8');
      const start = src.indexOf('function boundedTimeIntervals');
      if (start < 0) {
        console.log(JSON.stringify({missing: true}));
        process.exit(0);
      }
      const end = src.indexOf('\nfunction TimeSeriesPanel', start);
      eval(src.slice(start, end));
      const range = {start: 0, end: 10};
      const built = buildTimeSeriesData(
        [{ts: 1, value: 2}, {ts: 8, value: 3}],
        range, 6, event => event.value);
      const bins = built.bins;
      const rects = bins.map(bin => timeBarRect(bin, range, 60, 400));
      const cumulativeX = built.cumPts.map(
        point => timeX(point.ts, range, 60, 400));
      const indices = [59, 60, 300, 459, 460, 461]
        .map(x => timeBinIndexAtX(bins, range, 60, 400, x));
      console.log(JSON.stringify({
        missing: false, bins, rects, indices,
        cumulative: built.cumPts, cumulativeX, total: built.total,
      }));
    """)


def test_final_interval_is_capped_to_the_range(bounded_geometry):
    assert not bounded_geometry["missing"], "bounded geometry helpers missing"
    assert bounded_geometry["bins"] == [
        {"start": 0, "end": 6, "sum": 2, "count": 1},
        {"start": 6, "end": 10, "sum": 3, "count": 1},
    ]


def test_bar_rectangles_never_enter_the_axis_gutter(bounded_geometry):
    assert not bounded_geometry["missing"], "bounded geometry helpers missing"
    assert bounded_geometry["rects"] == [
        {"x": 60, "width": 216}, {"x": 300, "width": 144},
    ]
    assert all(
        rect["x"] + rect["width"] <= 460
        for rect in bounded_geometry["rects"]
    )


def test_hover_selection_uses_the_bounded_intervals(bounded_geometry):
    assert not bounded_geometry["missing"], "bounded geometry helpers missing"
    assert bounded_geometry["indices"] == [-1, 0, 1, 1, 1, -1]


def test_cumulative_series_ends_at_the_bounded_range_edge(bounded_geometry):
    assert bounded_geometry["cumulative"] == [
        {"ts": 0, "v": 0, "binIdx": -1},
        {"ts": 6, "v": 2, "binIdx": 0},
        {"ts": 10, "v": 5, "binIdx": 1},
    ]
    assert bounded_geometry["total"] == 5
    assert bounded_geometry["cumulativeX"] == [60, 300, 460]


def test_panel_wires_bounded_geometry_to_stable_dom_measurement_hooks():
    src = CHARTS_JSX.read_text()
    panel = src[src.index("function TimeSeriesPanel"):]
    assert "const { bins, cumPts, total } = buildTimeSeriesData(" in panel
    assert "events, range, binMs, event =>" in panel
    assert "timeBarRect(b, range, padL, plotW)" in panel
    assert "const xScale = ts => timeX(ts, range, padL, plotW)" in panel
    assert "timeBinIndexAtX(bins, range, padL, plotW, mx)" in panel
    assert 'data-plot-boundary=""' in panel
    assert 'data-time-bar=""' in panel
    assert 'data-cumulative-line=""' in panel
