#!/usr/bin/env python3
"""Tests for the Tier-0/1 cost work: true-cost shadow accounting, the
pause_turn cache-breakpoint fix, and the skip_visibility seam.

Quality invariants proven here:
  - _dictify_content produces the SAME wire content the SDK would have sent
    (byte-identical model inputs → the cache fix cannot change audit output)
  - _mark_cache_breakpoint now marks blocks inside a dictified assistant turn
    (the pause_turn regression) and never marks a non-cacheable block type
  - estimate_cost_usd is UNCHANGED (the abort ceiling's behavior is frozen);
    estimate_cost_usd_true adds the cache + search buckets on top
  - the Phase 13 prompt no longer mandates per-finding query_brain sweeps /
    verbatim citation copying (the runtime attaches citations; removing the
    mandate removes only discarded work)
  - StartAuditRequest accepts skipVisibility / skip_visibility / absent

Run from the service dir:
    cd service && python3 ../tests/test_cost_tier01.py
Prints COST_TIER01_OK on success, exits non-zero on failure.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'service'))

import agent

# ---------------------------------------------------------------------------
# 1) estimate_cost_usd frozen; estimate_cost_usd_true adds the hidden buckets
# ---------------------------------------------------------------------------
assert agent.estimate_cost_usd(1_000_000, 100_000) == round(3.0 + 1.5, 4)

true_cost = agent.estimate_cost_usd_true(
    100_000, 10_000,
    cache_read_tokens=1_000_000, cache_creation_tokens=200_000, web_searches=8)
expected = round(100_000/1e6*3.0 + 10_000/1e6*15.0
                 + 1_000_000/1e6*0.30 + 200_000/1e6*3.75 + 8*0.01, 4)
assert true_cost == expected, (true_cost, expected)
# true >= nominal always (it's a superset of the same buckets)
assert agent.estimate_cost_usd_true(5000, 5000) == agent.estimate_cost_usd(5000, 5000)


# ---------------------------------------------------------------------------
# 2) _dictify_content: Pydantic-like blocks -> wire dicts, dicts pass through
# ---------------------------------------------------------------------------
class FakeBlock:
    """Stands in for an SDK Pydantic content block."""
    def __init__(self, payload):
        self._payload = payload

    def model_dump(self, mode="python", exclude_none=False):
        d = dict(self._payload)
        if exclude_none:
            d = {k: v for k, v in d.items() if v is not None}
        return d


wire = {"type": "text", "text": "hello", "citations": None}
out = agent._dictify_content([FakeBlock(wire), {"type": "tool_use", "id": "t1",
                                                "name": "query_brain", "input": {}}])
assert out[0] == {"type": "text", "text": "hello"}, out[0]   # None dropped, content identical
assert out[1]["type"] == "tool_use"                           # dict passes through untouched
assert isinstance(out[0], dict)
# json round-trip is stable (what the SDK serializes is what we stored)
assert json.loads(json.dumps(out[0])) == out[0]


# ---------------------------------------------------------------------------
# 3) _mark_cache_breakpoint — the pause_turn regression
# ---------------------------------------------------------------------------
# 3a. BEFORE-fix failure mode: an assistant message of Pydantic blocks got no
#     marker. AFTER: we append dictified content, so the marker lands.
assistant_msg = {"role": "assistant",
                 "content": agent._dictify_content(
                     [FakeBlock({"type": "text", "text": "working..."}),
                      FakeBlock({"type": "tool_use", "id": "x", "name": "web_fetch",
                                 "input": {"url": "https://e.com"}})])}
msgs = [{"role": "user", "content": "start"}, assistant_msg]
agent._clear_cache_breakpoints(msgs)
agent._mark_cache_breakpoint(msgs)
marked = [b for b in assistant_msg["content"]
          if isinstance(b, dict) and b.get("cache_control")]
assert len(marked) == 1, f"expected exactly one marked block, got {len(marked)}"
assert marked[0]["type"] == "tool_use"   # newest cacheable block

# 3b. Non-cacheable trailing blocks are skipped, not marked (a bad marker
#     would 400 the request — worse than a cache miss).
msg2 = {"role": "assistant", "content": [
    {"type": "text", "text": "a"},
    {"type": "thinking", "thinking": "...", "signature": "s"},
]}
msgs2 = [msg2]
agent._mark_cache_breakpoint(msgs2)
assert "cache_control" not in msg2["content"][1], "thinking block must not be marked"
assert msg2["content"][0].get("cache_control") == {"type": "ephemeral"}

# 3c. All-non-cacheable content and no earlier message: nothing marked, no crash.
msg3 = {"role": "assistant", "content": [{"type": "thinking", "thinking": "x",
                                          "signature": "s"}]}
agent._mark_cache_breakpoint([msg3])
assert "cache_control" not in msg3["content"][0]

# 3c2. Last message all-server-blocks: marker falls back to the NEWEST
#      cacheable block in an EARLIER message (otherwise the just-cleared
#      conversation would have no breakpoint at all).
prev = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t",
                                     "content": "ok"}]}
tail = {"role": "assistant", "content": [{"type": "server_tool_use", "id": "s1",
                                          "name": "web_search", "input": {}}]}
agent._mark_cache_breakpoint([prev, tail])
assert prev["content"][0].get("cache_control") == {"type": "ephemeral"}
assert "cache_control" not in tail["content"][0]

# 3d. String content still upgrades to a marked text block (unchanged path).
msg4 = {"role": "user", "content": "plain"}
agent._mark_cache_breakpoint([msg4])
assert msg4["content"][0]["cache_control"] == {"type": "ephemeral"}

# 3e. clear removes every stale marker so only one moving breakpoint lives.
agent._clear_cache_breakpoints(msgs)
assert not any(isinstance(b, dict) and b.get("cache_control")
               for m in msgs for b in (m["content"] if isinstance(m["content"], list) else []))


# 3f. Revert tripwire: the fix only works if the LOOP's append site dictifies
#     the response content and the ceiling stays on the uncached estimator —
#     assert against the actual loop source, not just the helpers.
import inspect
loop_src = inspect.getsource(agent.run_agent_loop)
assert "_dictify_content(response.content)" in loop_src, \
    "run_agent_loop must append dictified assistant content (pause_turn cache fix)"
assert "estimate_cost_usd(input_tokens_total, output_tokens_total)" in loop_src, \
    "abort ceiling must stay on the uncached estimator (frozen guardrail)"


# ---------------------------------------------------------------------------
# 4) _fail / loop-result contract carries the new usage fields
# ---------------------------------------------------------------------------
f = agent._fail("nope")
for key in ("cache_read_tokens", "cache_creation_tokens", "web_search_requests"):
    assert f[key] == 0, key


# ---------------------------------------------------------------------------
# 5) Phase 13 prompt: mandate removed, investigation allowed, contract empty
# ---------------------------------------------------------------------------
from system_prompt import SYSTEM_PROMPT

assert "Citations — handled by the runtime" in SYSTEM_PROMPT
assert "For every failed/warned check, call" not in SYSTEM_PROMPT, \
    "per-finding query_brain sweep mandate must be gone"
assert "attach the FULL citation" not in SYSTEM_PROMPT, \
    "verbatim citation-copy mandate must be gone"
assert '"citations": []' in SYSTEM_PROMPT, \
    "output contract must instruct an empty citations array"
# query_brain stays available for investigation (tool list + investigate note)
assert "query_brain(check_id, page_type, industry" in SYSTEM_PROMPT
assert "INVESTIGATING" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# 6) ai_visibility usage summary arithmetic
# ---------------------------------------------------------------------------
import ai_visibility as av

summary = av._usage_summary({
    'anthropic': {'input_tokens': 1_000_000, 'output_tokens': 100_000,
                  'web_searches': 10},
    'openai': {'input_tokens': 1_000_000, 'output_tokens': 100_000,
               'web_searches': 8},
})
exp = round(3.0 + 1.5 + 0.10 + 0.15 + 0.06 + 0.08, 4)
assert summary['est_cost_usd'] == exp, (summary, exp)
assert summary['per_engine']['anthropic']['web_searches'] == 10

# The self-test's stub engines return no usage — summary degrades to zero.
assert av._usage_summary({})['est_cost_usd'] == 0.0


# ---------------------------------------------------------------------------
# 7) skip_visibility request seam (needs pydantic; skipped loudly without it)
# ---------------------------------------------------------------------------
try:
    import pydantic  # noqa: F401
    HAVE_PYDANTIC = True
except ImportError:
    HAVE_PYDANTIC = False
    print("  (pydantic unavailable — StartAuditRequest checks skipped)")

if HAVE_PYDANTIC:
    os.environ.setdefault('AUDIT_MODE', 'deterministic')
    from main import StartAuditRequest
    r = StartAuditRequest(url='https://e.com', skipVisibility=True)
    assert r.skip_visibility is True
    r = StartAuditRequest(url='https://e.com', skip_visibility=True)
    assert r.skip_visibility is True
    r = StartAuditRequest(url='https://e.com')
    assert r.skip_visibility is False, "absent field must default to running the sweep"
    # Unknown extras must not reject (older/newer client compatibility)
    r = StartAuditRequest(**{'url': 'https://e.com', 'someFutureField': 1})
    assert r.skip_visibility is False

print("COST_TIER01_OK")
