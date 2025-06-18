import os
import json
import time
import pyodbc
import numpy as np
import faiss
import requests
import threading
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, request
from langdetect import detect
from openai import OpenAI
from pyngrok import ngrok, conf

# ----------------------------------------------------------------------------
# 0. Load environment variables
# ----------------------------------------------------------------------------
load_dotenv()

FB_APP_ID         = os.getenv("FB_APP_ID")
FB_APP_SECRET     = os.getenv("FB_APP_SECRET")
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
VERIFY_TOKEN      = os.getenv("VERIFY_TOKEN")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
DB_HOST           = os.getenv("DB_HOST")
DB_NAME           = os.getenv("DB_NAME")
DB_USER           = os.getenv("DB_USER")
DB_PASS           = os.getenv("DB_PASS")
NGROK_AUTH_TOKEN = os.getenv("NGROK_AUTH_TOKEN")
if not NGROK_AUTH_TOKEN:
    raise RuntimeError("Missing NGROK_AUTH_TOKEN in .env")
# Configure pyngrok with it:
conf.get_default().auth_token = NGROK_AUTH_TOKEN

missing = [k for k,v in {
    "FB_APP_ID":FB_APP_ID, "FB_APP_SECRET":FB_APP_SECRET,
    "PAGE_ACCESS_TOKEN":PAGE_ACCESS_TOKEN, "VERIFY_TOKEN":VERIFY_TOKEN,
    "OPENAI_API_KEY":OPENAI_API_KEY,
    "DB_HOST":DB_HOST, "DB_NAME":DB_NAME,
    "DB_USER":DB_USER, "DB_PASS":DB_PASS
}.items() if not v]
if missing:
    raise RuntimeError(f"Missing environment variables: {', '.join(missing)}")

# Initialize OpenAI client
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# ----------------------------------------------------------------------------
# 1. Safe SQL helper for TSD.SystemLoss
# ----------------------------------------------------------------------------
def get_db_conn():
    conn_str = (
        "Driver={ODBC Driver 17 for SQL Server};"
        f"Server={DB_HOST};Database={DB_NAME};"
        f"UID={DB_USER};PWD={DB_PASS}"
    )
    return pyodbc.connect(conn_str)

def execute_sql(sql: str):
    sql_clean = sql.strip().lower()
    if not sql_clean.startswith("select"):
        raise ValueError("Only SELECT queries are allowed.")
    conn = get_db_conn()
    cur = conn.cursor()
    cur.execute(sql)
    cols = [c[0] for c in cur.description]
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(zip(cols, row)) for row in rows]

sql_function = {
    "name": "execute_sql",
    "description": (
        "Run a SELECT query against the TSD.SystemLoss table and return JSON results.\n"
        "Available columns:\n"
        "  • YEAR_MONTH_DAY (date)\n"
        "  • TotalEnergyInput (float)\n"
        "  • TotalEnergyOutput (float)\n"
        "  • SystemLoss (float)\n"
        "  • PercentSystemLoss (float)\n"
        "Only read-only SELECT statements are allowed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "A safe SQL SELECT statement against TSD.SystemLoss."
            }
        },
        "required": ["sql"]
    }
}

# ----------------------------------------------------------------------------
# 2. Webhook registration (runs after Flask is up)
# ----------------------------------------------------------------------------
def register_webhook():
    # 1) Wait up to ~10s for Flask to start responding on /health
    for _ in range(20):
        try:
            if requests.get("http://127.0.0.1:5000/health", timeout=1).status_code == 200:
                break
        except requests.exceptions.RequestException:
            time.sleep(0.5)
    else:
        print("⚠️ Flask did not start; skipping webhook registration.")
        return

    # 2) Open an HTTPS ngrok tunnel on port 5000
    public_url = ngrok.connect(5000, bind_tls=True).public_url
    print(f" * ngrok tunnel URL → {public_url}")

    # 3) Tear down any existing FB subscription to avoid duplicates
    teardown_resp = requests.delete(
        f"https://graph.facebook.com/v17.0/{FB_APP_ID}/subscriptions",
        params={"access_token": f"{FB_APP_ID}|{FB_APP_SECRET}"}
    )
    try:
        teardown_resp.raise_for_status()
    except Exception:
        # It’s okay if there was no prior subscription
        pass

    # 4) Register (or re-register) the webhook against the new tunnel URL
    resp = requests.post(
        f"https://graph.facebook.com/v17.0/{FB_APP_ID}/subscriptions",
        params={
            "access_token": f"{FB_APP_ID}|{FB_APP_SECRET}",
            "object":       "page",
            "callback_url": f"{public_url}/webhook",
            "verify_token": VERIFY_TOKEN,
            "fields":       "messages,message_deliveries,messaging_postbacks"
        }
    )
    resp.raise_for_status()
    print(" * Facebook webhook registered:", resp.json())

# ----------------------------------------------------------------------------
# 3. Knowledge base & FAISS setup (chunks omitted)
# ----------------------------------------------------------------------------
knowledge_chunks = [

# 1 ──────────────────────────────────────────────────────────────────────────
"""SERVICE APPLICATION
To apply for an electric-service connection under CEBECO III, a prospective consumer must:
• Fill out a membership form and pay the nominal membership fee.
• Attend a short Pre-Membership Orientation Seminar (PMES).
• Hire an accredited electrician to install house wiring compliant with the Philippine Building Code.
• Secure an electrical permit from the LGU’s Office of the Building Official.
• After inspection by CEBECO III, pay the service deposit/meter fee.
• Sign the service contract.
Once these steps are completed, the account is energized, and the applicant becomes a bonafide member-consumer of CEBECO III.""",

# 2 ──────────────────────────────────────────────────────────────────────────
"""MAGNA CARTA
The Magna Carta for Residential Electricity Consumers guarantees:
• Safe, continuous, and reliable supply.
• Transparent unbundled billing.
• 48-hour written notice prior to disconnection for non-payment.
• The right to contest a bill and seek mediation with the ERC.
• Refund or credit if over-billing is proven.
Bill deposits are refundable after 3 consecutive years of prompt payment.
The Magna Carta applies to all Distribution Utilities (DUs), including electric cooperatives such as CEBECO III.""",

# 3 ──────────────────────────────────────────────────────────────────────────
"""CONNECTION & DISCONNECTION
Reconnection is done within 24 hours after settlement of unpaid bills plus the reconnection fee.
Disconnections are NOT executed on weekends, holidays, or outside office hours.
Illegal use of electricity (e.g., meter tampering or jumper) is subject to immediate disconnection and penalties under RA 7832.""",

# 4 ──────────────────────────────────────────────────────────────────────────
"""CUT-OFF DATES, DUE DATES, PENALTIES & DISCONNECTION SCHEDULE
• Bill due date: exactly nine (9) days after bill delivery.
• Scheduled cut-off dates (all “of the month”):
    – Asturias – 4th of the month
    – Balamban – 8th of the month
    – Aloguinsan – 10th of the month
    – Pinamungajan – 12th of the month
    – Cebu City Areas – 12th of the month
    – Special Bills – 14th of the month
    – Lutopan – 24th of the month
    – Toledo City – 27th of the month
• Disconnection crews operate between 8 AM and 5 PM on those dates.
• Unpaid balance by cut-off → immediate service suspension + 2% monthly surcharge.
  Reconnection requires settlement of all past-due bills, surcharges, and reconnection fee.
• One-time grace per billing cycle: payment of the current bill within 24 hours
  of the attempted cut-off will suspend that operation (customer must present
  proof of payment at any area office).

*Magna Carta for Residential Electricity Consumers* (ERC Res. No. 12-09) exceptions
that *CEBECO III* strictly honors:
  • Final Disconnection Notice (FDN) must be served ≥ 48 hours before cut-off.
  • *Suspension of cut-off* if the customer provides:
      – medical certificate for life-support equipment  
      – official permit for a funeral wake held at the premises  
      – evidence of non-receipt of the disconnection notice  
      – proof of a billing error charging multiple months at once  
      – payment of the current bill within 24 hours after the first cut-off attempt  
  • *No disconnections* on:
      – Fridays, weekends, national/local holidays, or the day before any of these  
      – Holy Week (Maundy Thursday through Easter Sunday)  
      – any ERC-declared moratorium days (e.g. grid emergencies or public calamities)  
  • *Automatic 30-day extension* after FDN for registered senior citizens,
    PWDs, or households with life-support equipment.""",

# 5 ──────────────────────────────────────────────────────────────────────────
"""HISTORY & COVERAGE
CEBECO III (Cebu III Electric Cooperative, Inc.) was organized in 1979 and now serves:
• Toledo City
• Pinamungajan
• Aloguinsan
• Balamban
• Asturias
The cooperative operates multiple 69/13.2 kV substations and nearly 1,000 km of distribution lines.
Governance is via a Board of Directors elected by the member-consumers per district.""",

# 6 ──────────────────────────────────────────────────────────────────────────
"""AFFILIATED AGENCIES
• NEA – Supervisory & technical/financial standards for cooperatives.
• ERC – Approval of retail rates & Magna Carta enforcement.
• DOE – National energy policy & renewable energy programs.
• LGU – Permits, right-of-way & disaster coordination.""",

# 7 ──────────────────────────────────────────────────────────────────────────
"""NET METERING
Under RA 9513 (Renewable Energy Act), a consumer may install up to 100 kW of renewables (typically solar PV) and export surplus energy to the grid.
CEBECO III installs a bi-directional meter; the consumer is billed on net energy.
Excess exports earn credits equal to the generation charge minus line losses and administrative fees.""",

# 8 ──────────────────────────────────────────────────────────────────────────
"""SERVICE CONTRACT / MEMBER RIGHTS
Members may vote at the Annual General Assembly, elect directors, and share in patronage refunds if declared.
The service contract requires the consumer to pay bills on time, keep wiring safe, and allow meter access.""",

# 9 ──────────────────────────────────────────────────────────────────────────
"""ELECTRICITY RATES
Unbundled charges include:
• Generation
• Transmission
• Distribution
• Supply & Metering
• System Loss
• Universal Charges
• Taxes
Only Distribution, Supply, and Metering components are retained by CEBECO III and are ERC-regulated.
Generation cost fluctuates monthly; distribution charges remain until a new ERC rate case is approved.""",

# 10 ─────────────────────────────────────────────────────────────────────────
"""CAPITAL PROJECTS
All major Capital Expenditures (CAPEX), such as new substations, line uprating, and SCADA, require ERC approval.
Projects have reduced system loss from over 13% in 2005 to less than 8% today.""",

# 11 ─────────────────────────────────────────────────────────────────────────
"""AREA OFFICES & PAYMENT CHANNELS (as of April 2025)
• Toledo City Main Office – Mon–Fri 8 AM–5 PM
• Balamban Area Office – Mon–Fri 8 AM–5 PM
• Asturias Service Desk – Tue & Thu 9 AM–3 PM
Payments accepted via:
• GCash “CEBECO III” biller
• Palawan Pawnshop
• 7-Eleven CliQQ
• All Cebuana branches
Note: Post-dated cheques are not accepted.""",

# 12 ─────────────────────────────────────────────────────────────────────────
"""POWER-SITUATION BULLETIN (typical)
• NGCP Visayas grid is on YELLOW ALERT when available reserve is less than regulating plus contingency.
• During Yellow/Red alerts, CEBECO III may receive a load curtailment order and implement manual load shedding by feeder (30-minute blocks).
Real-time updates are posted on the official Facebook page and announced via local radio (station DYRD / 102.7 FM).""",

# 13 ─────────────────────────────────────────────────────────────────────────
"""REPORTING AN OUTAGE
Message the official Facebook page or call the 24×7 hotline: (032) 467-9-112.
Provide the following information:
• Account Name
• Consumer ID
• Exact Address
• Any visible cause (e.g., tree on line)
A crew is dispatched based on feeder priority and public safety considerations.""",

# 14 ─────────────────────────────────────────────────────────────────────────
"""BILLING & PAYMENT FAQ
• You may request a PDF e-bill via email at ebill@cebeco3.com.
• Senior Citizen Discount: 5% on the first 100 kWh for the account registered under the senior’s name and address.
• Re-print of Statement of Account (SOA) is free for the current month; ₱20 per copy for previous months.""",

# 15 ─────────────────────────────────────────────────────────────────────────
"""CONTACT INFORMATION & HOTLINES
Area office contact numbers (24×7 hotlines):
    • Asturias – 0927-655-6054
    • Balamban – 0915-163-1134
    • Aloguinsan – 0927-655-6053
    • Pinamungajan – 0906-411-4564
    • Bunga, Toledo City – 0917-505-6070
    • Main Office, Toledo City – 0917-624-4406
General email: cebeco_iii@cebeco3.com.ph
Website: https://www.cebeco3.com.ph/contact-us/""",

# 16 ─────────────────────────────────────────────────────────────────────────
"""KEY PERSONNEL (as of May 2025)
• Virgilio C. Fortich Jr. – General Manager
  • Contact #: (032) 467-8557
  • Email: TBA@cebeco3.com.ph

• Willard C. Sayson – Assistant General Manager
  • Contact #: (032) 467-8557
  • Email: wc_sayson@cebeco3.com.ph

• Edgardo H. Hernaez Jr. – TSD Manager
  • Contact #: (032) 467-8557
  • Email: radi_hernaez@cebeco3.com.ph

• Gilbert P. Provida – Network Services Division Manager / O&M Section Head - Toledo City
  • Contact #: (032) 467-8557
  • Email: gp_provida@cebeco3.com.ph

• Eric D. Itable – Distribution System Automation Head
  • Contact #: (032) 467-8557
  • Email: ed_itable@cebeco3.com.ph

• Mariano T. Pañares III – O&M Supervisor - Asturias
  • Contact #: (032) 464-9220
  • Email: mt_pañares@cebeco3.com.ph

• Bryan P. Bael – O&M Supervisor - Aloguinsan
  • Contact #: (032) 469-9026
  • Email: bp_bael@cebeco3.com.ph

• Rolly L. Cabañero – O&M Supervisor - Pinamungajan
  • Contact #: (032) 468-9671
  • Email: rl_cabanero@cebeco3.com.ph

• Kim Derrick V. Rosell – O&M Supervisor - Balamban
  • Contact #: (032) 465-3016
  • Email: kv_rosell@cebeco3.com.ph

• Gerardo C. Villafuerte Jr. – O&M Supervisor - DAS
  • Contact #: 0926-785-7588
  • Email: gc_villafuerte@cebeco3.com.ph

• Richyield Roentgen C. Hernando – Substation & Equipment Maintenance Supervisor
  • Contact #: (032) 467-8557
  • Email: rrc_hernando@cebeco3.com.ph

• Marlon C. Dupal-ag – Staking / Design Supervisor
  • Contact #: (032) 467-8557
  • Email: mc_dupalag@cebeco3.com.ph

• Teresito M. Ohagan – Construction Supervisor
  • Contact #: (032) 467-8557
  • Email: tm_ohagan@cebeco3.com.ph

• Sandra T. Candelada – Technical Staff Engineer
  • Contact #: (032) 467-8557
  • Email: st_candelada@cebeco3.com.ph

• Fritzie B. Cabanilla – Meter Reader, Billing & Collection Section Head
  • Contact #: (032) 467-8131
  • Email: fbcabanilla@cebeco3.com.ph
""",

# 17 ─────────────────────────────────────────────────────────────────────────
"""ONLINE BILL INQUIRY
Consumers can check their electricity bills online by visiting: https://www.cebeco3.com.ph/online-bill-inquiry/
For assistance, call or text: 0917-624-4406""",

# 18 ─────────────────────────────────────────────────────────────────────────
"""MOBILE APPLICATION
CEBECO III offers a mobile application for checking electricity bills, viewing payment and consumption history, submitting complaints, and checking for news and announcements.
Available on Google Play: https://play.google.com/store/apps/details?id=com.cebeco3.mobile""",

# 19 ─────────────────────────────────────────────────────────────────────────
"""RATE SCHEDULE FOR THE YEAR 2025
As of March 2025, the rate schedule includes:
• Generation System Charge: Php 6.2976/kWh
• Transmission System Charge: Php 1.0563/kWh
• Distribution System Charge: Php 0.5432/kWh
• Supply System Charge: Php 0.5143/kWh
• Metering System Charge: Php 0.1134/kWh
• System Loss Charge: Php 0.8961/kWh
• Universal Charges: Php 0.0156/kWh
• Taxes: Php 0.0123/kWh
Note: Rates are subject to change based on ERC approvals and market conditions.""",

# 20 ─────────────────────────────────────────────────────────────────────────
"""SERVICE INTERRUPTION NOTICES
CEBECO III schedules power service interruptions for maintenance and upgrades.
Notices are posted on the official website: https://www.cebeco3.com.ph/news/
Consumers are advised to check regularly for updates.""",

# 21 ─────────────────────────────────────────────────────────────────────────
"""BRANDING GUIDELINES
Always refer to the organization simply as “CEBECO III.” Do not use terms like
“utility company,” “distribution utility,” or “electric cooperative” when naming
or describing CEBECO III in any response."""

]

print("Embedding knowledge base…")
emb_resp = openai_client.embeddings.create(
    input=knowledge_chunks,
    model="text-embedding-ada-002"
)
vectors = [np.array(d.embedding, dtype=np.float32) for d in emb_resp.data]
dim = vectors[0].shape[0]
faiss_idx = faiss.IndexFlatL2(dim)
faiss_idx.add(np.stack(vectors))
print("Knowledge base ready ✔")

def top_chunks(query, k=3):
    qvec = np.array(
        openai_client.embeddings.create(input=query, model="text-embedding-ada-002")
                      .data[0].embedding,
        dtype=np.float32
    )
    D, I = faiss_idx.search(qvec.reshape(1, -1), k)
    out = []
    for idx, dist in zip(I[0], D[0]):
        if idx == -1:
            continue
        cos = np.dot(vectors[idx], qvec) / (np.linalg.norm(vectors[idx]) * np.linalg.norm(qvec))
        if cos >= 0.45:
            out.append(knowledge_chunks[idx])
    return out

# ----------------------------------------------------------------------------
# 4. Conversation memory
# ----------------------------------------------------------------------------
MAX_TURNS = 10
convo = {}

def remember(psid, role, text):
    convo.setdefault(psid, []).append({"role": role, "text": text})
    if len(convo[psid]) > MAX_TURNS * 2:
        convo[psid] = convo[psid][-MAX_TURNS * 2:]

def reset_memory(psid):
    convo.pop(psid, None)

# ----------------------------------------------------------------------------
# 5. Intent classifier
# ----------------------------------------------------------------------------
def intent(msg):
    txt = msg.lower()
    if any(w in txt for w in ["cut-off", "cutoff", "pamutol"]):
        return "cutoff"
    if any(w in txt for w in ["bill", "payment", "balance"]):
        return "billing"
    if any(w in txt for w in ["outage", "brownout", "no power", "power situation"]):
        return "outage"
    return "general"


# ----------------------------------------------------------------------------
# 6. Flask app & webhook handlers
# ----------------------------------------------------------------------------
app = Flask(__name__)

@app.route("/health", methods=["GET"])
def health_check():
    return "OK", 200

@app.route("/webhook", methods=["GET"])
def fb_verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge")
    return "Verification failed", 403

@app.route("/webhook", methods=["POST"])
def fb_webhook():
    data = request.get_json()
    if data.get("object") != "page":
        return "bad object", 400

    for entry in data.get("entry", []):
        for evt in entry.get("messaging", []):
            psid = evt["sender"]["id"]
            if "message" in evt and "text" in evt["message"]:
                txt = evt["message"]["text"].strip()
                if txt.lower() in ("reset", "restart"):
                    reset_memory(psid)
                    send(psid, "Conversation context cleared ✅")
                else:
                    send(psid, generate_reply(psid, txt))
    return "OK", 200

# ----------------------------------------------------------------------------
# 7. Reply generation with function‐calling
# ----------------------------------------------------------------------------
def generate_reply(psid, user_msg):
    remember(psid, "user", user_msg)

    # canned responses

    # 1) Cut-off dates intent → static, full response
    if intent(user_msg) == "cutoff":
        return """\
Here are the scheduled cut-off dates (“Petsa sa Pamutol”) by area:
  • Asturias – 4th of the month  
  • Balamban – 8th of the month  
  • Aloguinsan – 10th of the month  
  • Pinamungajan – 12th of the month  
  • Cebu City Areas – 12th of the month  
  • Special Bills – 14th of the month  
  • Lutopan – 24th of the month  
  • Toledo City – 27th of the month  

*Payments not received* by the cut-off date will result in service suspension plus a 2% monthly surcharge.  
However, per the ERC’s Magna Carta for Residential Electricity Consumers (Res. 12-09), CEBECO III *must* suspend any disconnection if:
  1. A customer’s household has someone on life-support equipment (medical cert. required).  
  2. A funeral wake is being held on the premises.  
  3. The customer never received the Final Disconnection Notice (FDN).  
  4. A billing error charged multiple months at once.  
  5. The customer settles the current bill within 24 hours of the first cut-off attempt (once per billing cycle).  

And *no disconnections* may occur on:
  – Fridays, weekends, national/local holidays, or the day before these.  
  – Holy Week (Maundy Thursday through Easter Sunday).  
  – Any ERC-declared moratorium days (e.g. grid emergencies, public calamities).  

Registered senior citizens, PWDs, or life-support households also get an automatic 30-day extension after the FDN."""

    if intent(user_msg) == "billing":
        return (
            "To check your current balance please send your Consumer ID.\n"
            "Payments can be made via GCash → Bills → CEBECO III, "
            "Palawan Pawnshop, 7-Eleven CliQQ or at any area office."
        )
    if intent(user_msg) == "outage":
        return (
            "For outage reports please include your complete address and Consumer ID. "
            "Current grid advisory: see Power-Situation bulletins on our FB page. "
            "You may also call our 24×7 hotline (032) 467-9-112."
        )

    # RAG + function‐calling
    chunks = top_chunks(user_msg)
    system = (
        "You are the CEBECO3 assistant. You have a knowledge base and can call "
        "execute_sql(sql) to fetch system‐loss data from TSD.SystemLoss.\n\n"
        "Knowledge Base:\n" + "\n".join(chunks)
    )

    messages = [{"role": "system", "content": system}]
    for turn in convo.get(psid, []):
        messages.append({"role": turn["role"], "content": turn["text"]})
    messages.append({"role": "user", "content": user_msg})

    resp = openai_client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=messages,
        functions=[sql_function],
        function_call="auto"
    )
    msg = resp.choices[0].message

    if msg.function_call is not None:
        args = json.loads(msg.function_call.arguments)
        try:
            result = execute_sql(args["sql"])
        except Exception as e:
            answer = f"⚠️ SQL error: {e}"
        else:
            messages.append({
                "role": "assistant",
                "content": None,
                "function_call": msg.function_call
            })
            messages.append({
                "role": "function",
                "name": "execute_sql",
                "content": json.dumps(result, default=str)
            })
            followup = openai_client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=messages
            )
            answer = followup.choices[0].message.content.strip()
    else:
        answer = msg.content.strip()

    remember(psid, "assistant", answer)
    return answer

# ----------------------------------------------------------------------------
# 8. Send replies via Messenger API
# ----------------------------------------------------------------------------
def send(psid, text):
    requests.post(
        f"https://graph.facebook.com/v17.0/me/messages?access_token={PAGE_ACCESS_TOKEN}",
        json={"recipient": {"id": psid}, "message": {"text": text}},
        timeout=10
    )

# ----------------------------------------------------------------------------
# Entrypoint
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    threading.Thread(target=register_webhook, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)
