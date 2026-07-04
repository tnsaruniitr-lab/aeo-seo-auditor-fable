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
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

from tools import TOOLS_SPEC, dispatch_tool, SERVER_TOOL_NAMES
from system_prompt import SYSTEM_PROMPT

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
# Cached-read tokens are ~10% of input price; we don't separate them in the
# usage rollup here, so this estimate is a conservative upper bound.
MAX_AUDIT_COST_USD = float(os.getenv('MAX_AUDIT_COST_USD', '2.50'))

# Transient API failures that are worth retrying rather than scrapping a
# half-finished (paid) audit. 429 = rate limit, 529 = overloaded, 5xx = server.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 529})
MAX_STREAM_RETRIES = int(os.getenv('MAX_STREAM_RETRIES', '4'))


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Rough per-audit cost estimate in USD (list price, no cache discount)."""
    return round(
        input_tokens / 1_000_000 * PRICE_INPUT_PER_MTOK
        + output_tokens / 1_000_000 * PRICE_OUTPUT_PER_MTOK,
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


def _mark_cache_breakpoint(messages: List[Dict[str, Any]]) -> None:
    """Add a cache_control breakpoint to the last block of the last message so
    the entire conversation prefix (system + tools + all prior turns) is served
    from cache on the next call. Mutates in place; safe on both string and
    list-shaped message content."""
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
    if isinstance(content, list) and content:
        blk = content[-1]
        if isinstance(blk, dict):
            blk["cache_control"] = {"type": "ephemeral"}


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
                    progress_callback: Optional[Any] = None) -> Dict[str, Any]:
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
        ),
    }]

    tool_call_log: List[Dict[str, Any]] = []
    errors: List[str] = []
    input_tokens_total = 0
    output_tokens_total = 0
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
                ) as stream:
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

        input_tokens_total += response.usage.input_tokens
        output_tokens_total += response.usage.output_tokens
        stop_reason = response.stop_reason

        log.info('%sturn=%d stop=%s in=%d out=%d',
                 pfx, turns, stop_reason,
                 response.usage.input_tokens, response.usage.output_tokens)
        if verbose:
            print(f"[turn {turns}] stop={stop_reason} "
                  f"in={response.usage.input_tokens} "
                  f"out={response.usage.output_tokens}", flush=True)

        # Append assistant turn (includes text + tool_use blocks)
        messages.append({"role": "assistant", "content": response.content})

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

    log.info('%sloop done turns=%d stop=%s tokens=%d+%d errors=%d',
             pfx, turns, stop_reason, input_tokens_total, output_tokens_total,
             len(errors))

    return {
        "audit": audit,
        "raw_final_text": raw_final_text[:5000],
        "tool_calls": tool_call_log,
        "turns": turns,
        "stop_reason": stop_reason,
        "input_tokens": input_tokens_total,
        "output_tokens": output_tokens_total,
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
                     progress_callback: Optional[Any] = None) -> Dict[str, Any]:
    """Run the agent loop, attach metadata, render artifacts, return result.

    Output shape matches the existing `run_audit()` from audit_pipeline.py so
    main.py and the rest of the FastAPI service work without changes.
    """
    audit_id = str(uuid.uuid4())
    started = time.time()
    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if verbose:
        print(f"[agent] starting audit {audit_id} for {url}", flush=True)

    loop_result = run_agent_loop(url, verbose=verbose,
                                  log_prefix=f'[{audit_id[:8]}] ',
                                  progress_callback=progress_callback)

    audit = loop_result.get("audit")

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
    md["agent_errors"] = loop_result.get("errors", [])
    md["cost_usd"] = estimate_cost_usd(loop_result.get("input_tokens") or 0,
                                       loop_result.get("output_tokens") or 0)

    # ------------------------------------------------------------------
    # AUTHORITATIVE SCORING — the model only CLASSIFIES checks (pass/warn/
    # fail/na). Python GRADES. recompute_scores() overwrites whatever the
    # model put in `scoring` with numbers derived deterministically from the
    # per-check statuses; validate_audit() then clamps/enum-guards every
    # score-bearing field so nothing malformed can reach persistence or the
    # public renderer (closes the LLM-arithmetic + score-forgery/XSS vector).
    # ------------------------------------------------------------------
    try:
        from scoring import finalize_scoring
        audit = finalize_scoring(audit)
        md["scoring_authority"] = "runtime-deterministic"
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
        md["citation_grounding"] = {"applied": False,
                                    "error": f"{type(e).__name__}: {e}"}
        log.error('%scitation re-grounding failed: %s\n%s',
                  f'[{audit_id[:8]}] ', e, traceback.format_exc())

    # Render artifacts using the existing renderers from audit_pipeline.py
    # (they consume the same shape we produce).
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
