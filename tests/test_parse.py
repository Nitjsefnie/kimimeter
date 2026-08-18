"""parse.py — per-file extraction from Kimi Code wire.jsonl.

Each parse_file call returns:
  - records: one entry per StatusUpdate with token_usage.
  - ctx_turns: per-turn (idx, ts, line, input, output, delta) array.
  - tool_uses: matched against ToolResult.is_error.

Cross-file uuid dedup is a query-time concern, not here.
Cost is precomputed per record using pricing.MODEL_RATES.
"""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend import parse, pricing
from backend.parse import _canonical_model, _line_count, _model_for


FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parser"


def _read(name):
    return (FIX / name).read_bytes()


def _status_update_at(epoch: float) -> bytes:
    """One classic-wire StatusUpdate line stamped at `epoch` (UTC)."""
    ts = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    return (
        '{"timestamp": "%s", "message": {"type": "StatusUpdate", "payload": '
        '{"message_id": "m1", "token_usage": {"input_other": 400, '
        '"input_cache_creation": 100, "input_cache_read": 200, '
        '"output": 50}}}}\n' % ts
    ).encode()


def test_single_turn_emits_one_record_one_turn():
    out = parse.parse_file("sessions/projA/sess-A/wire.jsonl", _read("single_turn.jsonl"))
    assert len(out["records"]) == 1
    r = out["records"][0]
    assert r["uuid"] == "a1"
    assert r["model"] == "kimi-k2-7-code"
    assert r["fresh_tokens"] == 100
    assert r["cache_creation_tokens"] == 0
    assert r["cache_read_tokens"] == 0
    assert r["output_tokens"] == 200
    assert r["ctx_input"] == 100
    expected_cost = pricing.compute_cost(
        "kimi-k2-7-code",
        fresh=100, create=0, read=0, output=200,
    )
    assert r["cost_usd"] == pytest.approx(expected_cost, rel=1e-9)
    assert r["text_chars"] == len("hello world")
    assert r["reply_latency_s"] == pytest.approx(3.0, rel=1e-9)
    assert len(out["ctx_turns"]) == 1
    t = out["ctx_turns"][0]
    assert t["idx"] == 1
    assert t["input"] == 100
    assert t["output"] == 200
    assert t["delta"] == 100   # first turn delta == input


def test_multiple_status_updates_within_turn_keep_last_as_representative():
    """Kimi wire.jsonl may emit several StatusUpdates inside one turn.
    parse.py keeps every record, but ctx_turns uses the LAST StatusUpdate
    in the turn as the turn's representative.
    """
    out = parse.parse_file(
        "sessions/projA/sess-B/wire.jsonl", _read("streaming_merge.jsonl")
    )
    assert len(out["records"]) == 2
    assert out["records"][0]["output_tokens"] == 50
    assert out["records"][1]["output_tokens"] == 200
    assert out["records"][1]["fresh_tokens"] == 100
    assert len(out["ctx_turns"]) == 1
    assert out["ctx_turns"][0]["input"] == 100
    assert out["ctx_turns"][0]["output"] == 200


def test_cache_creation_and_read_are_flat_rate_billed():
    """Kimi wire format has no TTL split; cache_creation is billed at a
    flat rate and cache_read is billed at the model's read rate.
    """
    out = parse.parse_file(
        "sessions/projA/sess-C/wire.jsonl", _read("unsplit_cache.jsonl")
    )
    r = out["records"][0]
    assert r["fresh_tokens"] == 400
    assert r["cache_creation_tokens"] == 100
    assert r["cache_read_tokens"] == 200
    assert r["output_tokens"] == 50
    expected_cost = pricing.compute_cost(
        "kimi-k2-7-code",
        fresh=400, create=100, read=200, output=50,
    )
    assert r["cost_usd"] == pytest.approx(expected_cost, rel=1e-9)


def test_pre_cutoff_timestamp_labels_k2_6():
    """Sessions whose first event is strictly before MODEL_CUTOFF_EPOCH are
    labelled kimi-k2-6; newer sessions are kimi-k2-7-code.
    """
    out = parse.parse_file(
        "sessions/projA/sess-D/wire.jsonl", _read("ttl_split.jsonl")
    )
    r = out["records"][0]
    assert r["model"] == "kimi-k2-6"
    expected_cost = pricing.compute_cost(
        "kimi-k2-6",
        fresh=1000, create=0, read=1000, output=1000,
    )
    assert r["cost_usd"] == pytest.approx(expected_cost, rel=1e-9)


def test_at_k3_cutoff_labels_k3():
    """The K3 cutoff is inclusive: a first event exactly at K3_CUTOFF_EPOCH
    is kimi-k3, mirroring the k2-6 boundary's strictly-before rule.
    """
    out = parse.parse_file(
        "sessions/projA/sess-k3/wire.jsonl",
        _status_update_at(parse.K3_CUTOFF_EPOCH),
    )
    assert out["records"][0]["model"] == "kimi-k3"


def test_one_second_before_k3_cutoff_still_labels_k2_7_code():
    out = parse.parse_file(
        "sessions/projA/sess-k27/wire.jsonl",
        _status_update_at(parse.K3_CUTOFF_EPOCH - 1),
    )
    assert out["records"][0]["model"] == "kimi-k2-7-code"


def test_post_k3_cutoff_cost_uses_k3_rates():
    out = parse.parse_file(
        "sessions/projA/sess-k3b/wire.jsonl",
        _status_update_at(parse.K3_CUTOFF_EPOCH + 3600),
    )
    r = out["records"][0]
    assert r["model"] == "kimi-k3"
    expected_cost = pricing.compute_cost(
        "kimi-k3",
        fresh=400, create=100, read=200, output=50,
    )
    assert r["cost_usd"] == pytest.approx(expected_cost, rel=1e-9)


def test_canonical_model_maps_k3_provider_id():
    assert _canonical_model("kimi-code/k3") == "kimi-k3"


def test_canonical_model_returns_none_for_ambiguous_and_unknown():
    # kimi-for-coding spans both k2.6 and k2.7-code: the wire cannot resolve it.
    assert _canonical_model("kimi-code/kimi-for-coding") is None
    assert _canonical_model("some/unknown-model") is None
    assert _canonical_model(None) is None
    assert _canonical_model("") is None


def test_model_for_k3_wire_string_beats_an_earlier_date():
    """A wire that says k3 is k3, even before K3_CUTOFF_EPOCH. Real k3 records
    predate the constant by ~20 minutes.
    """
    ts = datetime.fromtimestamp(parse.K3_CUTOFF_EPOCH - 3600, tz=timezone.utc)
    assert _model_for("kimi-code/k3", ts) == "kimi-k3"


def test_model_for_kimi_for_coding_never_becomes_k3():
    """The reported bug: k2.7-code is still selectable after the K3 cutoff.
    A wire that says kimi-for-coding is not k3, whatever the date.
    """
    ts = datetime.fromtimestamp(parse.K3_CUTOFF_EPOCH + 86400, tz=timezone.utc)
    assert _model_for("kimi-code/kimi-for-coding", ts) == "kimi-k2-7-code"


def test_model_for_unrecognized_wire_id_is_never_promoted_to_k3():
    """An unrecognized id that is PRESENT is not the "no model string" case
    that justifies the date ladder's k3 rung. Bill it conservatively: a wrong
    k2-7-code undercount beats a wrong k3 overcount at ~3x.
    """
    ts = datetime.fromtimestamp(parse.K3_CUTOFF_EPOCH + 86400, tz=timezone.utc)
    assert _model_for("kimi-code/k4-future", ts) == "kimi-k2-7-code"
    assert _model_for("garbage", ts) == "kimi-k2-7-code"


def test_model_for_kimi_for_coding_uses_model_cutoff_for_the_k2_era():
    before = datetime.fromtimestamp(parse.MODEL_CUTOFF_EPOCH - 1, tz=timezone.utc)
    at = datetime.fromtimestamp(parse.MODEL_CUTOFF_EPOCH, tz=timezone.utc)
    assert _model_for("kimi-code/kimi-for-coding", before) == "kimi-k2-6"
    assert _model_for("kimi-code/kimi-for-coding", at) == "kimi-k2-7-code"


def test_model_for_without_wire_string_uses_the_full_date_ladder():
    """Legacy transcripts carry no model string; dates are all we have."""
    k26 = datetime.fromtimestamp(parse.MODEL_CUTOFF_EPOCH - 1, tz=timezone.utc)
    k27 = datetime.fromtimestamp(parse.K3_CUTOFF_EPOCH - 1, tz=timezone.utc)
    k3 = datetime.fromtimestamp(parse.K3_CUTOFF_EPOCH, tz=timezone.utc)
    assert _model_for(None, k26) == "kimi-k2-6"
    assert _model_for(None, k27) == "kimi-k2-7-code"
    assert _model_for(None, k3) == "kimi-k3"


def test_model_for_without_timestamp_falls_back_to_k2_7_code():
    assert _model_for(None, None) == "kimi-k2-7-code"
    assert _model_for("kimi-code/kimi-for-coding", None) == "kimi-k2-7-code"


def test_k3_cutoff_matches_earliest_observed_k3_record():
    """1784213155 == 2026-07-16 14:45:55 UTC, the earliest k3 usage.record in
    the corpus. The prior value (1784214394) postdated real k3 usage.
    """
    assert parse.K3_CUTOFF_EPOCH == 1784213155


def test_missing_timestamp_still_labels_k2_7_code():
    """A session with no usable timestamp must NOT drift to the newest label.
    It predates the K3 cutoff by construction (it is already ingested), so it
    stays kimi-k2-7-code rather than being repriced at K3's much higher rates.
    """
    blob = (
        b'{"message": {"type": "StatusUpdate", "payload": {"message_id": "n1", '
        b'"token_usage": {"input_other": 400, "input_cache_creation": 100, '
        b'"input_cache_read": 200, "output": 50}}}}\n'
    )
    out = parse.parse_file("sessions/projA/sess-nots/wire.jsonl", blob)
    assert out["records"][0]["model"] == "kimi-k2-7-code"


def test_kimi_code_post_k3_cutoff_kimi_for_coding_stays_k2_7_code():
    """A wire that says kimi-for-coding is NOT k3, whatever the date.

    This test previously asserted the opposite — that the date promoted it to
    k3 — which was the bug: k2.7-code remains selectable after the K3 cutoff,
    so a date cannot override a wire string that names a different model.
    """
    ms = int((parse.K3_CUTOFF_EPOCH + 10) * 1000)
    blob = (
        b'{"type":"metadata","protocol_version":"1.4","created_at":%d}\n'
        b'{"type":"turn.prompt","time":%d,"input":[{"type":"text","text":"Hi"}],'
        b'"origin":{"kind":"user"}}\n'
        b'{"type":"usage.record","time":%d,"model":"kimi-code/kimi-for-coding",'
        b'"usage":{"inputOther":1000,"output":200,"inputCacheRead":100,'
        b'"inputCacheCreation":50}}\n'
    ) % (ms, ms + 1000, ms + 2000)
    out = parse.parse_file("sessions/projKC/sess-k3/wire.jsonl", blob)
    r = out["records"][0]
    assert r["model"] == "kimi-k2-7-code"
    expected_cost = pricing.compute_cost(
        "kimi-k2-7-code",
        fresh=1000, create=50, read=100, output=200,
    )
    assert r["cost_usd"] == pytest.approx(expected_cost, rel=1e-9)


def test_ctx_turns_match_canonical_shape():
    out = parse.parse_file(
        "sessions/projA/sess-A/wire.jsonl", _read("single_turn.jsonl")
    )
    t = out["ctx_turns"][0]
    assert set(t.keys()) == {"idx", "ts", "line", "input", "output", "delta"}


def test_record_without_message_id_has_null_uuid():
    blob = b"""{"timestamp":"2026-06-14T12:00:00Z","message":{"type":"StatusUpdate","payload":{"token_usage":{"input_other":10,"input_cache_creation":0,"input_cache_read":0,"output":5}}}}
"""
    out = parse.parse_file("sessions/projX/sess-X/wire.jsonl", blob)
    assert len(out["records"]) == 1
    assert out["records"][0]["uuid"] is None


def test_two_records_without_message_id_both_kept():
    """Kimi parse.py does no per-file merge/dedup, so two StatusUpdates
    always produce two records even without a message_id.
    """
    blob = b"""{"timestamp":"2026-06-14T12:00:01Z","message":{"type":"StatusUpdate","payload":{"token_usage":{"input_other":10,"input_cache_creation":0,"input_cache_read":0,"output":5}}}}
{"timestamp":"2026-06-14T12:00:02Z","message":{"type":"StatusUpdate","payload":{"token_usage":{"input_other":10,"input_cache_creation":0,"input_cache_read":0,"output":5}}}}
"""
    out = parse.parse_file("sessions/projX/sess-X/wire.jsonl", blob)
    assert len(out["records"]) == 2
    assert out["records"][0]["uuid"] is None
    assert out["records"][1]["uuid"] is None


def test_tool_use_matched_to_error_result():
    out = parse.parse_file(
        "sessions/projErr/sess-err/wire.jsonl", _read("tool_error.jsonl")
    )
    assert len(out["tool_uses"]) == 1
    tu = out["tool_uses"][0]
    assert tu["tool_name"] == "Bash"
    assert tu["is_error"] is True


def test_tool_use_matched_to_success_result():
    out = parse.parse_file(
        "sessions/projOk/sess-ok/wire.jsonl", _read("tool_success.jsonl")
    )
    assert len(out["tool_uses"]) == 1
    assert out["tool_uses"][0]["is_error"] is False


def test_tool_use_unmatched_stays_null():
    out = parse.parse_file(
        "sessions/projPending/sess-pending/wire.jsonl", _read("tool_unmatched.jsonl")
    )
    assert len(out["tool_uses"]) == 1
    assert out["tool_uses"][0]["is_error"] is None


def test_kimi_code_usage_record_drives_record_and_turn():
    """kimi-code usage.record is the primary cost/turn signal."""
    out = parse.parse_file(
        "sessions/projKC/sess-kc/wire.jsonl", _read("kimi_code.jsonl")
    )
    assert len(out["records"]) == 1
    r = out["records"][0]
    assert r["uuid"] == "sessions/projKC/sess-kc/wire.jsonl:5"
    assert r["model"] == "kimi-k2-7-code"
    assert r["fresh_tokens"] == 1000
    assert r["cache_creation_tokens"] == 50
    assert r["cache_read_tokens"] == 100
    assert r["output_tokens"] == 200
    assert r["ctx_input"] == 1150
    assert r["text_chars"] == len("hello back")
    assert r["reply_latency_s"] == pytest.approx(4.0, rel=1e-9)
    expected_cost = pricing.compute_cost(
        "kimi-k2-7-code",
        fresh=1000, create=50, read=100, output=200,
    )
    assert r["cost_usd"] == pytest.approx(expected_cost, rel=1e-9)

    assert len(out["ctx_turns"]) == 1
    t = out["ctx_turns"][0]
    assert t["input"] == 1150
    assert t["output"] == 200
    assert t["delta"] == 1150


def test_kimi_code_ambiguous_wire_id_falls_through_to_the_date():
    """kimi-code usage.record embeds the raw provider id and parse.py honors
    it — but "kimi-for-coding" spans both k2 generations, so it cannot settle
    the model alone and the date decides between them. Fixture is stamped
    2026-07-02, i.e. after MODEL_CUTOFF, so it lands on kimi-k2-7-code.
    """
    out = parse.parse_file(
        "sessions/projKC/sess-kc/wire.jsonl", _read("kimi_code_raw_model.jsonl")
    )
    assert len(out["records"]) == 1
    r = out["records"][0]
    assert r["model"] == "kimi-k2-7-code"
    expected_cost = pricing.compute_cost(
        "kimi-k2-7-code",
        fresh=1000, create=50, read=100, output=200,
    )
    assert r["cost_usd"] == pytest.approx(expected_cost, rel=1e-9)


def test_kimi_code_pre_cutoff_ambiguous_wire_id_resolves_to_k2_6():
    """"kimi-for-coding" cannot settle the model alone, so a record stamped
    before MODEL_CUTOFF_EPOCH resolves to kimi-k2-6 via the date rung.
    """
    # metadata.created_at of 1780000000000 ms is well before MODEL_CUTOFF_EPOCH.
    blob = (
        b'{"type":"metadata","protocol_version":"1.4","created_at":1780000000000}\n'
        b'{"type":"turn.prompt","time":1780000001000,"input":[{"type":"text","text":"Hello"}],"origin":{"kind":"user"}}\n'
        b'{"type":"usage.record","time":1780000002000,"model":"kimi-code/kimi-for-coding","usage":{"inputOther":1000,"output":200,"inputCacheRead":100,"inputCacheCreation":50}}\n'
    )
    out = parse.parse_file("sessions/projKC/sess-old/wire.jsonl", blob)
    assert len(out["records"]) == 1
    r = out["records"][0]
    assert r["model"] == "kimi-k2-6"
    expected_cost = pricing.compute_cost(
        "kimi-k2-6",
        fresh=1000, create=50, read=100, output=200,
    )
    assert r["cost_usd"] == pytest.approx(expected_cost, rel=1e-9)


def test_kimi_code_tool_call_result_pairing():
    out = parse.parse_file(
        "sessions/projKC/sess-kc/wire.jsonl", _read("kimi_code.jsonl")
    )
    assert len(out["tool_uses"]) == 1
    tu = out["tool_uses"][0]
    assert tu["tool_name"] == "Bash"
    assert tu["is_error"] is False


def test_kimi_code_detection_does_not_misclassify_legacy():
    """A legacy fixture must still be parsed as legacy."""
    out = parse.parse_file("sessions/projA/sess-A/wire.jsonl", _read("single_turn.jsonl"))
    assert len(out["records"]) == 1
    assert out["records"][0]["uuid"] == "a1"


def test_turn_begin_drives_turn_boundaries():
    """In Kimi wire format turns are bounded by TurnBegin/TurnEnd.
    Build a blob: turn 1 has two StatusUpdates, turn 2 has one.
    Expect 2 ctx_turns; each turn's representative is its LAST StatusUpdate.
    """
    blob = b"""{"timestamp":"2026-06-14T12:00:00Z","message":{"type":"TurnBegin","payload":{}}}
{"timestamp":"2026-06-14T12:00:01Z","message":{"type":"StatusUpdate","payload":{"message_id":"a1","token_usage":{"input_other":50,"input_cache_creation":0,"input_cache_read":0,"output":1}}}}
{"timestamp":"2026-06-14T12:00:02Z","message":{"type":"StatusUpdate","payload":{"message_id":"a2","token_usage":{"input_other":100,"input_cache_creation":0,"input_cache_read":0,"output":2}}}}
{"timestamp":"2026-06-14T12:00:03Z","message":{"type":"TurnEnd","payload":{}}}
{"timestamp":"2026-06-14T12:00:04Z","message":{"type":"TurnBegin","payload":{}}}
{"timestamp":"2026-06-14T12:00:05Z","message":{"type":"StatusUpdate","payload":{"message_id":"a3","token_usage":{"input_other":200,"input_cache_creation":0,"input_cache_read":0,"output":3}}}}
{"timestamp":"2026-06-14T12:00:06Z","message":{"type":"TurnEnd","payload":{}}}
"""
    out = parse.parse_file("sessions/projX/sess-X/wire.jsonl", blob)
    assert len(out["records"]) == 3
    assert len(out["ctx_turns"]) == 2
    assert out["ctx_turns"][0]["input"] == 100   # a2 wins turn 1
    assert out["ctx_turns"][0]["output"] == 2
    assert out["ctx_turns"][1]["input"] == 200   # a3 wins turn 2
    assert out["ctx_turns"][1]["delta"] == 100   # 200 - 100


# --- wire-first model attribution, end to end -----------------------------
# The ladder in _model_for was already correct and unit-tested; what was not
# tested was that parse_file actually PASSES the wire model into it. It did
# not — both call sites passed None — so the wire-first rung was dead code and
# every kimi-code record was labelled by date alone. These cover the call
# sites, not the ladder.

def _kc_blob(rows):
    """kimi-code wire from (epoch_seconds, model_or_None) pairs."""
    head = (
        b'{"type":"metadata","protocol_version":"1.4","created_at":%d}\n'
        % int(rows[0][0] * 1000)
    )
    body = b""
    for secs, model in rows:
        ms = int(secs * 1000)
        model_field = b'"model":"%s",' % model.encode() if model else b""
        body += (
            b'{"type":"usage.record","time":%d,%s'
            b'"usage":{"inputOther":1000,"output":200,"inputCacheRead":100,'
            b'"inputCacheCreation":50}}\n' % (ms, model_field)
        )
    return head + body


VALID_MODELS = {"kimi-k3", "kimi-k2-7-code", "kimi-k2-6"}


def test_kimi_code_k3_wire_string_wins_over_an_earlier_date():
    """The whole point of the wire-first rung: a record the wire calls k3 is
    k3 even before K3_CUTOFF. Real k3 records predate the constant.
    """
    out = parse.parse_file(
        "sessions/projKC/sess-k3w/wire.jsonl",
        _kc_blob([(parse.K3_CUTOFF_EPOCH - 3600, "kimi-code/k3")]),
    )
    r = out["records"][0]
    assert r["model"] == "kimi-k3"


def test_kimi_code_k3_record_is_priced_at_k3_rates():
    """Guards the rate_for substring trap directly: a raw "kimi-code/k3"
    reaching compute_cost would match no key and bill at DEFAULT_RATES
    (k2-6) — a ~3x undercount that no label assertion would catch.
    """
    out = parse.parse_file(
        "sessions/projKC/sess-k3p/wire.jsonl",
        _kc_blob([(parse.K3_CUTOFF_EPOCH - 3600, "kimi-code/k3")]),
    )
    r = out["records"][0]
    k3 = pricing.compute_cost("kimi-k3", fresh=1000, create=50, read=100, output=200)
    k26 = pricing.compute_cost("kimi-k2-6", fresh=1000, create=50, read=100, output=200)
    assert r["cost_usd"] == pytest.approx(k3, rel=1e-9)
    assert r["cost_usd"] != pytest.approx(k26, rel=1e-9), "billed at DEFAULT_RATES"


def test_kimi_code_mid_session_model_switch_yields_two_labels():
    """Model is resolved per RECORD, not per session. A session that switches
    kimi-for-coding -> k3 mid-flight must produce both labels; using the
    session's first event for every record collapsed them into one.
    """
    base = parse.K3_CUTOFF_EPOCH - 3600
    out = parse.parse_file(
        "sessions/projKC/sess-mix/wire.jsonl",
        _kc_blob([(base, "kimi-code/kimi-for-coding"), (base + 24, "kimi-code/k3")]),
    )
    assert [r["model"] for r in out["records"]] == ["kimi-k2-7-code", "kimi-k3"]


def test_legacy_records_split_across_the_model_cutoff():
    """Legacy transcripts have no model string, so dates are all we have —
    but applied per record, so a session straddling MODEL_CUTOFF splits
    instead of taking one label from its first event.
    """
    before = parse.MODEL_CUTOFF_EPOCH - 60
    after = parse.MODEL_CUTOFF_EPOCH + 60
    blob = b""
    for secs in (before, after):
        ts = datetime.fromtimestamp(secs, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        blob += (
            b'{"timestamp": "%s", "message": {"type": "StatusUpdate", "payload": '
            b'{"message_id": "m%d", "token_usage": {"input_other": 10, '
            b'"input_cache_creation": 0, "input_cache_read": 0, "output": 5}}}}\n'
            % (ts.encode(), secs)
        )
    out = parse.parse_file("sessions/projA/sess-split/wire.jsonl", blob)
    assert [r["model"] for r in out["records"]] == ["kimi-k2-6", "kimi-k2-7-code"]


def test_legacy_after_k3_cutoff_still_labels_k3():
    """No regression on the real legacy files that sit after the K3 cutoff:
    with no wire model, the date ladder's k3 rung still applies.
    """
    secs = parse.K3_CUTOFF_EPOCH + 3600
    ts = datetime.fromtimestamp(secs, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    blob = (
        b'{"timestamp": "%s", "message": {"type": "StatusUpdate", "payload": '
        b'{"message_id": "m1", "token_usage": {"input_other": 10, '
        b'"input_cache_creation": 0, "input_cache_read": 0, "output": 5}}}}\n'
        % ts.encode()
    )
    out = parse.parse_file("sessions/projA/sess-late/wire.jsonl", blob)
    assert out["records"][0]["model"] == "kimi-k3"


def test_parser_only_ever_emits_the_three_canonical_models():
    """There are exactly three real models. Anything else means a raw provider
    id leaked past canonicalisation — which is precisely what would bill at
    DEFAULT_RATES without any label looking obviously wrong.
    """
    base = parse.K3_CUTOFF_EPOCH - 3600
    blob = _kc_blob([
        (base, "kimi-code/k3"),
        (base + 1, "kimi-code/kimi-for-coding"),
        (base + 2, "kimi-code/k4-not-yet-invented"),
        (base + 3, None),
        (parse.MODEL_CUTOFF_EPOCH - 60, "kimi-code/k3"),
    ])
    out = parse.parse_file("sessions/projKC/sess-all/wire.jsonl", blob)
    assert out["records"], "fixture must produce records"
    for r in out["records"]:
        assert r["model"] in VALID_MODELS, r["model"]


# --- llm.error -> rate_limit_hits (issue #1) --------------------------------
#
# Shape taken from the producer that emits it, Nitjsefnie-OSC/kimi-code@e7bb820
# (`llm.error` op, packages/agent-core-v2/src/agent/llmRequester/llmRequestOps.ts):
# payload fields are flattened onto the wire line next to "type" and "time",
# the same envelope every other op uses.

def _kc_llm_error_blob(kind: str, message: str = "You are out of quota.") -> bytes:
    """kimi-code wire carrying one llm.error of the given `kind`."""
    base_ms = int(parse.K3_CUTOFF_EPOCH * 1000)
    return (
        b'{"type":"metadata","protocol_version":"1.4","created_at":%d}\n'
        b'{"type":"llm.error","time":%d,"kind":"%s","statusCode":429,'
        b'"retryable":false,"errorName":"APIProviderQuotaExhaustedError",'
        b'"message":"%s","model":"k3","durationMs":812}\n'
        % (base_ms, base_ms + 1000, kind.encode(), message.encode())
    )


def test_quota_exhausted_llm_error_is_recorded_as_a_rate_limit_hit():
    """The one event worth recording: a hard quota stop."""
    out = parse.parse_file(
        "sessions/projKC/sess-rl/wire.jsonl",
        _kc_llm_error_blob("quota_exhausted"),
    )
    assert len(out["rate_limit_hits"]) == 1
    hit = out["rate_limit_hits"][0]
    assert hit["line"] == 2
    assert hit["content"] == "You are out of quota."
    assert hit["ts"].startswith(
        datetime.fromtimestamp(
            parse.K3_CUTOFF_EPOCH + 1, tz=timezone.utc
        ).isoformat()[:19]
    )


def test_transient_rate_limit_llm_error_is_not_recorded():
    """kind="rate_limit" is the provider shaping traffic per minute, not the
    wall the user hits. claudit excludes the same case by text-matching "out
    of extra usage"; here the classification is a field, so the two dashboards
    count the same thing.
    """
    out = parse.parse_file(
        "sessions/projKC/sess-rl/wire.jsonl",
        _kc_llm_error_blob("rate_limit"),
    )
    assert out["rate_limit_hits"] == []


def test_llm_error_content_is_capped_at_500_chars():
    """Provider messages are unbounded free text. The producer truncates at
    500 (LLM_ERROR_MESSAGE_MAX_LENGTH) but the journal is not ours to trust.
    """
    out = parse.parse_file(
        "sessions/projKC/sess-rl/wire.jsonl",
        _kc_llm_error_blob("quota_exhausted", "x" * 900),
    )
    assert len(out["rate_limit_hits"][0]["content"]) == 500


def _churn_by_tool(out):
    """tool_name -> (lines_added, lines_deleted), one entry per tool_uses row."""
    return [
        (tu["tool_name"], tu["is_error"], tu["lines_added"], tu["lines_deleted"])
        for tu in out["tool_uses"]
    ]


def test_kimi_code_edit_write_churn_from_call_args():
    """The wire's tool RESULT carries no diff ("Replaced 1 occurrence in
    <path>"), so added/deleted line counts come from the call's args:
    Edit -> lines(new_string) / lines(old_string), Write -> lines(content).
    """
    out = parse.parse_file(
        "sessions/projLC/sess-lc/wire.jsonl", _read("line_churn_kimi_code.jsonl")
    )
    assert _churn_by_tool(out) == [
        ("Edit", False, 4, 3),     # "a\nb\nc" -> "a\nB\nc\nd"
        ("Write", False, 2, 0),    # content "x\ny\n"; overwrite size unknowable
        ("Edit", True, 0, 0),      # is_error -> the rejected edit changed nothing
        ("Bash", False, 0, 0),     # "ls" enumerates nothing
    ]


def test_legacy_str_replace_and_write_churn():
    """Legacy StrReplaceFile takes {edit: {old, new}} OR {edit: [edits]};
    WriteFile contributes added lines only."""
    out = parse.parse_file(
        "sessions/projLC/sess-lcl/wire.jsonl", _read("line_churn_legacy.jsonl")
    )
    assert _churn_by_tool(out) == [
        ("StrReplaceFile", False, 1, 2),  # single edit object
        ("StrReplaceFile", False, 4, 3),  # list of edits, summed
        ("WriteFile", False, 2, 0),
        ("WriteFile", True, 0, 0),        # is_error -> zeroed
    ]


def test_no_edit_tools_means_zero_churn_everywhere():
    """The empty/no-churn case: a file with no file-mutating calls parses
    with explicit zeros, not missing keys, so ingest can insert blindly."""
    out = parse.parse_file(
        "sessions/projA/sess-A/wire.jsonl", _read("single_turn.jsonl")
    )
    assert out["tool_uses"] == []
    out = parse.parse_file(
        "sessions/projErr/sess-err/wire.jsonl", _read("tool_error.jsonl")
    )
    assert len(out["tool_uses"]) == 1
    tu = out["tool_uses"][0]
    assert tu["lines_added"] == 0
    assert tu["lines_deleted"] == 0


def test_line_count_conventions():
    """Trailing newline terminates, a final partial line still counts."""
    assert _line_count("") == 0
    assert _line_count(None) == 0
    assert _line_count("a") == 1
    assert _line_count("a\n") == 1
    assert _line_count("a\nb") == 2
    assert _line_count("a\nb\n") == 2


# --- Claude Code transcripts ------------------------------------------------
#
# A Claude Code session reaches the Kimi bucket whenever claude-code-proxy
# routes one through Kimi: archive_sessions.py picks the bucket from the
# provider marker in the transcript, so the bytes are Claude's, not Kimi's.
# There is no parser for them yet; what matters is that the attempt is loud
# rather than a silent empty parse, which the ingest would persist as a
# `files` row stamped with the current parser_version — i.e. marked parsed.

_CLAUDE_FIX = FIX / "claude_transcript.jsonl"


def test_claude_transcript_raises_instead_of_parsing_empty():
    with pytest.raises(parse.UnsupportedTranscriptError) as e:
        parse.parse_file(
            "sessions/projA/sess-A/wire.jsonl", _CLAUDE_FIX.read_bytes()
        )
    assert "sessions/projA/sess-A/wire.jsonl" in str(e.value)


def test_claude_detection_survives_a_leading_unrecognized_line():
    """Detection scans until a line identifies the format, so a sidecar
    record Claude Code adds in some future version cannot make the file
    fall through to the legacy parser and come back empty."""
    blob = (b'{"type":"some-future-record","payload":{}}\n'
            + _CLAUDE_FIX.read_bytes())
    with pytest.raises(parse.UnsupportedTranscriptError):
        parse.parse_file("sessions/p/s/wire.jsonl", blob)


def test_file_history_lines_are_claude_even_without_a_session_id():
    """The two file-history record types carry no sessionId; their names
    carry the identification instead."""
    blob = b'{"type":"file-history-delta","messageId":"m1","delta":{}}\n'
    with pytest.raises(parse.UnsupportedTranscriptError):
        parse.parse_file("sessions/p/s/wire.jsonl", blob)


@pytest.mark.parametrize("name", [
    "single_turn.jsonl", "tool_error.jsonl", "line_churn_kimi_code.jsonl",
])
def test_kimi_fixtures_are_not_mistaken_for_claude(name):
    """The guard must not fire on the formats we do parse."""
    out = parse.parse_file("sessions/p/s/wire.jsonl", _read(name))
    assert isinstance(out["records"], list)


def test_kimi_code_bash_heredoc_churn():
    """Most editing goes through the shell, not Edit/Write: a heredoc
    redirected into a file is a write, counted from the command text.
    A command with nothing enumerable in it stays at 0/0."""
    out = parse.parse_file(
        "sessions/projBC/sess-bc/wire.jsonl", _read("bash_churn_kimi_code.jsonl")
    )
    assert _churn_by_tool(out) == [
        ("Bash", False, 3, 0),
        ("Bash", False, 0, 0),
    ]


def test_legacy_shell_heredoc_churn():
    """The legacy Shell tool carries the same {command: ...} payload as
    Bash, so it counts the same way — and an errored call is zeroed."""
    out = parse.parse_file(
        "sessions/projBC/sess-bcl/wire.jsonl", _read("bash_churn_legacy.jsonl")
    )
    assert _churn_by_tool(out) == [
        ("Shell", False, 2, 0),
        ("Shell", True, 0, 0),
    ]
