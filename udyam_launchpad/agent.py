"""
Udyam Launchpad — Agentverse HOSTED agent (Blank Agent -> paste into agent.py)

The founder-facing orchestrator:
  idea in plain language -> business profile (ASI:One LLM + keyword fallback)
  -> review/form cards -> Udyam Compliance Engine (agent-to-agent, deterministic rules)
  -> dated launch plan (carousel + detail cards, Google Calendar links, progress tracking)
  -> hands off to Customer Care Autopilot, which reserves a storefront for the founder.

Secrets (Agentverse -> Build -> Agent Secrets):
  ENGINE_ADDRESS     agent1... address of your hosted Udyam Compliance Engine   (required)
  AUTOPILOT_ADDRESS  agent1... address of your hosted Customer Care Autopilot   (required for hand-off)
  ASI_ONE_API_KEY    ASI:One API key (optional - improves understanding of free text)
  ASI_ONE_MODEL      optional, default "asi1"
"""

import asyncio
import json
import os
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

agent = Agent()

IST = timezone(timedelta(hours=5, minutes=30))
ENGINE_TIMEOUT_S = 20
MAX_TRIES = 2

# ---------------------------------------------------------------------------
# Agent-to-agent models (IDENTICAL copies live in the other agents)
# ---------------------------------------------------------------------------


class ComplianceRequest(Model):
    request_id: str
    kind: str  # "plan" | "check_link"
    payload_json: str


class ComplianceResponse(Model):
    request_id: str
    ok: bool
    result_json: str
    error: Optional[str] = None


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
# Reference lists (UI only - the legal logic lives in the Compliance Engine)
# ---------------------------------------------------------------------------

BUSINESS_TYPES = {
    "home_food": "Home food (bakery, tiffin, snacks)",
    "restaurant_cafe": "Restaurant / cafe / cloud kitchen",
    "kirana_retail": "Kirana / retail shop",
    "online_seller": "Online seller (products)",
    "services": "Services (tuition, salon, design, repairs)",
    "manufacturing": "Small manufacturing / crafts",
}
STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat",
    "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh",
    "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab",
    "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh",
    "Uttarakhand", "West Bengal", "Andaman and Nicobar Islands", "Chandigarh",
    "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Jammu and Kashmir", "Ladakh",
    "Lakshadweep", "Puducherry",
]
CITY_STATE = {
    "bengaluru": "Karnataka", "bangalore": "Karnataka", "mysuru": "Karnataka", "mysore": "Karnataka", "mangaluru": "Karnataka",
    "hubli": "Karnataka", "mumbai": "Maharashtra", "pune": "Maharashtra", "nagpur": "Maharashtra", "nashik": "Maharashtra",
    "delhi": "Delhi", "chennai": "Tamil Nadu", "coimbatore": "Tamil Nadu", "madurai": "Tamil Nadu", "hyderabad": "Telangana",
    "kolkata": "West Bengal", "ahmedabad": "Gujarat", "surat": "Gujarat", "vadodara": "Gujarat", "jaipur": "Rajasthan",
    "lucknow": "Uttar Pradesh", "noida": "Uttar Pradesh", "kanpur": "Uttar Pradesh", "varanasi": "Uttar Pradesh",
    "gurugram": "Haryana", "gurgaon": "Haryana", "kochi": "Kerala", "thiruvananthapuram": "Kerala", "patna": "Bihar",
    "bhopal": "Madhya Pradesh", "indore": "Madhya Pradesh", "bhubaneswar": "Odisha", "chandigarh": "Chandigarh",
    "guwahati": "Assam", "visakhapatnam": "Andhra Pradesh", "vijayawada": "Andhra Pradesh", "dehradun": "Uttarakhand",
    "ranchi": "Jharkhand", "raipur": "Chhattisgarh", "panaji": "Goa", "ludhiana": "Punjab", "amritsar": "Punjab",
}

# ---------------------------------------------------------------------------
# Small helpers
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
        ctx.logger.warning(f"[state] storage set failed for {key}: {e}")


def text_msg(text: str) -> ChatMessage:
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=[TextContent(type="text", text=text)])


def card_msg(narration: str, kind: str, payload: dict) -> ChatMessage:
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


def remember_card(ctx: Context, sender: str, actions: list, ids: Optional[dict] = None, fields: Optional[list] = None):
    """Store what the last card offered, so planner prose can be mapped back to a selection."""
    sset(ctx, f"lp:card:{sender}", {"actions": actions, "ids": ids or {}, "fields": fields or []})


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


def asi1_json(system: str, user: str, max_tokens: int = 500) -> Optional[dict]:
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


def canonical_state(s) -> Optional[str]:
    if not s:
        return None
    t = str(s).strip().lower()
    for st in STATES:
        if st.lower() == t:
            return st
    for st in sorted(STATES, key=len, reverse=True):
        if re.search(r"\b" + re.escape(st.lower()) + r"\b", t):
            return st
    for city, st in CITY_STATE.items():
        if re.search(r"\b" + city + r"\b", t):
            return st
    return None


def keyword_profile(text: str) -> dict:
    low = text.lower()
    p: dict = {}
    types = [
        ("home_food", ["bakery", "bake", "cake", "tiffin", "home food", "pickle", "snack", "sweets", "cookies", "homemade food"]),
        ("restaurant_cafe", ["restaurant", "cafe", "café", "cloud kitchen", "dhaba", "food truck", "eatery"]),
        ("kirana_retail", ["kirana", "grocery", "general store", "provision store", "retail shop", "supermarket"]),
        ("online_seller", ["amazon", "flipkart", "meesho", "online store", "sell online", "e-commerce", "ecommerce", "brand", "t-shirt", "tshirt"]),
        ("services", ["tuition", "salon", "repair", "design", "freelanc", "consult", "coaching", "photograph"]),
        ("manufacturing", ["manufactur", "handicraft", "craft", "candle", "soap", "pottery", "workshop"]),
    ]
    for k, words in types:
        if any(w in low for w in words):
            p["business_type"] = k
            break
    p["state"] = canonical_state(text)
    for city in CITY_STATE:
        if re.search(r"\b" + city + r"\b", low):
            p["city"] = city.title()
            break
    m = re.search(r"([\d,.]+)\s*(lakh|lac|l\b|k\b|thousand|crore|cr\b)", low)
    if m:
        n = float(m.group(1).replace(",", ""))
        unit = m.group(2)
        val = n if unit in ("lakh", "lac", "l") else n / 100 if unit in ("k", "thousand") else n * 100
        if re.search(r"(per|a|each|/)\s*month|monthly", low[m.end():m.end() + 25]):
            val *= 12
        p["turnover_lakh"] = round(val, 2)
    if any(w in low for w in ["amazon", "flipkart", "meesho", "marketplace"]):
        p["sells_online"] = "marketplace"
    elif any(w in low for w in ["instagram", "whatsapp", "website", "online"]):
        p["sells_online"] = "social"
    if any(w in low for w in ["all over india", "across india", "other states", "pan india", "pan-india", "ship to"]):
        p["interstate"] = True
    if any(w in low for w in ["shop", "store", "outlet", "premises"]) and p.get("business_type") != "online_seller":
        p["has_shop"] = True
    if any(w in low for w in ["packed", "packaged", "jar", "pouch"]):
        p["packaged"] = True
    m = re.search(r"(\d+)\s*(employees|staff|workers|people working)", low)
    if m:
        p["employees"] = int(m.group(1))
    m = re.search(r"(?:called|named|brand name is|name is)\s+[\"']?([A-Za-z0-9&' ]{2,40})", text)
    if m:
        p["business_name"] = m.group(1).strip().rstrip(".")
    return {k: v for k, v in p.items() if v not in (None, "")}


PROFILE_SYSTEM = (
    "Extract a small-business profile for India from the user's message. Reply with ONLY a JSON object with keys: "
    "business_name (string or null), business_type (one of home_food, restaurant_cafe, kirana_retail, online_seller, services, manufacturing, or null), "
    "state (Indian state/UT name or null; infer from city), city (string or null), turnover_lakh (expected annual sales in lakh rupees, number or null; "
    "convert monthly figures x12, thousands /100), sells_online (none|social|marketplace or null), interstate (true/false/null), "
    "has_shop (true/false/null), packaged (true/false/null), employees (integer or null). Never guess values that are not implied."
)


def extract_profile(text: str) -> dict:
    p = keyword_profile(text)
    llm = asi1_json(PROFILE_SYSTEM, text) or {}
    for k, v in llm.items():
        if v not in (None, "", "null") and k in {"business_name", "business_type", "state", "city", "turnover_lakh", "sells_online", "interstate", "has_shop", "packaged", "employees"}:
            if k == "state":
                v = canonical_state(v)
            if k == "business_type" and v not in BUSINESS_TYPES:
                continue
            if v is not None:
                p[k] = v
    return p


def profile_form() -> dict:
    return {
        "title": "Your business in 30 seconds",
        "fields": [
            {"name": "business_name", "kind": "text", "label": "Business / brand name"},
            {"name": "business_type", "kind": "select", "label": "Type of business", "required": True,
             "options": [{"value": k, "label": v} for k, v in BUSINESS_TYPES.items()]},
            {"name": "state", "kind": "select", "label": "State / UT", "required": True,
             "options": [{"value": s, "label": s} for s in STATES]},
            {"name": "city", "kind": "text", "label": "City"},
            {"name": "turnover_lakh", "kind": "number", "label": "Expected sales in first year (₹ lakh, e.g. 3)", "required": True},
            {"name": "sells_online", "kind": "select", "label": "Where will you sell?",
             "options": [{"value": "none", "label": "In person / local only"},
                         {"value": "social", "label": "WhatsApp / Instagram / own website"},
                         {"value": "marketplace", "label": "Amazon / Flipkart / Meesho"}]},
            {"name": "interstate", "kind": "checkbox", "label": "I will ship goods to other states"},
            {"name": "has_shop", "kind": "checkbox", "label": "I have a separate shop or premises"},
            {"name": "packaged", "kind": "checkbox", "label": "I sell pre-packed products"},
            {"name": "employees", "kind": "number", "label": "Employees (0 if just you)"},
        ],
        "submit_cta": {"label": "Continue", "selection": {"action": "lp_submit_profile"}},
    }


PROFILE_FIELDS = ["business_name", "business_type", "state", "city", "turnover_lakh", "sells_online", "interstate", "has_shop", "packaged", "employees"]


def profile_rows(p: dict) -> list:
    yn = lambda v: "Yes" if str(v).lower() in {"true", "yes", "1", "on"} else "No"
    rows = [
        {"label": "Business", "value": p.get("business_name") or "(unnamed)"},
        {"label": "Type", "value": BUSINESS_TYPES.get(p.get("business_type"), "?")},
        {"label": "Location", "value": ", ".join([x for x in [p.get("city"), p.get("state")] if x]) or "?"},
        {"label": "Expected first-year sales", "value": f"₹{p.get('turnover_lakh', 0)} lakh"},
        {"label": "Selling channel", "value": {"none": "Local / in person", "social": "WhatsApp / Instagram / website", "marketplace": "Marketplaces"}.get(p.get("sells_online") or "none")},
        {"label": "Ships to other states", "value": yn(p.get("interstate"))},
        {"label": "Separate shop", "value": yn(p.get("has_shop"))},
        {"label": "Employees", "value": str(p.get("employees") or 0)},
    ]
    return rows


def gcal_link(title: str, day, details: str) -> str:
    d1 = day.strftime("%Y%m%d")
    d2 = (day + timedelta(days=1)).strftime("%Y%m%d")
    return "https://calendar.google.com/calendar/render?" + urlencode(
        {"action": "TEMPLATE", "text": title, "dates": f"{d1}/{d2}", "details": details[:900]}, quote_via=quote)


def step_date(start_iso: str, offset: int):
    start = datetime.fromisoformat(start_iso).date()
    d = start + timedelta(days=int(offset))
    if d.weekday() == 6:  # government offices closed Sunday; online portals fine, but keep the plan humane
        d += timedelta(days=1)
    return d


BADGE = {"mandatory": ("Required", "warning"), "conditional": ("Check", "info"), "recommended": ("Recommended", "success"),
         "optional": ("Optional", "info"), "prerequisite": ("Do first", "info")}


def render_plan(plan: dict) -> tuple:
    r = plan["result"]
    p = r["profile"]
    done = set(plan.get("done", []))
    title_name = p.get("business_name") or BUSINESS_TYPES.get(p["business_type"], "your business")
    lines = [f"## Launch plan: {title_name}",
             f"{BUSINESS_TYPES.get(p['business_type'])} · {', '.join([x for x in [p.get('city'), p.get('state')] if x])} · "
             f"**{r['classification']}** · rules v{r['rules_version']} from the Udyam Compliance Engine", ""]
    items = []
    for i, s in enumerate(r["steps"], 1):
        d = step_date(plan["start"], s["day_offset"])
        tick = "✅ " if s["id"] in done else ""
        cal = gcal_link(f"[{title_name}] {s['title']}", d, f"{s['why']}\nOfficial portal: {s.get('portal', '')}\nFee: {s['fee']}")
        portal = f" · [official portal]({s['portal']})" if s.get("portal") else ""
        lines.append(f"{i}. {tick}**{s['title']}** — by {d.strftime('%a %d %b')} · {s['fee'].split('.')[0]}{portal} · [add to calendar]({cal})")
        lbl, var = BADGE.get(s["category"], ("Info", "info"))
        badges = [{"label": "Done", "variant": "success"}] if s["id"] in done else [{"label": lbl, "variant": var}]
        if s.get("confidence") == "verify_locally":
            badges.append({"label": "Varies by state", "variant": "info"})
        items.append({
            "id": s["id"], "title": s["title"], "subtitle": f"By {d.strftime('%d %b')} · {s['authority']}",
            "badges": badges, "secondary_text": s["fee"].split(".")[0][:60],
            "primary_cta": {"label": "Details", "selection": {"action": "lp_step_detail", "step_id": s["id"]}},
        })
    lines += ["", "🚨 " + r["warnings"][0],
              "", "Tap a step for documents and links. When you're ready to take orders, say **set up my front desk**.",
              "", "_" + r["disclaimer"] + "_"]
    payload = {"title": "Your launch plan", "subtitle": f"{len(done)}/{len(r['steps'])} done", "items": items}
    return "\n".join(lines), payload


def step_detail_card(plan: dict, step_id: str) -> Optional[dict]:
    s = next((x for x in plan["result"]["steps"] if x["id"] == step_id), None)
    if not s:
        return None
    d = step_date(plan["start"], s["day_offset"])
    rows = [
        {"label": "Why", "value": s["why"]},
        {"label": "Do it by", "value": f"{d.strftime('%a %d %b %Y')} ({s['deadline']})"},
        {"label": "Authority", "value": s["authority"]},
        {"label": "Government fee", "value": s["fee"]},
        {"label": "Documents", "value": "; ".join(s["documents"])},
    ]
    if s.get("portal"):
        rows.append({"label": "Official portal", "value": s["portal"]})
    if s.get("penalty"):
        rows.append({"label": "If skipped", "value": s["penalty"]})
    rows.append({"label": "Source", "value": "; ".join(f"{x['name']} ({x['url']})" for x in s["sources"])})
    if s.get("confidence") == "verify_locally":
        rows.append({"label": "Note", "value": "Rules vary by state/municipality - confirm locally."})
    return {
        "title": s["title"],
        "summary_rows": rows,
        "ctas": [
            {"label": "Mark as done", "primary": True, "selection": {"action": "lp_step_done", "step_id": s["id"]}},
            {"label": "Back to plan", "selection": {"action": "lp_show_plan"}},
        ],
    }


# ---------------------------------------------------------------------------
# Engine / Autopilot calls with retry + timeout recovery
# ---------------------------------------------------------------------------


def engine_address() -> str:
    return (os.getenv("ENGINE_ADDRESS") or "").strip()


def autopilot_address() -> str:
    return (os.getenv("AUTOPILOT_ADDRESS") or "").strip()


async def call_engine(ctx: Context, sender: str, kind: str, payload: dict):
    addr = engine_address()
    if not addr:
        await ctx.send(sender, text_msg("Setup issue: ENGINE_ADDRESS secret is not set on Udyam Launchpad, so I can't reach the Compliance Engine."))
        return
    rid = uuid4().hex[:12]
    sset(ctx, f"lp:pending:{rid}", {"sender": sender, "kind": kind, "payload": payload, "ts": time.time(), "tries": 1, "to": "engine"})
    idx = sget(ctx, "lp:pending_index", [])
    sset(ctx, "lp:pending_index", (idx + [rid])[-50:])
    await ctx.send(addr, ComplianceRequest(request_id=rid, kind=kind, payload_json=json.dumps(payload)))
    ctx.logger.info(f"[outbound] ComplianceRequest {kind} rid={rid}")


def pop_pending(ctx: Context, rid: str) -> Optional[dict]:
    rec = sget(ctx, f"lp:pending:{rid}")
    if rec:
        try:
            ctx.storage.remove(f"lp:pending:{rid}")
        except Exception:
            sset(ctx, f"lp:pending:{rid}", None)
        sset(ctx, "lp:pending_index", [x for x in sget(ctx, "lp:pending_index", []) if x != rid])
    return rec


# ---------------------------------------------------------------------------
# Conversation flow
# ---------------------------------------------------------------------------


async def show_review(ctx: Context, sender: str, p: dict, intro: str):
    remember_card(ctx, sender, ["lp_confirm_profile", "lp_edit_profile"])
    await ctx.send(sender, card_msg(intro, "review", {
        "title": "Is this right?",
        "summary_rows": profile_rows(p),
        "approve_cta": {"label": "Build my launch plan", "primary": True, "selection": {"action": "lp_confirm_profile"}},
        "reject_cta": {"label": "Edit details", "selection": {"action": "lp_edit_profile"}},
    }))


async def show_form(ctx: Context, sender: str, intro: str):
    remember_card(ctx, sender, ["lp_submit_profile"], fields=PROFILE_FIELDS)
    await ctx.send(sender, card_msg(intro, "form", profile_form()))


async def show_plan(ctx: Context, sender: str, intro: str = ""):
    plan = sget(ctx, f"lp:plan:{sender}")
    if not plan:
        await show_form(ctx, sender, "Let's build your plan first. Tell me about the business:")
        return
    text, payload = render_plan(plan)
    remember_card(ctx, sender, ["lp_step_detail"], ids={"step_id": [s["id"] for s in plan["result"]["steps"]]})
    await ctx.send(sender, card_msg((intro + "\n\n" if intro else "") + text, "carousel", payload))


def resolve_selection(ctx: Context, sender: str, text: str) -> Optional[dict]:
    sel = try_json(text)
    if sel and sel.get("action"):
        return sel
    card = sget(ctx, f"lp:card:{sender}")
    if not card:
        return None
    low = text.lower()
    action = next((a for a in card.get("actions", []) if a.lower() in low or a.replace("_", " ") in low), None)
    if not action:
        return None
    sel = {"action": action}
    for key, values in (card.get("ids") or {}).items():
        for v in sorted(values, key=len, reverse=True):
            if v.lower() in low or v.replace("_", " ") in low:
                sel[key] = v
                break
    if card.get("fields"):
        sel.update(extract_profile(text))
    return sel


async def handle_selection(ctx: Context, sender: str, sel: dict):
    action = sel.get("action")
    if action == "lp_submit_profile":
        draft = sget(ctx, f"lp:profile:{sender}", {})
        for k in PROFILE_FIELDS:
            if k in sel and sel[k] not in (None, ""):
                draft[k] = sel[k]
        if sel.get("state"):
            draft["state"] = canonical_state(sel["state"]) or sel["state"]
        sset(ctx, f"lp:profile:{sender}", draft)
        missing = [k for k in ("business_type", "state", "turnover_lakh") if not draft.get(k) and draft.get(k) != 0]
        if missing:
            await show_form(ctx, sender, f"Almost there - I still need: {', '.join(missing)}.")
            return
        await show_review(ctx, sender, draft, "Here's your business profile:")
    elif action == "lp_edit_profile":
        await show_form(ctx, sender, "No problem - correct anything here:")
    elif action == "lp_confirm_profile":
        draft = sget(ctx, f"lp:profile:{sender}", {})
        if not draft.get("business_type") or not draft.get("state"):
            await show_form(ctx, sender, "I need a couple more details first:")
            return
        await ctx.send(sender, text_msg("Checking the official rules with the **Udyam Compliance Engine** (a separate rules agent - no AI guessing on legal steps)..."))
        await call_engine(ctx, sender, "plan", draft)
    elif action == "lp_step_detail":
        plan = sget(ctx, f"lp:plan:{sender}")
        card = step_detail_card(plan, sel.get("step_id", "")) if plan else None
        if not card:
            await show_plan(ctx, sender, "I couldn't find that step - here's the full plan:")
            return
        remember_card(ctx, sender, ["lp_step_done", "lp_show_plan"], ids={"step_id": [x["id"] for x in plan["result"]["steps"]]})
        await ctx.send(sender, card_msg(f"**{card['title']}** - everything you need:", "detail", card))
    elif action == "lp_step_done":
        plan = sget(ctx, f"lp:plan:{sender}")
        if not plan:
            await show_form(ctx, sender, "Let's build your plan first:")
            return
        valid = {x["id"] for x in plan["result"]["steps"]}
        if sel.get("step_id") not in valid:
            await show_plan(ctx, sender, "Which step did you finish? Tap it below and press Mark as done.")
            return
        done = set(plan.get("done", []))
        done.add(sel["step_id"])
        plan["done"] = sorted(x for x in done if x)
        sset(ctx, f"lp:plan:{sender}", plan)
        req = [s["id"] for s in plan["result"]["steps"] if s["category"] in ("mandatory", "prerequisite")]
        left = [x for x in req if x not in done]
        msg = "Marked done. " + ("All required steps are complete - you're ready to sell! Say **set up my front desk** to start taking orders." if not left else f"{len(left)} required step(s) left.")
        await show_plan(ctx, sender, msg)
    elif action == "lp_show_plan":
        await show_plan(ctx, sender)
    elif action == "lp_setup_autopilot":
        await start_handoff(ctx, sender)
    else:
        await welcome(ctx, sender)


async def start_handoff(ctx: Context, sender: str):
    addr = autopilot_address()
    draft = sget(ctx, f"lp:profile:{sender}", {})
    if not addr:
        await ctx.send(sender, text_msg("Setup issue: AUTOPILOT_ADDRESS secret is not set on Udyam Launchpad."))
        return
    rid = uuid4().hex[:12]
    sset(ctx, f"lp:pending:{rid}", {"sender": sender, "kind": "storefront", "payload": draft, "ts": time.time(), "tries": 1, "to": "autopilot"})
    sset(ctx, "lp:pending_index", (sget(ctx, "lp:pending_index", []) + [rid])[-50:])
    await ctx.send(addr, StorefrontDraft(
        request_id=rid, owner_address=sender, business_name=str(draft.get("business_name") or "My Shop"),
        business_type=str(draft.get("business_type") or ""), city=str(draft.get("city") or ""), state=str(draft.get("state") or "")))
    await ctx.send(sender, text_msg("Asking **Customer Care Autopilot** to reserve a storefront for you..."))


async def welcome(ctx: Context, sender: str):
    await show_form(ctx, sender,
        "Namaste! I'm **Udyam Launchpad**. Tell me your business idea in one line - e.g. *\"I want to start a home bakery in Bengaluru, about 30k sales a month on Instagram\"* - "
        "and I'll build your dated launch plan: Udyam, GST, FSSAI and local licences, with official links, documents, fees and reminders. "
        "Or fill this quick form:")


chat_proto = Protocol(spec=chat_protocol_spec)


@chat_proto.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    text = strip_mentions(" ".join(c.text for c in msg.content if isinstance(c, TextContent)))
    if not text:
        return
    ctx.logger.info(f"[inbound] chat from {sender[:16]}: {text[:120]!r}")
    try:
        sel = resolve_selection(ctx, sender, text)
        if sel:
            await handle_selection(ctx, sender, sel)
            return

        low = text.lower()
        if low in {"retry", "try again"} and sget(ctx, f"lp:profile:{sender}"):
            await handle_selection(ctx, sender, {"action": "lp_confirm_profile"})
            return
        if re.search(r"\b(front desk|autopilot|customer care|take orders|storefront)\b", low):
            await start_handoff(ctx, sender)
            return
        if re.search(r"(https?://|www\.|\b[a-z0-9\-]+\.(com|in|org|net|co\.in|online|site|info)\b)", low) and not re.search(r"\b(start|open|launch)\b", low):
            await ctx.send(sender, text_msg("Checking that website with the Compliance Engine..."))
            await call_engine(ctx, sender, "check_link", {"text": text})
            return
        if re.search(r"\b(my plan|show plan|plan)\b", low) and sget(ctx, f"lp:plan:{sender}") and len(low) < 40:
            await show_plan(ctx, sender)
            return
        if low in {"hi", "hello", "hey", "start", "namaste", "help"}:
            await welcome(ctx, sender)
            return

        # Free-text idea -> profile
        found = await asyncio.to_thread(extract_profile, text)
        draft = sget(ctx, f"lp:profile:{sender}", {})
        draft.update(found)
        sset(ctx, f"lp:profile:{sender}", draft)
        if draft.get("business_type") and draft.get("state") and draft.get("turnover_lakh") not in (None, ""):
            await show_review(ctx, sender, draft, "Got it! Here's what I understood from your idea:")
        else:
            known = ", ".join(f"{k}={v}" for k, v in found.items()) or "nothing specific yet"
            await show_form(ctx, sender, f"Great idea! I picked up: {known}. Please fill in the rest so the rules are exact:")
    except Exception as e:
        ctx.logger.error(f"[error] chat handling failed: {e}")
        await ctx.send(sender, text_msg("Sorry, something went wrong on my side. Please try again, or say **hi** to restart."))


@chat_proto.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


# ---------------------------------------------------------------------------
# Replies from the other agents
# ---------------------------------------------------------------------------

peer_proto = Protocol(name="UdyamLaunchpadPeers", version="1.0.0")


@peer_proto.on_message(ComplianceResponse)
async def on_engine_reply(ctx: Context, sender: str, msg: ComplianceResponse):
    if engine_address() and sender != engine_address():
        ctx.logger.warning(f"[security] ComplianceResponse from unknown sender {sender[:16]}")
        return
    rec = pop_pending(ctx, msg.request_id)
    if not rec:
        return  # late duplicate after a retry
    user = rec["sender"]
    result = json.loads(msg.result_json or "{}")
    if rec["kind"] == "check_link":
        if not result.get("ok"):
            await ctx.send(user, text_msg(result.get("error") or "I couldn't read that link."))
            return
        icon = {"official": "✅", "government": "ℹ️", "suspicious": "🚨", "not_government": "⚠️"}.get(result.get("verdict"), "ℹ️")
        extra = f"\nUse the official portal instead: {result['official']}" if result.get("official") else ""
        await ctx.send(user, text_msg(f"{icon} **{result.get('host')}** - {result.get('detail')}{extra}"))
        return
    if not msg.ok or not result.get("ok"):
        if result.get("missing"):
            await show_form(ctx, user, f"The rules engine needs: {', '.join(result['missing'])}.")
        else:
            await ctx.send(user, text_msg(f"The Compliance Engine couldn't process that ({msg.error or 'unknown error'}). Say **retry** to try again."))
        return
    plan = {"result": result, "start": datetime.now(IST).date().isoformat(), "done": []}
    sset(ctx, f"lp:plan:{user}", plan)
    await show_plan(ctx, user, "Here's your personalised, dated launch plan:")


@peer_proto.on_message(StorefrontCreated)
async def on_storefront(ctx: Context, sender: str, msg: StorefrontCreated):
    if autopilot_address() and sender != autopilot_address():
        return
    rec = pop_pending(ctx, msg.request_id)
    if not rec:
        return
    user = rec["sender"]
    if not msg.ok:
        await ctx.send(user, text_msg(f"Autopilot couldn't reserve a storefront ({msg.error}). Open Customer Care Autopilot and say **new shop**."))
        return
    link = f"https://agentverse.ai/agents/details/{autopilot_address()}/profile"
    await ctx.send(user, text_msg(
        f"🎉 Your storefront **{msg.shop_code}** is reserved.\n\n"
        f"1. Open **Customer Care Autopilot** ({link}) or type `@customer-care-autopilot` in ASI:One.\n"
        f"2. Send: `setup {msg.shop_code} {msg.claim_pin}` (the PIN proves it's you - keep it private).\n"
        f"3. Add your menu/prices, UPI ID and delivery rules. Customers then message it with code **{msg.shop_code}** to ask questions and place paid orders."))


@agent.on_interval(period=10.0)
async def recover_timeouts(ctx: Context):
    """Retry once if a peer agent doesn't answer, then tell the user instead of hanging."""
    idx = sget(ctx, "lp:pending_index", [])
    if not idx:
        return
    now = time.time()
    keep = []
    for rid in idx:
        rec = sget(ctx, f"lp:pending:{rid}")
        if not rec:
            continue
        if now - rec["ts"] < ENGINE_TIMEOUT_S:
            keep.append(rid)
            continue
        if rec["tries"] < MAX_TRIES:
            rec["tries"] += 1
            rec["ts"] = now
            sset(ctx, f"lp:pending:{rid}", rec)
            keep.append(rid)
            ctx.logger.warning(f"[retry] {rec['kind']} rid={rid} try={rec['tries']}")
            if rec["to"] == "engine" and engine_address():
                await ctx.send(engine_address(), ComplianceRequest(request_id=rid, kind=rec["kind"], payload_json=json.dumps(rec["payload"])))
            elif rec["to"] == "autopilot" and autopilot_address():
                d = rec["payload"] or {}
                await ctx.send(autopilot_address(), StorefrontDraft(
                    request_id=rid, owner_address=rec["sender"], business_name=str(d.get("business_name") or "My Shop"),
                    business_type=str(d.get("business_type") or ""), city=str(d.get("city") or ""), state=str(d.get("state") or "")))
        else:
            try:
                ctx.storage.remove(f"lp:pending:{rid}")
            except Exception:
                pass
            who = "Compliance Engine" if rec["to"] == "engine" else "Customer Care Autopilot"
            await ctx.send(rec["sender"], text_msg(f"The {who} isn't responding right now (I retried). Your details are saved - say **retry** in a minute."))
    sset(ctx, "lp:pending_index", keep)


agent.include(chat_proto, publish_manifest=True)
agent.include(peer_proto, publish_manifest=True)


@agent.on_event("startup")
async def startup(ctx: Context):
    ctx.logger.info(f"Udyam Launchpad up. Address: {agent.address}")
    if not engine_address():
        ctx.logger.warning("ENGINE_ADDRESS secret not set")
    if not autopilot_address():
        ctx.logger.warning("AUTOPILOT_ADDRESS secret not set")


if __name__ == "__main__":
    agent.run()
