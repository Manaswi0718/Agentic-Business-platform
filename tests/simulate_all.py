"""Offline end-to-end test: runs all three agents in one process with a fake router.
Run from the repo root:  python tests/simulate_all.py
"""
import asyncio, importlib.util, json, os, sys, logging
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from uuid import uuid4
from datetime import datetime, timezone
sys.path.insert(0, ROOT)
logging.disable(logging.WARNING)

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
E = load("engine", os.path.join(ROOT, "udyam_compliance_engine", "agent.py"))
L = load("launch", os.path.join(ROOT, "udyam_launchpad", "agent.py"))
A = load("auto", os.path.join(ROOT, "customer_care_autopilot", "agent.py"))
from uagents_core.contrib.protocols.chat import ChatMessage, TextContent, MetadataContent, ChatAcknowledgement
os.environ["ENGINE_ADDRESS"]=E.agent.address; os.environ["AUTOPILOT_ADDRESS"]=A.agent.address; os.environ["LAUNCHPAD_ADDRESS"]=L.agent.address
os.environ.pop("ASI_ONE_API_KEY", None)

class Store(dict):
    def get(self,k): return super().get(k)
    def set(self,k,v): json.dumps(v); self[k]=json.loads(json.dumps(v))
    def remove(self,k): self.pop(k,None)
    def has(self,k): return k in self
class Log:
    def info(self,*a): pass
    warning=error=exception=debug=info
outbox=[]
class Ctx:
    def __init__(s, me, store): s.me=me; s.storage=store; s.logger=Log(); s.session=uuid4()
    async def send(s, to, msg): outbox.append((s.me, to, msg))
stores={E.agent.address:Store(), L.agent.address:Store(), A.agent.address:Store()}
def ctx(a): return Ctx(a, stores[a])

USERS={}
def show(to, msg):
    if isinstance(msg, ChatAcknowledgement): return
    who = USERS.get(to, to[:10])
    if isinstance(msg, ChatMessage):
        for c in msg.content:
            if isinstance(c, TextContent): print(f"  -> {who}: {c.text[:700]}")
            if isinstance(c, MetadataContent):
                p=json.loads(c.metadata["card_payload"]); print(f"  -> {who}: [CARD {c.metadata['card_kind']}] {json.dumps(p)[:500]}")
    else: print(f"  -> {who}: <{type(msg).__name__}> {str(msg)[:300]}")

async def pump():
    while outbox:
        frm,to,msg = outbox.pop(0)
        if to==E.agent.address:
            if isinstance(msg,E.ComplianceRequest) or type(msg).__name__=="ComplianceRequest":
                await E.on_rules_request(ctx(to), frm, E.ComplianceRequest(**msg.model_dump()))
        elif to==L.agent.address:
            n=type(msg).__name__
            if n=="ComplianceResponse": await L.on_engine_reply(ctx(to), frm, L.ComplianceResponse(**msg.model_dump()))
            elif n=="StorefrontCreated": await L.on_storefront(ctx(to), frm, L.StorefrontCreated(**msg.model_dump()))
        elif to==A.agent.address:
            if type(msg).__name__=="StorefrontDraft": await A.on_draft(ctx(to), frm, A.StorefrontDraft(**msg.model_dump()))
        else:
            show(to,msg)

async def say(agent_mod, user, text):
    print(f"\n[{USERS[user]} -> {agent_mod.__name__}] {text}")
    m=ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=[TextContent(type="text", text=text)])
    await agent_mod.on_chat(ctx(agent_mod.agent.address), user, m); await pump()

def last_card(user, store_key):
    pass

async def main():
    founder="agent1qfounder000"; cust="agent1qcustomer00"; USERS[founder]="FOUNDER"; USERS[cust]="CUSTOMER"
    await A.startup(ctx(A.agent.address))
    await say(L, founder, "hi")
    await say(L, founder, "I want to start a home bakery in Bengaluru called Sweet Crumbs, about 30k sales per month on Instagram")
    await say(L, founder, json.dumps({"action":"lp_confirm_profile"}))
    await say(L, founder, json.dumps({"action":"lp_step_detail","step_id":"fssai"}))
    # planner prose variant
    await say(L, founder, "The user chose action lp_step_done for step_id udyam.")
    await say(L, founder, "is udyam-registration.org legit?")
    await say(L, founder, "set up my front desk")
    # claim the storefront
    code=[k for k in stores[A.agent.address] if k.startswith("shop:") and k!="shop:DEMO"][0][5:]
    pin=stores[A.agent.address]["shop:"+code]["claim_pin"]
    await say(A, founder, f"setup {code} {pin}")
    await say(A, founder, json.dumps({"action":"ap_save_shop","shop":code,"business_name":"Sweet Crumbs","upi_id":"sweetcrumbs@okicici","payee":"Sweet Crumbs","menu":"Chocolate cake 1kg - 650; Brownie box (6) - 300; Cookies 250g - ₹180","delivery_areas":"Indiranagar, HSR Layout","delivery_fee":"40","min_order":"200","hours":"10am-8pm","facts":"All cakes eggless on request; Need 24 hours notice"}))
    await say(A, founder, json.dumps({"action":"ap_go_live","shop":code}))
    await say(A, cust, f"shop {code}")
    await say(A, cust, "how much is the brownie box?")
    await say(A, cust, "do you deliver to HSR Layout?")
    await say(A, cust, "do you make sugar-free cakes for diabetics?")
    qid=[k for k in stores[A.agent.address] if k.startswith("esc:")][0][4:]
    await say(A, founder, f"answer {qid} Yes, sugar-free (stevia) chocolate cake at the same price")
    await say(A, cust, "order")
    await say(A, cust, json.dumps({"action":"ap_place_order","shop":code,"qty_i1":"1","qty_i2":"2","qty_i3":"","fulfilment":"delivery","customer_name":"Ravi","phone":"98450 12345","address":"12th Main, HSR Layout","notes":"eggless"}))
    d=stores[A.agent.address]["draft:"+cust]["id"]
    await say(A, cust, json.dumps({"action":"ap_confirm_order","draft_id":d}))
    oid=[k for k in stores[A.agent.address] if k.startswith("order:")][0][6:]
    await say(A, cust, json.dumps({"action":"ap_customer_paid","order_id":oid}))
    await say(A, founder, json.dumps({"action":"ap_owner_mark_paid","order_id":oid}))
    await say(A, founder, "dashboard")
    # error path: below min / bad phone
    await say(A, cust, json.dumps({"action":"ap_place_order","shop":code,"qty_i3":"1","fulfilment":"pickup","customer_name":"R","phone":"123"}))
    # upgrade (no stripe keys -> FET only)
    await say(A, founder, "upgrade")
    # customer tries owner action
    await say(A, cust, json.dumps({"action":"ap_go_live","shop":code}))
    # engine direct chat
    USERS["agent1qjudge"]="JUDGE"
    await say(E, "agent1qjudge", json.dumps({"action":"ce_submit_profile","business_type":"online_seller","state":"Telangana","turnover_lakh":"15","sells_online":"marketplace","interstate":"true"}))
asyncio.run(main())
