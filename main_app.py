import streamlit as st
import os
import re
import time
import calendar
import sys
import asyncio
from datetime import datetime
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from pathlib import Path

# --- SETUP AMBIENTE CLOUD ---
try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    pass

# Auto-installazione browser per Streamlit Cloud
if "installed" not in st.session_state:
    os.system("playwright install chromium")
    st.session_state["installed"] = True

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# ==============================================================================
# CONFIG & COSTANTI
# ==============================================================================
st.set_page_config(page_title="Gottardo Payroll", page_icon="💶", layout="wide", initial_sidebar_state="collapsed")

TIMEOUT_API = 30000
TIMEOUT_NAV = 45000

CALENDAR_DEFAULT = {
    "FEP": "Ferie",
    "OMT": "Omessa Timbratura",
    "RCS": "Riposo Compensativo",
    "RIC": "Riposo Compensativo",
    "MAL": "Malattia",
    "PER": "Permesso",
    "ROL": "Permesso ROL",
    "INF": "Infortunio"
}

MESI_IT = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", 
           "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]

# ==============================================================================
# UTILS & PARSERS (REGEX)
# ==============================================================================
def format_date_it(date_str):
    if not date_str or len(date_str) < 10: return ""
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        mesi = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]
        return f"{dt.day} {mesi[dt.month - 1]}"
    except: return date_str

def extract_text(pdf_path):
    if not pdf_path or not os.path.exists(pdf_path): return ""
    text = ""
    try:
        doc = fitz.open(pdf_path)
        for page in doc: text += page.get_text() + "\n"
    except: pass
    return text

class BustaParser:
    @staticmethod
    def parse(pdf_path):
        text = extract_text(pdf_path)
        data = {"netto": 0.0, "giorni_pagati": 0, "lordo_totale": 0.0}
        
        # 1. Netto (Cerca cifre vicino a PROGRESSIVI o NETTO)
        lines = text.split('\n')
        re_money = r"([\d\.]+,\d{2})"
        for line in reversed(lines):
            if "PROGRESSIVI" in line and "NETTO" not in line:
                matches = re.findall(re_money, line)
                if matches:
                    data["netto"] = float(matches[-1].replace('.', '').replace(',', '.'))
                    break
        if data["netto"] == 0:
            m = re.search(r"NETTO.*?([\d\.]+,\d{2})", text)
            if m: data["netto"] = float(m.group(1).replace('.', '').replace(',', '.'))

        # 2. Giorni Pagati
        m = re.search(r"GG\. INPS\s+(\d+)", text)
        if m: data["giorni_pagati"] = int(m.group(1))
        
        # 3. Lordo
        m = re.search(r"(TOTALE COMPETENZE|TOTALE LORDO).*?([\d\.]+,\d{2})", text)
        if m: data["lordo_totale"] = float(m.group(2).replace('.', '').replace(',', '.'))
        
        return data

class CartellinoParser:
    @staticmethod
    def parse(pdf_path):
        text = extract_text(pdf_path)
        data = {"giorni_reali": 0, "note": ""}
        if not text: return data
        
        giorni_lavorati = 0
        lines = text.split('\n')
        # Cerca righe con data dd/mm e almeno un orario hh:mm
        for line in lines:
            if re.search(r"\b([0-3]?\d)/([0-1]?\d)\b", line):
                orari = re.findall(r"\d{1,2}:\d{2}", line)
                if len(orari) >= 1:
                    giorni_lavorati += 1
        
        data["giorni_reali"] = giorni_lavorati
        return data

# ==============================================================================
# LOGICA COERENZA
# ==============================================================================
def calcola_coerenza(pagati, lavorati_cartellino, eventi_agenda, giorni_teorici):
    report = {"status": "ok", "warnings": [], "errors": [], "details_calcolo": ""}
    
    # Conteggi agenda
    counts = eventi_agenda.get("counts", {})
    count_fe = counts.get("FEP", 0) # Ferie
    count_ma = counts.get("MAL", 0) # Malattia
    count_ri = counts.get("RCS", 0) + counts.get("RIC", 0) # Riposi
    count_om = counts.get("OMT", 0) # Omesse
    count_pe = counts.get("PER", 0) + counts.get("ROL", 0) # Permessi

    # Logica fondamentale: 
    # Pagati (Busta) ≈ Lavorati (Badge) + Giustificati (Assenze + Omesse)
    
    giustificati_totali = count_fe + count_ma + count_ri + count_om + count_pe
    giorni_coperti = lavorati_cartellino + giustificati_totali
    
    diff = giorni_coperti - pagati
    
    # Dettaglio comprensibile
    dettagli = []
    if count_fe: dettagli.append(f"{count_fe} Ferie")
    if count_ma: dettagli.append(f"{count_ma} Malattia")
    if count_ri: dettagli.append(f"{count_ri} Riposi")
    if count_om: dettagli.append(f"{count_om} Omesse")
    desc_giust = ", ".join(dettagli) if dettagli else "0 assenze"
    
    report["details_calcolo"] = (
        f"**Confronto:** {giorni_coperti} Giorni Coperti vs {pagati} Pagati<br>"
        f"<small>Coperti = {lavorati_cartellino} Lavorati + {giustificati_totali} Giustificati ({desc_giust})</small>"
    )
    
    # Check principale
    if abs(diff) > 1:
        if diff < 0:
            # Es: 16 lav + 4 giust = 20 coperti. Pagati 24. Mancano 4 giorni all'appello.
            report["errors"].append(f"⚠️ Mancano {abs(diff)} giorni coperti rispetto ai pagati ({giorni_coperti} vs {pagati}).")
            report["errors"].append("Hai lavorato dei giorni senza timbrare o mancano giustificativi?")
        else:
            # Es: 22 lav + 5 giust = 27 coperti. Pagati 24.
            report["warnings"].append(f"ℹ️ Risultano {diff} giorni coperti in più dei pagati (es. straordinari o riposi non goduti?).")
    
    # Check Omesse
    if count_om > 0:
        report["warnings"].append(f"⚠️ Ci sono {count_om} omesse timbrature da sistemare.")

    # Status
    if report["errors"]: report["status"] = "error"
    elif report["warnings"]: report["status"] = "warning"
    
    return report

# ==============================================================================
# CLIENT GOTTARDO
# ==============================================================================
class GottardoClient:
    def __init__(self, user, pwd):
        self.u = user
        self.p = pwd
        self.browser = None
        self.page = None
        
    def login_and_scrape(self, mese_num, anno):
        data = {"busta_pdf": None, "cart_pdf": None, "agenda": {"events": [], "counts": {}}}
        p = sync_playwright().start()
        self.browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = self.browser.new_context(accept_downloads=True)
        context.set_default_timeout(TIMEOUT_NAV)
        page = context.new_page()
        
        try:
            # Login
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y")
            page.fill('input[type="text"]', self.u)
            page.fill('input[type="password"]', self.p)
            page.press('input[type="password"]', "Enter")
            page.wait_for_selector("text=I miei dati", timeout=15000)
            
            # API Agenda
            time.sleep(2)
            base_url = "https://selfservice.gottardospa.it/js_rev/JSipert2"
            codes = ["FEP", "OMT", "RCS", "RIC", "MAL", "PER", "ROL", "INF"]
            
            for code in codes:
                try:
                    url = f"{base_url}/api/time/v2/events?$filter_api=calendarCode={code},startTime={anno}-01-01T00:00:00,endTime={anno}-12-31T00:00:00"
                    resp = context.request.get(url, timeout=TIMEOUT_API)
                    if resp.ok:
                        evs = resp.json()
                        if isinstance(evs, list):
                            for e in evs:
                                start = e.get("startTime", "") or e.get("start", "")
                                if start and len(start) >= 7 and int(start[5:7]) == mese_num:
                                    data["agenda"]["events"].append({
                                        "date": format_date_it(start), 
                                        "type_code": code,
                                        "desc": CALENDAR_DEFAULT.get(code, code)
                                    })
                                    data["agenda"]["counts"][code] = data["agenda"]["counts"].get(code, 0) + 1
                except: pass
            
            # Download Busta (Navigazione classica robusta)
            # ... (Logica semplificata per brevità: riusare quella di v2 se file non trovato)
            # Per ora supponiamo download non critico se l'utente carica o se implementiamo download completo
            # Qui inseriamo logica download se vuoi full automation
            pass 
            
        except Exception as e:
            st.error(f"Errore connessione: {e}")
            
        finally:
            self.browser.close()
            p.stop()
            
        return data

# ==============================================================================
# UI STREAMLIT (ORIZZONTALE & MODERNA)
# ==============================================================================
st.markdown("""
<style>
    [data-testid="stSidebar"] { display: none; }
    .status-ok { background: #d4edda; border-left: 5px solid #28a745; padding: 15px; border-radius: 5px; color: #155724; margin-bottom: 20px;}
    .status-warning { background: #fff3cd; border-left: 5px solid #ffc107; padding: 15px; border-radius: 5px; color: #856404; margin-bottom: 20px;}
    .status-error { background: #f8d7da; border-left: 5px solid #dc3545; padding: 15px; border-radius: 5px; color: #721c24; margin-bottom: 20px;}
    .metric-box { background: #ffffff; padding: 15px; border-radius: 8px; text-align: center; border: 1px solid #e9ecef; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
    .metric-val { font-size: 28px; font-weight: bold; color: #2c3e50; }
    .metric-lbl { font-size: 13px; color: #6c757d; text-transform: uppercase; letter-spacing: 0.5px; }
</style>
""", unsafe_allow_html=True)

if "data" not in st.session_state: st.session_state["data"] = None

st.title("💶 Gottardo Payroll Analysis")

# --- LOGIN BAR ---
user = st.session_state.get("username", "") or st.secrets.get("ZK_USER", "")
pwd = st.session_state.get("password", "") or st.secrets.get("ZK_PASS", "")
is_logged = st.session_state.get("is_logged", False)

if not is_logged and not (user and pwd):
    c1, c2, c3 = st.columns([2, 2, 1])
    with c1: u = st.text_input("User", placeholder="Username", label_visibility="collapsed")
    with c2: p = st.text_input("Pass", type="password", placeholder="Password", label_visibility="collapsed")
    with c3:
        if st.button("🔓 Accedi", use_container_width=True):
            if u and p:
                st.session_state["username"] = u
                st.session_state["password"] = p
                st.session_state["is_logged"] = True
                st.rerun()
else:
    st.session_state["username"] = user
    st.session_state["password"] = pwd
    st.session_state["is_logged"] = True

    c_user, c_mese, c_anno, c_btn, c_out = st.columns([1.5, 1.5, 1, 1.5, 0.5])
    with c_user: st.markdown(f"#### 👤 {user}")
    with c_mese: sel_mese = st.selectbox("Mese", MESI_IT, index=11, label_visibility="collapsed")
    with c_anno: sel_anno = st.selectbox("Anno", [2024, 2025, 2026], index=1, label_visibility="collapsed")
    with c_btn: do_run = st.button("🚀 ANALIZZA", type="primary", use_container_width=True)
    with c_out: 
        if st.button("🔄"): 
            st.session_state.clear()
            st.rerun()

    if do_run:
        mese_num = MESI_IT.index(sel_mese) + 1
        with st.status("🔄 Elaborazione in corso...", expanded=True) as status:
            st.write("📡 Connessione al portale...")
            client = GottardoClient(user, pwd)
            raw_data = client.login_and_scrape(mese_num, sel_anno)
            
            # Fake/Simulazione download locale per demo (integrare download reale qui)
            b_path = f"busta_{mese_num}_{sel_anno}.pdf"
            c_path = f"cartellino_{mese_num}_{sel_anno}.pdf"
            
            busta_res = BustaParser.parse(b_path) if os.path.exists(b_path) else {"giorni_pagati": 0, "netto": 0}
            cart_res = CartellinoParser.parse(c_path) if os.path.exists(c_path) else {"giorni_reali": 0, "is_est": True}
            
            # Stima se cartellino manca
            if cart_res.get("is_est"):
                _, last = calendar.monthrange(sel_anno, mese_num)
                teorici = sum(1 for d in range(1, last+1) if datetime(sel_anno, mese_num, d).weekday() < 5)
                assenze = sum(raw_data["agenda"]["counts"].values()) # Somma brutale, affinare
                cart_res["giorni_reali"] = max(0, teorici - assenze)
            
            report = calcola_coerenza(
                pagati=busta_res.get("giorni_pagati", 0),
                lavorati_cartellino=cart_res.get("giorni_reali", 0),
                eventi_agenda=raw_data["agenda"],
                giorni_teorici=22
            )
            
            st.session_state["data"] = {
                "busta": busta_res, 
                "cart": cart_res, 
                "agenda": raw_data["agenda"], 
                "report": report
            }
            status.update(label="✅ Completato", state="complete")

# --- OUTPUT ---
if st.session_state["data"]:
    d = st.session_state["data"]
    rep = d["report"]
    ag = d["agenda"]
    
    # STATUS
    if rep["status"] == "ok":
        st.markdown(f'<div class="status-ok"><h3>✅ DATI COERENTI</h3>Tutto OK.<br>{rep["details_calcolo"]}</div>', unsafe_allow_html=True)
    elif rep["status"] == "warning":
        st.markdown(f'<div class="status-warning"><h3>⚠️ ATTENZIONE</h3>{ "<br>".join(rep["warnings"]) }<br><hr>{rep["details_calcolo"]}</div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="status-error"><h3>❌ PROBLEMI</h3>{ "<br>".join(rep["errors"]) }<br><hr>{rep["details_calcolo"]}</div>', unsafe_allow_html=True)
        
    st.markdown("---")
    
    # METRICS
    m1, m2, m3, m4 = st.columns(4)
    with m1: st.markdown(f'<div class="metric-box"><div class="metric-val">€ {d["busta"].get("netto",0):,.2f}</div><div class="metric-lbl">Netto</div></div>', unsafe_allow_html=True)
    with m2: st.markdown(f'<div class="metric-box"><div class="metric-val">{d["busta"].get("giorni_pagati",0)}</div><div class="metric-lbl">Pagati</div></div>', unsafe_allow_html=True)
    with m3: st.markdown(f'<div class="metric-box"><div class="metric-val">{d["cart"].get("giorni_reali",0)}</div><div class="metric-lbl">Lavorati</div></div>', unsafe_allow_html=True)
    
    # BOX AGENDONA
    ass_tot = sum(ag["counts"].values())
    with m4: st.markdown(f'<div class="metric-box"><div class="metric-val">{ass_tot}</div><div class="metric-lbl">Eventi Agenda</div></div>', unsafe_allow_html=True)
    
    # LISTA EVENTI PULITA
    if ag["events"]:
        st.markdown("### 📅 Dettaglio Eventi")
        # Raggruppa per tipo
        gr = {}
        for e in ag["events"]:
            k = e["desc"]
            if k not in gr: gr[k] = []
            gr[k].append(e["date"])
            
        cols = st.columns(len(gr) if gr else 1)
        for i, (k, dates) in enumerate(gr.items()):
            with cols[i%4]:
                st.info(f"**{k} ({len(dates)})**\n\n" + ", ".join(dates))
