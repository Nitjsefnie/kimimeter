"""Behavioral geometry coverage for dashboard aggregate time series."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

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
