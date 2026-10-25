"""
Udyam Compliance Engine — Agentverse HOSTED agent (Blank Agent -> paste into agent.py)

A deterministic, source-checked rules engine. No LLM decides any legal step.
  * Other agents call it with ComplianceRequest -> ComplianceResponse
  * Users can also chat with it directly on ASI:One (Chat Protocol + Interactive Cards)
  * Also checks whether a website is an official government portal (anti-scam)

Secrets (Agentverse -> Build -> Agent Secrets): none required.
Optional: ALLOWED_CALLERS = comma-separated agent addresses allowed to call the rules API
          (leave empty to allow any agent).
"""

import json
import os
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse
from uuid import uuid4

from uagents import Agent, Context, Model, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    MetadataContent,
    TextContent,
    chat_protocol_spec,
)

agent = Agent()

RULES_VERSION = "2026-10-24"

# ---------------------------------------------------------------------------
# Agent-to-agent API (keep these two models IDENTICAL in every agent file)
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


# ---------------------------------------------------------------------------
# Reference data (every rule cites one of these sources)
# ---------------------------------------------------------------------------

SOURCES = {
    "udyam": ("Udyam Registration portal, Ministry of MSME", "https://udyamregistration.gov.in"),
    "msme_2025": ("MSME classification, revised limits effective 1 Apr 2025", "https://msme.gov.in"),
    "gst_portal": ("GST portal (FORM GST REG-01)", "https://www.gst.gov.in"),
    "cgst_act": ("CGST Act 2017, s.22 (threshold), s.24 (compulsory registration), s.25, s.122", "https://cbic-gst.gov.in"),
    "fssai_2026": ("FSSAI order dated 13 Mar 2026: revised licensing thresholds from 1 Apr 2026", "https://www.fssai.gov.in"),
    "foscos": ("FSSAI FoSCoS portal", "https://foscos.fssai.gov.in"),
    "fss_act": ("Food Safety and Standards Act 2006, s.31 and s.63", "https://www.fssai.gov.in"),
    "legal_metrology": ("Legal Metrology Act 2009 and Packaged Commodities Rules 2011", "https://consumeraffairs.nic.in"),
    "epfo": ("EPF & MP Act 1952 (applies at 20+ employees)", "https://www.epfindia.gov.in"),
    "esic": ("ESI Act 1948 (applies at 10+ employees in notified areas)", "https://www.esic.gov.in"),
    "ipindia": ("Trade Marks Rules 2017, fee schedule", "https://ipindia.gov.in"),
    "state_labour": ("State Shops & Commercial Establishments Act (varies by state)", "https://shramsuvidha.gov.in"),
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

# States/UTs that did not adopt the Rs 40 lakh goods threshold (remain at Rs 20 lakh)
GOODS_20L_STATES = {
    "Arunachal Pradesh", "Manipur", "Meghalaya", "Mizoram", "Nagaland",
    "Puducherry", "Sikkim", "Telangana", "Tripura", "Uttarakhand",
}
# Special category states with Rs 10 lakh threshold for services
SERVICES_10L_STATES = {"Manipur", "Mizoram", "Nagaland", "Tripura"}

BUSINESS_TYPES = {
    "home_food": {"label": "Home food (bakery, tiffin, snacks)", "supply": "goods", "food": True},
    "restaurant_cafe": {"label": "Restaurant / cafe / cloud kitchen", "supply": "services", "food": True},
    "kirana_retail": {"label": "Kirana / retail shop", "supply": "goods", "food": True},
    "online_seller": {"label": "Online seller (products)", "supply": "goods", "food": False},
    "services": {"label": "Services (tuition, salon, design, repairs)", "supply": "services", "food": False},
    "manufacturing": {"label": "Small manufacturing / crafts", "supply": "goods", "food": False},
}

# Official domains for the link checker
OFFICIAL_DOMAINS = {
    "udyamregistration.gov.in": "Udyam Registration (Ministry of MSME) - registration is FREE",
    "msme.gov.in": "Ministry of MSME",
    "gst.gov.in": "GST portal - registration is FREE",
    "cbic-gst.gov.in": "CBIC GST",
    "foscos.fssai.gov.in": "FSSAI FoSCoS licensing portal",
    "fssai.gov.in": "Food Safety and Standards Authority of India",
    "ipindia.gov.in": "Intellectual Property India (trademarks)",
    "epfindia.gov.in": "EPFO",
    "esic.gov.in": "ESIC",
    "incometax.gov.in": "Income Tax Department",
    "shramsuvidha.gov.in": "Shram Suvidha (labour)",
    "consumeraffairs.nic.in": "Department of Consumer Affairs (Legal Metrology)",
    "startupindia.gov.in": "Startup India",
}
SCAM_KEYWORDS = ["udyam", "udyog", "msme", "gst", "fssai", "foscos", "trademark", "shop-act", "gumasta", "aadhaar"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    return str(v).strip().lower() in {"true", "yes", "y", "1", "on", "checked"}


def to_num(v, default=0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(str(v).replace(",", "").replace("₹", "").strip())
    except Exception:
        return default


def canonical_state(s) -> Optional[str]:
    if not s:
        return None
    t = str(s).strip().lower()
    aliases = {"nct of delhi": "Delhi", "new delhi": "Delhi", "orissa": "Odisha", "pondicherry": "Puducherry",
               "j&k": "Jammu and Kashmir", "up": "Uttar Pradesh", "mp": "Madhya Pradesh", "tn": "Tamil Nadu",
               "wb": "West Bengal", "ap": "Andhra Pradesh"}
    if t in aliases:
        return aliases[t]
    for st in STATES:
        if st.lower() == t:
            return st
    for st in STATES:
        if st.lower() in t:
            return st
    return None


def normalize_profile(p: dict) -> dict:
    p = dict(p or {})
    bt = str(p.get("business_type") or "").strip()
    if bt not in BUSINESS_TYPES:
        bt = "home_food" if "food" in bt or "bak" in bt else ("services" if "serv" in bt else "online_seller" if "online" in bt else bt)
    if bt not in BUSINESS_TYPES:
        bt = None
    info = BUSINESS_TYPES.get(bt or "", {})
    channel = str(p.get("sells_online") or p.get("online_channel") or "none").strip().lower()
    if channel in {"true", "yes"}:
        channel = "social"
    if channel not in {"none", "social", "marketplace"}:
        channel = "marketplace" if any(k in channel for k in ["amazon", "flipkart", "meesho", "market"]) else "social"
    if bt == "online_seller" and channel == "none":
        channel = "marketplace"
    sells_food = to_bool(p.get("sells_food")) or bool(info.get("food"))
    has_shop = to_bool(p.get("has_shop"))
    return {
        "business_name": str(p.get("business_name") or "").strip()[:80],
        "business_type": bt,
        "state": canonical_state(p.get("state")),
        "city": str(p.get("city") or "").strip()[:60],
        "turnover_lakh": max(0.0, to_num(p.get("turnover_lakh"), 0.0)),
        "sells_online": channel,
        "interstate": to_bool(p.get("interstate")),
        "employees": int(max(0, to_num(p.get("employees"), 0))),
        "has_shop": has_shop or bt in {"kirana_retail", "restaurant_cafe"},
        "packaged": to_bool(p.get("packaged")),
        "sells_food": sells_food,
        "uses_scale": to_bool(p.get("uses_scale")) or bt == "kirana_retail",
    }


def missing_fields(p: dict) -> list:
    return [k for k in ("business_type", "state") if not p.get(k)]


def src(*keys):
    return [{"name": SOURCES[k][0], "url": SOURCES[k][1]} for k in keys]


def lakh(x: float) -> str:
    if x >= 100:
        return f"₹{x / 100:g} crore"
    return f"₹{x:g} lakh"


# ---------------------------------------------------------------------------
# THE RULES (deterministic)
# ---------------------------------------------------------------------------


def gst_decision(p: dict) -> dict:
    info = BUSINESS_TYPES[p["business_type"]]
    is_goods = info["supply"] == "goods"
    state = p["state"]
    if is_goods:
        threshold = 20.0 if state in GOODS_20L_STATES else 40.0
    else:
        threshold = 10.0 if state in SERVICES_10L_STATES else 20.0
    t = p["turnover_lakh"]
    reasons, category = [], "optional"

    if is_goods and p["interstate"]:
        category = "mandatory"
        reasons.append("You will sell goods to customers in other states: inter-state taxable supply of goods needs GST registration from the first sale, whatever your turnover (CGST Act s.24(i)).")
    if is_goods and p["sells_online"] == "marketplace":
        if category != "mandatory" and t <= threshold:
            category = "conditional"
            reasons.append("Selling goods through a marketplace (Amazon, Flipkart, Meesho...) normally needs GST from the first sale (s.24(ix)). A narrow exemption (Notification 34/2023-CT) exists only for intra-state supplies below the threshold, with a PAN-based enrolment on the GST portal. Check the marketplace's rules before listing.")
        else:
            category = "mandatory"
            reasons.append("Supplying goods through an e-commerce operator requires GST registration regardless of turnover (CGST Act s.24(ix)).")
    if t > threshold:
        category = "mandatory"
        reasons.append(f"Your expected turnover ({lakh(t)}) is above the {lakh(threshold)} threshold for {'goods' if is_goods else 'services'} in {state} (CGST Act s.22).")
    if category == "optional":
        reasons.append(f"Below the {lakh(threshold)} threshold for {'goods' if is_goods else 'services'} in {state} and no compulsory-registration trigger, so GST is optional now. Register within 30 days of crossing the threshold (s.25). Voluntary registration lets you claim input tax credit but adds monthly/quarterly returns.")
    return {"category": category, "threshold_lakh": threshold, "reasons": reasons}


def fssai_tier(t: float) -> dict:
    # Revised thresholds effective 1 Apr 2026 (FSSAI order 13 Mar 2026)
    if t <= 150:
        return {"tier": "Basic Registration (Form A)", "fee": "About ₹100 per year (pay only on foscos.fssai.gov.in)", "form": "A"}
    if t <= 5000:
        return {"tier": "State Licence (Form B)", "fee": "₹2,000–₹5,000 per year as shown on FoSCoS", "form": "B"}
    return {"tier": "Central Licence (Form B)", "fee": "₹7,500 per year as shown on FoSCoS", "form": "B"}


def evaluate(profile: dict) -> dict:
    p = normalize_profile(profile)
    miss = missing_fields(p)
    if miss:
        return {"ok": False, "missing": miss, "profile": p}

    t = p["turnover_lakh"]
    steps = []

    def add(**kw):
        kw.setdefault("confidence", "verified")
        kw.setdefault("penalty", "")
        steps.append(kw)

    add(id="prereq_pan_bank", title="PAN + bank account for the business", category="prerequisite",
        why="A proprietor can use their personal PAN. Udyam and GST both ask for PAN and bank details, so keep them ready first.",
        authority="Income Tax Department / your bank", portal="https://www.incometax.gov.in", fee="PAN application fee as charged by NSDL/UTIITSL; bank account free",
        documents=["Aadhaar (linked to mobile)", "PAN card", "Bank passbook or cancelled cheque"],
        day_offset=0, deadline="Before starting registrations", sources=src("udyam"))

    add(id="udyam", title="Udyam Registration (MSME)", category="recommended",
        why="Free, Aadhaar-based MSME registration. Unlocks priority-sector loans, MSME schemes and delayed-payment protection. "
            + ("Your expected turnover fits a MICRO enterprise (investment up to ₹2.5 crore and turnover up to ₹10 crore)." if t <= 1000
               else "Your expected turnover fits a SMALL enterprise (up to ₹25 crore investment / ₹100 crore turnover)." if t <= 10000
               else "Your expected turnover fits a MEDIUM enterprise (up to ₹125 crore investment / ₹500 crore turnover)." if t <= 50000
               else "Turnover above ₹500 crore is outside the MSME definition, so Udyam benefits won't apply."),
        authority="Ministry of MSME", portal="https://udyamregistration.gov.in", fee="FREE (₹0). Any site asking for money is not the official portal.",
        documents=["Aadhaar of proprietor (OTP to linked mobile)", "PAN", "Bank account number + IFSC", "Business address", "Main activity (NIC code)", "Estimated investment and turnover"],
        day_offset=1, deadline="Any time; do it first (about 15 minutes)",
        penalty="Not mandatory, but without it you cannot claim MSME benefits.", sources=src("udyam", "msme_2025"))

    if p["sells_food"]:
        f = fssai_tier(t)
        add(id="fssai", title=f"FSSAI {f['tier']}", category="mandatory",
            why=f"Every food business, including home kitchens, needs FSSAI registration or a licence before selling. For expected turnover of {lakh(t)}: {f['tier']} (thresholds revised from 1 Apr 2026: registration up to ₹1.5 crore, State licence up to ₹50 crore).",
            authority="Food Safety and Standards Authority of India", portal="https://foscos.fssai.gov.in", fee=f["fee"],
            documents=["Photo ID (Aadhaar/PAN)", "Passport-size photo", "Proof of premises (rent agreement / utility bill; home address is fine)", "List of food products"]
            + (["Layout plan, equipment list, water test report, food safety plan"] if f["form"] == "B" else []),
            day_offset=2, deadline="Before your first sale",
            penalty="Operating without registration/licence: up to 6 months' imprisonment and fine up to ₹5 lakh (FSS Act s.63).",
            sources=src("fssai_2026", "foscos", "fss_act"))

    g = gst_decision(p)
    gst_title = {"mandatory": "GST registration (required)", "conditional": "GST registration (check marketplace rule)", "optional": "GST registration (optional for now)"}[g["category"]]
    add(id="gst", title=gst_title, category=g["category"], why=" ".join(g["reasons"]),
        authority="GSTN / CBIC", portal="https://www.gst.gov.in", fee="FREE (₹0) on gst.gov.in",
        documents=["PAN", "Aadhaar (authentication)", "Photo", "Proof of place of business (rent agreement/utility bill; consent letter if home)", "Bank proof (cancelled cheque/passbook)", "Mobile and email"],
        day_offset=3 if g["category"] == "mandatory" else 30,
        deadline="Within 30 days of becoming liable (s.25)" if g["category"] != "optional" else "Review when turnover nears the threshold",
        penalty="If liable and not registered: penalty of ₹10,000 or the tax evaded, whichever is higher, plus tax and interest (s.122)." if g["category"] != "optional" else "",
        sources=src("cgst_act", "gst_portal"))

    if p["has_shop"] or p["employees"] > 0:
        add(id="shops_establishment", title="Shops & Establishments registration", category="mandatory",
            why="You have a shop/premises or employees. Most states require registration under their Shops & Commercial Establishments Act, usually soon after opening. Rules and exemptions differ by state.",
            authority=f"Labour Department, {p['state']}", portal="https://shramsuvidha.gov.in", fee="Varies by state and number of employees",
            documents=["ID and address proof of owner", "Proof of premises", "Photo of shop with name board", "Employee details"],
            day_offset=7, deadline="Usually within 30 days of opening (check your state rule)",
            confidence="verify_locally", sources=src("state_labour"))

    if p["has_shop"] and p["sells_food"]:
        add(id="trade_licence", title="Municipal trade / health licence", category="conditional",
            why="Many municipal corporations (e.g. BBMP in Bengaluru) require a trade or health licence for shops and eateries. Home-based kitchens are often exempt; confirm with your ward office.",
            authority=f"Municipal corporation, {p['city'] or p['state']}", portal="", fee="Varies by municipality",
            documents=["Proof of premises", "ID proof", "FSSAI registration", "Property tax receipt / NOC from owner"],
            day_offset=10, deadline="Before opening the shop", confidence="verify_locally", sources=src("state_labour"))

    if p["uses_scale"]:
        add(id="legal_metrology_scale", title="Get your weighing scale verified & stamped", category="mandatory",
            why="Weights and measures used for trade must be verified and stamped by the Legal Metrology department before use, and re-verified periodically.",
            authority=f"Legal Metrology Department, {p['state']}", portal="https://consumeraffairs.nic.in", fee="Small state fee per instrument",
            documents=["Scale purchase invoice", "Shop address"], day_offset=5, deadline="Before using the scale for sales",
            penalty="Using unverified weights/measures is punishable with fines under the Legal Metrology Act 2009.",
            sources=src("legal_metrology"))

    if p["packaged"]:
        add(id="packaged_labelling", title="Labels for pre-packed products", category="mandatory",
            why="Pre-packed goods must carry declarations (name, MRP, net quantity, manufacturer/packer address, date, consumer-care contact) under the Packaged Commodities Rules; packers may need registration with Legal Metrology. Packaged food also needs FSSAI labelling.",
            authority="Legal Metrology / FSSAI", portal="https://consumeraffairs.nic.in", fee="Depends on state",
            documents=["Label artwork", "Product list with net quantity and MRP"], day_offset=6, deadline="Before selling packed products",
            confidence="verify_locally", sources=src("legal_metrology"))

    if p["employees"] >= 10:
        add(id="esic", title="ESIC registration", category="mandatory",
            why="Establishments with 10 or more employees in notified areas must register under the ESI Act.",
            authority="ESIC", portal="https://www.esic.gov.in", fee="FREE registration; monthly contributions apply",
            documents=["Udyam/PAN", "Employee list with wages", "Bank details"], day_offset=12,
            deadline="Within 15 days of becoming applicable", sources=src("esic"))
    if p["employees"] >= 20:
        add(id="epfo", title="EPF registration", category="mandatory",
            why="Establishments with 20 or more employees must register with EPFO.",
            authority="EPFO", portal="https://www.epfindia.gov.in", fee="FREE registration; monthly contributions apply",
            documents=["PAN", "Employee list with wages", "Bank details"], day_offset=12,
            deadline="On reaching 20 employees", sources=src("epfo"))

    add(id="trademark", title="Protect your brand name (trademark, optional)", category="optional",
        why="Optional, but stops others using your brand. File after Udyam to get the lower fee for individuals and small enterprises.",
        authority="Intellectual Property India", portal="https://ipindia.gov.in", fee="₹4,500 per class (e-filing; individual/startup/small enterprise)",
        documents=["Brand name / logo", "Udyam certificate (for reduced fee)", "Date of first use or 'proposed to be used'"],
        day_offset=14, deadline="Any time; earlier is safer", sources=src("ipindia"))

    warnings = [
        "Udyam registration is FREE and only on udyamregistration.gov.in. Look-alike sites charge ₹1,000–₹5,000 for the same form.",
        "GST and FSSAI applications are filed only on gst.gov.in and foscos.fssai.gov.in. Never share OTPs with agents.",
    ]
    return {
        "ok": True,
        "rules_version": RULES_VERSION,
        "profile": p,
        "classification": "Micro enterprise" if t <= 1000 else ("Small enterprise" if t <= 10000 else ("Medium enterprise" if t <= 50000 else "Large (not MSME)")),
        "gst": g,
        "steps": sorted(steps, key=lambda s: s["day_offset"]),
        "warnings": warnings,
        "disclaimer": "Rules engine output, not legal advice. Items marked 'verify_locally' vary by state/municipality.",
    }


def check_link(url_or_text: str) -> dict:
    text = (url_or_text or "").strip()
    m = re.search(r"((?:https?://)?[a-z0-9\-]+(?:\.[a-z0-9\-]+)+[^\s]*)", text, re.I)
    if not m:
        return {"ok": False, "error": "No website found in the message."}
    raw = m.group(1)
    if not raw.lower().startswith("http"):
        raw = "https://" + raw
    host = (urlparse(raw).hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    for dom, name in OFFICIAL_DOMAINS.items():
        if host == dom or host.endswith("." + dom):
            return {"ok": True, "host": host, "verdict": "official", "detail": f"Official government site: {name}."}
    if host.endswith(".gov.in") or host.endswith(".nic.in"):
        return {"ok": True, "host": host, "verdict": "government", "detail": "Government domain (.gov.in / .nic.in), but not one of the registration portals I track. Check it is the right department."}
    if any(k in host for k in SCAM_KEYWORDS):
        return {"ok": True, "host": host, "verdict": "suspicious",
                "detail": "NOT a government website, but its name imitates a government service. Such sites often charge for registrations that are free. Use the official portal instead.",
                "official": "https://udyamregistration.gov.in" if ("udyam" in host or "msme" in host or "udyog" in host) else ("https://www.gst.gov.in" if "gst" in host else "https://foscos.fssai.gov.in")}
    return {"ok": True, "host": host, "verdict": "not_government", "detail": "Not a government website. Government registrations are only done on .gov.in portals."}


# ---------------------------------------------------------------------------
# Chat helpers + cards
# ---------------------------------------------------------------------------


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


def profile_form(action: str = "ce_submit_profile") -> dict:
    return {
        "title": "Tell me about the business",
        "fields": [
            {"name": "business_type", "kind": "select", "label": "Type of business", "required": True,
             "options": [{"value": k, "label": v["label"]} for k, v in BUSINESS_TYPES.items()]},
            {"name": "state", "kind": "select", "label": "State / UT", "required": True,
             "options": [{"value": s, "label": s} for s in STATES]},
            {"name": "city", "kind": "text", "label": "City"},
            {"name": "turnover_lakh", "kind": "number", "label": "Expected sales in first year (₹ lakh)", "required": True},
            {"name": "sells_online", "kind": "select", "label": "Where will you sell?",
             "options": [{"value": "none", "label": "In person / local only"},
                         {"value": "social", "label": "WhatsApp / Instagram / own website"},
                         {"value": "marketplace", "label": "Amazon / Flipkart / Meesho"}]},
            {"name": "interstate", "kind": "checkbox", "label": "I will ship goods to other states"},
            {"name": "has_shop", "kind": "checkbox", "label": "I have a separate shop or premises"},
            {"name": "packaged", "kind": "checkbox", "label": "I sell pre-packed products"},
            {"name": "employees", "kind": "number", "label": "Number of employees (0 if just you)"},
        ],
        "submit_cta": {"label": "Check my rules", "selection": {"action": action}},
    }


def format_result(r: dict) -> str:
    p = r["profile"]
    lines = [f"**Compliance check** ({BUSINESS_TYPES[p['business_type']]['label']}, {p['state']}, about {lakh(p['turnover_lakh'])}/year) - rules version {r['rules_version']}", ""]
    tag = {"mandatory": "REQUIRED", "conditional": "CHECK", "recommended": "RECOMMENDED", "optional": "OPTIONAL", "prerequisite": "FIRST"}
    for i, s in enumerate(r["steps"], 1):
        lines.append(f"{i}. **{s['title']}** [{tag.get(s['category'], s['category'])}] - {s['fee']}")
        lines.append(f"   {s['why']}")
        if s.get("portal"):
            lines.append(f"   Official portal: {s['portal']}")
        if s.get("confidence") == "verify_locally":
            lines.append("   (Varies by state/municipality - confirm locally.)")
    lines += ["", "**Scam alert:** " + r["warnings"][0], "", "_" + r["disclaimer"] + "_"]
    return "\n".join(lines)


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


def prose_profile(text: str) -> dict:
    """Recover form values from planner-mediated prose."""
    low = text.lower()
    out = {}
    for k in BUSINESS_TYPES:
        if k in low:
            out["business_type"] = k
    st = canonical_state(text)
    if st:
        out["state"] = st
    m = re.search(r"turnover_lakh\D{0,15}([\d.]+)", low) or re.search(r"([\d.]+)\s*lakh", low)
    if m:
        out["turnover_lakh"] = m.group(1)
    m = re.search(r"employees\D{0,15}(\d+)", low)
    if m:
        out["employees"] = m.group(1)
    for k in ("marketplace", "social"):
        if k in low:
            out["sells_online"] = k
    for k in ("interstate", "has_shop", "packaged"):
        if re.search(k + r"\W{0,5}(true|yes|checked)", low):
            out[k] = True
    return out


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------

rules_proto = Protocol(name="UdyamComplianceRules", version="1.0.0")


def caller_allowed(sender: str) -> bool:
    allowed = [a.strip() for a in (os.getenv("ALLOWED_CALLERS") or "").split(",") if a.strip()]
    return not allowed or sender in allowed


@rules_proto.on_message(ComplianceRequest, replies={ComplianceResponse})
async def on_rules_request(ctx: Context, sender: str, msg: ComplianceRequest):
    ctx.logger.info(f"[inbound] ComplianceRequest kind={msg.kind} id={msg.request_id} from {sender[:16]}")
    if not caller_allowed(sender):
        await ctx.send(sender, ComplianceResponse(request_id=msg.request_id, ok=False, result_json="{}", error="caller not allowed"))
        return
    try:
        payload = json.loads(msg.payload_json or "{}")
        if msg.kind == "plan":
            result = evaluate(payload)
        elif msg.kind == "check_link":
            result = check_link(payload.get("text", ""))
        else:
            raise ValueError(f"unknown kind {msg.kind}")
        await ctx.send(sender, ComplianceResponse(request_id=msg.request_id, ok=bool(result.get("ok")), result_json=json.dumps(result)))
    except Exception as e:
        ctx.logger.error(f"[error] rules request failed: {e}")
        await ctx.send(sender, ComplianceResponse(request_id=msg.request_id, ok=False, result_json="{}", error=str(e)[:300]))


chat_proto = Protocol(spec=chat_protocol_spec)

# ---- chat UI: menu -> form -> results carousel -> step detail; link check form -> verdict card ----

CATEGORY_BADGE = {"mandatory": ("Required", "warning"), "conditional": ("Check", "info"), "recommended": ("Recommended", "success"),
                  "optional": ("Optional", "info"), "prerequisite": ("Do first", "info")}
CITY_STATE = {"bengaluru": "Karnataka", "bangalore": "Karnataka", "mysuru": "Karnataka", "mysore": "Karnataka", "mumbai": "Maharashtra",
              "pune": "Maharashtra", "nagpur": "Maharashtra", "chennai": "Tamil Nadu", "coimbatore": "Tamil Nadu", "hyderabad": "Telangana",
              "kolkata": "West Bengal", "ahmedabad": "Gujarat", "surat": "Gujarat", "jaipur": "Rajasthan", "lucknow": "Uttar Pradesh",
              "noida": "Uttar Pradesh", "gurugram": "Haryana", "gurgaon": "Haryana", "kochi": "Kerala", "patna": "Bihar", "bhopal": "Madhya Pradesh",
              "indore": "Madhya Pradesh", "bhubaneswar": "Odisha", "guwahati": "Assam", "visakhapatnam": "Andhra Pradesh", "dehradun": "Uttarakhand"}


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


def remember_card(ctx: Context, sender: str, actions: list, ids: Optional[dict] = None):
    sset(ctx, f"ce:card:{sender}", {"actions": actions, "ids": ids or {}})


def free_text_profile(text: str) -> dict:
    """Understand plain sentences like 'kirana shop in Pune, 20 lakh sales a year'."""
    low = text.lower()
    p = prose_profile(text)
    kinds = [("home_food", ["bakery", "cake", "tiffin", "home food", "pickle", "snack", "sweets", "cookies"]),
             ("restaurant_cafe", ["restaurant", "cafe", "café", "cloud kitchen", "dhaba", "eatery"]),
             ("kirana_retail", ["kirana", "grocery", "general store", "provision", "retail shop", "supermarket"]),
             ("online_seller", ["amazon", "flipkart", "meesho", "online store", "sell online", "ecommerce", "e-commerce"]),
             ("services", ["tuition", "salon", "repair", "design", "freelanc", "consult", "coaching"]),
             ("manufacturing", ["manufactur", "handicraft", "craft", "candle", "soap", "pottery"])]
    if not p.get("business_type"):
        for k, words in kinds:
            if any(w in low for w in words):
                p["business_type"] = k
                break
    if not p.get("state"):
        for city, st in CITY_STATE.items():
            if re.search(r"\b" + city + r"\b", low):
                p["state"], p["city"] = st, city.title()
                break
    m = re.search(r"([\d,.]+)\s*(lakh|lac|k\b|thousand|crore|cr\b)", low)
    if m:
        n = float(m.group(1).replace(",", "") or 0)
        u = m.group(2)
        v = n if u in ("lakh", "lac") else n / 100 if u in ("k", "thousand") else n * 100
        if re.search(r"(per|a|/)\s*month|monthly", low[m.end():m.end() + 25]):
            v *= 12
        p["turnover_lakh"] = round(v, 2)
    if any(w in low for w in ["amazon", "flipkart", "meesho", "marketplace"]):
        p["sells_online"] = "marketplace"
    elif any(w in low for w in ["instagram", "whatsapp", "website"]):
        p["sells_online"] = "social"
    if any(w in low for w in ["other states", "all over india", "across india", "pan india"]):
        p["interstate"] = True
    m = re.search(r"(\d+)\s*(employees|staff|workers|helpers)", low)
    if m:
        p["employees"] = int(m.group(1))
    return p


def menu_card() -> dict:
    return {
        "title": "Udyam Compliance Engine",
        "summary_rows": [
            {"label": "What I do", "value": "List exactly which registrations a small business in India needs - with official portal, fee, documents, deadline, penalty and legal source."},
            {"label": "Covers", "value": "Udyam (MSME), GST, FSSAI (2026 limits), Shops & Establishments, trade licence, Legal Metrology, ESIC, EPFO, trademark"},
            {"label": "Scam check", "value": "Paste any website and I'll tell you if it is an official government portal"},
            {"label": "Tip", "value": "You can also just type: 'kirana shop in Pune, 20 lakh sales a year, 3 helpers'"},
        ],
        "ctas": [
            {"label": "Check my registrations", "primary": True, "selection": {"action": "ce_new_check"}},
            {"label": "Check a website", "selection": {"action": "ce_link_form"}},
        ],
    }


def link_form() -> dict:
    return {"title": "Is this website official?",
            "fields": [{"name": "url", "kind": "text", "label": "Website address (e.g. udyam-registration.org)", "required": True}],
            "submit_cta": {"label": "Check website", "selection": {"action": "ce_check_link"}}}


def result_carousel(r: dict) -> tuple:
    p = r["profile"]
    req = [s for s in r["steps"] if s["category"] in ("mandatory", "prerequisite")]
    narration = (f"**{r['classification']}** - {BUSINESS_TYPES[p['business_type']]['label']}, {p['state']}, " + (f"about {lakh(p['turnover_lakh'])}/year" if p['turnover_lakh'] else "first-year sales not given") + ".\n"
                 f"{len(req)} required step(s), {len(r['steps'])} in total. Tap any step for documents, fees, deadline and the legal source.\n\n"
                 f"🚨 {r['warnings'][0]}")
    items = []
    for s in r["steps"]:
        lbl, var = CATEGORY_BADGE.get(s["category"], ("Info", "info"))
        badges = [{"label": lbl, "variant": var}]
        if s.get("confidence") == "verify_locally":
            badges.append({"label": "Varies by state", "variant": "info"})
        items.append({"id": s["id"], "title": s["title"], "subtitle": s["authority"], "badges": badges,
                      "secondary_text": s["fee"].split(".")[0][:60],
                      "primary_cta": {"label": "Details", "selection": {"action": "ce_step_detail", "step_id": s["id"]}}})
    return narration, {"title": "Your registrations", "subtitle": f"Rules version {r['rules_version']}", "items": items}


def step_card(s: dict) -> dict:
    rows = [{"label": "Why", "value": s["why"]},
            {"label": "When", "value": s["deadline"]},
            {"label": "Authority", "value": s["authority"]},
            {"label": "Government fee", "value": s["fee"]},
            {"label": "Documents", "value": "; ".join(s["documents"])}]
    if s.get("portal"):
        rows.append({"label": "Official portal", "value": s["portal"]})
    if s.get("penalty"):
        rows.append({"label": "If skipped", "value": s["penalty"]})
    rows.append({"label": "Legal source", "value": "; ".join(f"{x['name']} ({x['url']})" for x in s["sources"])})
    if s.get("confidence") == "verify_locally":
        rows.append({"label": "Note", "value": "Varies by state/municipality - confirm locally."})
    return {"title": s["title"], "summary_rows": rows,
            "ctas": [{"label": "Back to list", "primary": True, "selection": {"action": "ce_show_result"}},
                     {"label": "Check another business", "selection": {"action": "ce_new_check"}}]}


def link_card(r: dict) -> dict:
    head = {"official": "✅ Official government site", "government": "ℹ️ Government domain", "suspicious": "🚨 Look-alike - not official", "not_government": "⚠️ Not a government site"}[r["verdict"]]
    rows = [{"label": "Website", "value": r["host"]}, {"label": "Verdict", "value": head}, {"label": "Details", "value": r["detail"]}]
    if r.get("official"):
        rows.append({"label": "Use instead", "value": r["official"]})
    return {"title": head, "summary_rows": rows,
            "ctas": [{"label": "Check another website", "primary": True, "selection": {"action": "ce_link_form"}},
                     {"label": "Check my registrations", "selection": {"action": "ce_new_check"}}]}


def resolve_selection(ctx: Context, sender: str, text: str) -> Optional[dict]:
    sel = try_json(text)
    if sel and sel.get("action"):
        return sel
    low = text.lower()
    if "ce_submit_profile" in low:
        return {"action": "ce_submit_profile", **prose_profile(text)}
    card = sget(ctx, f"ce:card:{sender}") or {}
    action = next((a for a in card.get("actions", []) if a in low or a.replace("_", " ") in low), None)
    if not action:
        return None
    sel = {"action": action}
    for key, values in (card.get("ids") or {}).items():
        for v in sorted(values, key=len, reverse=True):
            if v in low or v.replace("_", " ") in low:
                sel[key] = v
                break
    if action == "ce_check_link":
        sel["url"] = text
    return sel


async def send_form(ctx: Context, sender: str, intro: str):
    remember_card(ctx, sender, ["ce_submit_profile"])
    await ctx.send(sender, card_msg(intro, "form", profile_form()))


async def send_result(ctx: Context, sender: str, r: dict):
    sset(ctx, f"ce:last:{sender}", r)
    narration, payload = result_carousel(r)
    remember_card(ctx, sender, ["ce_step_detail", "ce_new_check"], ids={"step_id": [s["id"] for s in r["steps"]]})
    await ctx.send(sender, card_msg(narration, "carousel", payload))


async def send_link_verdict(ctx: Context, sender: str, text: str):
    r = check_link(text)
    if not r.get("ok"):
        remember_card(ctx, sender, ["ce_check_link"])
        await ctx.send(sender, card_msg(r.get("error", "Could not read that link.") + " Please type the website address:", "form", link_form()))
        return
    remember_card(ctx, sender, ["ce_link_form", "ce_new_check"])
    await ctx.send(sender, card_msg(f"Here's what I found about **{r['host']}**:", "detail", link_card(r)))


async def handle_selection(ctx: Context, sender: str, sel: dict) -> bool:
    a = sel.get("action")
    if a == "ce_submit_profile":
        r = evaluate(sel)
        if not r["ok"]:
            await send_form(ctx, sender, f"I still need: {', '.join(r['missing'])}.")
            return True
        await send_result(ctx, sender, r)
    elif a == "ce_new_check":
        await send_form(ctx, sender, "Tell me about the business:")
    elif a == "ce_step_detail":
        r = sget(ctx, f"ce:last:{sender}")
        s = next((x for x in (r or {}).get("steps", []) if x["id"] == sel.get("step_id")), None)
        if not s:
            await (send_result(ctx, sender, r) if r else send_form(ctx, sender, "Let's run a check first:"))
            return True
        remember_card(ctx, sender, ["ce_show_result", "ce_new_check"])
        await ctx.send(sender, card_msg(f"**{s['title']}**", "detail", step_card(s)))
    elif a == "ce_show_result":
        r = sget(ctx, f"ce:last:{sender}")
        await (send_result(ctx, sender, r) if r else send_form(ctx, sender, "Let's run a check first:"))
    elif a == "ce_link_form":
        remember_card(ctx, sender, ["ce_check_link"])
        await ctx.send(sender, card_msg("Paste the website you want me to check:", "form", link_form()))
    elif a == "ce_check_link":
        await send_link_verdict(ctx, sender, str(sel.get("url") or ""))
    else:
        return False
    return True


@chat_proto.on_message(ChatMessage)
async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
    text = strip_mentions(" ".join(c.text for c in msg.content if isinstance(c, TextContent)))
    if not text:
        return
    ctx.logger.info(f"[inbound] chat from {sender[:16]}: {text[:120]!r}")
    try:
        sel = resolve_selection(ctx, sender, text)
        if sel and await handle_selection(ctx, sender, sel):
            return
        low = text.lower()
        if re.search(r"(https?://|www\.|\b[a-z0-9\-]+\.(com|in|org|net|co\.in|online|site|info)\b)", low):
            await send_link_verdict(ctx, sender, text)
            return
        if re.search(r"\b(back|list|result|results)\b", low) and sget(ctx, f"ce:last:{sender}") and len(low) < 30:
            await send_result(ctx, sender, sget(ctx, f"ce:last:{sender}"))
            return
        p = free_text_profile(text)
        if p.get("business_type") and p.get("state"):
            if "turnover_lakh" not in p:
                p["turnover_lakh"] = 0
                note = " (no sales figure given, so I assumed a small first year - tell me e.g. '20 lakh a year' to refine)"
            else:
                note = ""
            r = evaluate(p)
            if r["ok"]:
                await ctx.send(sender, text_msg(f"Understood: {BUSINESS_TYPES[r['profile']['business_type']]['label']} in {r['profile']['state']}{note}."))
                await send_result(ctx, sender, r)
                return
        if p.get("business_type") or p.get("state"):
            await send_form(ctx, sender, "Thanks! I need a couple more details to apply the rules exactly:")
            return
        remember_card(ctx, sender, ["ce_new_check", "ce_link_form"])
        await ctx.send(sender, card_msg("Namaste! What would you like to do?", "detail", menu_card()))
    except Exception as e:
        ctx.logger.error(f"[error] chat failed: {e}")
        await ctx.send(sender, text_msg("Sorry, something went wrong on my side. Please try again or say hi."))


@chat_proto.on_message(ChatAcknowledgement)
async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    pass


agent.include(rules_proto, publish_manifest=True)
agent.include(chat_proto, publish_manifest=True)


@agent.on_event("startup")
async def startup(ctx: Context):
    ctx.logger.info(f"Udyam Compliance Engine up. Address: {agent.address} rules={RULES_VERSION}")


if __name__ == "__main__":
    agent.run()
