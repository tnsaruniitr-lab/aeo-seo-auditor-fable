"""
agent.py — Audit agent harness.

Runs the 15-phase playbook in `system_prompt.py` as a Claude tool-use loop.
The agent calls tools defined in tools.py until it emits a final
`<audit>...</audit>` JSON payload.

This is the parity layer: same model (claude-sonnet-4-6), same playbook
(skill-unified/SKILL.md adaptation), same tools (web_fetch, web_search,
Playwright render, deterministic scripts, brain ranker, references) as the
chat skill — just headless.

USAGE
    from agent import run_audit_agent
    result = run_audit_agent("https://example.com", output_dir="./audits/")

ENV
    ANTHROPIC_API_KEY     required (web_search/web_fetch use Anthropic server tools)
    SUPABASE_URL/KEY      optional for persist_audit
"""

from __future__ import annotations

import json
import logging
import os
import re
import statistics
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

from tools import TOOLS_SPEC, dispatch_tool, SERVER_TOOL_NAMES
from system_prompt import SYSTEM_PROMPT
from site_context import site_context_block, metadata_entry as site_context_metadata

log = logging.getLogger('audit.agent')


# -----------------------------------------------------------------------------
# CONFIG
# -----------------------------------------------------------------------------

MODEL = "claude-sonnet-4-6"
# Output token cap per turn. The final audit JSON is ~26KB (~7-8K tokens)
# and intermediate turns are tiny (~200 tokens), so a generous ceiling
# avoids the "max_tokens cut off the final JSON mid-object" failure mode.
# Sonnet 4.6 supports up to 64K output tokens.
MAX_TOKENS_PER_TURN = 32768
MAX_AGENT_TURNS = 80              # hard cap on tool-use iterations
MAX_PAUSE_TURNS = 10             # cap consecutive server-tool pause_turn continuations
MAX_TOOL_RESULT_BYTES = 50_000     # truncate big tool outputs (e.g. raw scripts JSON)
# Total time budget per audit. Bumped 480s → 900s after excellage.ae
# hit 480s at turn 8 with all the WORK done (just ran out before the
# final emission). Slow sites (high TTFB, large pages) make every tool
# call slower, and competitor crawls amplify this. 900s gives ~50%
# headroom over the slowest observed audit (~440s).
TOTAL_BUDGET_SECONDS = 900

# Cost guardrails. A pathological site (huge pages, many competitors) can drive
# the agent into a long, expensive loop. We track spend per audit and abort
# cleanly once it crosses the ceiling — emitting whatever we have rather than
# billing without bound. Sonnet 4.6 list price (USD per 1M tokens).
PRICE_INPUT_PER_MTOK = float(os.getenv('PRICE_INPUT_PER_MTOK', '3.0'))
PRICE_OUTPUT_PER_MTOK = float(os.getenv('PRICE_OUTPUT_PER_MTOK', '15.0'))
PRICE_CACHE_READ_PER_MTOK = float(os.getenv('PRICE_CACHE_READ_PER_MTOK', '0.30'))
PRICE_CACHE_WRITE_PER_MTOK = float(os.getenv('PRICE_CACHE_WRITE_PER_MTOK', '3.75'))
PRICE_PER_WEB_SEARCH = float(os.getenv('PRICE_PER_WEB_SEARCH', '0.01'))
# NOTE: usage.input_tokens is only the UNCACHED remainder — with the moving
# cache breakpoint most real input billing flows through cache_read/-creation.
# The $2.50 ceiling deliberately stays on the uncached metric until the
# true-spend distribution is measured in prod (see metadata.cost_usd_true);
# recalibrating both together would silently change abort behavior.
MAX_AUDIT_COST_USD = float(os.getenv('MAX_AUDIT_COST_USD', '2.50'))

# Transient API failures that are worth retrying rather than scrapping a
# half-finished (paid) audit. 429 = rate limit, 529 = overloaded, 5xx = server.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 529})
MAX_STREAM_RETRIES = int(os.getenv('MAX_STREAM_RETRIES', '4'))

# AUDIT_TEMPERATURE (e.g. 0) pins classification variance — OPT-IN only:
# temperature changes the classification DISTRIBUTION, so it must be validated
# against back-to-back ground-truth audits before riding to prod as a default.
# Parsed ONCE at import and fail-safe: a malformed value must degrade to
# 'unset' with a loud log, never break every stream attempt with a ValueError
# misattributed to the API.
_TEMP_KW: dict = {}
try:
    _t = os.getenv('AUDIT_TEMPERATURE')
    if _t not in (None, ''):
        _TEMP_KW = {'temperature': float(_t)}
except ValueError:
    logging.getLogger('audit.agent').error(
        'AUDIT_TEMPERATURE=%r is not a number — ignoring it', os.getenv('AUDIT_TEMPERATURE'))


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Uncached-traffic cost estimate in USD — the historical guardrail metric.
    Excludes cache reads/writes and web_search fees; see estimate_cost_usd_true
    for the full bill."""
    return round(
        input_tokens / 1_000_000 * PRICE_INPUT_PER_MTOK
        + output_tokens / 1_000_000 * PRICE_OUTPUT_PER_MTOK,
        4,
    )


def estimate_cost_usd_true(input_tokens: int, output_tokens: int,
                            cache_read_tokens: int = 0,
                            cache_creation_tokens: int = 0,
                            web_searches: int = 0) -> float:
    """Full per-audit cost estimate: all four token buckets + web_search fees.
    Shadow accounting only — logged and persisted, never wired to the abort
    ceiling (that stays on estimate_cost_usd for behavioral continuity)."""
    return round(
        input_tokens / 1_000_000 * PRICE_INPUT_PER_MTOK
        + output_tokens / 1_000_000 * PRICE_OUTPUT_PER_MTOK
        + cache_read_tokens / 1_000_000 * PRICE_CACHE_READ_PER_MTOK
        + cache_creation_tokens / 1_000_000 * PRICE_CACHE_WRITE_PER_MTOK
        + web_searches * PRICE_PER_WEB_SEARCH,
        4,
    )


def _is_retryable(exc: Exception) -> bool:
    """True for transient API errors (rate-limit / overloaded / 5xx / network)."""
    status_code = getattr(exc, 'status_code', None)
    if isinstance(status_code, int) and status_code in RETRYABLE_STATUS:
        return True
    name = type(exc).__name__
    return name in {
        'APIConnectionError', 'APITimeoutError', 'InternalServerError',
        'RateLimitError', 'OverloadedError', 'ServiceUnavailableError',
    }


def _system_blocks():
    """System prompt as a cacheable content block. Caching the ~4K-token static
    system prompt removes it from full-price re-billing on every one of up to
    80 turns (the dominant quadratic cost in the old loop)."""
    return [{
        "type": "text",
        "text": SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }]


# Block types the API accepts cache_control on. Marking anything else (e.g. a
# thinking block or a server-tool result) risks a 400, which would be worse
# than a cache miss — walk backward to the newest markable block instead.
_CACHEABLE_BLOCK_TYPES = frozenset({
    "text", "tool_use", "tool_result", "image", "document",
})


def _dictify_content(content: Any) -> List[Any]:
    """Convert SDK response content blocks (Pydantic objects) to plain
    wire-shape dicts before appending them to `messages`. Without this, a
    pause_turn continuation carried Pydantic blocks that
    _mark_cache_breakpoint's isinstance(blk, dict) check silently skipped —
    so the request had no conversation breakpoint and re-billed the ENTIRE
    accumulated context at full input price, up to MAX_PAUSE_TURNS times."""
    out: List[Any] = []
    for blk in (content or []):
        if isinstance(blk, dict):
            out.append(blk)
        elif hasattr(blk, "model_dump"):
            out.append(blk.model_dump(mode="json", exclude_none=True))
        else:
            out.append(blk)
    return out


def _mark_cache_breakpoint(messages: List[Dict[str, Any]]) -> None:
    """Add a cache_control breakpoint to the newest cacheable block of the last
    message so the conversation prefix (system + tools + all prior turns) is
    served from cache on the next call. Mutates in place; safe on both string
    and list-shaped message content."""
    if not messages:
        return
    last = messages[-1]
    content = last.get("content")
    if isinstance(content, str):
        last["content"] = [{
            "type": "text", "text": content,
            "cache_control": {"type": "ephemeral"},
        }]
        return
    # Walk messages newest-first: a pause_turn assistant message can consist
    # entirely of server-tool blocks (no cacheable type) — fall back to the
    # newest cacheable block in an earlier message rather than marking nothing
    # (all older markers were just cleared, so returning empty-handed would
    # re-bill the whole conversation at full price).
    for m in reversed(messages):
        c = m.get("content")
        if not isinstance(c, list):
            continue
        for blk in reversed(c):
            if isinstance(blk, dict) and blk.get("type") in _CACHEABLE_BLOCK_TYPES:
                blk["cache_control"] = {"type": "ephemeral"}
                return


def _clear_cache_breakpoints(messages: List[Dict[str, Any]]) -> None:
    """Remove stale cache_control markers so only the newest breakpoint is live
    (the API allows at most 4; we keep system + one moving message breakpoint)."""
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict):
                    blk.pop("cache_control", None)


# -----------------------------------------------------------------------------
# CORE LOOP
# -----------------------------------------------------------------------------

def _derive_phase_label(tool_name: str, tool_input: Dict[str, Any],
                         tool_call_history: List[Dict[str, Any]]) -> str:
    """Map a tool call to a human-readable phase label for UX feedback.

    Heuristic — uses tool name + history context to infer which of the 15
    phases the agent is currently executing. Not exhaustive (the agent has
    discretion to call tools in any order) but covers the common patterns.
    """
    web_fetch_count = sum(1 for c in tool_call_history if c['name'] == 'web_fetch')
    web_search_count = sum(1 for c in tool_call_history if c['name'] == 'web_search')
    query_brain_count = sum(1 for c in tool_call_history if c['name'] == 'query_brain')

    if tool_name == 'run_deterministic_scripts':
        return "Phase 1.6: Running deterministic scripts (robots, sitemap, schema, FAQ, etc.)"
    if tool_name == 'render_page_js':
        return "Phase 1.5: Measuring performance (TTFB, LCP, CLS) via Playwright"
    if tool_name == 'web_fetch':
        if web_fetch_count <= 1:
            return "Phase 1: Fetching target page content"
        return f"Phase 8: Crawling competitors ({web_fetch_count - 1} of 5)"
    if tool_name == 'web_search':
        if web_search_count == 1:
            return "Phase 3a: Discovering company context"
        if web_search_count <= 3:
            return f"Phase 3b: Discovering competitors (search {web_search_count})"
        return f"Phase 9: GEO brand presence search ({web_search_count} queries)"
    if tool_name == 'read_reference':
        ref_name = tool_input.get('name', '')
        return f"Loading reference: {ref_name}"
    if tool_name == 'query_brain':
        check_id = tool_input.get('check_id', '')
        return f"Phase 13: Querying Sieve brain ({query_brain_count} citations attached) — {check_id}"
    if tool_name == 'persist_audit':
        return "Phase 14: Persisting audit + finalizing report"
    return f"Running {tool_name}"


def run_agent_loop(url: str, verbose: bool = False,
                    log_prefix: str = '',
                    progress_callback: Optional[Any] = None,
                    site_context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Drive the Claude tool-use loop until the agent emits <audit>...</audit>.

    Returns:
        {
          "audit": dict | None,        # parsed audit JSON
          "raw_final_text": str,       # last assistant text (for debugging)
          "tool_calls": [...],         # log of (name, input_summary, ms)
          "turns": int,
          "stop_reason": str,
          "input_tokens": int,
          "output_tokens": int,
          "errors": [str],
        }
    """
    try:
        from anthropic import Anthropic
    except ImportError:
        return _fail("anthropic SDK not installed")

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return _fail("ANTHROPIC_API_KEY not set")

    client = Anthropic(api_key=api_key)

    messages: List[Dict[str, Any]] = [{
        "role": "user",
        "content": (
            f"Audit this URL: {url}\n\n"
            "Execute all 15 phases in order. Use the tools as specified. "
            "When finished, your FINAL message must be ONLY a single JSON "
            "object wrapped in <audit>...</audit> tags."
            # Optional measured site-wide crawl signals (narrative-only —
            # empty string when absent, keeping the prompt byte-identical).
            + site_context_block(site_context)
        ),
    }]

    tool_call_log: List[Dict[str, Any]] = []
    errors: List[str] = []
    # Retain the FULL deterministic-scripts output (with each check's structured
    # `detail`) so the post-loop chain can join the OBSERVED half of the proof
    # onto the LLM's findings. The LLM only sees a slimmed copy.
    det_scripts_output: Optional[Dict[str, Any]] = None
    # Runtime-measured word counts of every successful server web_fetch —
    # *_tool_result blocks are API-authored (the model cannot fabricate them),
    # so these measures are runtime-owned evidence for the competitor producer.
    web_fetch_measures: List[Dict[str, Any]] = []
    input_tokens_total = 0
    output_tokens_total = 0
    cache_read_total = 0
    cache_creation_total = 0
    web_searches_total = 0
    raw_final_text = ""
    stop_reason = "unknown"
    turns = 0
    pause_turns = 0

    started = time.time()
    pfx = log_prefix or ''
    log.info('%sloop start url=%s', pfx, url)

    def _emit_progress(phase: str, tool_name: str = '',
                        turn_num: int = 0, tool_count: int = 0,
                        last_tool_ms: int = 0):
        """Fire progress_callback if provided. Safe — failures don't crash loop."""
        if progress_callback is None:
            return
        try:
            progress_callback({
                'phase': phase,
                'tool': tool_name,
                'turn': turn_num,
                'tool_count': tool_count,
                'elapsed_seconds': round(time.time() - started, 1),
                'last_tool_ms': last_tool_ms,
            })
        except Exception as e:
            log.warning('%sprogress_callback failed: %s', pfx, e)

    _emit_progress('Phase 0: Starting audit — initializing agent loop')

    for turn in range(MAX_AGENT_TURNS):
        turns = turn + 1

        if time.time() - started > TOTAL_BUDGET_SECONDS:
            msg = f"hit total budget {TOTAL_BUDGET_SECONDS}s at turn {turns}"
            errors.append(msg)
            log.warning('%s%s', pfx, msg)
            break

        cost_so_far = estimate_cost_usd(input_tokens_total, output_tokens_total)
        if cost_so_far > MAX_AUDIT_COST_USD:
            msg = (f"hit cost ceiling ${MAX_AUDIT_COST_USD:.2f} "
                   f"(est ${cost_so_far:.2f}) at turn {turns}")
            errors.append(msg)
            log.warning('%s%s', pfx, msg)
            break

        # Stream with app-level retry on transient failures (429/529/5xx/network).
        # Before this, a single transient error mid-audit scrapped the whole run
        # and all collected deterministic work. Prompt caching (system block +
        # a moving conversation-prefix breakpoint) removes the static prompt and
        # prior turns from full-price re-billing on every turn.
        response = None
        for attempt in range(MAX_STREAM_RETRIES + 1):
            try:
                _clear_cache_breakpoints(messages)
                _mark_cache_breakpoint(messages)
                with client.messages.stream(
                    model=MODEL,
                    max_tokens=MAX_TOKENS_PER_TURN,
                    system=_system_blocks(),
                    tools=TOOLS_SPEC,
                    messages=messages,
                    **_TEMP_KW,
                ) as stream:
                    # Watch the text stream for the final composing turn. The
                    # last turn emits the whole <audit> JSON with zero tool
                    # calls, so without this the phase label (and pct, which
                    # was tool-count-driven) froze on the LAST tool for the
                    # several minutes the model spends writing the report.
                    _compose_buf = ''
                    _compose_emitted = False
                    for _txt in stream.text_stream:
                        if _compose_emitted:
                            continue
                        _compose_buf += _txt
                        if '<audit' in _compose_buf or len(_compose_buf) > 4000:
                            _compose_emitted = True
                            _emit_progress(
                                'Phase 14: Composing final audit report',
                                '', turns, len(tool_call_log))
                    response = stream.get_final_message()
                break
            except Exception as e:
                if attempt < MAX_STREAM_RETRIES and _is_retryable(e):
                    backoff = min(2 ** attempt, 30)
                    log.warning('%smessages.stream retryable %s: %s — retry %d/%d in %ds',
                                pfx, type(e).__name__, e, attempt + 1,
                                MAX_STREAM_RETRIES, backoff)
                    time.sleep(backoff)
                    continue
                errors.append(f"messages.stream failed turn {turns}: "
                              f"{type(e).__name__}: {e}")
                log.error('%smessages.stream failed turn=%d: %s: %s',
                          pfx, turns, type(e).__name__, e)
                log.error('%s%s', pfx, traceback.format_exc())
                break
        if response is None:
            break

        usage = response.usage
        input_tokens_total += usage.input_tokens
        output_tokens_total += usage.output_tokens
        turn_cache_read = getattr(usage, 'cache_read_input_tokens', 0) or 0
        turn_cache_write = getattr(usage, 'cache_creation_input_tokens', 0) or 0
        cache_read_total += turn_cache_read
        cache_creation_total += turn_cache_write
        _stu = getattr(usage, 'server_tool_use', None)
        web_searches_total += (getattr(_stu, 'web_search_requests', 0) or 0) if _stu else 0
        stop_reason = response.stop_reason

        log.info('%sturn=%d stop=%s in=%d out=%d cache_read=%d cache_write=%d',
                 pfx, turns, stop_reason,
                 usage.input_tokens, usage.output_tokens,
                 turn_cache_read, turn_cache_write)
        if verbose:
            print(f"[turn {turns}] stop={stop_reason} "
                  f"in={response.usage.input_tokens} "
                  f"out={response.usage.output_tokens}", flush=True)

        # Append assistant turn (includes text + tool_use blocks) as plain
        # dicts — REQUIRED so _mark_cache_breakpoint can mark a block inside
        # it on pause_turn continuations (Pydantic blocks were unmarkable and
        # silently re-billed the whole conversation at full input price).
        messages.append({"role": "assistant",
                         "content": _dictify_content(response.content)})

        # Capture latest assistant text + log a preview of reasoning,
        # log server-tool invocations (which Anthropic dispatched itself).
        for block in response.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                txt = (block.text or "").strip()
                if txt:
                    raw_final_text = block.text
                    preview = txt[:240].replace("\n", " ")
                    log.info('%s  text: %s%s', pfx, preview,
                             '...' if len(txt) > 240 else '')
            elif btype in ("server_tool_use", "web_search_tool_use", "web_fetch_tool_use"):
                # Anthropic-dispatched server tool. Log so we know it ran.
                tname = getattr(block, "name", "server_tool")
                tin = getattr(block, "input", None) or {}
                log.info('%s  → [server] %s(%s)', pfx, tname, _short(tin))
                # Record in tool_call_log so phase derivation sees the right counts
                tool_call_log.append({
                    "turn": turns, "name": tname,
                    "input_keys": list(tin.keys()) if isinstance(tin, dict) else [],
                    "input_preview": _short(tin),
                    "ms": 0,
                    "result_size": 0,
                    "had_error": False,
                    "server_side": True,
                })
                # Emit progress for server tools too
                phase_label = _derive_phase_label(tname, tin if isinstance(tin, dict) else {}, tool_call_log)
                _emit_progress(phase_label, tname, turns, len(tool_call_log))
            elif btype in ("web_search_tool_result", "web_fetch_tool_result"):
                # Result of a server tool — Anthropic already executed it.
                # Log size if we can.
                content = getattr(block, "content", None)
                csize = (len(json.dumps(content, default=str))
                         if content is not None else 0)
                log.info('%s  ← [server] result %dB', pfx, csize)
                if btype == "web_fetch_tool_result":
                    m = _webfetch_measure(block)
                    if m:
                        web_fetch_measures.append(m)

        if stop_reason == "end_turn":
            log.info('%send_turn at turn=%d', pfx, turns)
            _emit_progress(f'Phase 14: Final audit JSON emitted (turn {turns})',
                           '', turns, len(tool_call_log))
            break

        # Anthropic server tools (web_search / web_fetch) can pause a long turn
        # with stop_reason='pause_turn'. The correct response is to resend the
        # conversation so the model continues — the assistant turn is already
        # appended above, so just loop again. Cap consecutive pauses so a
        # misbehaving turn can't spin forever.
        if stop_reason == "pause_turn":
            pause_turns += 1
            log.info('%spause_turn at turn=%d (#%d) — continuing', pfx, turns, pause_turns)
            if pause_turns > MAX_PAUSE_TURNS:
                msg = f"exceeded MAX_PAUSE_TURNS={MAX_PAUSE_TURNS} at turn {turns}"
                errors.append(msg)
                log.warning('%s%s', pfx, msg)
                break
            continue
        pause_turns = 0

        if stop_reason != "tool_use":
            msg = f"unexpected stop_reason '{stop_reason}' at turn {turns}"
            errors.append(msg)
            log.warning('%s%s', pfx, msg)
            break

        # Run all CLIENT tool_use blocks. Server tools (web_search, web_fetch)
        # are dispatched by Anthropic itself; their results appear inline as
        # *_tool_result blocks in the same assistant turn — we don't process
        # them here and the SDK handles serializing them in the next turn.
        tool_result_blocks: List[Dict[str, Any]] = []
        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            name = block.name
            if name in SERVER_TOOL_NAMES:
                continue
            tinput = block.input or {}

            t0 = time.time()
            try:
                result = dispatch_tool(name, tinput)
            except Exception as e:
                result = {"error": f"dispatch crash {type(e).__name__}: {e}"}
                log.error('%s  dispatch crash %s: %s\n%s', pfx, name, e,
                          traceback.format_exc())
            elapsed_ms = int((time.time() - t0) * 1000)

            # Capture the full deterministic output before it is slimmed for the
            # model — this is the source of the OBSERVED proof (per-check detail).
            if name == 'run_deterministic_scripts' and isinstance(result, dict) \
                    and 'error' not in result:
                det_scripts_output = result

            # Keep context tractable. Prefer structure-aware shrinking (drop
            # known-bulky, low-signal keys) over a blind byte-slice, which
            # would hand the model syntactically broken JSON of its own
            # foundational data.
            result_str = json.dumps(result, default=str, ensure_ascii=False)
            if len(result_str) > MAX_TOOL_RESULT_BYTES:
                slim = _slim_tool_result(name, result)
                result_str = json.dumps(slim, default=str, ensure_ascii=False)
            if len(result_str) > MAX_TOOL_RESULT_BYTES:
                # Still oversize — byte-slice as a last resort, but signal it.
                result_str = (
                    result_str[:MAX_TOOL_RESULT_BYTES]
                    + f'... [truncated {len(result_str) - MAX_TOOL_RESULT_BYTES} bytes — '
                    f'JSON intentionally incomplete]'
                )

            had_error = "error" in (result if isinstance(result, dict) else {})
            tool_call_log.append({
                "turn": turns, "name": name,
                "input_keys": list(tinput.keys()),
                "input_preview": _short(tinput),
                "ms": elapsed_ms,
                "result_size": len(result_str),
                "had_error": had_error,
            })

            log_method = log.error if had_error else log.info
            log_method('%s  → %s(%s) %dms %dB%s',
                       pfx, name, _short(tinput), elapsed_ms, len(result_str),
                       ' ERROR' if had_error else '')
            if had_error:
                err_msg = (result.get("error") if isinstance(result, dict) else "?")
                log.error('%s    error: %s', pfx, err_msg)

            # Emit live progress so the homepage can show "Phase X: ..." in spinner
            phase_label = _derive_phase_label(name, tinput, tool_call_log)
            _emit_progress(phase_label, name, turns, len(tool_call_log), elapsed_ms)
            if verbose:
                print(f"  → {name}({_short(tinput)}) "
                      f"[{elapsed_ms}ms, {len(result_str)}B]", flush=True)

            tool_result_blocks.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_str,
            })

        if not tool_result_blocks:
            msg = f"stop_reason=tool_use but no client tool_use blocks turn {turns}"
            errors.append(msg)
            log.warning('%s%s', pfx, msg)
            break

        messages.append({"role": "user", "content": tool_result_blocks})

    else:
        msg = f"hit MAX_AGENT_TURNS={MAX_AGENT_TURNS}"
        errors.append(msg)
        log.warning('%s%s', pfx, msg)

    audit, from_tag = _extract_audit_json(raw_final_text, errors)

    # Only accept the audit if the model actually finished (end_turn). When
    # the loop broke on budget / turn-cap / error, raw_final_text is the last
    # INTERMEDIATE message; a stray parseable JSON fragment there must not be
    # promoted to a "completed" audit and persisted. The explicit <audit> tag
    # is the only trustworthy signal of a deliberate final emission.
    if audit is not None and stop_reason != "end_turn" and not from_tag:
        errors.append(
            f"discarding loosely-parsed JSON: loop exited stop_reason='{stop_reason}' "
            f"without an <audit> tag — not a deliberate final audit")
        log.warning('%s%s', pfx, errors[-1])
        audit = None

    # Structural validation — a hollow object missing scoring/findings is not
    # a usable audit; route it to the error envelope rather than persisting it.
    if audit is not None:
        missing = _audit_missing_fields(audit)
        if missing:
            errors.append(f"extracted audit missing required fields: {', '.join(missing)}")
            log.warning('%s%s', pfx, errors[-1])
            audit = None

    if audit is not None:
        log.info('%sextracted audit JSON (%d top-level keys, %dB raw)',
                 pfx, len(audit), len(json.dumps(audit, default=str)))
    else:
        log.error('%sfailed to extract usable audit JSON. raw_final_text len=%d preview=%s',
                  pfx, len(raw_final_text), raw_final_text[:300].replace('\n', ' \\n '))

    log.info('%sloop done turns=%d stop=%s tokens=%d+%d cache=%dr/%dw searches=%d errors=%d',
             pfx, turns, stop_reason, input_tokens_total, output_tokens_total,
             cache_read_total, cache_creation_total, web_searches_total,
             len(errors))

    return {
        "audit": audit,
        "scripts_output": det_scripts_output,
        "web_fetch_measures": web_fetch_measures,
        "raw_final_text": raw_final_text[:5000],
        "tool_calls": tool_call_log,
        "turns": turns,
        "stop_reason": stop_reason,
        "input_tokens": input_tokens_total,
        "output_tokens": output_tokens_total,
        "cache_read_tokens": cache_read_total,
        "cache_creation_tokens": cache_creation_total,
        "web_search_requests": web_searches_total,
        "errors": errors,
        "duration_seconds": round(time.time() - started, 1),
    }


# -----------------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------------

def _fail(msg: str) -> Dict[str, Any]:
    return {
        "audit": None, "raw_final_text": "", "tool_calls": [],
        "turns": 0, "stop_reason": "error",
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
        "web_search_requests": 0,
        "errors": [msg], "duration_seconds": 0,
    }


def _short(d: Dict[str, Any], max_len: int = 80) -> str:
    s = json.dumps(d, default=str, ensure_ascii=False)
    return s[:max_len] + ("..." if len(s) > max_len else "")


def _slim_tool_result(name: str, result: Any) -> Any:
    """Shrink an oversize tool result by dropping bulky, low-signal keys while
    preserving valid JSON and every decision-relevant field. The big offender
    is the deterministic-scripts output, whose sitemap URL list and per-probe
    raw blobs dwarf the actual check verdicts."""
    if not isinstance(result, dict):
        return result
    slim = dict(result)
    sm = slim.get('sitemap_analysis')
    if isinstance(sm, dict) and isinstance(sm.get('sitemap_urls'), list):
        sm = dict(sm)
        n = len(sm['sitemap_urls'])
        sm['sitemap_urls'] = sm['sitemap_urls'][:20]
        sm['sitemap_urls_truncated'] = {'shown': min(20, n), 'total': n}
        slim['sitemap_analysis'] = sm
    # Per-probe HTML/raw fields are not present in the contract, but cloaking
    # delta arrays and reference dumps can be large — trim defensively.
    bev = slim.get('bots_eye_view')
    if isinstance(bev, dict) and isinstance(bev.get('cloaking_deltas'), list) \
            and len(bev['cloaking_deltas']) > 8:
        bev = dict(bev)
        bev['cloaking_deltas'] = bev['cloaking_deltas'][:8]
        slim['bots_eye_view'] = bev
    return slim


_AUDIT_TAG_RE = re.compile(r"<audit>\s*(\{.*?\})\s*</audit>", re.DOTALL)


_REQUIRED_AUDIT_FIELDS = ("scoring", "findings")


def _webfetch_measure(block: Any) -> Optional[Dict[str, Any]]:
    """Runtime word-count of one server web_fetch result: {url, words} or None.

    The *_tool_result block is API-authored (the model cannot fabricate one in
    its own output), and the count is computed HERE over the fetched text, so
    the result is runtime-owned evidence. Defensive: error results and
    unexpected shapes return None; never raises."""
    try:
        c = getattr(block, "content", None)
        if c is not None and hasattr(c, "model_dump"):
            c = c.model_dump()
        if not isinstance(c, dict) or c.get("type") != "web_fetch_result":
            return None  # fetch error, or a shape this parser doesn't know
        url = c.get("url")
        doc = c.get("content") if isinstance(c.get("content"), dict) else {}
        src = doc.get("source") if isinstance(doc.get("source"), dict) else {}
        text = src.get("data")
        if not (isinstance(url, str) and url and src.get("type") == "text"
                and isinstance(text, str) and text.strip()):
            return None
        # Crude tag strip so raw-HTML fetches count words, not markup.
        return {"url": url, "words": len(re.sub(r"<[^>]+>", " ", text).split())}
    except Exception:
        return None


COMPETITOR_CHECK_ID = "H1_content_depth_vs_competitors"


def _host_of(u: Any) -> str:
    """Lowercased host of a URL, www-stripped; '' when unparsable."""
    try:
        h = urlsplit(str(u or "")).netloc.split("@")[-1].split(":")[0].strip().lower()
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def _competitor_depth_check(target_url: str,
                            measures: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """RUNTIME COMPETITOR PRODUCER — the deterministic H-family check that
    makes 'observed-competitor' reachable in production. Built ONLY from the
    server web_fetch results the loop actually received (API-authored blocks,
    runtime-counted words — never the model's competitor_comparison
    transcription, which would be forgeable). Fetches of the target's own
    host measure the target; every other host is a competitor page, collapsed
    to its max word count. Honesty rule: no successfully-fetched off-host
    page means no check (None) — the H section then simply carries no
    runtime evidence.

    The comparative VERDICT stays with the model finding this joins onto
    (status here is 'na'); this entry carries the MEASUREMENT only."""
    tdom = _host_of(target_url)
    if not tdom:
        return None
    target_words: Optional[int] = None
    competitors: Dict[str, int] = {}
    for m in measures or []:
        if not isinstance(m, dict):
            continue
        host, words = _host_of(m.get("url")), m.get("words")
        if not host or isinstance(words, bool) or not isinstance(words, int) \
                or words <= 0:
            continue
        if host == tdom:
            target_words = max(target_words or 0, words)
        else:
            competitors[host] = max(competitors.get(host, 0), words)
    if not competitors:
        return None
    med = statistics.median(competitors.values())
    med = int(med) if float(med).is_integer() else round(med, 1)
    evidence = (f"runtime-measured competitor crawl: {len(competitors)} "
                f"page(s), median {med} words"
                + (f" vs target {target_words}" if target_words else ""))
    return {
        "status": "na",
        "evidence": evidence,
        "detail": {
            "url": target_url,
            "competitors_crawled": len(competitors),
            "competitor_domains": sorted(competitors),
            "competitor_words": competitors,
            "competitor_median_words": med,
            "target_words": target_words,
            "measured_by": "agent-loop web_fetch capture (runtime word count)",
        },
    }


def _join_observed(audit: Dict[str, Any], scripts_output: Any) -> Dict[str, Any]:
    """OBSERVED PROOF (D25, agent path) — join the deterministic scripts'
    structured detail back onto the LLM's findings by canonical check_id.
    all_checks may also carry runtime-produced entries (the 'runtime:' H1
    competitor measurement) — same honesty rules apply to those.
    Honesty rules (contract §5):

      - `observed` attaches ONLY when the join matched a real script check
        (no URL-only fallback: a finding the scripts never measured gets no
        observed block at all)
      - `observed` is runtime-owned: any model-emitted observed block is
        stripped before the join (counted in stripped_model_observed)
      - `measured_value` is the SCRIPT's evidence string, never the model's
        rewording of it
      - `method` derives from scoring.observed_method_for (evidence tier +
        off-page family + competitor detail), one of measured-on-page |
        observed-off-page | observed-competitor | model-judgment

    Mutates the audit in place; returns the stats dict for metadata."""
    from scoring import observed_method_for
    scripts_output = scripts_output if isinstance(scripts_output, dict) else {}
    all_checks = scripts_output.get("all_checks", {})
    all_checks = all_checks if isinstance(all_checks, dict) else {}
    by_clean = {str(k).split(":", 1)[-1]: v for k, v in all_checks.items()
                if isinstance(v, dict)}
    joined = unmatched = stripped = 0
    for f in (audit.get("findings") or []):
        if not isinstance(f, dict):
            continue
        # `observed` is RUNTIME-OWNED on the agent path (this join is its only
        # legitimate source): discard any model-emitted block so a fabricated
        # proof can't ride through the honesty gate below.
        if f.pop("observed", None) is not None:
            stripped += 1
        cd = by_clean.get(str(f.get("check_id")))
        if not isinstance(cd, dict):
            unmatched += 1  # no deterministic match -> no observed block
            continue
        detail = cd.get("detail") if isinstance(cd.get("detail"), dict) else None
        url = None
        if detail:
            url = detail.get("url") or detail.get("page_url") or detail.get("final_url")
        url = url or scripts_output.get("final_url") or scripts_output.get("url")
        measured = cd.get("evidence")
        f["observed"] = {
            "customer_url": url,
            "measured_value": measured if isinstance(measured, str) and measured else None,
            "detail": detail,
            "method": observed_method_for(f.get("check_id"), detail),
        }
        joined += 1
    return {"applied": True, "joined": joined, "unmatched": unmatched,
            "stripped_model_observed": stripped,
            "findings": len(audit.get("findings") or [])}


_FIX_MAX_CHARS = 500


def _backstop_finding_fixes(audit: Dict[str, Any]) -> Dict[str, Any]:
    """PER-FINDING FIX (contract §2) — every fail/warn finding should carry an
    executable `fix` (1–3 imperative steps, joined "; ", ≤500 chars). The model
    authors it in the findings block; this deterministic backstop covers the
    findings it left empty by joining the narrative fix written for the same
    check_id (top_5_fixes first, then all_fixes; the pre-rename id counts too
    since narrative fixes are keyed on the model's own spelling). No LLM, no
    invention: a finding with no matching narrative fix simply stays fix-less.

    Mutates the audit in place; returns the stats dict for metadata."""
    stats = {"applied": True, "eligible": 0, "model_provided": 0,
             "joined": 0, "unfilled": 0}
    findings = audit.get("findings")
    if not isinstance(findings, list):
        return stats

    narrative = audit.get("narrative")
    narrative = narrative if isinstance(narrative, dict) else {}
    by_check: Dict[str, str] = {}
    for fx in list(narrative.get("top_5_fixes") or []) + \
            list(narrative.get("all_fixes") or []):
        if not isinstance(fx, dict):
            continue
        cid = fx.get("check_id")
        if not isinstance(cid, str) or not cid or cid in by_check:
            continue
        text = str(fx.get("title") or "").strip()
        if not text:
            after = str(fx.get("after") or "").strip()
            text = after.splitlines()[0].strip() if after else ""
        if text:
            by_check[cid] = text[:_FIX_MAX_CHARS]

    for f in findings:
        if not isinstance(f, dict) or f.get("status") not in ("fail", "warn"):
            continue
        stats["eligible"] += 1
        fix = f.get("fix")
        if isinstance(fix, (list, tuple)):  # steps as a list -> join per contract
            fix = "; ".join(str(s).strip() for s in fix if str(s).strip())
        fix = fix.strip() if isinstance(fix, str) else ""
        if fix:
            f["fix"] = fix[:_FIX_MAX_CHARS]
            stats["model_provided"] += 1
            continue
        joined = by_check.get(str(f.get("check_id") or "")) or \
            by_check.get(str(f.get("original_check_id") or ""))
        if joined:
            f["fix"] = joined
            stats["joined"] += 1
        else:
            stats["unfilled"] += 1
    return stats


def _audit_missing_fields(audit: Dict[str, Any]) -> List[str]:
    """Return the required top-level fields absent or wrong-typed in `audit`.
    A usable audit must carry a scoring dict and a findings list."""
    missing = []
    if not isinstance(audit.get("scoring"), dict):
        missing.append("scoring")
    if not isinstance(audit.get("findings"), list):
        missing.append("findings")
    return missing


def _extract_audit_json(text: str, errors: List[str]):
    """Pull the JSON object from the final assistant text.

    Returns (audit_dict_or_None, from_tag). `from_tag` is True only when the
    JSON came from an explicit <audit>...</audit> wrapper — the caller treats
    the loose fallbacks as untrustworthy unless the loop ended on end_turn.
    """
    if not text:
        errors.append("no final text to parse")
        return None, False

    m = _AUDIT_TAG_RE.search(text)
    candidate = m.group(1) if m else None
    from_tag = candidate is not None

    if candidate is None:
        # Fallback 1: strip ```json fences
        stripped = re.sub(r"^```(?:json)?\s*", "", text.strip())
        stripped = re.sub(r"\s*```\s*$", "", stripped)
        if stripped.startswith("{") and stripped.endswith("}"):
            candidate = stripped

    if candidate is None:
        # Fallback 2: first {...} block in text
        brace_match = re.search(r"\{[\s\S]*\}", text)
        if brace_match:
            candidate = brace_match.group(0)

    if candidate is None:
        errors.append("no JSON object found in final text")
        return None, False

    try:
        return json.loads(candidate), from_tag
    except json.JSONDecodeError as e:
        errors.append(f"final JSON parse failed: {e}")
        return None, False


# -----------------------------------------------------------------------------
# PUBLIC ENTRYPOINT — analog of audit_pipeline.run_audit()
# -----------------------------------------------------------------------------

def run_audit_agent(url: str, output_dir: str = "./audits/",
                     verbose: bool = False,
                     progress_callback: Optional[Any] = None,
                     site_context: Optional[Dict[str, Any]] = None,
                     skip_visibility: bool = False) -> Dict[str, Any]:
    """Run the agent loop, attach metadata, render artifacts, return result.

    Output shape matches the existing `run_audit()` from audit_pipeline.py so
    main.py and the rest of the FastAPI service work without changes.

    site_context (optional, sanitized upstream): measured site-wide crawl
    signals for this page. Fed to the agent as narrative-only CONTEXT and
    recorded in metadata.site_context — it never enters scoring.

    skip_visibility: set by callers that measure AI visibility themselves
    (AnswerMonk's scoring phase already probes the same engines per session).
    Skips the post-loop measure_visibility sweep — everything else (checks,
    citations, scoring, report) is unaffected; the report's measured-
    visibility section simply doesn't render, exactly as when no engine
    keys are configured.
    """
    audit_id = str(uuid.uuid4())
    started = time.time()
    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if verbose:
        print(f"[agent] starting audit {audit_id} for {url}", flush=True)

    loop_result = run_agent_loop(url, verbose=verbose,
                                  log_prefix=f'[{audit_id[:8]}] ',
                                  progress_callback=progress_callback,
                                  site_context=site_context)

    audit = loop_result.get("audit")

    def _stage(phase: str, pct_hint: int) -> None:
        """Post-loop progress beacon. pct_hint pins the polled progressPct
        into the 90–99 finalization band (the loop's tool-count signal is
        exhausted once the agent stops calling tools)."""
        if progress_callback is None:
            return
        try:
            progress_callback({
                'phase': phase,
                'tool': '',
                'turn': loop_result.get('turns') or 0,
                'tool_count': len(loop_result.get('tool_calls', [])),
                'elapsed_seconds': round(time.time() - started, 1),
                'pct_hint': pct_hint,
            })
        except Exception as e:
            log.warning('[%s] stage progress callback failed: %s', audit_id[:8], e)

    domain = re.sub(r"^https?://", "", url).rstrip("/").split("/")[0]
    duration = round(time.time() - started, 1)

    # Build the wrapped result regardless of agent success
    if audit is None:
        # Agent failed to produce parseable output. Return error envelope.
        return {
            "audit_id": audit_id,
            "url": url,
            "domain": domain,
            "duration_seconds": duration,
            "error": "agent did not return valid audit JSON",
            "agent_errors": loop_result.get("errors", []),
            "agent_turns": loop_result.get("turns"),
            "agent_stop_reason": loop_result.get("stop_reason"),
            "raw_final_text_preview": loop_result.get("raw_final_text", "")[:1500],
            "tool_call_count": len(loop_result.get("tool_calls", [])),
            "input_tokens": loop_result.get("input_tokens"),
            "output_tokens": loop_result.get("output_tokens"),
            "cache_read_tokens": loop_result.get("cache_read_tokens"),
            "cache_creation_tokens": loop_result.get("cache_creation_tokens"),
            "web_search_requests": loop_result.get("web_search_requests"),
            "cost_usd_true": estimate_cost_usd_true(
                loop_result.get("input_tokens") or 0,
                loop_result.get("output_tokens") or 0,
                loop_result.get("cache_read_tokens") or 0,
                loop_result.get("cache_creation_tokens") or 0,
                loop_result.get("web_search_requests") or 0),
        }

    # Inject our authoritative metadata into the agent's audit
    audit["audit_id"] = audit_id
    audit["url"] = url
    audit["domain"] = domain
    audit["date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    audit["duration_seconds"] = duration

    md = audit.setdefault("metadata", {})
    md["version"] = md.get("version", "5.0-agent")
    md["model"] = MODEL
    md["tool_call_count"] = len(loop_result.get("tool_calls", []))
    md["agent_turns"] = loop_result.get("turns")
    md["agent_stop_reason"] = loop_result.get("stop_reason")
    md["input_tokens"] = loop_result.get("input_tokens")
    md["output_tokens"] = loop_result.get("output_tokens")
    md["cache_read_tokens"] = loop_result.get("cache_read_tokens")
    md["cache_creation_tokens"] = loop_result.get("cache_creation_tokens")
    md["web_search_requests"] = loop_result.get("web_search_requests")
    md["agent_errors"] = loop_result.get("errors", [])
    md["cost_usd"] = estimate_cost_usd(loop_result.get("input_tokens") or 0,
                                       loop_result.get("output_tokens") or 0)
    # Shadow true-cost: all four token buckets + web_search fees. ai_visibility
    # spend (if the sweep runs) is added below once its usage is known.
    md["cost_usd_true"] = estimate_cost_usd_true(
        loop_result.get("input_tokens") or 0,
        loop_result.get("output_tokens") or 0,
        loop_result.get("cache_read_tokens") or 0,
        loop_result.get("cache_creation_tokens") or 0,
        loop_result.get("web_search_requests") or 0)
    # Site-wide crawl context this audit ran with (roadmap 1.4). Tagged as
    # measured evidence, scoped to narrative severity only — additive metadata;
    # absent context leaves the record shape untouched.
    _sc_meta = site_context_metadata(site_context)
    if _sc_meta:
        md["site_context"] = _sc_meta

    _stage('Finalizing: verifying findings & evidence', 90)

    # ------------------------------------------------------------------
    # CHECK-ID VOCABULARY — the model emits variant check_ids between runs
    # (A10_robots_txt vs A10_robots_txt_crawling), breaking cross-run
    # comparability and the delta engine. Canonicalize against the
    # brain-mappings registry BEFORE scoring so scores, persistence and
    # deltas all see stable ids.
    # ------------------------------------------------------------------
    try:
        from check_vocab import normalize_check_ids
        audit, vocab_stats = normalize_check_ids(audit)
        md["check_id_normalization"] = vocab_stats
        if vocab_stats.get("renamed"):
            log.info('%scheck_ids canonicalized: %d renamed, %d unknown, %d collisions',
                     f'[{audit_id[:8]}] ', vocab_stats["renamed"],
                     vocab_stats["unknown"], vocab_stats["collisions"])
    except Exception as e:
        md["check_id_normalization"] = {"applied": False,
                                        "error": f"{type(e).__name__}: {e}"}
        log.error('%scheck_id normalization failed: %s', f'[{audit_id[:8]}] ', e)

    # ------------------------------------------------------------------
    # OBSERVED PROOF (D25, agent path) — the LLM emits findings without the
    # structured detail the deterministic scripts measured. Join it back by
    # check_id so a finding the scripts REALLY measured carries
    # observed{customer_url, measured_value, detail, method} — the 'on YOUR
    # page, X=Y' half of the proof. Honesty-gated (§5): no script match means
    # no observed block. Additive; runs after check_ids are canonical.
    # ------------------------------------------------------------------
    try:
        so = loop_result.get("scripts_output")
        so = dict(so) if isinstance(so, dict) else {}
        ac = so.get("all_checks")
        so["all_checks"] = dict(ac) if isinstance(ac, dict) else {}
        # RUNTIME COMPETITOR PRODUCER — inject the H1 competitor measurement
        # (runtime word counts of API-authored web_fetch results) so the join
        # below can attach a real 'observed-competitor' block to the model's
        # H1 finding. Copies above keep loop_result's scripts_output pristine.
        comp = _competitor_depth_check(url, loop_result.get("web_fetch_measures") or [])
        if comp:
            so["all_checks"]["runtime:" + COMPETITOR_CHECK_ID] = comp
        md["observed_join"] = _join_observed(audit, so)
        md["observed_join"]["competitor_producer"] = bool(comp)
    except Exception as e:
        md["observed_join"] = {"applied": False, "error": f"{type(e).__name__}: {e}"}
        log.error('%sobserved join failed: %s', f'[{audit_id[:8]}] ', e)
        # FAIL-CLOSED: a join that died mid-loop may have left model-emitted
        # observed blocks unstripped. Without a completed join no observed
        # block is trustworthy, so drop them all rather than let a fabricated
        # proof reach the shadow's evidence gate.
        _fs = audit.get("findings")
        for _f in (_fs if isinstance(_fs, list) else []):
            if isinstance(_f, dict):
                _f.pop("observed", None)

    # ------------------------------------------------------------------
    # PER-FINDING FIX BACKSTOP (§2) — fail/warn findings the model left
    # without a `fix` get the narrative fix authored for the same check_id
    # (deterministic join, no LLM). Runs after check-id canonicalization so
    # original_check_id is available for renamed findings.
    # ------------------------------------------------------------------
    try:
        md["fix_backstop"] = _backstop_finding_fixes(audit)
    except Exception as e:
        md["fix_backstop"] = {"applied": False, "error": f"{type(e).__name__}: {e}"}
        log.error('%sfix backstop failed: %s', f'[{audit_id[:8]}] ', e)

    # ------------------------------------------------------------------
    # DETERMINISTIC RULE BINDING (mode C) — for MEASURED checks (status came
    # from the deterministic scripts, not the LLM) bind a curated rule VERBATIM
    # when its predicate holds. binding_verified=True by construction; these
    # bindings can carry scoring weight (Phase 4). Runs after check_id
    # canonicalization so the base letter is stable; unmapped bases get no
    # binding, never a wrong one.
    # ------------------------------------------------------------------
    try:
        from rule_eval import evaluate_measured_bindings
        audit, rule_eval_stats = evaluate_measured_bindings(audit)
        md["rule_binding"] = rule_eval_stats
        log.info('%sdeterministic rule bindings: %d bound of %d measured findings',
                 f'[{audit_id[:8]}] ', rule_eval_stats.get("bound", 0),
                 rule_eval_stats.get("measured_findings", 0))
    except Exception as e:
        md["rule_binding"] = {"applied": False, "error": f"{type(e).__name__}: {e}"}
        log.error('%sdeterministic rule binding failed: %s', f'[{audit_id[:8]}] ', e)

    # ------------------------------------------------------------------
    # DETERMINISTIC CITATIONS — Phase 13 is a runtime responsibility now:
    # every fail/warn finding gets the top-3 query_brain citations,
    # replacing whatever subset the model chose (it cited only ~17% of
    # eligible checks, inconsistently between runs). Runs after check_id
    # canonicalization so lookups hit stable ids; the grounding pass below
    # then verifies/flags every attached citation.
    # ------------------------------------------------------------------
    try:
        from citation_attach import attach_citations
        audit, attach_stats = attach_citations(audit)
        md["citation_attachment"] = attach_stats
        log.info('%sdeterministic citations: %d/%d checks cited (%d attached, %d LLM lists replaced)',
                 f'[{audit_id[:8]}] ', attach_stats.get("checks_cited", 0),
                 attach_stats.get("checks_eligible", 0),
                 attach_stats.get("citations_attached", 0),
                 attach_stats.get("llm_lists_replaced", 0))
    except Exception as e:
        from sieve_brain import SieveLiveError
        if isinstance(e, SieveLiveError):
            raise  # SIEVE_STRICT: fail the audit rather than ship snapshot citations
        md["citation_attachment"] = {"applied": False,
                                     "error": f"{type(e).__name__}: {e}"}
        log.error('%scitation attachment failed: %s', f'[{audit_id[:8]}] ', e)

    # ------------------------------------------------------------------
    # AUTHORITATIVE SCORING — the model only CLASSIFIES checks (pass/warn/
    # fail/na). Python GRADES. recompute_scores() overwrites whatever the
    # model put in `scoring` with numbers derived deterministically from the
    # per-check statuses; validate_audit() then clamps/enum-guards every
    # score-bearing field so nothing malformed can reach persistence or the
    # public renderer (closes the LLM-arithmetic + score-forgery/XSS vector).
    # ------------------------------------------------------------------
    # `scoring_shadow` is RUNTIME-OWNED metadata: discard anything the model
    # emitted under these keys so a forged shadow can't survive the
    # recompute-failure path below (where they would otherwise be kept as-is).
    _stage('Finalizing: computing deterministic scores', 93)
    md.pop("scoring_shadow", None)
    md.pop("scoring_shadow_reason", None)
    try:
        from scoring import finalize_scoring
        audit = finalize_scoring(audit)
        md["scoring_authority"] = "runtime-deterministic"
        # SHADOW dual-score rides the metadata column: fetch_audit reassembles
        # `scoring` from flat DB columns, which would silently drop
        # scoring.shadow on reload. Renderers fall back to metadata.
        _sc = audit.get("scoring") if isinstance(audit.get("scoring"), dict) else {}
        md["scoring_shadow"] = _sc.get("shadow")
        if _sc.get("shadow_reason"):
            md["scoring_shadow_reason"] = _sc.get("shadow_reason")
    except Exception as e:
        # Never let a scoring bug drop a completed audit — but flag loudly.
        md["scoring_authority"] = f"MODEL-REPORTED (recompute failed: {type(e).__name__}: {e})"
        log.error('%sscore recompute failed: %s\n%s',
                  f'[{audit_id[:8]}] ', e, traceback.format_exc())

    # ------------------------------------------------------------------
    # CITATION RE-GROUNDING — the model only MAPS checks to citation ids.
    # Quoted rule text, source org/url and tier are re-fetched from the
    # brain by (kind, id) and overwritten here, so every quote that reaches
    # the renderers and persistence is verbatim-by-construction (the LLM
    # copy step measurably paraphrases ~half of them otherwise).
    # ------------------------------------------------------------------
    try:
        from citation_grounding import reground_citations
        audit, ground_stats = reground_citations(audit)
        md["citation_grounding"] = ground_stats
        if ground_stats.get("text_corrected"):
            log.info('%sre-grounded citations: %d corrected, %d live, %d snapshot, %d unresolved',
                     f'[{audit_id[:8]}] ', ground_stats["text_corrected"],
                     ground_stats["regrounded_live"], ground_stats["regrounded_snapshot"],
                     ground_stats["unresolved"])
    except Exception as e:
        from sieve_brain import SieveLiveError
        if isinstance(e, SieveLiveError):
            raise  # SIEVE_STRICT: fail the audit rather than ship snapshot citations
        md["citation_grounding"] = {"applied": False,
                                    "error": f"{type(e).__name__}: {e}"}
        log.error('%scitation re-grounding failed: %s\n%s',
                  f'[{audit_id[:8]}] ', e, traceback.format_exc())

    # ------------------------------------------------------------------
    # CITATION ENTAILMENT — the DISPLAY decision moves off the lexical
    # supports_finding gate (measured 50.4% strict displayed-proof
    # precision, 30.3% missed-support on the 2026-07-19 labelled set) to a
    # cached claude-haiku judgment stamped per citation: supports (proof) /
    # related (collapsed see-also) / unrelated (hidden, kept in JSON) /
    # unjudged (renderers fall back to supports_finding). Runs AFTER
    # re-grounding so judgments read the final verbatim text. Fail-safe:
    # no key / API error / timeout stamps 'unjudged' — an audit is never
    # blocked on the judge. Default ON; CITATION_ENTAILMENT=0 disables.
    # ------------------------------------------------------------------
    if os.getenv('CITATION_ENTAILMENT', '1') not in ('0', 'false', 'False'):
        _stage('Finalizing: judging citation relevance', 95)
        try:
            from citation_entailment import judge_citations
            ent_stats = judge_citations(audit.get("findings") or [])
            md["citation_entailment"] = ent_stats
            log.info('%scitation entailment: %d judged (%d cached, %d api) — '
                     '%d supports / %d related / %d unrelated / %d unjudged',
                     f'[{audit_id[:8]}] ', ent_stats.get("judged", 0),
                     ent_stats.get("cache_hits", 0), ent_stats.get("api_calls", 0),
                     ent_stats.get("supports", 0), ent_stats.get("related", 0),
                     ent_stats.get("unrelated", 0), ent_stats.get("unjudged", 0))
        except Exception as e:
            md["citation_entailment"] = {"applied": False,
                                         "error": f"{type(e).__name__}: {e}"}
            log.error('%scitation entailment failed: %s', f'[{audit_id[:8]}] ', e)
    else:
        md["citation_entailment"] = {"applied": False,
                                     "reason": "disabled via CITATION_ENTAILMENT=0"}

    # ------------------------------------------------------------------
    # BINDING VERIFICATION (mode B) — any LLM-authored bound_rule must survive
    # three code tests: the id exists, it is a MEMBER of the candidate pool
    # retrieval returns for this finding (kills hallucinated/prose-scraped ids),
    # and the finding's evidence topically supports the rule. Never drops a
    # finding; an unverified binding is flagged and excluded from scoring
    # weight. Deterministic (mode-C) bindings pass through pre-verified. No-op
    # until the model emits bound_rule (system_prompt Phase 13).
    # ------------------------------------------------------------------
    try:
        from binding_gate import verify_bindings
        from rule_eval import _default_resolver
        _resolve = _default_resolver()

        def _candidates_for(f):
            try:
                from tools import query_brain
                cls = audit.get("classification") or {}
                res = query_brain(f.get("check_id"), cls.get("page_type") or "homepage",
                                  cls.get("industry") or "other", 8,
                                  evidence=f.get("evidence") if isinstance(f.get("evidence"), str) else None)
                out = set()
                for c in (res or {}).get("citations", []):
                    if isinstance(c, dict) and c.get("id") is not None:
                        out.add((c.get("kind"), str(c.get("id"))))
                return out
            except Exception:
                return set()

        audit, gate_stats = verify_bindings(audit, _resolve, _candidates_for)
        md["binding_verification"] = gate_stats
        if gate_stats.get("bindings"):
            log.info('%sbinding gate: %d verified, %d not-found, %d not-candidate, %d unsupported',
                     f'[{audit_id[:8]}] ', gate_stats.get("verified", 0),
                     gate_stats.get("not_found", 0), gate_stats.get("not_candidate", 0),
                     gate_stats.get("unsupported", 0))
    except Exception as e:
        md["binding_verification"] = {"applied": False, "error": f"{type(e).__name__}: {e}"}
        log.error('%sbinding verification failed: %s', f'[{audit_id[:8]}] ', e)

    # ------------------------------------------------------------------
    # FIX-SOURCE RESOLUTION — the top-fix WHY paragraphs reference brain
    # objects inline ("Sieve Principle #1109"); resolve each reference to
    # its actual source (org, URL, verified date) so the claim links to
    # its receipt instead of a bare id.
    # ------------------------------------------------------------------
    try:
        from citation_grounding import ground_fix_sources
        audit, fix_src_stats = ground_fix_sources(audit)
        md["fix_sources"] = fix_src_stats
        if fix_src_stats.get("resolved"):
            log.info('%sfix sources resolved: %d of %d refs',
                     f'[{audit_id[:8]}] ', fix_src_stats["resolved"],
                     fix_src_stats.get("refs_found", 0))
    except Exception as e:
        md["fix_sources"] = {"applied": False, "error": f"{type(e).__name__}: {e}"}
        log.error('%sfix-source grounding failed: %s', f'[{audit_id[:8]}] ', e)

    # ------------------------------------------------------------------
    # MEASURED AI VISIBILITY — execute the audit's own test queries
    # against real answer engines (per available API keys), record who
    # gets cited/mentioned, compute share of voice vs the crawled
    # competitors, and log every raw answer to public.ai_answer_runs.
    # Replaces inference with measurement; no-ops safely without keys.
    # ------------------------------------------------------------------
    if skip_visibility:
        # Caller (AnswerMonk) measures AI visibility itself in its scoring
        # phase — running the sweep here would probe the same engines twice
        # per audit and produce numbers the caller never reads.
        md["ai_visibility"] = {"applied": False,
                               "skipped": "caller measures visibility (skip_visibility)"}
        log.info('%sai visibility skipped: caller measures visibility',
                 f'[{audit_id[:8]}] ')
    else:
        _stage('Finalizing: measuring AI visibility', 96)
        try:
            from ai_visibility import measure_visibility
            audit, vis_stats = measure_visibility(audit)
            md["ai_visibility"] = vis_stats
            if vis_stats.get("applied"):
                log.info('%smeasured AI visibility: engines=%s runs=%d errors=%d',
                         f'[{audit_id[:8]}] ', ','.join(vis_stats.get("engines", [])),
                         vis_stats.get("runs_total", 0), vis_stats.get("errors", 0))
            # Fold the sweep's own spend into the shadow true-cost figure.
            _vis_cost = (vis_stats.get("usage") or {}).get("est_cost_usd") \
                if isinstance(vis_stats, dict) else None
            if _vis_cost:
                md["ai_visibility_cost_usd"] = _vis_cost
                md["cost_usd_true"] = round(
                    (md.get("cost_usd_true") or 0) + _vis_cost, 4)
        except Exception as e:
            md["ai_visibility"] = {"applied": False,
                                   "error": f"{type(e).__name__}: {e}"}
            log.error('%sai visibility failed: %s', f'[{audit_id[:8]}] ', e)

    # Render artifacts using the existing renderers from audit_pipeline.py
    # (they consume the same shape we produce).
    _stage('Finalizing: rendering report artifacts', 97)
    try:
        from audit_pipeline import render_markdown_report, render_pdf_summary
    except ImportError as e:
        audit["render_warning"] = f"renderers unavailable: {e}"
        render_markdown_report = None
        render_pdf_summary = None

    slug = domain.replace(".", "-")
    base_path = out_dir / f"{slug}-{audit_id[:8]}"

    json_path = base_path.with_suffix(".json")
    json_path.write_text(json.dumps(audit, indent=2, ensure_ascii=False, default=str))
    audit["json_path"] = str(json_path)

    if render_markdown_report:
        try:
            md_text = render_markdown_report(_render_compat(audit))
            md_path = base_path.with_suffix(".md")
            md_path.write_text(md_text)
            audit["md_path"] = str(md_path)
        except Exception as e:
            audit["md_render_error"] = f"{type(e).__name__}: {e}"
            audit["md_path"] = None
    else:
        audit["md_path"] = None

    if render_pdf_summary:
        try:
            pdf_path = render_pdf_summary(_render_compat(audit), base_path)
            audit["pdf_path"] = str(pdf_path) if pdf_path else None
        except Exception as e:
            audit["pdf_render_error"] = f"{type(e).__name__}: {e}"
            audit["pdf_path"] = None
    else:
        audit["pdf_path"] = None

    # ------------------------------------------------------------------
    # Persist to Supabase — POST-LOOP, with the real audit_id + complete
    # audit dict. This is NOT an agent tool: the agent never knows the real
    # audit_id (it's injected above) and the full audit only exists here.
    # Best-effort: a persistence failure never fails the audit itself.
    # ------------------------------------------------------------------
    _stage('Finalizing: persisting audit', 99)
    try:
        from tools import persist_audit
        persist_result = persist_audit(audit)
        md["persistence"] = persist_result
        if persist_result.get("persisted"):
            log.info('%spersisted to Supabase: row_id=%s findings=%d',
                     f'[{audit_id[:8]}] ',
                     persist_result.get("supabase_row_id"),
                     persist_result.get("findings_persisted", 0))
        else:
            log.warning('%snot persisted: %s',
                        f'[{audit_id[:8]}] ',
                        persist_result.get("error") or persist_result.get("note"))
    except Exception as e:
        md["persistence"] = {"persisted": False,
                             "error": f"{type(e).__name__}: {e}"}
        log.error('%spersist_audit crashed: %s\n%s',
                  f'[{audit_id[:8]}] ', e, traceback.format_exc())

    # Push the result to AnswerMonk's visibility report (best-effort, no-op
    # unless ANSWERMONK_BASE_URL + EXTERNAL_AUDITOR_KEY are set). Runs even if
    # Supabase persistence is unconfigured — the audit dict is complete here
    # either way, and the receiver upserts by audit_id so re-posts are safe.
    try:
        from persistence import post_to_answermonk
        md["answermonk_sync"] = post_to_answermonk(audit)
    except Exception as e:
        md["answermonk_sync"] = {"posted": False,
                                 "error": f"{type(e).__name__}: {e}"}
        log.error('%sanswermonk sync crashed: %s',
                  f'[{audit_id[:8]}] ', e)

    if verbose:
        print(f"[agent] complete in {duration}s, "
              f"{md['tool_call_count']} tool calls, "
              f"{md['input_tokens']}+{md['output_tokens']} tokens, "
              f"persisted={md.get('persistence', {}).get('persisted')}",
              flush=True)

    return audit


def _render_compat(audit: Dict[str, Any]) -> Dict[str, Any]:
    """Adapt the agent's audit shape to what render_markdown_report expects.

    The legacy renderer wants: scripts_output, brain_stats, classification,
    scoring, narrative, findings. The agent produces the same fields, but
    'scripts_output' is nested under bots_eye_view differently. Stitch them.
    """
    return {
        "audit_id": audit.get("audit_id"),
        "url": audit.get("url"),
        "domain": audit.get("domain"),
        "date": audit.get("date"),
        "duration_seconds": audit.get("duration_seconds"),
        "classification": audit.get("classification", {}),
        "scoring": audit.get("scoring", {}),
        "findings": audit.get("findings", []),
        "narrative": audit.get("narrative", {}),
        "scripts_output": {
            "bots_eye_view": audit.get("bots_eye_view", {}),
            "all_checks": {f["check_id"]: f for f in audit.get("findings", [])},
        },
        "brain_stats": audit.get("metadata", {}).get("brain_stats", {}),
    }


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Run the agent-mode audit pipeline.")
    p.add_argument("url")
    p.add_argument("--output", "-o", default="./audits/")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    result = run_audit_agent(args.url, args.output, verbose=args.verbose)
    print(json.dumps({k: v for k, v in result.items()
                      if k not in ("findings", "scripts_output", "bots_eye_view")},
                     indent=2, ensure_ascii=False, default=str)[:5000])
    if result.get("error"):
        sys.exit(1)
