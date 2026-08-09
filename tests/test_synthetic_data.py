"""Behavioral coverage for the no-backend dashboard preview (#22 and #23).

The preview must emit the same canonical models kimimeter ingests and price
them through parser.js's shared resolver at timestamps where the backend would
assign those models. The deterministic generator is run through Node so these
tests cover the shipped JavaScript rather than a Python reimplementation.
"""
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend import pricing
from backend.parse import K3_CUTOFF_EPOCH, MODEL_CUTOFF_EPOCH, _model_for

ROOT = Path(__file__).resolve().parents[1]
PARSER_JS = ROOT / "src" / "parser.js"
SYNTHETIC_JS = ROOT / "src" / "synthetic-data.js"
CANONICAL_MODELS = {"kimi-k2-6", "kimi-k2-7-code", "kimi-k3"}

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def _node_probe():
    script = f"""
      global.window = {{}};
      const fs = require('fs');
      eval(fs.readFileSync({str(PARSER_JS)!r}, 'utf8'));
      eval(fs.readFileSync({str(SYNTHETIC_JS)!r}, 'utf8'));

      const data = window.generateSyntheticData();
      const generated = [...new Set(
        data.events.map((event) => event.model)
      )].sort();
      const representatives = {{}};
      for (const model of ['kimi-k2-6', 'kimi-k2-7-code', 'kimi-k3']) {{
        const event = data.events.find((candidate) => candidate.model === model);
        if (event) representatives[model] = event;
      }}
      const markerZeroCost = data.events
        .filter((event) => event.model === '<synthetic>')
        .every((event) => event.cost_usd === 0);

      window.rateForModel = () => ({{ fresh: 7, output: 11, read: 13 }});
      const injected = window.generateSyntheticData();
      const usesInjectedRates = injected.events.every((event) => {{
        if (event.model === '<synthetic>') return event.cost_usd === 0;
        const expectedCost = (
          event.input_tokens * 7
          + event.output_tokens * 11
          + event.cache_read * 13
        ) / 1e6;
        return Math.abs(event.cost_usd - expectedCost) < 1e-12;
      }});

      console.log(JSON.stringify({{
        generated, representatives, markerZeroCost, usesInjectedRates,
        range: data.range,
        assignments: data.events.map((event) => ({{
          ts: event.ts,
          model: event.model,
        }})),
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


def test_preview_generates_every_canonical_kimi_model(js):
    assert set(js["generated"]) == CANONICAL_MODELS | {"<synthetic>"}


def test_preview_costs_match_backend_pricing(js):
    assert set(js["representatives"]) == CANONICAL_MODELS
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
    assert js["markerZeroCost"]


def test_preview_uses_the_shared_rate_resolver(js):
    assert js["usesInjectedRates"]


def test_preview_models_follow_backend_timestamp_cutoffs(js):
    start_epoch = js["range"]["start"] / 1000
    end_epoch = js["range"]["end"] / 1000
    assert start_epoch < MODEL_CUTOFF_EPOCH < K3_CUTOFF_EPOCH < end_epoch

    mismatches = []
    for event in js["assignments"]:
        if event["model"] == "<synthetic>":
            continue
        timestamp = datetime.fromtimestamp(
            event["ts"] / 1000, tz=timezone.utc
        )
        expected = _model_for(None, timestamp)
        if event["model"] != expected:
            mismatches.append((timestamp.isoformat(), event["model"], expected))

    assert not mismatches, mismatches[:5]
