"""wire.jsonl → per-file (records list + ctx_turns array).

Each call to parse_file processes ONE wire.jsonl. Cross-file uuid dedup is a
query-time concern (DISTINCT ON (uuid) in the read endpoints).

Cost is precomputed per-record using pricing.MODEL_RATES so the
read path doesn't need to JOIN against rates. Bumps to the rate
table OR to the parse algorithm both require a PARSER_VERSION
bump to invalidate every files row.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import orjson

from backend import pricing
from backend.bash_churn import bash_churn

# Model attribution, oldest first. Each constant is a frozen UTC epoch, NOT a
# live expression.
#
# The wire is the primary source: a kimi-code usage.record carries the provider
# id (e.g. "kimi-code/k3") on the record that becomes a billing row, so it is
# authoritative when it names a model unambiguously.
#
# Dates are the fallback, needed for two real cases the wire cannot express:
#   - Legacy-format transcripts carry NO model string at all (and are still
#     being produced).
#   - "kimi-for-coding" spans BOTH k2.6 and k2.7-code — the branding did not
#     change at that transition — so only a date can separate those two.
#
# MODEL_CUTOFF applies to any record the wire did not settle; K3_CUTOFF applies
# ONLY to records with no wire model string at all. A model string that is
# present but unrecognized is not the "no model string" case, so it never
# reaches the k3 rung — see _model_for.
#
# Boundaries are strictly-before / inclusive-at, so each cutoff instant
# belongs to the NEWER model.

# 2026-06-11 22:30:35 UTC  k2-6 -> k2-7-code
MODEL_CUTOFF_EPOCH = 1781217035
# 2026-07-16 14:45:55 UTC  earliest observed k3 usage.record; only applies
# to records with no wire model string.
K3_CUTOFF_EPOCH = 1784213155
MODEL_CUTOFF_DT = datetime.fromtimestamp(MODEL_CUTOFF_EPOCH, tz=timezone.utc)
K3_CUTOFF_DT = datetime.fromtimestamp(K3_CUTOFF_EPOCH, tz=timezone.utc)

# Raw provider id -> canonical pricing label. Only ids that identify a pricing
# model on their own belong here; "kimi-for-coding" deliberately does not.
_WIRE_MODEL_MAP = {"k3": "kimi-k3"}


def _canonical_model(wire_model: str | None) -> str | None:
    """Raw provider id -> canonical pricing label, or None if the wire cannot
    settle it alone (ambiguous id, unrecognized id, or no id at all).

    Only canonical labels may reach pricing.compute_cost: pricing.rate_for
    matches keys anchored at the start of the id, so a raw "kimi-code/k3"
    would match no key and silently bill at DEFAULT_RATES (k2-6) — a ~3x
    undercount.
    """
    if not wire_model:
        return None
    return _WIRE_MODEL_MAP.get(wire_model.rsplit("/", 1)[-1])


def _model_for(wire_model: str | None, ts: datetime | None) -> str:
    """Pricing model for ONE billing record.

    The wire wins when it names a model unambiguously; dates decide only what
    the wire cannot express. Resolved per-record, not per-session: a session
    can switch model mid-flight (observed: kimi-for-coding -> k3, 24s apart).

    Unknowns resolve conservatively, never to the newest label, because K3's
    rates are ~3x higher and a wrong undercount beats a wrong overcount:
      - No usable timestamp -> kimi-k2-7-code. An unstamped record is
        already-ingested history, so it cannot postdate the K3 cutoff.
      - A model string that is present but unrecognized -> the k2 rungs only.
        It is not the "transcript carries no model string" case that justifies
        the date ladder's k3 rung, so the date must not promote it to k3.
    """
    canonical = _canonical_model(wire_model)
    if canonical is not None:
        return canonical

    if ts is None:
        return "kimi-k2-7-code"

    if ts < MODEL_CUTOFF_DT:
        return "kimi-k2-6"

    # The wire named a model and it is not k3 (else _canonical_model caught
    # it), so the date may only separate the k2 generations — never promote
    # to k3.
    if wire_model:
        return "kimi-k2-7-code"

    if ts < K3_CUTOFF_DT:
        return "kimi-k2-7-code"
    return "kimi-k3"


def _to_dt(s: str | float | None):
    if not s:
        return None
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(s, tz=datetime.now().astimezone().tzinfo)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


@dataclass
class _ParseState:
    """Mutable per-file parse state threaded through the message handlers.

    turns entries are {begin_line, begin_ts, end_line, end_ts,
    status_lines: [line_num]}; records entries carry the keys documented
    on parse_file plus an internal ctx_input used to build ctx_turns.
    """
    file_key: str
    records: list[dict] = field(default_factory=list)
    tool_uses: list[dict] = field(default_factory=list)
    rate_limit_hits: list[dict] = field(default_factory=list)
    # Per-file map of tool_call_id -> bool(is_error)
    tool_result_is_error: dict[str, bool] = field(default_factory=dict)
    turns: list[dict] = field(default_factory=list)
    current_turn: dict | None = None
    current_turn_id: str | None = None
    # For reply latency: track TurnBegin ts, then find first assistant event
    pending_turn_begin_ts: datetime | None = None
    turn_has_assistant_event: bool = False
    # For text_chars: accumulate ContentPart.text since last TurnBegin
    text_chars_since_turn: int = 0
    # First event timestamp drives the per-session model label.
    first_event_ts: datetime | None = None


def _close_turn(st: _ParseState, line_num: int, ts: datetime | None) -> None:
    if st.current_turn is not None:
        st.current_turn["end_line"] = line_num
        st.current_turn["end_ts"] = ts
        st.turns.append(st.current_turn)
        st.current_turn = None


def _start_turn(st: _ParseState, line_num: int, ts: datetime | None) -> None:
    st.current_turn = {
        "begin_line": line_num,
        "begin_ts": ts,
        "end_line": None,
        "end_ts": None,
        "status_lines": [],
    }
    st.turn_has_assistant_event = False
    st.text_chars_since_turn = 0


def _turn_boundary(st: _ParseState, line_num: int, ts: datetime | None) -> None:
    """Close any open turn, open the next, and arm the reply-latency anchor."""
    _close_turn(st, line_num, ts)
    _start_turn(st, line_num, ts)
    st.pending_turn_begin_ts = ts


def _mark_assistant_event(st: _ParseState) -> None:
    if not st.turn_has_assistant_event and st.pending_turn_begin_ts is not None:
        st.turn_has_assistant_event = True


def _consume_reply_latency(st: _ParseState, ts: datetime | None) -> float | None:
    """Gap from the turn's anchor to this record, if the anchor is open.

    The anchor is consumed either way: one reply latency per turn, taken
    from its first billing record.
    """
    latency = None
    if st.pending_turn_begin_ts is not None and ts is not None:
        delta_s = (ts - st.pending_turn_begin_ts).total_seconds()
        if delta_s >= 0:
            latency = delta_s
    st.pending_turn_begin_ts = None
    return latency


def _line_count(text: object) -> int:
    """Lines in an edit-payload string. A trailing newline terminates the
    last line rather than starting another, so "a\n" is 1 line; a final
    partial line still counts, so "a\nb" is 2."""
    if not isinstance(text, str) or not text:
        return 0
    n = text.count("\n")
    return n if text.endswith("\n") else n + 1


def _edit_churn(tool_name: str, args: dict) -> tuple[int, int]:
    """(lines_added, lines_deleted) for ONE file-mutating tool call.

    The wire's tool RESULT carries no diff — an Edit result is a prose
    string like "Replaced 1 occurrence in <path>" — so the counts are
    derived from the call's ARGS. Verified against the R2 corpus
    (2026-07, ~300 files sampled): the file-mutating tools come in
    exactly these shapes, two per wire format:

      kimi-code  Edit  {path, old_string, new_string}   (163/163 calls)
                 Write {path, content, mode?}
      legacy     StrReplaceFile {path, edit: {old, new} | [{old, new}, ...]}
                 WriteFile      {path, content, mode?}

    A Write's deleted count is unknowable — the args carry only the new
    content, not what was overwritten — so Writes contribute added lines
    only (append mode and fresh files are the common case anyway).

    Bash and its legacy Shell spelling carry a plain {command: "..."}
    and route into backend.bash_churn, which recovers what the command
    TEXT enumerates: heredoc bodies written to a file, inline patch
    hunks, literal python replacements. They are 29,668 calls against
    10,734 edit/write ones, so leaving them at zero hid most of the
    churn. Every other tool returns (0, 0).

    The counts describe the ATTEMPT: a call whose result is an error
    changed nothing, and _resolve_tool_errors zeroes its churn once the
    result's is_error is known.
    """
    if tool_name == "Edit":
        return (
            _line_count(args.get("new_string")),
            _line_count(args.get("old_string")),
        )
    if tool_name in ("Write", "WriteFile"):
        return (_line_count(args.get("content")), 0)
    if tool_name in ("Bash", "Shell"):
        return bash_churn(str(args.get("command") or ""))
    if tool_name == "StrReplaceFile":
        edit = args.get("edit")
        edits = edit if isinstance(edit, list) else [edit]
        added = deleted = 0
        for e in edits:
            if isinstance(e, dict):
                added += _line_count(e.get("new"))
                deleted += _line_count(e.get("old"))
        return (added, deleted)
    return (0, 0)


def _args_to_dict(args) -> dict:
    """Tool-call arguments as a dict, whatever the wire shape: kimi-code
    carries them already-parsed, legacy as a JSON string. Anything
    unreadable is {} — the churn of an unparsable edit is 0, not a
    dropped tool call."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args) if args else {}
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _append_tool_use(st: _ParseState, line_num: int, ts: datetime | None,
                     tool_name: str, tool_call_id: str,
                     churn: tuple[int, int] = (0, 0)) -> None:
    added, deleted = churn
    st.tool_uses.append({
        "file_key": st.file_key,
        "line_num": line_num,
        "idx": len(st.tool_uses),
        "ts": ts,
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "is_error": None,
        "lines_added": added,
        "lines_deleted": deleted,
    })


def _append_usage_record(st: _ParseState, line_num: int,
                         ts: datetime | None, uuid: str | None,
                         wire_model: str | None, toks: tuple[int, int, int, int]
                         ) -> None:
    """Build one billing record from its token counts and append it.

    The model is resolved per-record (a session can switch model
    mid-flight): the wire id wins when it settles the model
    unambiguously, dates decide only what the wire cannot express.
    first_event_ts is the fallback for a record with no timestamp of its
    own.
    """
    fresh, create, read, output = toks
    latency = _consume_reply_latency(st, ts)
    model = _model_for(wire_model, ts or st.first_event_ts)
    cost = pricing.compute_cost(
        model,
        fresh=fresh, create=create, read=read, output=output,
    )
    st.records.append({
        "file_key": st.file_key,
        "line_num": line_num,
        "uuid": uuid,
        "ts": ts,
        "model": model,
        "fresh_tokens": fresh,
        "cache_creation_tokens": create,
        "cache_read_tokens": read,
        "output_tokens": output,
        "cost_usd": round(cost, 6),
        "text_chars": st.text_chars_since_turn,
        "reply_latency_s": latency,
        "ctx_input": fresh + create + read,
    })
    if st.current_turn is not None:
        st.current_turn["status_lines"].append(line_num)


def _resolve_tool_errors(tool_uses: list[dict],
                         tool_result_is_error: dict[str, bool]) -> None:
    """Resolve tool_result.is_error onto each tool_uses entry, and zero the
    line churn of calls that failed — a rejected edit changed no lines."""
    for tu in tool_uses:
        tc_id = tu.pop("tool_call_id", "")
        if tc_id and tc_id in tool_result_is_error:
            tu["is_error"] = tool_result_is_error[tc_id]
            if tu["is_error"]:
                tu["lines_added"] = 0
                tu["lines_deleted"] = 0


def _ctx_turns_from_turns(turns: list[dict], records: list[dict]) -> list[dict]:
    """Build ctx_turns from turns + records.

    The last StatusUpdate/usage.record in each turn is the canonical one.
    """
    rec_by_line = {r["line_num"]: r for r in records}
    ctx_turns: list[dict] = []
    prev_input = 0
    turn_idx = 0
    for turn in turns:
        if not turn["status_lines"]:
            continue
        last_line = turn["status_lines"][-1]
        rec = rec_by_line.get(last_line)
        if not rec or rec["ctx_input"] <= 0:
            continue
        turn_idx += 1
        ctx_input = rec["ctx_input"]
        ctx_turns.append({
            "idx": turn_idx,
            "ts": rec["ts"].isoformat() if rec["ts"] else "",
            "line": last_line,
            "input": ctx_input,
            "output": rec["output_tokens"],
            "delta": ctx_input - prev_input,
        })
        prev_input = ctx_input
    return ctx_turns


def _finish_parse(st: _ParseState, end_line: int | None = None) -> dict:
    """Shared tail for both formats: close any dangling turn, settle the
    tool-call is_error flags, and build ctx_turns from the turns."""
    if end_line is not None:
        _close_turn(st, end_line, None)
    elif st.current_turn is not None:
        st.turns.append(st.current_turn)
    _resolve_tool_errors(st.tool_uses, st.tool_result_is_error)
    ctx_turns = _ctx_turns_from_turns(st.turns, st.records)
    return {
        "records": st.records,
        "ctx_turns": ctx_turns,
        "turn_count": len(ctx_turns),
        "rate_limit_hits": st.rate_limit_hits,
        "tool_uses": st.tool_uses,
    }


# --------------------------------------------------------------------------
# Legacy kimi-cli format
# --------------------------------------------------------------------------


def _legacy_turn_end(st: _ParseState, line_num: int,
                     ts: datetime | None) -> None:
    if st.current_turn is not None:
        _close_turn(st, line_num, ts)
        st.pending_turn_begin_ts = None


def _legacy_content_part(st: _ParseState, payload: dict) -> None:
    if payload.get("type", "") == "text":
        st.text_chars_since_turn += len(str(payload.get("text", "")))
    # Any ContentPart or ToolCall counts as an assistant event
    _mark_assistant_event(st)


def _legacy_tool_call(st: _ParseState, line_num: int, ts: datetime | None,
                      payload: dict) -> None:
    func = payload.get("function", {})
    name = func.get("name", "")
    churn = _edit_churn(name, _args_to_dict(func.get("arguments")))
    _append_tool_use(st, line_num, ts, name, payload.get("id", ""), churn)
    _mark_assistant_event(st)


def _legacy_tool_result(st: _ParseState, payload: dict) -> None:
    return_value = payload.get("return_value", {})
    tc_id = payload.get("tool_call_id", "")
    is_err = isinstance(return_value, dict) and bool(
        return_value.get("is_error", False)
    )
    if tc_id:
        st.tool_result_is_error[str(tc_id)] = is_err


def _legacy_status_update(st: _ParseState, line_num: int,
                          ts: datetime | None, payload: dict) -> None:
    tu = payload.get("token_usage")
    if not tu:
        return
    toks = (
        int(tu.get("input_other", 0) or 0),
        int(tu.get("input_cache_creation", 0) or 0),
        int(tu.get("input_cache_read", 0) or 0),
        int(tu.get("output", 0) or 0),
    )
    # Legacy transcripts carry no model string anywhere — verified across
    # the corpus: StatusUpdate payloads expose only context_usage /
    # context_tokens / max_context_tokens / token_usage / message_id /
    # plan_mode / mcp_status. Dates are all we have here. Use the RECORD's
    # own ts so a session spanning a cutoff splits correctly;
    # first_event_ts is only the fallback for a record with no timestamp.
    _append_usage_record(
        st, line_num, ts, payload.get("message_id") or None, None, toks
    )


def _legacy_dispatch(st: _ParseState, msg_type: str, line_num: int,
                     ts: datetime | None, payload: dict) -> None:
    if msg_type == "TurnBegin":
        _turn_boundary(st, line_num, ts)
    elif msg_type == "TurnEnd":
        _legacy_turn_end(st, line_num, ts)
    elif msg_type == "ContentPart":
        _legacy_content_part(st, payload)
    elif msg_type == "ToolCall":
        _legacy_tool_call(st, line_num, ts, payload)
    elif msg_type == "ToolResult":
        _legacy_tool_result(st, payload)
    elif msg_type == "StatusUpdate":
        _legacy_status_update(st, line_num, ts, payload)


def _parse_legacy(file_key: str, blob: bytes) -> dict:
    """Parse one legacy kimi-cli wire.jsonl.

    records: list of dicts with keys
      file_key, line_num, uuid, ts, model,
      fresh_tokens, cache_creation_tokens, cache_read_tokens,
      output_tokens, text_chars, reply_latency_s, cost_usd

    ctx_turns: list of dicts with keys
      idx, ts, line, input, output, delta
    """
    st = _ParseState(file_key)
    for line_num, raw in enumerate(blob.splitlines(), 1):
        if not raw:
            continue
        try:
            obj = orjson.loads(raw)
        except orjson.JSONDecodeError:
            continue

        ts_dt = _to_dt(obj.get("timestamp"))
        if ts_dt is not None and st.first_event_ts is None:
            st.first_event_ts = ts_dt

        msg = obj.get("message") or {}
        _legacy_dispatch(
            st, msg.get("type", ""), line_num, ts_dt, msg.get("payload", {})
        )

    return _finish_parse(st)


# --------------------------------------------------------------------------
# kimi-code format
# --------------------------------------------------------------------------


def _kc_parse_tool_call(tc: dict) -> tuple[str, str | None, str]:
    """Extract name, arguments, id from a kimi-code ToolCall (v1.0/v1.1)."""
    if tc.get("type") != "function":
        return "", None, ""
    tcid = tc.get("id", "")
    if "name" in tc:
        return str(tc.get("name", "")), tc.get("arguments"), tcid
    func = tc.get("function") or {}
    return str(func.get("name", "")), func.get("arguments"), tcid


def _kc_args_to_input(args) -> dict:
    if args is None:
        return {}
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            return json.loads(args) if args else {}
        except json.JSONDecodeError:
            return {"_raw": args}
    return {"_raw": args}


def _kc_metadata(st: _ParseState, obj: dict) -> None:
    ts_ms = obj.get("created_at")
    if ts_ms and st.first_event_ts is None:
        st.first_event_ts = datetime.fromtimestamp(
            ts_ms / 1000.0, tz=timezone.utc
        )


def _kc_event_ts(st: _ParseState, obj: dict) -> datetime | None:
    ts_ms = obj.get("time")
    ts_dt = (
        datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        if ts_ms else None
    )
    if ts_dt is not None and st.first_event_ts is None:
        st.first_event_ts = ts_dt
    return ts_dt


def _count_content_text(content: list[dict]) -> int:
    chars = 0
    for p in content:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            chars += len(str(p.get("text", "")))
    return chars


def _kc_append_message(st: _ParseState, line_num: int, ts: datetime | None,
                       obj: dict) -> None:
    msg = obj.get("message") or {}
    role = msg.get("role")
    if role == "assistant":
        st.text_chars_since_turn += _count_content_text(
            msg.get("content") or []
        )
        for tc in msg.get("toolCalls") or []:
            name, args, tcid = _kc_parse_tool_call(tc)
            churn = _edit_churn(name, _args_to_dict(args))
            _append_tool_use(st, line_num, ts, name, tcid, churn)
        _mark_assistant_event(st)
    elif role == "tool":
        tcid = msg.get("toolCallId", "")
        if tcid:
            st.tool_result_is_error[str(tcid)] = bool(msg.get("isError"))
    # role == user / system: no parser-side action


def _kc_step_begin(st: _ParseState, line_num: int, ts: datetime | None,
                   turn_id: str | None) -> None:
    if turn_id != st.current_turn_id:
        _close_turn(st, line_num, ts)
        st.current_turn_id = turn_id
        _start_turn(st, line_num, ts)


def _kc_loop_event(st: _ParseState, line_num: int, ts: datetime | None,
                   obj: dict) -> None:
    ev = obj.get("event") or {}
    et = ev.get("type")
    if et == "step.begin":
        _kc_step_begin(st, line_num, ts, ev.get("turnId"))
    elif et == "content.part":
        part = ev.get("part") or {}
        if part.get("type") == "text":
            st.text_chars_since_turn += len(str(part.get("text", "")))
        _mark_assistant_event(st)
    elif et == "tool.call":
        name = str(ev.get("name", ""))
        churn = _edit_churn(name, _args_to_dict(ev.get("args")))
        _append_tool_use(
            st, line_num, ts, name, str(ev.get("toolCallId", "")), churn,
        )
        _mark_assistant_event(st)
    elif et == "tool.result":
        res = ev.get("result") or {}
        tcid = ev.get("toolCallId", "")
        if tcid:
            st.tool_result_is_error[str(tcid)] = bool(
                res.get("isError") if isinstance(res, dict) else False
            )
    # et == "step.end": informational; the turn stays open until the next
    # turn boundary (new turnId or turn.prompt).


def _kc_usage_record(st: _ParseState, line_num: int, ts: datetime | None,
                     obj: dict) -> None:
    usage = obj.get("usage") or {}
    toks = (
        int(usage.get("inputOther") or 0),
        int(usage.get("inputCacheCreation") or 0),
        int(usage.get("inputCacheRead") or 0),
        int(usage.get("output") or 0),
    )
    # Every usage.record names its model (verified: 7,397/7,397 in the
    # corpus, values "kimi-code/k3" and "kimi-code/kimi-for-coding").
    # Honour it; dates decide only what the wire cannot express.
    _append_usage_record(
        st, line_num, ts, f"{st.file_key}:{line_num}", obj.get("model"), toks
    )


def _kc_llm_error(st: _ParseState, line_num: int, ts: datetime | None,
                  obj: dict) -> None:
    """Book a failed provider request as a rate-limit hit, if it is one.

    kimi-code journals every non-aborted provider failure as an `llm.error`
    op (Nitjsefnie-OSC/kimi-code@e7bb820) and carries its own classification
    in `kind`, so a hard quota stop is told apart from an ordinary
    per-minute 429 by a field rather than by matching message text — the
    signal claudit has to recover from "out of extra usage" in the body.

    Only `quota_exhausted` is recorded. `rate_limit` is the provider
    shaping traffic; it clears itself and is not the wall the user hits.
    claudit excludes the same case deliberately, so counting it here would
    make the two dashboards' stats mean different things.

    Content is re-capped at 500 even though the producer already truncates
    there: the cap belongs to whatever writes the column, not to a
    journal written by a different program.
    """
    if obj.get("kind") != "quota_exhausted":
        return
    st.rate_limit_hits.append({
        "line": line_num,
        "ts": ts.isoformat() if ts is not None else "",
        "content": str(obj.get("message") or "")[:500],
    })


def _kc_dispatch(st: _ParseState, typ: str, line_num: int,
                 ts: datetime | None, obj: dict) -> None:
    # Turn boundary: turn.prompt / turn.steer
    if typ in ("turn.prompt", "turn.steer"):
        _turn_boundary(st, line_num, ts)
    elif typ == "context.append_message":
        _kc_append_message(st, line_num, ts, obj)
    elif typ == "context.append_loop_event":
        _kc_loop_event(st, line_num, ts, obj)
    elif typ == "usage.record":
        _kc_usage_record(st, line_num, ts, obj)
    elif typ == "llm.error":
        _kc_llm_error(st, line_num, ts, obj)


def _parse_kimi_code(file_key: str, blob: bytes) -> dict:
    """Parse one kimi-code wire.jsonl.

    Returns the same shape as _parse_legacy.
    """
    st = _ParseState(file_key)
    for line_num, raw in enumerate(blob.splitlines(), 1):
        if not raw:
            continue
        try:
            obj = orjson.loads(raw)
        except orjson.JSONDecodeError:
            continue

        typ = obj.get("type")
        if typ == "metadata":
            _kc_metadata(st, obj)
            continue

        ts_dt = _kc_event_ts(st, obj)
        _kc_dispatch(st, typ, line_num, ts_dt, obj)

    return _finish_parse(st, len(blob.splitlines()))


class UnsupportedTranscriptError(Exception):
    """A transcript this parser recognises but does not handle yet.

    Raised instead of returning an empty parse. An empty parse looks like a
    success to the ingest, which persists it as a `files` row stamped with
    the current parser_version — that marks the transcript PARSED, so no
    later run revisits it until PARSER_VERSION is bumped. Raising keeps the
    object unrecorded, so support landing later picks it up on its own.
    """


# Claude Code writes ~/.claude/projects/<slug>/<uuid>.jsonl, and every line it
# emits — conversation turns as well as the sidecar bookkeeping records it
# interleaves into the same file — carries the session id under `sessionId`.
# Neither Kimi format nor the Codex rollout format has any such field
# (verified against production objects in both buckets: zero occurrences), so
# its presence alone identifies the format, and it keeps identifying it as
# Claude Code adds line types.
#
# The two exceptions are file-history records, which carry no sessionId; their
# type names are distinctive enough to match on their own.
_CLAUDE_UNKEYED_TYPES = frozenset({
    "file-history-snapshot", "file-history-delta",
})


def _is_claude_line(obj: dict) -> bool:
    """Is this one line of a Claude Code transcript?"""
    if obj.get("type") in _CLAUDE_UNKEYED_TYPES:
        return True
    return isinstance(obj.get("sessionId"), str)


def parse_file(file_key: str, blob: bytes) -> dict:
    """Parse one wire.jsonl. Returns {records, ctx_turns, turn_count, rate_limit_hits, tool_uses}.

    Auto-detects legacy kimi-cli format vs new kimi-code format per file,
    and raises UnsupportedTranscriptError for a Claude Code transcript —
    which reaches this bucket whenever claude-code-proxy routes a Claude
    Code session through Kimi.
    records: list of dicts with keys
      file_key, line_num, uuid, ts, model,
      fresh_tokens, cache_creation_tokens, cache_read_tokens,
      output_tokens, text_chars, reply_latency_s, cost_usd

    ctx_turns: list of dicts with keys
      idx, ts, line, input, output, delta
    """
    fmt = "legacy"
    for raw in blob.splitlines():
        if not raw:
            continue
        try:
            obj = orjson.loads(raw)
        except orjson.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("type") == "metadata":
            fmt = "kimi-code" if "created_at" in obj else "legacy"
            break
        if obj.get("type") in {
            "context.append_message",
            "context.append_loop_event",
            "usage.record",
            "turn.prompt",
            "turn.steer",
        }:
            fmt = "kimi-code"
            break
        if "timestamp" in obj and (obj.get("message") or {}).get("type") in {
            "StatusUpdate", "TurnBegin", "ToolCall", "ContentPart"
        }:
            fmt = "legacy"
            break
        # Last rung on purpose: a line that somehow carried both a Kimi
        # marker and a sessionId belongs to the format we can parse.
        if _is_claude_line(obj):
            fmt = "claude"
            break

    if fmt == "claude":
        raise UnsupportedTranscriptError(
            f"{file_key}: Claude Code transcript, no parser yet"
        )
    if fmt == "kimi-code":
        return _parse_kimi_code(file_key, blob)
    return _parse_legacy(file_key, blob)
