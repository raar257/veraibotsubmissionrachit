"""
bot.py — Vera-Beat: magicpin AI Challenge submission.

Implements the exact 5-endpoint:
  POST /v1/context   — receive category/merchant/customer/trigger context pushes
  POST /v1/tick       — periodic wake-up; bot may proactively send messages
  POST /v1/reply      — receive a merchant/customer reply; respond synchronously
  GET  /v1/healthz    — liveness probe
  GET  /v1/metadata   — bot identity

Design notes for the judge:
  - Fully deterministic & offline: no external LLM calls, so no timeout /
    rate-limit / API-key risk during the live 60-minute test window.
  - In-memory store (dict) is fine per testing-brief.md §2.1 — "storing in
    memory is fine, just don't restart between calls." A process-wide lock
    keeps concurrent requests safe.
  - Idempotent context pushes keyed on (scope, context_id, version), exactly
    per testing-brief.md §2.1.
  - Anti-repetition: we never resend the same suppression_key for the same
    merchant, and we never resend a verbatim body within a conversation.
  - /v1/tick always returns fast (no blocking work) and respects the
    20-actions-per-tick cap and one-action-per-(merchant,conversation) rule.
"""

from __future__ import annotations

import time
import threading
import itertools
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field

import composer

app = FastAPI(title="Vera-Beat", version="1.0.0")

START_TIME = time.time()
_LOCK = threading.RLock()

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------

# (scope, context_id) -> {"version": int, "payload": dict}
CONTEXTS: dict[tuple[str, str], dict] = {}

# conversation_id -> {
#   "merchant_id", "customer_id", "history": [ {from_role/"bot", message, ts} ],
#   "turns": int, "ended": bool, "last_offer": str
# }
CONVERSATIONS: dict[str, dict] = {}

# merchant_id -> set of suppression_keys already sent (never repeat)
SENT_SUPPRESSION_KEYS: dict[str, set] = {}

# merchant_id -> conversation_id currently open (so we don't double-message the
# same merchant with a second unrelated conversation while one is in flight)
OPEN_CONVERSATION_FOR_MERCHANT: dict[str, str] = {}

_CONV_COUNTER = itertools.count(1)

TEAM_INFO = {
    "team_name": "FabAnalyst",
    "team_member": "Rachit",
    "model": "rules-engine-v1 (deterministic, no external LLM at inference time)",
    "approach": "Context-grounded template composer keyed by trigger.kind, with a "
                "conversation state machine handling auto-reply detection, explicit "
                "intent hand-off, hostile/off-topic de-escalation, and anti-repetition.",
    "contact_email": "rachitarora_23ae052@dtu.ac.in",
    "version": "1.0.0",
    "submitted_at": datetime.now(timezone.utc).isoformat(),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_payload(scope: str, context_id: Optional[str]) -> Optional[dict]:
    if not context_id:
        return None
    entry = CONTEXTS.get((scope, context_id))
    return entry["payload"] if entry else None


def _find_category_for_merchant(merchant: dict) -> Optional[dict]:
    slug = merchant.get("category_slug")
    return _get_payload("category", slug)


def _counts_loaded() -> dict:
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for (scope, _cid) in CONTEXTS.keys():
        counts[scope] = counts.get(scope, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# GET /v1/healthz
# ---------------------------------------------------------------------------

@app.get("/v1/healthz")
async def healthz():
    return {
        "status": "ok",
        "uptime_seconds": int(time.time() - START_TIME),
        "contexts_loaded": _counts_loaded(),
    }


# ---------------------------------------------------------------------------
# GET /v1/metadata
# ---------------------------------------------------------------------------

@app.get("/v1/metadata")
async def metadata():
    return TEAM_INFO


# ---------------------------------------------------------------------------
# POST /v1/context
# ---------------------------------------------------------------------------

class ContextBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: Optional[str] = None


@app.post("/v1/context")
async def push_context(body: ContextBody):
    if body.scope not in ("category", "merchant", "customer", "trigger"):
        return {"accepted": False, "reason": "invalid_scope", "details": f"unknown scope '{body.scope}'"}

    key = (body.scope, body.context_id)
    with _LOCK:
        cur = CONTEXTS.get(key)
        if cur and cur["version"] >= body.version:
            return {"accepted": False, "reason": "stale_version", "current_version": cur["version"]}
        CONTEXTS[key] = {"version": body.version, "payload": body.payload}

    return {
        "accepted": True,
        "ack_id": f"ack_{body.context_id}_v{body.version}",
        "stored_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# POST /v1/tick
# ---------------------------------------------------------------------------

class TickBody(BaseModel):
    now: str
    available_triggers: list[str] = Field(default_factory=list)


MAX_ACTIONS_PER_TICK = 20


@app.post("/v1/tick")
async def tick(body: TickBody):
    actions: list[dict] = []

    with _LOCK:
        for trg_id in body.available_triggers:
            if len(actions) >= MAX_ACTIONS_PER_TICK:
                break

            trigger = _get_payload("trigger", trg_id)
            if not trigger:
                continue

            merchant_id = trigger.get("merchant_id")
            merchant = _get_payload("merchant", merchant_id)
            if not merchant:
                continue

            category = _find_category_for_merchant(merchant)
            if not category:
                continue

            customer = None
            customer_id = trigger.get("customer_id")
            if customer_id:
                customer = _get_payload("customer", customer_id)

            # Don't double-message a merchant who already has an open conversation
            if merchant_id in OPEN_CONVERSATION_FOR_MERCHANT:
                continue

            composed = composer.compose_proactive(category, merchant, trigger, customer)
            suppression_key = composed["suppression_key"]

            already_sent = SENT_SUPPRESSION_KEYS.setdefault(merchant_id, set())
            if suppression_key in already_sent:
                continue  # anti-repetition: never resend the same suppression key

            conv_id = f"conv_{next(_CONV_COUNTER):06d}_{merchant_id}"
            CONVERSATIONS[conv_id] = {
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "history": [{"from_role": "bot", "message": composed["body"], "ts": body.now}],
                "turns": 1,
                "ended": False,
                "last_offer": composed["body"],
            }
            OPEN_CONVERSATION_FOR_MERCHANT[merchant_id] = conv_id
            already_sent.add(suppression_key)

            actions.append({
                "conversation_id": conv_id,
                "merchant_id": merchant_id,
                "customer_id": customer_id,
                "send_as": composed["send_as"],
                "trigger_id": trg_id,
                "template_name": f"vera_{trigger.get('kind', 'generic')}_v1",
                "template_params": [merchant.get("identity", {}).get("name", "")],
                "body": composed["body"],
                "cta": composed["cta"],
                "suppression_key": suppression_key,
                "rationale": composed["rationale"],
            })

    return {"actions": actions}


# ---------------------------------------------------------------------------
# POST /v1/reply
# ---------------------------------------------------------------------------

class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    from_role: str
    message: str
    received_at: Optional[str] = None
    turn_number: int = 0


MAX_TURNS = 5


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    with _LOCK:
        conv = CONVERSATIONS.get(body.conversation_id)
        if conv is None:
            # Unknown conversation (e.g. judge restarted mid-test) — create a
            # minimal shell so we can still respond sensibly instead of erroring.
            conv = {
                "merchant_id": body.merchant_id,
                "customer_id": body.customer_id,
                "history": [],
                "turns": 0,
                "ended": False,
                "last_offer": None,
            }
            CONVERSATIONS[body.conversation_id] = conv

        if conv["ended"]:
            return {"action": "end", "rationale": "Conversation already ended previously; not re-engaging."}

        history_before = list(conv["history"])  # snapshot BEFORE this incoming message, for auto-reply/dup checks
        conv["turns"] += 1

        merchant = _get_payload("merchant", conv.get("merchant_id"))
        customer = _get_payload("customer", conv.get("customer_id")) if conv.get("customer_id") else None
        category = _find_category_for_merchant(merchant) if merchant else None

        if conv["turns"] > MAX_TURNS:
            conv["ended"] = True
            conv["history"].append({"from_role": body.from_role, "message": body.message, "ts": body.received_at})
            OPEN_CONVERSATION_FOR_MERCHANT.pop(conv.get("merchant_id"), None)
            return {"action": "end", "rationale": "Reached the 5-turn conversation depth cap; exiting gracefully."}

        decision = composer.compose_reply(
            history=history_before,
            from_role=body.from_role,
            message=body.message,
            turn_number=body.turn_number,
            merchant=merchant,
            customer=customer,
            category=category,
            already_offered=conv.get("last_offer"),
        )

        # Now append the incoming message (after the auto-reply/dup check used the pre-message snapshot)
        conv["history"].append({"from_role": body.from_role, "message": body.message, "ts": body.received_at})

        if decision["action"] == "send":
            conv["history"].append({"from_role": "bot", "message": decision["body"], "ts": _now_iso()})
        elif decision["action"] == "end":
            conv["ended"] = True
            OPEN_CONVERSATION_FOR_MERCHANT.pop(conv.get("merchant_id"), None)

    return decision


# ---------------------------------------------------------------------------
# Optional teardown hook (testing-brief.md §11 — wipe state at end of test)
# ---------------------------------------------------------------------------

@app.post("/v1/teardown")
async def teardown():
    with _LOCK:
        CONTEXTS.clear()
        CONVERSATIONS.clear()
        SENT_SUPPRESSION_KEYS.clear()
        OPEN_CONVERSATION_FOR_MERCHANT.clear()
    return {"status": "wiped"}


@app.get("/")
async def root():
    return {"service": "Vera-Beat", "status": "running", "docs": "/docs"}
