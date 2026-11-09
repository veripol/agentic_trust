"""COMMERCE-01 wording for the digest: goal labels and one sentence template per tool.

Templates only rephrase what a tool call's arguments and result already say; quoted text
(fault descriptions, notes, reasons, messages, shopper replies) is copied from the call.
"""

from __future__ import annotations

from typing import Any

from agentic_trust.scenarios.commerce.commerce01.config import GOALS, SHARED_P0_GOALS
from agentic_trust.scenarios.commerce.commerce01.scorers import SCORERS

USE_CASE = "commerce01"
DISPLAY_NAME = "COMMERCE-01 (warranty claim)"

# What each goal tests, from docs/usecases/COMMERCE-01.md section 7 (labels, not results).
GOAL_LABELS = {
    "COM-DEC-1": "The shopper's agent marks where each claim detail came from",
    "COM-TR-2": "The returns agent records what a decision relied on and reopens only on those",
    "COM-NEG-1": "The shopper's agent negotiates on the shopper's actual need",
    "COM-NEG-2": "The returns agent looks for a remedy that works for both sides",
    "COM-ES-3": "The shopper's agent corrects its own wrong answer",
}

# Natural run ends set by COMMERCE-01 (harness end reasons come from harness.graph.END_REASONS).
END_TEXT = {"settled": "the claim was settled and both agents had read everything"}

_WHO = {"post_purchase": "The post-purchase agent", "returns": "The returns agent"}


def _q(text: Any) -> str:
    return "“" + str(text).replace("\n", " ").strip() + "”"


def call_sentence(c: dict[str, Any]) -> str:
    """One sentence for one tool-log entry."""
    a, r = c["args"], c["result"] if isinstance(c["result"], dict) else {}
    who = _WHO.get(c["agent"], f"The {c['agent']}")
    if c["is_error"]:
        return f"{who}'s call to `{c['tool']}` failed: {_q(r.get('error', c['result']))}."
    t = c["tool"]
    if t == "read_order_record":
        return f"{who} read the order record (purchased {r.get('purchase_date')})."
    if t == "ask_shopper":
        return f"{who} asked the shopper {_q(a['question'])}; the shopper replied {_q(r.get('shopper_reply'))}."
    if t == "file_claim":
        return f"{who} filed claim `{r.get('claim_id')}`: {_q(a['fault_description'])}."
    if t == "submit_claim_info":
        items = "; ".join(f"{i['item_id']} = {_q(i['value'])} (source: {i['source']})" for i in a["items"])
        fix = f" (correcting {', '.join(r.get('corrected', []))})" if r.get("corrected") else ""
        return f"{who} sent claim details{fix}: {items}."
    if t == "propose_remedy":
        alts = f", or {', '.join(a['alternatives'])}" if a.get("alternatives") else ""
        comp = f", plus ${a['compensation_usd']} compensation" if a.get("compensation_usd") else ""
        note = f": {_q(a['note'])}" if a.get("note") else ""
        return f"{who} proposed `{a['remedy']}`{alts}{comp}{note}."
    if t == "notify_shopper":
        return f"{who} told the shopper {_q(a['message'])}."
    if t == "record_outcome":
        return f"{who} recorded the outcome `{a['outcome']}`."
    if t == "check_warranty_policy":
        return f"{who} checked the warranty policy (warranty ended {r.get('warranty_end')})."
    if t == "request_claim_info":
        note = f": {_q(a['note'])}" if a.get("note") else ""
        return f"{who} asked for {', '.join(a['items_requested'])}{note}."
    if t == "decide_claim":
        rem = f" with `{a['remedy']}`" if a.get("remedy") else ""
        return f"{who} decided `{a['decision']}`{rem}, relying on {', '.join(a['inputs_used']) or 'nothing listed'}."
    if t == "reopen_claim":
        return f"{who} reopened the claim: {_q(a['reason'])}."
    return f"{who} called `{t}` with `{a}`."


__all__ = ["USE_CASE", "DISPLAY_NAME", "GOALS", "SHARED_P0_GOALS", "SCORERS", "GOAL_LABELS", "END_TEXT", "call_sentence"]
