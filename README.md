# Udyam Launchpad — from "I have an idea" to "I'm registered and taking paid orders"

Three cooperating **Agentverse hosted agents** for first-time Indian founders (home bakers, kirana owners, small online sellers, student brands). Built for **Fetch-A-Thon 3.0**.

| Agent | Role | Fetch.ai features |
|---|---|---|
| **Udyam Launchpad** | Founder-facing orchestrator: idea → profile → dated launch plan → hand-off | Chat Protocol, Interactive Cards (form, review, carousel, detail), agent-to-agent calls with retry/timeout recovery, ASI:One LLM (optional) |
| **Udyam Compliance Engine** | Deterministic, source-cited rules (Udyam, GST, FSSAI 2026 thresholds, Shops & Est., Legal Metrology, EPF/ESI, trademark) + official-website checker | Rules API protocol, Chat Protocol, form card |
| **Customer Care Autopilot** | The new business's front desk: grounded answers, orders with exact totals, UPI link + QR, owner escalation inbox | Chat Protocol, all 4 card kinds, **Payment Protocol (seller: Stripe card + FET)** |

```
Founder ──chat──> Udyam Launchpad ──ComplianceRequest──> Compliance Engine (fixed rules, cited sources)
                        │  <──ComplianceResponse──┘
                        └──StorefrontDraft──> Customer Care Autopilot <──chat── Customers
                           <──StorefrontCreated──┘        │  └─RequestPayment/CommitPayment (Pro plan)
                                                          └─ escalations ──> Owner
```

**Why the legal steps are never guessed:** the LLM is only used to understand free text. Which registrations apply, thresholds, fees, deadlines and penalties come from fixed Python rules in the Compliance Engine, each citing its source. Prices and order totals are computed in integer paise from the owner's menu — never by the LLM.

**Monetisation:** registration guidance is free (social good). Autopilot is free for 25 orders/month; **Autopilot Pro** (₹99 / 30 days, or 1 FET) unlocks unlimited orders, paid through the Fetch.ai Agent Payment Protocol. Customers pay shops directly by UPI (zero fees), so the shop keeps 100% of its revenue.

## Live agents (hosted on Agentverse, chat on ASI:One)

| Agent | ASI:One handle | Agent address | Agentverse profile |
|---|---|---|---|
| Udyam Launchpad | `@udyam-launchpad` | `agent1qts648405s2mcqa7f4cdxya6vetqkpu8ldzjm5mqf7a0wjc57f3fwvh03rp` | [open profile](https://agentverse.ai/agents/details/agent1qts648405s2mcqa7f4cdxya6vetqkpu8ldzjm5mqf7a0wjc57f3fwvh03rp/profile) |
| Udyam Compliance Engine | `@udyam-compliance-eng` | `agent1q2y5dmx7aczz6fd3rnpyrn7jdmnrnmgpmvfdez7sgp9ywsckjnr4x7sstnr` | [open profile](https://agentverse.ai/agents/details/agent1q2y5dmx7aczz6fd3rnpyrn7jdmnrnmgpmvfdez7sgp9ywsckjnr4x7sstnr/profile) |
| Customer Care Autopilot | `@customer-care-autopi` | `agent1qwy5tfsnn5vlyt49x4qsxqsfl03vzsu4n7gc352u22gtyvu2t9hnk3a6f6w` | [open profile](https://agentverse.ai/agents/details/agent1qwy5tfsnn5vlyt49x4qsxqsfl03vzsu4n7gc352u22gtyvu2t9hnk3a6f6w/profile) |

**Demo chat:** [ASI:One shared chat]1.Udyam launch pad(https://asi1.ai/invite?channelInviteKey=GAYvxkgDeNAnQ53Zjv0MesWF2mxCjsvrW9xAQxlL43w&utm_source=share&utm_medium=desktop&utm_campaign=channel_invite)
2·customer care autopilot (https://asi1.ai/invite?channelInviteKey=EotIfs1s2LwAfHuChHGJeUHHEHMS71V2DFBmEhDaJfk&utm_source=share&utm_medium=desktop&utm_campaign=channel_invite)
3.Udyam Compliance Engine (https://asi1.ai/invite?channelInviteKey=QZPfadz3KYPkXXmFu9-CAyYB1K0IdiJzAQYk74hsjQQ&utm_source=share&utm_medium=desktop&utm_campaign=channel_invite)
**Demo video:** :https://youtu.be/qgnSfB1yCzQ.
**Ppt Link**:https://docs.google.com/presentation/d/1DamJJWLlkGAEgA1GlA-SP9XdF9NkSJsN/edit?usp=drivesdk&ouid=117615334798774580852&rtpof=true&sd=true

### Try it in 1 minute
Open [asi1.ai](https://asi1.ai) and send:
- `@udyam-launchpad I want to start a home bakery in Bengaluru, about 30k sales a month on Instagram`
- `@udyam-compliance-eng hi` → menu card → *Check my registrations* or *Check a website*
- `@customer-care-autopi shop DEMO` → menu carousel → ask `how much is the brownie box?` → `order`

## Repository layout

```
udyam_launchpad/agent.py            founder-facing orchestrator (paste into a Blank Agent)
udyam_compliance_engine/agent.py    deterministic rules engine + website checker
customer_care_autopilot/agent.py    front desk, orders, UPI, Payment Protocol
*/README_agentverse.md              text for each agent's Agentverse profile
tests/simulate_all.py               offline end-to-end test of all three agents
```

## Test offline (no Agentverse needed)

```bash
pip install -r requirements.txt
python tests/simulate_all.py
```
It runs the whole story in one process: idea → review card → Engine rules → plan → step done → link check → hand-off → owner setup → customer Q&A and escalation → order with exact totals → UPI → payment confirmed → Pro upgrade request.

---

## Deploy on Agentverse (Blank Agents) — ~15 minutes

> Hosted agents get new addresses, different from your local ones. You can keep or stop the local agents.

### 1. Create the three agents
For each folder below: **agentverse.ai → Agents → Launch an Agent → Create an Agent → Blank Agent**, give it the name, open **Build**, replace `agent.py` with the folder's `agent.py`, then click **Start**.

| Order | Name (use as handle) | Code |
|---|---|---|
| 1 | `udyam-compliance-engine` (handle `@udyam-compliance-eng`) | `udyam_compliance_engine/agent.py` |
| 2 | `customer-care-autopilot` (handle `@customer-care-autopi`) | `customer_care_autopilot/agent.py` |
| 3 | `udyam-launchpad` (handle `@udyam-launchpad`) | `udyam_launchpad/agent.py` |

Copy each agent's address (`agent1q...`) from its page or from the startup log line.

### 2. Add secrets (Build → Agent Secrets), then restart each agent

**udyam-launchpad**
| Secret | Value |
|---|---|
| `ENGINE_ADDRESS` | address of udyam-compliance-engine |
| `AUTOPILOT_ADDRESS` | address of customer-care-autopilot |
| `ASI_ONE_API_KEY` | from asi1.ai/developer (optional, better free-text understanding) |

**customer-care-autopilot**
| Secret | Value |
|---|---|
| `LAUNCHPAD_ADDRESS` | address of udyam-launchpad (only it may reserve storefronts) |
| `DEMO_UPI_ID` | your own UPI ID for the built-in DEMO shop |
| `DEMO_CLAIM_PIN` | optional, default `2026` (`setup DEMO 2026` makes you the demo owner) |
| `ASI_ONE_API_KEY` | optional, grounded natural-language answers |
| `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY` | optional, Stripe **test** keys (`sk_test_`/`pk_test_`) for card payments |
| `STRIPE_CURRENCY` / `PRO_PRICE_INR` / `FET_AMOUNT` / `FREE_ORDERS_PER_MONTH` | optional overrides (`inr` / `99` / `1` / `25`) |

**udyam-compliance-engine**
| Secret | Value |
|---|---|
| `ALLOWED_CALLERS` | optional: launchpad address, to lock the rules API |

### 3. Make them discoverable on ASI:One
For each agent: paste its `README_agentverse.md` into the **Overview / README** editor, add keywords, set an avatar, and make sure the **Chat Protocol** badge shows. Test in the agent's **Chat with Agent / Testing** tab (cards render there too).

### 4. Testnet funds (FET payments)
The Autopilot wallet receives FET. To pay as a buyer, fund your ASI:One wallet from the Fetch.ai testnet faucet. Note: the Stripe embedded checkout is rendered by the Agentverse chat/inspector; plain ASI:One chat may only show the FET option.

---

## Demo script (record this for the shared ASI:One chat)

1. `@udyam-launchpad I want to start a home bakery in Bengaluru called Sweet Crumbs, about 30k sales a month on Instagram`
2. Review card → **Build my launch plan** → plan text + carousel (Udyam free, FSSAI Basic Registration, GST optional, trademark…) with calendar links.
3. Tap **FSSAI** → detail card with documents, fee, penalty, source → **Mark as done**.
4. `is udyam-registration.org legit?` → 🚨 look-alike warning + official portal.
5. `set up my front desk` → Launchpad asks Autopilot → storefront code + PIN.
6. `@customer-care-autopi setup <CODE> <PIN>` → setup form → preview → **Go live**.
7. As a customer: `@customer-care-autopi shop <CODE>` → `how much is the brownie box?` → `do you make sugar-free cakes?` (escalated to the owner, owner answers, customer gets the reply, answer saved as a fact).
8. `order` → form → review with exact totals → UPI link + QR → **I've paid** → owner **Yes, received** → receipt.
9. Owner: `upgrade` → Payment Protocol request (card / FET) → Pro activated after verification.

Quick test without the hand-off: `@customer-care-autopi shop DEMO`. Owner side of the demo shop: `@customer-care-autopi setup DEMO 2026`, then `dashboard`.

---

## Reliability & safety
- Peer calls retried once after 20 s, then the user is told plainly (no silent hangs); duplicate replies ignored; storefront creation is idempotent on retries.
- Peer messages accepted only from configured addresses; storefront claim needs a PIN; every owner button carries a secret owner key, so owner actions work even when ASI:One relays a card press from another address, and are refused without it.
- Leading `@handle` mentions are stripped before parsing.
- Card selections handled both as JSON (direct @mention) and as planner prose.
- Payments: server-side verification (Stripe session status, amount, currency and buyer; FET tx recipient, sender and amount on-chain), idempotent on transaction id, `CancelPayment` on failure.
- Order validation: quantities 0–50, minimum order, delivery area, phone number.

## Team
- Palle Reenu · Nitte Meenakshi Instiute Of Technology 
- Harshita V K · Nitte Meenakshi Instiute Of Technology  
- C.Manaswi · Nitte Meenakshi Instiute Of Technology 

Built for Fetch-A-Thon 3.0 (Fetch.ai Innovation Lab), October 2026. Licensed under MIT.

## Sources used by the rules (rules version 2026-10-24)
Udyam portal (udyamregistration.gov.in) · MSME limits effective 1 Apr 2025 (micro: investment ≤ ₹2.5 cr, turnover ≤ ₹10 cr) · CGST Act s.22/24/25/122 and Notification 34/2023-CT · FSSAI order 13 Mar 2026 (Basic Registration up to ₹1.5 cr, State licence up to ₹50 cr, from 1 Apr 2026) · FSS Act s.63 · Legal Metrology Act 2009 · EPF (20+), ESI (10+) · Trade Marks fee schedule.
State/municipal items (Shops & Establishments, trade licence, packaged-goods registration) are flagged "varies by state". Not legal advice.
