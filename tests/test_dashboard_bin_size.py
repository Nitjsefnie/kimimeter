"""Behavioral coverage for dashboard display-bin selection (issue #21).

The backend can return pre-aggregated rows whose bucket is wider than the
frontend's data-span picker would choose. These tests execute the real pure
helper from app.jsx through Node and ensure the display grain never splits one
server aggregate across multiple visual bins.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JSX = ROOT / "src" / "app.jsx"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def _node_probe():
    script = f"""
      const fs = require('fs');
      const src = fs.readFileSync({str(APP_JSX)!r}, 'utf8');
      const start = src.indexOf('function dashboardBinMs');
      const end = src.indexOf('function Dashboard');
      if (start < 0 || end < 0 || end <= start) {{
        console.log(JSON.stringify({{ missing: true }}));
        process.exit(0);
      }}
      eval(src.slice(start, end));

      const sixDays = {{ start: 0, end: 6 * 24 * 60 * 60 * 1000 }};
      const ninetyMinutes = {{ start: 0, end: 90 * 60 * 1000 }};
      console.log(JSON.stringify({{
        missing: false,
        sixDaysOffline: dashboardBinMs(sixDays),
        invalidBucket: dashboardBinMs(sixDays, 0),
        sevenDayServer: dashboardBinMs(sixDays, 3600),
        thirtyDayServer: dashboardBinMs(sixDays, 21600),
        shortTwentyFourHourServer: dashboardBinMs(ninetyMinutes, 300),
      }}));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture(scope="module", name="js")
def _js_probe():
    return _node_probe()


def test_data_span_picker_is_unchanged_without_server_metadata(js):
    assert not js["missing"], "dashboardBinMs is missing"
    assert js["sixDaysOffline"] == 60 * 60 * 1000
    assert js["invalidBucket"] == 60 * 60 * 1000


def test_display_bin_never_falls_below_server_bucket(js):
    assert not js["missing"], "dashboardBinMs is missing"
    assert js["sevenDayServer"] == 60 * 60 * 1000
    assert js["thirtyDayServer"] == 6 * 60 * 60 * 1000
    assert js["shortTwentyFourHourServer"] == 5 * 60 * 1000
