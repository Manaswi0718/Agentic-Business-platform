"""
Customer Care Autopilot — Agentverse HOSTED agent (Blank Agent -> paste into agent.py)

The new business's front desk on ASI:One:
  * Owner sets up a storefront (menu, prices, UPI ID, delivery rules, facts) with cards
  * Customers ask questions -> answered ONLY from the owner's facts; unknowns are escalated to the owner
  * Customers order -> totals calculated exactly in code (integer paise) -> UPI payment link + QR
  * Owner inbox: answer escalations (answers are learned as new facts), confirm payments
  * Monetisation: "Autopilot Pro" via the Fetch.ai Agent Payment Protocol (Stripe card and/or FET)

Secrets (Agentverse -> Build -> Agent Secrets):
  LAUNCHPAD_ADDRESS       agent1... of Udyam Launchpad (only it may reserve storefronts)  (recommended)
  ASI_ONE_API_KEY         optional; enables grounded natural-language answers
  ASI_ONE_MODEL           optional, default "asi1"
  DEMO_UPI_ID             UPI ID used by the built-in DEMO shop (e.g. yourname@okicici)
  DEMO_CLAIM_PIN          PIN to claim the DEMO shop as owner (default 2026)
  FREE_ORDERS_PER_MONTH   default 25
  PRO_PRICE_INR           default 99   (Pro plan, per 30 days)
  STRIPE_SECRET_KEY / STRIPE_PUBLISHABLE_KEY   optional, Stripe TEST keys for card payments
  STRIPE_CURRENCY         default inr
  FET_AMOUNT              default 1   (FET price of Pro; testnet by default)
  FET_USE_TESTNET         default true
"""

import asyncio
import json
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote, urlencode
from uuid import uuid4

import requests
from uagents import Agent, Context, Model, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    MetadataContent,
    TextContent,
    chat_protocol_spec,
)
from uagents_core.contrib.protocols.payment import (
    CancelPayment,
    CommitPayment,
    CompletePayment,
    Funds,
    RejectPayment,
    RequestPayment,
    payment_protocol_spec,
)

agent = Agent()

IST = timezone(timedelta(hours=5, minutes=30))
CODE_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Agent-to-agent models (identical copies live in Udyam Launchpad)
# ---------------------------------------------------------------------------


class StorefrontDraft(Model):
    request_id: str
    owner_address: str
    business_name: str
    business_type: str
    city: str
    state: str


class StorefrontCreated(Model):
    request_id: str
    ok: bool
    shop_code: str = ""
    claim_pin: str = ""
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def sget(ctx: Context, key: str, default=None):
    try:
        v = ctx.storage.get(key)
        return default if v is None else v
    except Exception:
        return default


def sset(ctx: Context, key: str, value) -> None:
    try:
        ctx.storage.set(key, value)
    except Exception as e:
        ctx.logger.warning(f"[state] storage set failed {key}: {e}")


def sdel(ctx: Context, key: str) -> None:
    try:
        ctx.storage.remove(key)
    except Exception:
        pass


def text_msg(text: str) -> ChatMessage:
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=[TextContent(type="text", text=text)])


OWNER_ACTIONS = {"ap_save_shop", "ap_edit_shop", "ap_go_live", "ap_upgrade"}


def owner_token(shop: dict) -> str:
    if not shop.get("owner_token"):
        shop["owner_token"] = "ok_" + "".join(random.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(10))
    return shop["owner_token"]


def sign_owner_payload(node, shop: dict):
    """Put the shop code and the secret owner key into every owner button, so a press is trusted
    even if ASI:One relays it from a different chat address."""
    if isinstance(node, dict):
        sel = node.get("selection")
        if isinstance(sel, dict) and (str(sel.get("action", "")).startswith("ap_owner") or sel.get("action") in OWNER_ACTIONS):
            sel["shop"] = shop["code"]
            sel["ok"] = owner_token(shop)
        for v in node.values():
            sign_owner_payload(v, shop)
    elif isinstance(node, list):
        for v in node:
            sign_owner_payload(v, shop)


def card_msg(narration: str, kind: str, payload: dict, shop: Optional[dict] = None) -> ChatMessage:
    if shop is not None:
        sign_owner_payload(payload, shop)
    return ChatMessage(
        timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
        content=[
            TextContent(type="text", text=narration),
            MetadataContent(type="metadata", metadata={
                "card_protocol_version": "1",
                "requires_card_interaction": "true",
                "card_kind": kind,
                "card_payload": json.dumps(payload),
                "preferred_drawer_width_px": "560",
            }),
        ],
    )


def rupees(paise: int) -> str:
    r, p = divmod(int(paise), 100)
    return f"₹{r:,}" if p == 0 else f"₹{r:,}.{p:02d}"


def to_paise(v) -> int:
    s = str(v or "0").replace(",", "").replace("₹", "").replace("rs", "").replace("Rs", "").strip()
    m = re.search(r"\d+(?:\.\d{1,2})?", s)
    if not m:
        return 0
    whole, _, frac = m.group(0).partition(".")
    return int(whole) * 100 + int((frac + "00")[:2])


def strip_mentions(text: str) -> str:
    """ASI:One may prefix messages with '@agent-handle'; remove leading mentions before parsing."""
    return re.sub(r"^(\s*@[\w.\-]+[:,]?\s*)+", "", text or "").strip()


def try_json(text: str) -> Optional[dict]:
    t = (text or "").strip()
    m = re.search(r"\{.*\}", t, re.S)
    for cand in (t, m.group(0) if m else None):
        if not cand:
            continue
        try:
            d = json.loads(cand)
            if isinstance(d, dict):
                return d
        except Exception:
            pass
    return None


def asi1_json(system: str, user: str, max_tokens: int = 400) -> Optional[dict]:
    key = os.getenv("ASI_ONE_API_KEY")
    if not key:
        return None
    try:
        r = requests.post(
            "https://api.asi1.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": os.getenv("ASI_ONE_MODEL", "asi1"), "temperature": 0, "max_tokens": max_tokens,
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
            timeout=25,
        )
        r.raise_for_status()
        return try_json(r.json()["choices"][0]["message"]["content"] or "")
    except Exception:
        return None


def new_id(prefix: str, n: int = 5) -> str:
    return prefix + "".join(random.choice(CODE_CHARS) for _ in range(n))


def month_key() -> str:
    return datetime.now(IST).strftime("%Y-%m")


def remember_card(ctx: Context, sender: str, actions: list, ids: Optional[dict] = None, fields: Optional[list] = None):
    sset(ctx, f"ap:card:{sender}", {"actions": actions, "ids": ids or {}, "fields": fields or []})


# ---------------------------------------------------------------------------
# Shop model
# ---------------------------------------------------------------------------


def parse_menu(text: str) -> list:
    """'Chocolate cake 1kg - 650; Brownie box (6) - 300' -> items with integer paise prices."""
    parts = [x.strip() for x in re.split(r"[;\n|]+", text or "") if x.strip()]
    if len(parts) == 1 and parts[0].count(",") >= 1:
        parts = [x.strip() for x in parts[0].split(",") if x.strip()]
    items = []
    for i, part in enumerate(parts[:12], 1):
        nums = list(re.finditer(r"(?:₹|rs\.?|inr)?\s*(\d[\d,]*(?:\.\d{1,2})?)\s*(?:/-|rs|inr)?\s*$", part, re.I))
        if not nums:
            continue
        m = nums[-1]
        price = to_paise(m.group(1))
        name = re.sub(r"[\s:\-–=@]+$", "", part[: m.start()]).strip(" :-–=@")
        if name and price > 0:
            items.append({"id": f"i{i}", "name": name[:60], "price": price})
    return items


def get_shop(ctx: Context, code: str) -> Optional[dict]:
    shop = sget(ctx, f"shop:{(code or '').upper()}")
    if shop and not shop.get("owner_token"):
        owner_token(shop)
        sset(ctx, f"shop:{shop['code']}", shop)
    return shop


def save_shop(ctx: Context, shop: dict) -> None:
    owner_token(shop)
    sset(ctx, f"shop:{shop['code']}", shop)


def is_pro(shop: dict) -> bool:
    return shop.get("pro_until", 0) > time.time()


def orders_this_month(shop: dict) -> int:
    return int((shop.get("usage") or {}).get(month_key(), 0))


def seed_demo(ctx: Context) -> None:
    existing = get_shop(ctx, "DEMO")
    if existing:
        upi = (os.getenv("DEMO_UPI_ID") or "").strip()
        if upi and existing.get("upi_id") != upi:  # pick up the secret if it was added/changed later
            existing["upi_id"] = upi
            save_shop(ctx, existing)
        return
    save_shop(ctx, {
        "code": "DEMO", "name": "Asha's Home Bakery (demo)", "owner": None, "status": "live",
        "claim_pin": os.getenv("DEMO_CLAIM_PIN", "2026"), "city": "Bengaluru",
        "upi_id": os.getenv("DEMO_UPI_ID", "set-DEMO_UPI_ID@upi"), "payee": "Asha's Home Bakery",
        "items": parse_menu("Chocolate truffle cake 1 kg - 650; Red velvet cake 1 kg - 750; Eggless brownie box (6) - 300; Butter cookies 250 g - 180"),
        "delivery_areas": ["Indiranagar", "Koramangala", "HSR Layout", "Domlur"], "delivery_fee": 4000, "min_order": 20000,
        "hours": "10 am to 8 pm, Tuesday to Sunday",
        "facts": ["All cakes are made to order and need 24 hours' notice", "Eggless versions of all cakes are available at no extra cost",
                  "We do not offer cash on delivery; payment is by UPI before dispatch", "FSSAI Basic Registration in progress"],
        "usage": {}, "pro_until": 0, "created": time.time(),
    })


# ---------------------------------------------------------------------------
# Cards
# ---------------------------------------------------------------------------

SETUP_FIELDS = ["business_name", "upi_id", "payee", "menu", "delivery_areas", "delivery_fee", "min_order", "hours", "facts"]


def setup_form(shop: dict) -> dict:
    return {
        "title": f"Set up {shop.get('name') or 'your storefront'} ({shop['code']})",
        "fields": [
            {"name": "business_name", "kind": "text", "label": f"Shop name (now: {shop.get('name') or '-'})", "required": True},
            {"name": "upi_id", "kind": "text", "label": "Your UPI ID for customer payments (e.g. asha@okicici)", "required": True},
            {"name": "payee", "kind": "text", "label": "Name shown on UPI (payee name)"},
            {"name": "menu", "kind": "text", "label": "Menu & prices: item - price; item - price", "required": True},
            {"name": "delivery_areas", "kind": "text", "label": "Delivery areas (comma separated; blank = pickup only)"},
            {"name": "delivery_fee", "kind": "number", "label": "Delivery fee (₹)"},
            {"name": "min_order", "kind": "number", "label": "Minimum order (₹)"},
            {"name": "hours", "kind": "text", "label": "Working hours"},
            {"name": "facts", "kind": "text", "label": "Other facts customers ask about (separate with ;)"},
        ],
        "submit_cta": {"label": "Preview storefront", "selection": {"action": "ap_save_shop", "shop": shop["code"]}},
    }


def shop_rows(shop: dict) -> list:
    rows = [{"label": "Shop", "value": f"{shop['name']} ({shop['code']})"},
            {"label": "UPI", "value": f"{shop.get('upi_id')} ({shop.get('payee') or shop['name']})"}]
    rows += [{"label": it["name"], "value": rupees(it["price"])} for it in shop.get("items", [])]
    rows += [{"label": "Delivery", "value": (", ".join(shop.get("delivery_areas") or []) + f" · fee {rupees(shop.get('delivery_fee', 0))}") if shop.get("delivery_areas") else "Pickup only"},
             {"label": "Minimum order", "value": rupees(shop.get("min_order", 0))},
             {"label": "Hours", "value": shop.get("hours") or "-"},
             {"label": "Facts", "value": "; ".join(shop.get("facts") or []) or "-"}]
    return rows


def order_form(shop: dict) -> dict:
    fields = [{"name": f"qty_{it['id']}", "kind": "number", "label": f"{it['name']} — {rupees(it['price'])} (qty)"} for it in shop["items"][:10]]
    opts = [{"value": "pickup", "label": "Pickup"}]
    if shop.get("delivery_areas"):
        opts.insert(0, {"value": "delivery", "label": f"Delivery ({', '.join(shop['delivery_areas'])}) +{rupees(shop.get('delivery_fee', 0))}"})
    fields += [
        {"name": "fulfilment", "kind": "select", "label": "Delivery or pickup", "required": True, "options": opts},
        {"name": "customer_name", "kind": "text", "label": "Your name", "required": True},
        {"name": "phone", "kind": "text", "label": "Phone number", "required": True},
        {"name": "address", "kind": "text", "label": "Delivery address (with area)"},
        {"name": "notes", "kind": "text", "label": "Notes (e.g. 'eggless', message on cake)"},
    ]
    return {"title": f"Order from {shop['name']}", "fields": fields,
            "submit_cta": {"label": "Review order", "selection": {"action": "ap_place_order", "shop": shop["code"]}}}


def compute_order(shop: dict, sel: dict) -> dict:
    """Exact totals in integer paise. Prices always come from the shop record, never from the user."""
    lines, subtotal = [], 0
    for it in shop["items"]:
        raw = sel.get(f"qty_{it['id']}")
        try:
            q = int(float(str(raw).strip())) if raw not in (None, "") else 0
        except Exception:
            q = 0
        if q < 0 or q > 50:
            return {"error": f"Quantity for {it['name']} must be between 0 and 50."}
        if q:
            amt = it["price"] * q
            subtotal += amt
            lines.append({"id": it["id"], "name": it["name"], "qty": q, "unit": it["price"], "amount": amt})
    if not lines:
        return {"error": "Please choose at least one item."}
    if subtotal < int(shop.get("min_order", 0)):
        return {"error": f"Minimum order is {rupees(shop['min_order'])}; your items come to {rupees(subtotal)}."}
    ful = str(sel.get("fulfilment") or "pickup").lower()
    ful = "delivery" if ful.startswith("deliv") and shop.get("delivery_areas") else "pickup"
    address = str(sel.get("address") or "").strip()
    if ful == "delivery":
        if not address:
            return {"error": "Please add a delivery address."}
        if not any(a.lower() in address.lower() for a in shop["delivery_areas"]):
            return {"error": f"We currently deliver only to {', '.join(shop['delivery_areas'])}. Choose pickup or include one of these areas in the address."}
    fee = int(shop.get("delivery_fee", 0)) if ful == "delivery" else 0
    phone = re.sub(r"[^\d+]", "", str(sel.get("phone") or ""))
    if len(re.sub(r"\D", "", phone)) < 10:
        return {"error": "Please give a valid 10-digit phone number."}
    return {"lines": lines, "subtotal": subtotal, "delivery_fee": fee, "total": subtotal + fee, "fulfilment": ful,
            "address": address[:200], "customer_name": str(sel.get("customer_name") or "Customer")[:60], "phone": phone[:15],
            "notes": str(sel.get("notes") or "")[:200]}


def upi_link(shop: dict, amount_paise: int, note: str) -> str:
    return "upi://pay?" + urlencode({"pa": shop["upi_id"], "pn": (shop.get("payee") or shop["name"])[:40],
                                    "am": f"{amount_paise / 100:.2f}", "cu": "INR", "tn": note[:50]}, quote_via=quote)


def qr_url(data: str) -> str:
    return "https://api.qrserver.com/v1/create-qr-code/?size=320x320&data=" + quote(data, safe="")


# ---------------------------------------------------------------------------
# Grounded Q&A + escalation
# ---------------------------------------------------------------------------

QA_SYSTEM = (
    "You are the front desk of a small Indian shop. Answer the customer's question using ONLY the FACTS JSON. "
    "Never invent prices, timings, areas, ingredients or policies. If the facts do not clearly answer the question, "
    "reply answerable=false. Reply ONLY with JSON: {\"answerable\": true|false, \"answer\": \"short friendly answer\"}."
)
STOP = {"the", "a", "an", "do", "you", "is", "are", "can", "i", "to", "of", "for", "and", "or", "in", "on", "my", "your", "we", "it", "what", "how", "any", "have", "with", "there", "will", "be"}


def deterministic_answer(shop: dict, q: str) -> Optional[str]:
    low = q.lower()
    priceish = bool(re.search(r"(price|cost|how much|\brate\b|₹|\brs\b|menu|what do you sell|\bitems\b)", low))
    hits = [it for it in shop["items"] if it["name"].lower() in low or (len(it["name"].split()[0]) > 3 and it["name"].split()[0].lower() in low)]
    if hits and priceish:
        return "; ".join(f"{it['name']}: {rupees(it['price'])}" for it in hits) + "."
    if priceish and any(w in low for w in ["menu", "what do you sell", "items"]):
        return "Our menu: " + "; ".join(f"{it['name']} {rupees(it['price'])}" for it in shop["items"]) + "."
    if any(w in low for w in ["timing", "hours", "open", "close", "when are you"]) and shop.get("hours"):
        return f"We're open {shop['hours']}."
    if "deliver" in low:
        areas = shop.get("delivery_areas") or []
        hit = [a for a in areas if a.lower() in low]
        if hit:
            return f"Yes, we deliver to {hit[0]} (delivery fee {rupees(shop.get('delivery_fee', 0))}, minimum order {rupees(shop.get('min_order', 0))})."
        if not areas:
            return "We're pickup only at the moment."
        if re.search(r"\b(where|which areas|areas)\b", low):
            return f"We deliver to {', '.join(areas)} (fee {rupees(shop.get('delivery_fee', 0))})."
    if any(w in low for w in ["minimum order", "min order"]):
        return f"Minimum order is {rupees(shop.get('min_order', 0))}."
    if any(w in low for w in ["upi", "pay", "payment", "cod", "cash"]) and not any(w in low for w in ["cod", "cash"]):
        return f"We accept UPI ({shop['upi_id']}). I'll send you a payment link with your order total."
    return None


def facts_payload(shop: dict) -> dict:
    return {"shop": shop["name"], "menu": [{"item": it["name"], "price": rupees(it["price"])} for it in shop["items"]],
            "delivery_areas": shop.get("delivery_areas"), "delivery_fee": rupees(shop.get("delivery_fee", 0)),
            "minimum_order": rupees(shop.get("min_order", 0)), "hours": shop.get("hours"), "payment": "UPI " + str(shop.get("upi_id")),
            "facts": shop.get("facts") or []}


def overlap_answer(shop: dict, q: str) -> Optional[str]:
    words = {w for w in re.findall(r"[a-z]+", q.lower()) if w not in STOP and len(w) > 2}
    best, score = None, 0
    for f in shop.get("facts") or []:
        fw = {w for w in re.findall(r"[a-z]+", f.lower()) if w not in STOP and len(w) > 2}
        s = len(words & fw)
        if s > score:
            best, score = f, s
    return f"{best}." if best and score >= 2 else None


def answer_question(shop: dict, q: str) -> Optional[str]:
    a = deterministic_answer(shop, q)
    if a:
        return a
    r = asi1_json(QA_SYSTEM, json.dumps({"FACTS": facts_payload(shop), "QUESTION": q}))
    if r is not None:
        if r.get("answerable") is True and r.get("answer"):
            return str(r["answer"])[:600]
        return None
    return overlap_answer(shop, q)


async def escalate(ctx: Context, shop: dict, customer: str, q: str):
    qid = new_id("Q-", 4)
    sset(ctx, f"esc:{qid}", {"id": qid, "shop": shop["code"], "customer": customer, "question": q[:500], "status": "open", "ts": time.time()})
    sset(ctx, f"shop_esc:{shop['code']}", (sget(ctx, f"shop_esc:{shop['code']}", []) + [qid])[-50:])
    await ctx.send(customer, text_msg(f"Good question - I don't want to guess. I've asked the owner of **{shop['name']}** and will reply here as soon as they answer (ref {qid})."))
    if shop.get("owner"):
        await ctx.send(shop["owner"], card_msg(
            f"📩 New customer question at **{shop['name']}** ({qid}):\n> {q[:300]}\n\nAnswer below, or reply `answer {qid} <your answer>`. I'll save it so I can answer this myself next time.",
            "form", {"title": f"Answer {qid}", "fields": [{"name": "answer", "kind": "text", "label": "Your answer", "required": True}],
                     "submit_cta": {"label": "Send answer", "selection": {"action": "ap_owner_answer", "qid": qid}}}, shop=shop))
        remember_card(ctx, shop["owner"], ["ap_owner_answer"], ids={"qid": [qid]}, fields=["answer"])


# ---------------------------------------------------------------------------
# Payment Protocol: Autopilot Pro (seller role). Stripe via REST (no SDK needed) + FET on-chain.
# ---------------------------------------------------------------------------


def stripe_ready() -> bool:
    return bool(os.getenv("STRIPE_SECRET_KEY") and os.getenv("STRIPE_PUBLISHABLE_KEY"))


def stripe_create_session(user: str, shop_code: str, amount_minor: int, currency: str) -> dict:
    data = {
        "mode": "payment", "payment_method_types[0]": "card", "redirect_on_completion": "if_required",
        "return_url": "https://agentverse.ai/payment-success?session_id={CHECKOUT_SESSION_ID}",
        "expires_at": str(int(time.time()) + 1860),
        "line_items[0][quantity]": "1",
        "line_items[0][price_data][currency]": currency,
        "line_items[0][price_data][unit_amount]": str(amount_minor),
        "line_items[0][price_data][product_data][name]": f"Customer Care Autopilot Pro - 30 days ({shop_code})",
        "metadata[user_address]": user, "metadata[shop]": shop_code,
    }
    last = ""
    for ui_mode in ("embedded_page", "embedded"):
        try:
            r = requests.post("https://api.stripe.com/v1/checkout/sessions", auth=(os.getenv("STRIPE_SECRET_KEY"), ""),
                              data={**data, "ui_mode": ui_mode}, timeout=20)
            j = r.json()
            if r.status_code == 200:
                return {"id": j["id"], "client_secret": j["client_secret"]}
            last = (j.get("error") or {}).get("message", r.text)[:300]
            if "ui_mode" not in last:
                break
        except Exception as e:
            last = str(e)
    return {"error": last}


def stripe_verify(session_id: str) -> dict:
    try:
        r = requests.get(f"https://api.stripe.com/v1/checkout/sessions/{session_id}", auth=(os.getenv("STRIPE_SECRET_KEY"), ""), timeout=20)
        j = r.json()
        return {"paid": j.get("payment_status") == "paid", "amount": j.get("amount_total"), "currency": (j.get("currency") or "").lower(),
                "user": (j.get("metadata") or {}).get("user_address"), "shop": (j.get("metadata") or {}).get("shop")}
    except Exception as e:
        return {"paid": False, "error": str(e)}


def fet_verify(tx_hash: str, expected_fet: str, buyer: str, recipient: str, testnet: bool) -> bool:
    try:
        from cosmpy.aerial.client import LedgerClient, NetworkConfig
        ledger = LedgerClient(NetworkConfig.fetchai_stable_testnet() if testnet else NetworkConfig.fetchai_mainnet())
        tx = ledger.query_tx(tx_hash)
        if tx is None or (hasattr(tx, "is_successful") and not tx.is_successful()):
            return False
        denom = "atestfet" if testnet else "afet"
        need = int(round(float(expected_fet) * 10 ** 18))
        ev = getattr(tx, "events", {}) or {}
        tr = ev.get("transfer") or {}
        amt = str(tr.get("amount") or (ev.get("coin_received") or {}).get("amount") or "")
        ok_amt = amt.endswith(denom) and int(amt[: -len(denom)] or 0) >= need
        ok_to = str(tr.get("recipient") or (ev.get("coin_received") or {}).get("receiver") or "") == recipient
        ok_from = str(tr.get("sender") or (ev.get("coin_spent") or {}).get("spender") or "") == buyer
        return ok_amt and ok_to and ok_from
    except Exception:
        return False


async def request_pro_payment(ctx: Context, owner: str, shop: dict):
    price_inr = env_int("PRO_PRICE_INR", 99)
    currency = (os.getenv("STRIPE_CURRENCY") or "inr").lower()
    testnet = (os.getenv("FET_USE_TESTNET", "true").lower() == "true")
    funds, meta = [], {"service": "autopilot_pro", "shop": shop["code"]}
    if stripe_ready():
        s = stripe_create_session(owner, shop["code"], price_inr * 100, currency)
        if s.get("id"):
            funds.append(Funds(currency=currency.upper(), amount=f"{price_inr:.2f}", payment_method="stripe"))
            meta["stripe"] = {"ui_mode": "embedded", "publishable_key": os.getenv("STRIPE_PUBLISHABLE_KEY"), "client_secret": s["client_secret"],
                              "checkout_session_id": s["id"], "currency": currency, "amount_cents": str(price_inr * 100)}
        else:
            ctx.logger.error(f"[payment] stripe session failed: {s.get('error')}")
    fet_amount = (os.getenv("FET_AMOUNT") or "1").strip()
    funds.append(Funds(currency="FET", amount=fet_amount, payment_method="fet_direct"))
    meta.update({"fet_network": "stable-testnet" if testnet else "mainnet", "mainnet": "false" if testnet else "true",
                 "provider_agent_wallet": str(agent.wallet.address())})
    ref = uuid4().hex
    sset(ctx, f"pay:pending:{owner}", {"shop": shop["code"], "ref": ref, "ts": time.time(), "amount_inr": price_inr, "currency": currency,
                                      "checkout": (meta.get("stripe") or {}).get("checkout_session_id"), "fet": fet_amount})
    await ctx.send(owner, RequestPayment(accepted_funds=funds, recipient=str(agent.wallet.address()), deadline_seconds=1800, reference=ref,
                                         description=f"Autopilot Pro for {shop['name']}: unlimited orders for 30 days", metadata=meta))
    methods = " or ".join(("card (₹%d)" % price_inr) if f.payment_method == "stripe" else f"{fet_amount} FET" for f in funds)
    await ctx.send(owner, text_msg(f"I've sent a payment request for **Autopilot Pro** ({methods}). Complete it in the payment card; I'll activate Pro the moment it's verified."))


payment_proto = Protocol(spec=payment_protocol_spec, role="seller")


@payment_proto.on_message(CommitPayment)
async def on_commit(ctx: Context, sender: str, msg: CommitPayment):
    tx = str(msg.transaction_id or "")
    method = msg.funds.payment_method
    ctx.logger.info(f"[inbound] CommitPayment method={method} tx={tx[:24]}")
    pending = sget(ctx, f"pay:pending:{sender}")
    if sget(ctx, f"pay:processed:{tx}"):
        await ctx.send(sender, CompletePayment(transaction_id=tx))
        return
    if not pending or not tx:
        await ctx.send(sender, CancelPayment(transaction_id=tx or None, reason="No matching payment request. Say 'upgrade' to start again."))
        return
    ok = False
    if method == "stripe":
        v = await asyncio.to_thread(stripe_verify, tx)
        ok = v.get("paid") and v.get("user") == sender and int(v.get("amount") or 0) == pending["amount_inr"] * 100 and v.get("currency") == pending["currency"]
    elif method == "fet_direct":
        buyer = (msg.metadata or {}).get("buyer_fet_wallet") or (msg.metadata or {}).get("buyer_fet_address")
        if isinstance(buyer, str) and msg.funds.currency == "FET":
            testnet = os.getenv("FET_USE_TESTNET", "true").lower() == "true"
            ok = await asyncio.to_thread(fet_verify, tx, pending["fet"], buyer, str(agent.wallet.address()), testnet)
    if not ok:
        await ctx.send(sender, CancelPayment(transaction_id=tx, reason="Payment not verified yet. Finish checkout, then press Pay again."))
        return
    sset(ctx, f"pay:processed:{tx}", True)
    sdel(ctx, f"pay:pending:{sender}")
    shop = get_shop(ctx, pending["shop"])
    await ctx.send(sender, CompletePayment(transaction_id=tx))
    if shop:
        shop["pro_until"] = max(time.time(), shop.get("pro_until", 0)) + 30 * 86400
        save_shop(ctx, shop)
        until = datetime.fromtimestamp(shop["pro_until"], IST).strftime("%d %b %Y")
        await ctx.send(sender, text_msg(f"✅ Payment verified. **Autopilot Pro** is active for {shop['name']} until {until}: unlimited orders and priority escalations."))


@payment_proto.on_message(RejectPayment)
async def on_reject(ctx: Context, sender: str, msg: RejectPayment):
    sdel(ctx, f"pay:pending:{sender}")
    await ctx.send(sender, text_msg("No problem - you're still on the free plan. Say **upgrade** any time."))


# ---------------------------------------------------------------------------
# Owner flows
# ---------------------------------------------------------------------------


async def owner_dashboard(ctx: Context, owner: str, shop: dict, intro: str = ""):
    open_q = [q for q in (sget(ctx, f"esc:{i}") for i in sget(ctx, f"shop_esc:{shop['code']}", [])) if q and q["status"] == "open"]
    orders = [o for o in (sget(ctx, f"order:{i}") for i in sget(ctx, f"shop_orders:{shop['code']}", [])) if o]
    unpaid = [o for o in orders if o["status"] in ("awaiting_payment", "payment_claimed")]
    plan = ("Pro until " + datetime.fromtimestamp(shop["pro_until"], IST).strftime("%d %b")) if is_pro(shop) else f"Free ({orders_this_month(shop)}/{env_int('FREE_ORDERS_PER_MONTH', 25)} orders this month)"
    remember_card(ctx, owner, ["ap_owner_orders", "ap_owner_inbox", "ap_edit_shop", "ap_upgrade"])
    await ctx.send(owner, card_msg(intro or f"Dashboard for **{shop['name']}**. Customers reach you by messaging me `shop {shop['code']}`.", "detail", {
        "title": f"{shop['name']} · {shop['code']}",
        "summary_rows": [
            {"label": "Status", "value": shop["status"].title()},
            {"label": "Plan", "value": plan},
            {"label": "Menu items", "value": str(len(shop.get("items", [])))},
            {"label": "Open customer questions", "value": str(len(open_q))},
            {"label": "Orders awaiting payment", "value": str(len(unpaid))},
            {"label": "Total orders", "value": str(len(orders))},
        ],
        "ctas": [
            {"label": "Orders", "primary": True, "selection": {"action": "ap_owner_orders", "shop": shop["code"]}},
            {"label": "Inbox", "selection": {"action": "ap_owner_inbox", "shop": shop["code"]}},
            {"label": "Edit menu & info", "selection": {"action": "ap_edit_shop", "shop": shop["code"]}},
            {"label": "Upgrade to Pro", "selection": {"action": "ap_upgrade", "shop": shop["code"]}},
        ],
    }, shop=shop))


async def owner_orders(ctx: Context, owner: str, shop: dict):
    ids = sget(ctx, f"shop_orders:{shop['code']}", [])[-10:][::-1]
    orders = [o for o in (sget(ctx, f"order:{i}") for i in ids) if o]
    if not orders:
        await ctx.send(owner, text_msg("No orders yet. Share your code with customers: they message me `shop " + shop["code"] + "`."))
        return
    items = []
    for o in orders:
        badge = {"awaiting_payment": ("Awaiting UPI", "warning"), "payment_claimed": ("Customer says paid", "info"), "paid": ("Paid", "success"), "cancelled": ("Cancelled", "info")}[o["status"]]
        it = {"id": o["id"], "title": f"{o['id']} · {rupees(o['total'])}", "subtitle": f"{o['customer_name']} · {o['phone']} · {o['fulfilment']}",
              "badges": [{"label": badge[0], "variant": badge[1]}], "secondary_text": ", ".join(f"{l['qty']}× {l['name']}" for l in o["lines"])[:80]}
        if o["status"] in ("awaiting_payment", "payment_claimed"):
            it["primary_cta"] = {"label": "Confirm UPI received", "selection": {"action": "ap_owner_mark_paid", "order_id": o["id"]}}
        else:
            it["primary_cta"] = {"label": "Details", "selection": {"action": "ap_owner_order_view", "order_id": o["id"]}}
        items.append(it)
    remember_card(ctx, owner, ["ap_owner_mark_paid", "ap_owner_order_view"], ids={"order_id": [o["id"] for o in orders]})
    await ctx.send(owner, card_msg("Latest orders. Check your UPI app, then confirm payments so customers get a receipt:", "carousel",
                                   {"title": f"Orders · {shop['name']}", "items": items}, shop=shop))


async def owner_inbox(ctx: Context, owner: str, shop: dict):
    qs = [q for q in (sget(ctx, f"esc:{i}") for i in sget(ctx, f"shop_esc:{shop['code']}", [])) if q and q["status"] == "open"]
    if not qs:
        await ctx.send(owner, text_msg("Inbox is empty - I answered everything from your facts. 🎉"))
        return
    remember_card(ctx, owner, ["ap_owner_answer_open"], ids={"qid": [q["id"] for q in qs]})
    await ctx.send(owner, card_msg("Questions I couldn't answer from your facts:", "carousel", {
        "title": "Customer questions", "items": [
            {"id": q["id"], "title": q["question"][:70], "subtitle": q["id"], "badges": [{"label": "Needs you", "variant": "warning"}],
             "primary_cta": {"label": "Answer", "selection": {"action": "ap_owner_answer_open", "qid": q["id"]}}} for q in qs[-10:]]}, shop=shop))


async def deliver_answer(ctx: Context, owner: str, shop: dict, qid: str, answer: str):
    q = sget(ctx, f"esc:{qid.upper()}")
    if not q or q["shop"] != shop["code"]:
        await ctx.send(owner, text_msg(f"I can't find question {qid} for your shop."))
        return
    answer = answer.strip()[:500]
    q.update(status="answered", answer=answer)
    sset(ctx, f"esc:{q['id']}", q)
    shop.setdefault("facts", []).append(f"Q: {q['question'][:150]} A: {answer}")
    shop["facts"] = shop["facts"][-40:]
    save_shop(ctx, shop)
    await ctx.send(q["customer"], text_msg(f"💬 Reply from **{shop['name']}** about your question ({q['id']}):\n{answer}"))
    await ctx.send(owner, text_msg(f"Sent to the customer and saved as a fact - I'll answer this myself next time."))


async def handle_owner_action(ctx: Context, sender: str, shop: dict, sel: dict) -> bool:
    a = sel.get("action")
    if a == "ap_save_shop":
        name = str(sel.get("business_name") or shop.get("name") or "").strip()[:60]
        upi = str(sel.get("upi_id") or "").strip()
        items = parse_menu(str(sel.get("menu") or "")) or shop.get("items", [])
        errors = []
        if not re.fullmatch(r"[A-Za-z0-9.\-_]{2,256}@[A-Za-z]{2,64}", upi):
            errors.append("UPI ID looks invalid (format: name@bank)")
        if not items:
            errors.append("I couldn't read any menu items. Use 'item - price; item - price'")
        if errors:
            remember_card(ctx, sender, ["ap_save_shop"], fields=SETUP_FIELDS)
            await ctx.send(sender, card_msg("Please fix: " + "; ".join(errors) + ".", "form", setup_form(shop), shop=shop))
            return True
        shop.update(name=name or shop.get("name"), upi_id=upi, payee=str(sel.get("payee") or name)[:40], items=items,
                    delivery_areas=[x.strip() for x in str(sel.get("delivery_areas") or "").split(",") if x.strip()][:15],
                    delivery_fee=to_paise(sel.get("delivery_fee")), min_order=to_paise(sel.get("min_order")),
                    hours=str(sel.get("hours") or "")[:120],
                    facts=[x.strip() for x in str(sel.get("facts") or "").split(";") if x.strip()][:30] or shop.get("facts", []))
        save_shop(ctx, shop)
        remember_card(ctx, sender, ["ap_go_live", "ap_edit_shop"])
        await ctx.send(sender, card_msg("Here's how customers will see your shop. Prices are used exactly as written:", "review", {
            "title": "Preview storefront", "summary_rows": shop_rows(shop),
            "approve_cta": {"label": "Go live", "primary": True, "selection": {"action": "ap_go_live", "shop": shop["code"]}},
            "reject_cta": {"label": "Edit", "selection": {"action": "ap_edit_shop", "shop": shop["code"]}}}, shop=shop))
    elif a == "ap_edit_shop":
        remember_card(ctx, sender, ["ap_save_shop"], fields=SETUP_FIELDS)
        await ctx.send(sender, card_msg("Update your storefront:", "form", setup_form(shop), shop=shop))
    elif a == "ap_go_live":
        shop["status"] = "live"
        save_shop(ctx, shop)
        await owner_dashboard(ctx, sender, shop, f"🚀 **{shop['name']}** is live! Share this with customers:\n\n> Message **@customer-care-autopilot** on ASI:One and say `shop {shop['code']}` to see the menu, ask questions and order.\n\nI'll answer from your facts, take orders with exact totals, send UPI links, and ping you for anything I don't know.")
    elif a == "ap_owner_orders":
        await owner_orders(ctx, sender, shop)
    elif a == "ap_owner_inbox":
        await owner_inbox(ctx, sender, shop)
    elif a == "ap_owner_answer_open":
        qid = sel.get("qid", "")
        remember_card(ctx, sender, ["ap_owner_answer"], ids={"qid": [qid]}, fields=["answer"])
        q = sget(ctx, f"esc:{qid}") or {}
        await ctx.send(sender, card_msg(f"> {q.get('question', '')}", "form", {"title": f"Answer {qid}", "fields": [{"name": "answer", "kind": "text", "label": "Your answer", "required": True}],
                                                                              "submit_cta": {"label": "Send answer", "selection": {"action": "ap_owner_answer", "qid": qid}}}, shop=shop))
    elif a == "ap_owner_answer":
        await deliver_answer(ctx, sender, shop, sel.get("qid", ""), str(sel.get("answer") or ""))
    elif a == "ap_owner_mark_paid":
        o = sget(ctx, f"order:{sel.get('order_id', '')}")
        if not o or o["shop"] != shop["code"]:
            await ctx.send(sender, text_msg("Order not found."))
            return True
        o["status"] = "paid"
        o["paid_at"] = time.time()
        sset(ctx, f"order:{o['id']}", o)
        await ctx.send(o["customer"], text_msg(f"✅ **{shop['name']}** confirmed your payment of {rupees(o['total'])} for order {o['id']}. Thank you! " +
                                               ("We'll deliver to your address." if o["fulfilment"] == "delivery" else "We'll message you when it's ready for pickup.")))
        await ctx.send(sender, text_msg(f"Marked {o['id']} as paid and sent the customer a receipt."))
    elif a == "ap_owner_order_view":
        o = sget(ctx, f"order:{sel.get('order_id', '')}")
        if o:
            await ctx.send(sender, text_msg(format_order(o)))
    elif a == "ap_upgrade":
        await request_pro_payment(ctx, sender, shop)
    else:
        return False
    return True


def format_order(o: dict) -> str:
    lines = [f"**Order {o['id']}** · {o['status'].replace('_', ' ')}"]
    lines += [f"- {l['qty']} × {l['name']} @ {rupees(l['unit'])} = {rupees(l['amount'])}" for l in o["lines"]]
    lines += [f"Subtotal {rupees(o['subtotal'])}", f"Delivery {rupees(o['delivery_fee'])}", f"**Total {rupees(o['total'])}**",
              f"{o['customer_name']} · {o['phone']} · {o['fulfilment']}" + (f" · {o['address']}" if o.get("address") else ""), (f"Notes: {o['notes']}" if o.get("notes") else "")]
    return "\n".join(x for x in lines if x)


# ---------------------------------------------------------------------------
# Customer flows
# ---------------------------------------------------------------------------


async def show_shop(ctx: Context, customer: str, shop: dict):
    sset(ctx, f"cust:{customer}", shop["code"])
    remember_card(ctx, customer, ["ap_start_order"])
    deliv = f"Delivery to {', '.join(shop['delivery_areas'])} ({rupees(shop.get('delivery_fee', 0))})" if shop.get("delivery_areas") else "Pickup only"
    await ctx.send(customer, card_msg(
        f"Welcome to **{shop['name']}**! {deliv} · Minimum order {rupees(shop.get('min_order', 0))} · {shop.get('hours') or ''}\n"
        "Ask me anything (prices, eggless, delivery...) or tap an item to order.", "carousel", {
            "title": shop["name"], "subtitle": "Tap to order",
            "items": [{"id": it["id"], "title": it["name"], "secondary_text": rupees(it["price"]),
                       "primary_cta": {"label": "Order", "selection": {"action": "ap_start_order", "shop": shop["code"]}}} for it in shop["items"][:10]]}))


async def handle_customer_action(ctx: Context, sender: str, sel: dict) -> bool:
    a = sel.get("action")
    code = (sel.get("shop") or sget(ctx, f"cust:{sender}") or "").upper()
    shop = get_shop(ctx, code) if code else None
    if a == "ap_start_order":
        if not shop or shop["status"] != "live":
            await ctx.send(sender, text_msg("That shop isn't taking orders right now."))
            return True
        if not is_pro(shop) and orders_this_month(shop) >= env_int("FREE_ORDERS_PER_MONTH", 25):
            await ctx.send(sender, text_msg(f"{shop['name']} has reached its order limit for this month on the free plan. I've let the owner know."))
            if shop.get("owner"):
                await ctx.send(shop["owner"], text_msg("A customer tried to order but you've used this month's free orders. Say **upgrade** to unlock unlimited orders."))
            return True
        sset(ctx, f"cust:{sender}", shop["code"])
        fields = [f"qty_{it['id']}" for it in shop["items"]] + ["fulfilment", "customer_name", "phone", "address", "notes"]
        remember_card(ctx, sender, ["ap_place_order"], fields=fields)
        await ctx.send(sender, card_msg("Choose quantities (leave blank for 0):", "form", order_form(shop)))
    elif a == "ap_place_order":
        if not shop:
            return False
        calc = compute_order(shop, sel)
        if calc.get("error"):
            remember_card(ctx, sender, ["ap_place_order"], fields=[f"qty_{it['id']}" for it in shop["items"]] + ["fulfilment", "customer_name", "phone", "address", "notes"])
            await ctx.send(sender, card_msg("⚠️ " + calc["error"], "form", order_form(shop)))
            return True
        draft_id = new_id("D-", 6)
        sset(ctx, f"draft:{sender}", {"id": draft_id, "shop": shop["code"], **calc})
        rows = [{"label": f"{l['qty']} × {l['name']}", "value": rupees(l["amount"])} for l in calc["lines"]]
        rows += [{"label": "Subtotal", "value": rupees(calc["subtotal"])}, {"label": "Delivery", "value": rupees(calc["delivery_fee"])},
                 {"label": "Total (pay by UPI)", "value": rupees(calc["total"])}, {"label": "Name / phone", "value": f"{calc['customer_name']} / {calc['phone']}"},
                 {"label": "Fulfilment", "value": calc["fulfilment"].title() + (f" - {calc['address']}" if calc["address"] else "")}]
        remember_card(ctx, sender, ["ap_confirm_order", "ap_cancel_order"], ids={"draft_id": [draft_id]})
        await ctx.send(sender, card_msg("Please check your order:", "review", {
            "title": f"Order from {shop['name']}", "summary_rows": rows,
            "approve_cta": {"label": f"Place order · {rupees(calc['total'])}", "primary": True, "selection": {"action": "ap_confirm_order", "draft_id": draft_id}},
            "reject_cta": {"label": "Cancel", "selection": {"action": "ap_cancel_order", "draft_id": draft_id}}}))
    elif a == "ap_confirm_order":
        d = sget(ctx, f"draft:{sender}")
        if not d or d["id"] != sel.get("draft_id", d["id"] if d else None):
            await ctx.send(sender, text_msg("That order draft expired. Say **order** to start again."))
            return True
        shop = get_shop(ctx, d["shop"])
        sdel(ctx, f"draft:{sender}")
        oid = new_id("ORD-", 5)
        order = {**{k: v for k, v in d.items() if k != "id"}, "id": oid, "customer": sender, "status": "awaiting_payment", "ts": time.time()}
        sset(ctx, f"order:{oid}", order)
        sset(ctx, f"shop_orders:{shop['code']}", (sget(ctx, f"shop_orders:{shop['code']}", []) + [oid])[-200:])
        shop.setdefault("usage", {})[month_key()] = orders_this_month(shop) + 1
        save_shop(ctx, shop)
        link = upi_link(shop, order["total"], f"{oid} {shop['name']}")
        remember_card(ctx, sender, ["ap_customer_paid", "ap_cancel_order"], ids={"order_id": [oid]})
        await ctx.send(sender, card_msg(
            f"🧾 Order **{oid}** placed. Pay **{rupees(order['total'])}** by UPI to `{shop['upi_id']}` (note: {oid}).\n"
            f"Scan the QR with any UPI app, or on your phone tap: {link}\n\nThen press **I've paid** - the owner confirms and you'll get a receipt here.",
            "detail", {"title": f"Pay {rupees(order['total'])} · {oid}", "hero_image": qr_url(link),
                       "summary_rows": [{"label": "Pay to", "value": f"{shop.get('payee') or shop['name']} ({shop['upi_id']})"},
                                        {"label": "Amount", "value": rupees(order["total"])}, {"label": "Note / reference", "value": oid}],
                       "ctas": [{"label": "I've paid", "primary": True, "selection": {"action": "ap_customer_paid", "order_id": oid}},
                                {"label": "Cancel order", "selection": {"action": "ap_cancel_order", "order_id": oid}}]}))
        if shop.get("owner"):
            await ctx.send(shop["owner"], text_msg("🛒 New order!\n" + format_order(order) + "\n\nI've sent the customer your UPI link. Say **orders** to confirm payment once it arrives."))
    elif a == "ap_customer_paid":
        o = sget(ctx, f"order:{sel.get('order_id', '')}")
        if not o or o["customer"] != sender:
            await ctx.send(sender, text_msg("I can't find that order."))
            return True
        if o["status"] == "awaiting_payment":
            o["status"] = "payment_claimed"
            sset(ctx, f"order:{o['id']}", o)
        shop = get_shop(ctx, o["shop"])
        await ctx.send(sender, text_msg(f"Thanks! I've asked {shop['name']} to confirm {rupees(o['total'])} for {o['id']}. You'll get a receipt here."))
        if shop and shop.get("owner"):
            remember_card(ctx, shop["owner"], ["ap_owner_mark_paid"], ids={"order_id": [o["id"]]})
            await ctx.send(shop["owner"], card_msg(f"💰 Customer says they paid **{rupees(o['total'])}** for {o['id']} (UPI note {o['id']}). Check your UPI app, then confirm:", "review", {
                "title": f"Confirm payment · {o['id']}", "summary_rows": [{"label": "Amount", "value": rupees(o["total"])}, {"label": "Customer", "value": f"{o['customer_name']} · {o['phone']}"}],
                "approve_cta": {"label": "Yes, received", "primary": True, "selection": {"action": "ap_owner_mark_paid", "order_id": o["id"]}},
                "reject_cta": {"label": "Not yet", "selection": {"action": "ap_owner_orders", "shop": o["shop"]}}}, shop=shop))
    elif a == "ap_cancel_order":
        oid = sel.get("order_id")
        if oid:
            o = sget(ctx, f"order:{oid}")
            if o and o["customer"] == sender and o["status"] in ("awaiting_payment", "payment_claimed"):
                o["status"] = "cancelled"
                sset(ctx, f"order:{oid}", o)
                sh = get_shop(ctx, o["shop"])
                if sh and sh.get("owner"):
                    await ctx.send(sh["owner"], text_msg(f"Order {oid} was cancelled by the customer."))
        sdel(ctx, f"draft:{sender}")
        await ctx.send(sender, text_msg("Cancelled. Anything else I can help with?"))
    else:
        return False
    return True


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def resolve_selection(ctx: Context, sender: str, text: str) -> Optional[dict]:
    sel = try_json(text)
    if sel and sel.get("action"):
        return sel
    card = sget(ctx, f"ap:card:{sender}") or {}
    low = text.lower()
    action = next((a for a in card.get("actions", []) if a.lower() in low or a.replace("_", " ") in low), None)
    if not action:
        m = re.search(r"\b(ap_[a-z_]+)\b", low)  # relayed press from an address with no remembered card
        if not m:
            return None
        action = m.group(1)
        for key, pat in (("order_id", r"\bORD-[A-Z0-9]{5}\b"), ("qid", r"\bQ-[A-Z0-9]{4}\b"), ("draft_id", r"\bD-[A-Z0-9]{6}\b")):
            mm = re.search(pat, text.upper())
            if mm:
                card.setdefault("ids", {})[key] = [mm.group(0)]
    sel = {"action": action}
    for key, values in (card.get("ids") or {}).items():
        for v in values:
            if v and v.lower() in low:
                sel[key] = v
                break
    mk = re.search(r"\bok_[a-z0-9]{10}\b", text)
    if mk:
        sel["ok"] = mk.group(0)
    for code in re.findall(r"\b[A-Z0-9]{4,6}\b", text):
        if "shop" not in sel and get_shop(ctx, code):
            sel["shop"] = code
    fields = card.get("fields") or []
    if fields:
        for f in fields:  # key-value pairs the planner echoed, e.g. "qty_i1: 2"
            m = re.search(re.escape(f) + r"\W{0,4}[:=]\s*\"?([^,;\"\n]+)", text, re.I)
            if m:
                sel[f] = m.group(1).strip()
        missing = [f for f in fields if f not in sel]
        if missing:
            got = asi1_json("Extract form values from the text. Reply ONLY with a JSON object using exactly these keys (omit unknown): " + ", ".join(missing), text) or {}
            sel.update({k: v for k, v in got.items() if k in missing and v not in (None, "")})
    return sel


OWNER_WORDS = re.compile(r"\b(dashboard|my shop|orders|inbox|upgrade|go pro|edit (?:my )?(?:menu|shop)|answer\s+q-|confirm\s+ord-|paid\s+ord-)\b", re.I)


chat_proto = Protocol(spec=chat_protocol_spec)


@chat_proto.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    text = strip_mentions(" ".join(c.text for c in msg.content if isinstance(c, TextContent)))
    if not text:
        return
    ctx.logger.info(f"[inbound] chat from {sender[:16]}: {text[:120]!r}")
    try:
        await route(ctx, sender, text)
    except Exception as e:
        ctx.logger.error(f"[error] {e}")
        await ctx.send(sender, text_msg("Sorry, something went wrong on my side. Please try again."))


async def route(ctx: Context, sender: str, text: str):
    low = text.lower().strip()
    owned = get_shop(ctx, sget(ctx, f"owner:{sender}") or "")
    if owned and owned.get("owner") != sender:
        owned = None  # ownership moved to another address
    ctx.logger.info(f"[route] sender={sender} owner_of={owned['code'] if owned else None} text={text[:80]!r}")

    # 1) Card selections (JSON from @mention, or planner prose)
    sel = resolve_selection(ctx, sender, text)
    if sel:
        a = str(sel.get("action", ""))
        if a.startswith("ap_owner") or a in {"ap_save_shop", "ap_edit_shop", "ap_go_live", "ap_upgrade"}:
            target = get_shop(ctx, sel.get("shop") or "") if sel.get("shop") else owned
            key_ok = bool(target and sel.get("ok") and sel.get("ok") == target.get("owner_token"))
            if not target or (target.get("owner") != sender and not key_ok):
                ctx.logger.warning(f"[owner] refused {a} from {sender} for {target.get('code') if target else None}")
                await ctx.send(sender, text_msg("Only the shop owner can do that. If this is your shop, type `setup <CODE> <PIN>` in this chat to link it."))
                return
            if await handle_owner_action(ctx, sender, target, sel):
                return
        elif await handle_customer_action(ctx, sender, sel):
            return

    # 2) Owner claim / creation (works even if ASI:One rewords "setup DEMO 2026")
    if re.search(r"\b(set\s*up|claim)\b", low):
        codes = [t.upper() for t in re.findall(r"\b[A-Za-z0-9]{4,6}\b", text) if not t.isdigit() and get_shop(ctx, t)]
        pins = re.findall(r"\b\d{4}\b", text)
        if codes and pins:
            shop = get_shop(ctx, codes[0])
            if shop.get("claim_pin") not in pins:
                await ctx.send(sender, text_msg(f"The PIN for storefront {shop['code']} is not correct."))
                return
            prev = shop.get("owner")
            if prev and prev != sender:  # the PIN is the proof of ownership; move ownership to this address
                sdel(ctx, f"owner:{prev}")
            shop["owner"] = sender
            save_shop(ctx, shop)
            sset(ctx, f"owner:{sender}", shop["code"])
            ctx.logger.info(f"[owner] {sender} now owns {shop['code']}")
            remember_card(ctx, sender, ["ap_owner_orders", "ap_owner_inbox", "ap_edit_shop", "ap_upgrade"])
            if shop.get("items") and shop.get("upi_id"):
                await owner_dashboard(ctx, sender, shop, f"✅ You now own **{shop['name']}** ({shop['code']}). Type `dashboard`, `inbox` or `orders` any time.")
            else:
                remember_card(ctx, sender, ["ap_save_shop"], fields=SETUP_FIELDS)
                await ctx.send(sender, card_msg(f"✅ You now own storefront **{shop['code']}**. Add your menu and payment details:", "form", setup_form(shop), shop=shop))
            return
    if re.match(r"^\s*(new shop|create (a )?shop|i am (a|the) (shop )?owner|setup)\s*$", low):
        if owned:
            await owner_dashboard(ctx, sender, owned, f"You already own **{owned['name']}** ({owned['code']}).")
            return
        code = new_id("", 5)
        while get_shop(ctx, code):
            code = new_id("", 5)
        shop = {"code": code, "name": "", "owner": sender, "status": "draft", "claim_pin": str(random.randint(1000, 9999)), "items": [], "facts": [], "usage": {}, "pro_until": 0, "created": time.time()}
        save_shop(ctx, shop)
        sset(ctx, f"owner:{sender}", code)
        remember_card(ctx, sender, ["ap_save_shop"], fields=SETUP_FIELDS)
        await ctx.send(sender, card_msg(f"Let's set up your storefront **{code}**:", "form", setup_form(shop), shop=shop))
        return

    # 3) Owner text commands (matched anywhere in the message)
    ow = OWNER_WORDS.search(text)
    if ow and owned:
        word = ow.group(1).lower()
        ma = re.search(r"answer\s+(q-[a-z0-9]+)\s+(.+)$", text, re.I | re.S)
        mp = re.search(r"(?:confirm|paid)\s+(ord-[a-z0-9]+)", text, re.I)
        if ma:
            await deliver_answer(ctx, sender, owned, ma.group(1).upper(), ma.group(2))
        elif mp:
            await handle_owner_action(ctx, sender, owned, {"action": "ap_owner_mark_paid", "order_id": mp.group(1).upper()})
        elif word == "orders":
            await owner_orders(ctx, sender, owned)
        elif word == "inbox":
            await owner_inbox(ctx, sender, owned)
        elif word in ("upgrade", "go pro"):
            await request_pro_payment(ctx, sender, owned)
        elif word.startswith("edit"):
            await handle_owner_action(ctx, sender, owned, {"action": "ap_edit_shop"})
        else:
            await owner_dashboard(ctx, sender, owned)
        return
    if ow and not owned and re.search(r"\b(dashboard|inbox|my shop|upgrade|go pro)\b", low):
        await ctx.send(sender, text_msg(
            "That's an owner command, but this chat isn't linked to a shop you own yet.\n"
            "• Demo shop: type `setup DEMO 2026`\n• Your own shop: type `new shop`\n\n"
            f"(Your chat address ends in …{sender[-6:]}; if it keeps saying this after setup, the address is changing between messages.)"))
        return

    # 4) Customer: choose a shop
    m = re.search(r"\b(?:shop|store|code|from)\s+([A-Za-z0-9]{4,6})\b", text, re.I) or re.search(r"\b(DEMO|[A-Z0-9]{5})\b", text)
    if m and get_shop(ctx, m.group(1)) and get_shop(ctx, m.group(1))["status"] == "live":
        await show_shop(ctx, sender, get_shop(ctx, m.group(1)))
        return
    if re.search(r"\bdemo\b", low):
        await show_shop(ctx, sender, get_shop(ctx, "DEMO"))
        return

    code = sget(ctx, f"cust:{sender}") or (owned["code"] if owned and owned["status"] == "live" else None)
    shop = get_shop(ctx, code) if code else None
    if not shop:
        await ctx.send(sender, text_msg(
            "Hi! I'm **Customer Care Autopilot** - the 24×7 front desk for small Indian businesses.\n\n"
            "• **Customers:** say `shop <CODE>` (try `shop DEMO`) to see the menu, ask questions and order with UPI.\n"
            "• **Owners:** say `new shop` (or `setup <CODE> <PIN>` from Udyam Launchpad) to put your business on autopilot."))
        return

    # 5) Customer: order status / start order / question
    mo = re.search(r"\b(ORD-[A-Z0-9]{5})\b", text.upper())
    if mo:
        o = sget(ctx, f"order:{mo.group(1)}")
        if o and o["customer"] == sender:
            await ctx.send(sender, text_msg(format_order(o)))
            return
    if re.search(r"\b(order|buy|i want|i'd like|book|menu)\b", low) and not re.search(r"\?$", low):
        if "menu" in low and "order" not in low:
            await show_shop(ctx, sender, shop)
        else:
            await handle_customer_action(ctx, sender, {"action": "ap_start_order", "shop": shop["code"]})
        return
    ans = await asyncio.to_thread(answer_question, shop, text)
    if ans:
        await ctx.send(sender, text_msg(ans + "\n\nWant to order? Just say **order**."))
    else:
        await escalate(ctx, shop, sender, text)


@chat_proto.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


# ---------------------------------------------------------------------------
# Hand-off from Udyam Launchpad
# ---------------------------------------------------------------------------

peer_proto = Protocol(name="AutopilotStorefronts", version="1.0.0")


@peer_proto.on_message(StorefrontDraft, replies={StorefrontCreated})
async def on_draft(ctx: Context, sender: str, msg: StorefrontDraft):
    allowed = (os.getenv("LAUNCHPAD_ADDRESS") or "").strip()
    if allowed and sender != allowed:
        await ctx.send(sender, StorefrontCreated(request_id=msg.request_id, ok=False, error="caller not allowed"))
        return
    prev = sget(ctx, f"draft_req:{msg.request_id}")  # idempotent on retries
    if prev:
        await ctx.send(sender, StorefrontCreated(request_id=msg.request_id, ok=True, shop_code=prev["code"], claim_pin=prev["pin"]))
        return
    code = new_id("", 5)
    while get_shop(ctx, code):
        code = new_id("", 5)
    pin = str(random.randint(1000, 9999))
    save_shop(ctx, {"code": code, "name": msg.business_name[:60], "owner": None, "status": "draft", "claim_pin": pin,
                    "city": msg.city, "state": msg.state, "business_type": msg.business_type, "items": [], "facts": [],
                    "usage": {}, "pro_until": 0, "created": time.time()})
    sset(ctx, f"draft_req:{msg.request_id}", {"code": code, "pin": pin})
    await ctx.send(sender, StorefrontCreated(request_id=msg.request_id, ok=True, shop_code=code, claim_pin=pin))


agent.include(chat_proto, publish_manifest=True)
agent.include(payment_proto, publish_manifest=True)
agent.include(peer_proto, publish_manifest=True)


@agent.on_event("startup")
async def startup(ctx: Context):
    seed_demo(ctx)
    ctx.logger.info(f"Customer Care Autopilot up. Address: {agent.address} wallet: {agent.wallet.address()}")


if __name__ == "__main__":
    agent.run()
