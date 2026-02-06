import streamlit as st
import os
import re
import time
import calendar
import sys
import asyncio
import json
from datetime import datetime
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
from pathlib import Path

# --- SETUP AMBIENTE & DIPENDENZE ---
try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    import google.generativeai as genai
except ImportError:
    genai = None

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    pass

# Auto-installazione browser
if "installed" not in st.session_state:
    os.system("playwright install chromium")
    st.session_state["installed"] = True

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# ==============================================================================
# CONFIG & COSTANTI
# ==============================================================================
st.set_page_config(page_title="Gottardo Payroll", page_icon="💶", layout="wide", initial_sidebar_state="collapsed")

TIMEOUT_API = 40000 
TIMEOUT_NAV = 60000 # Aumentato per siti lenti

CALENDAR_DEFAULT = {
    "FEP": "Ferie", "OMT": "Omessa Timbratura", "RCS": "Riposo Compensativo",
    "RIC": "Riposo Compensativo", "MAL": "Malattia", "PER": "Permesso",
    "ROL": "Permesso ROL", "INF": "Infortunio"
}

MESI_IT = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", 
           "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]

# ==============================================================================
# AI & PARSING HELPERS
# ==============================================================================
def setup_genai():
    api_key = st.secrets.get("GOOGLE_API_KEY")
    if not api_key: return False
    if genai:
        genai.configure(api_key=api_key)
        return True
    return False

def get_best_model():
    """Trova dinamicamente un modello funzionante."""
    try:
        models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        # Priorità: Flash > Pro > Altri
        for m in models:
            if "flash" in m.lower() and "1.5" in m: return m
        for m in models:
            if "pro" in m.lower() and "1.5" in m: return m
        for m in models:
            if "gemini" in m.lower(): return m
        return 'gemini-pro' # Fallback estremo
    except:
        return 'gemini-pro'

def clean_json_response(text):
    try:
        if not text: return None
        text = re.sub(r"```json|```", "", text).strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        return json.loads(text[start:end]) if start != -1 else None
    except: return None

def format_date_it(date_str):
    if not date_str or len(date_str) < 10: return ""
    try:
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        mesi = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]
        return f"{dt.day} {mesi[dt.month - 1]}"
    except: return date_str

class AIParser:
    @staticmethod
    def analyze(pdf_path, prompt):
        if not pdf_path or not os.path.exists(pdf_path): return None
        if not setup_genai(): return None
        
        try:
            model_name = get_best_model() # Dinamico
            model = genai.GenerativeModel(model_name)
            
            with open(pdf_path, "rb") as f:
                blob = {"mime_type": "application/pdf", "data": f.read()}
            
            resp = model.generate_content([prompt, blob])
            return clean_json_response(resp.text)
        except Exception as e:
            st.warning(f"AI Warning: {e}") # Non error bloccante
            return None

class BustaParser:
    @staticmethod
    def parse(pdf_path, is_13ma=False):
        prompt = """
        Sei un esperto paghe italiano. Analizza questo CEDOLINO PAGA GOTTARDO S.p.A.
        Estrai i seguenti dati in formato JSON rigoroso:
        
        1. "netto": Il valore NETTO DEL MESE o PROGRESSIVI netti (numero float, es: 1400.50).
        2. "giorni_pagati": Giorni retribuiti INPS (es: 26 o 22). Se non trovi, cerca giorni lavorati. (int)
        3. "lordo_totale": Totale competenze o lordo (float).
        
        Output JSON atteso:
        { "netto": 0.0, "giorni_pagati": 0, "lordo_totale": 0.0 }
        """
        data = AIParser.analyze(pdf_path, prompt)
        if not data:
            return {"netto": 0.0, "giorni_pagati": 0, "lordo_totale": 0.0}
        return data

class CartellinoParser:
    @staticmethod
    def parse(pdf_path):
        prompt = """
        Analizza questo CARTELLINO PRESENZE.
        Conta ESATTAMENTE quanti giorni hanno almeno una timbratura di ingresso/uscita (es: 08:00 12:00).
        Ignora righe totalmente vuote o con soli giustificativi senza orari.
        
        Output JSON:
        { "giorni_reali": 0 }
        """
        data = AIParser.analyze(pdf_path, prompt)
        if not data: return {"giorni_reali": 0, "is_est": True}
        return data

# ==============================================================================
# LOGICA COERENZA
# ==============================================================================
def calcola_coerenza(pagati, lavorati_cartellino, eventi_agenda, giorni_teorici):
    report = {"status": "ok", "warnings": [], "errors": [], "details_calcolo": ""}
    
    counts = eventi_agenda.get("counts", {})
    giustificati = counts.get("FEP",0) + counts.get("MAL",0) + counts.get("RCS",0) + counts.get("RIC",0) + counts.get("OMT",0) + counts.get("PER",0) + counts.get("ROL",0)
    
    giorni_coperti = lavorati_cartellino + giustificati
    diff = giorni_coperti - pagati
    
    dett = []
    if counts.get("FEP"): dett.append(f"{counts['FEP']} Ferie")
    if counts.get("MAL"): dett.append(f"{counts['MAL']} Malattia")
    if counts.get("OMT"): dett.append(f"{counts['OMT']} Omesse")
    desc_giust = ", ".join(dett) if dett else "0 assenze"
    
    report["details_calcolo"] = f"<small>Confronto: {giorni_coperti} Coperti ({lavorati_cartellino} Lav + {giustificati} Giust) vs {pagati} Pagati</small>"
    
    # Check principale (Tolleranza 1 gg)
    if abs(diff) > 1:
        if diff < 0:
            report["errors"].append(f"⚠️ Mancano {abs(diff)} giorni coperti ({giorni_coperti}) rispetto ai pagati ({pagati}).")
        else:
            report["warnings"].append(f"ℹ️ {diff} giorni coperti in più dei pagati.")
            
    if counts.get("OMT", 0) > 0:
        report["warnings"].append(f"⚠️ {counts['OMT']} Omesse Timbrature.")

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
        self.context = None

    def login_and_scrape(self, mese_num, anno):
        data = {"agenda": {"events": [], "counts": {}}, "files": False}
        p = sync_playwright().start()
        
        # Opzioni anti-detection basiche
        self.browser = p.chromium.launch(
            headless=True, 
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
        )
        self.context = self.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            accept_downloads=True
        )
        self.context.set_default_timeout(TIMEOUT_NAV)
        page = self.context.new_page()
        
        try:
            # Login
            st.toast("Autenticazione...", icon="🔐")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y")
            page.fill('input[type="text"]', self.u)
            page.fill('input[type="password"]', self.p)
            time.sleep(0.5)
            page.press('input[type="password"]', "Enter")
            
            # Attesa intelligente
            try:
                page.wait_for_selector("text=I miei dati", timeout=20000)
            except:
                st.warning("Login lento o popup imprevisto, procedo comunque...")
            
            # API Agenda
            st.toast("Recupero Agenda...", icon="📅")
            time.sleep(1)
            base_url = "https://selfservice.gottardospa.it/js_rev/JSipert2"
            codes = ["FEP", "OMT", "RCS", "RIC", "MAL", "PER", "ROL", "INF"]
            
            for code in codes:
                try:
                    url = f"{base_url}/api/time/v2/events?$filter_api=calendarCode={code},startTime={anno}-01-01T00:00:00,endTime={anno}-12-31T00:00:00"
                    resp = self.context.request.get(url, timeout=TIMEOUT_API)
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
            
        except Exception as e:
            st.error(f"Errore Scraper: {e}")
        finally:
            self.browser.close()
            p.stop()
        
        return data

# ==============================================================================
# UI STREAMLIT
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

st.title("💶 Gottardo Payroll (AI Powered)")

# --- CREDENZIALI ---
user = st.session_state.get("username", "") or st.secrets.get("ZK_USER", "")
pwd = st.session_state.get("password", "") or st.secrets.get("ZK_PASS", "")
is_logged = st.session_state.get("is_logged", False)

if not is_logged and not (user and pwd):
    col1, col2, col3 = st.columns([2, 2, 1])
    with col1: u = st.text_input("User", placeholder="Username", label_visibility="collapsed")
    with col2: p = st.text_input("Pass", type="password", placeholder="Password", label_visibility="collapsed")
    with col3:
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

    # --- BARRA COMANDI ---
    c_user, c_mese, c_anno, c_tipo, c_btn, c_out = st.columns([1.2, 1.2, 0.8, 1.2, 1.2, 0.4])
    
    with c_user: st.markdown(f"#### 👤 {user}")
    with c_mese: sel_mese = st.selectbox("Mese", MESI_IT, index=11, label_visibility="collapsed")
    with c_anno: sel_anno = st.selectbox("Anno", [2024, 2025, 2026], index=1, label_visibility="collapsed")
    
    tipo_doc = "Cedolino"
    with c_tipo:
        if sel_mese == "Dicembre":
            tipo_doc = st.selectbox("Tipo", ["Cedolino", "Tredicesima"], label_visibility="collapsed")
        else:
            st.write("")

    with c_btn: do_run = st.button("🚀 ANALIZZA (AI)", type="primary", use_container_width=True)
    with c_out: 
        if st.button("🔄"): 
            st.session_state.clear()
            st.rerun()

    if do_run:
        if not st.secrets.get("GOOGLE_API_KEY"):
            st.error("❌ GOOGLE_API_KEY mancante!")
            st.stop()
            
        mese_num = MESI_IT.index(sel_mese) + 1
        is_13ma = (tipo_doc == "Tredicesima")
        
        with st.status("🔄 Elaborazione...", expanded=True) as status:
            client = GottardoClient(user, pwd)
            raw_data = client.login_and_scrape(mese_num, sel_anno)
            
            # --- FILE HANDLING ---
            # Assume file locali per fallback se scraper non scarica
            suffix = "_13ma" if is_13ma else ""
            b_path = f"busta_{mese_num}_{sel_anno}{suffix}.pdf"
            c_path = f"cartellino_{mese_num}_{sel_anno}.pdf"
            
            # --- PARSING AI ---
            st.write("🧠 Avvio AI (Auto-Select)...")
            
            if os.path.exists(b_path):
                busta_res = BustaParser.parse(b_path, is_13ma)
            else:
                st.warning("⚠️ PDF Busta non trovato (Scraper download dummy attivo)")
                busta_res = {"giorni_pagati": 0, "netto": 0}

            if not is_13ma:
                if os.path.exists(c_path):
                    cart_res = CartellinoParser.parse(c_path)
                else:
                    st.info("ℹ️ Stima presenze da Agenda (PDF Cartellino assente)")
                    _, last = calendar.monthrange(sel_anno, mese_num)
                    teorici = sum(1 for d in range(1, last+1) if datetime(sel_anno, mese_num, d).weekday() < 5)
                    ass = sum(raw_data["agenda"]["counts"].values())
                    cart_res = {"giorni_reali": max(0, teorici - ass), "is_est": True}
                
                report = calcola_coerenza(
                    pagati=busta_res.get("giorni_pagati", 0),
                    lavorati_cartellino=cart_res.get("giorni_reali", 0),
                    eventi_agenda=raw_data["agenda"],
                    giorni_teorici=22
                )
            else:
                cart_res = {"giorni_reali": 0}
                report = {"status": "ok", "warnings": [], "errors": [], "details_calcolo": "Analisi Tredicesima"}

            st.session_state["data"] = {
                "busta": busta_res, 
                "cart": cart_res, 
                "agenda": raw_data["agenda"], 
                "report": report,
                "is_13ma": is_13ma
            }
            status.update(label="✅ Completato", state="complete")

# --- OUTPUT ---
if st.session_state["data"]:
    d = st.session_state["data"]
    rep = d["report"]
    ag = d["agenda"]
    is_13ma = d.get("is_13ma", False)
    
    if rep["status"] == "ok":
        icon = "🎄" if is_13ma else "✅"
        msg = "TREDICESIMA OK" if is_13ma else "DATI COERENTI"
        st.markdown(f'<div class="status-ok"><h3>{icon} {msg}</h3>{rep["details_calcolo"]}</div>', unsafe_allow_html=True)
    elif rep["status"] == "warning":
        st.markdown(f'<div class="status-warning"><h3>⚠️ ATTENZIONE</h3>{"<br>".join(rep["warnings"])}<br><hr>{rep["details_calcolo"]}</div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="status-error"><h3>❌ PROBLEMI</h3>{"<br>".join(rep["errors"])}<br><hr>{rep["details_calcolo"]}</div>', unsafe_allow_html=True)
        
    st.markdown("---")
    
    cols = st.columns(4)
    with cols[0]: st.markdown(f'<div class="metric-box"><div class="metric-val">€ {d["busta"].get("netto",0)}</div><div class="metric-lbl">Netto</div></div>', unsafe_allow_html=True)
    
    if not is_13ma:
        with cols[1]: st.markdown(f'<div class="metric-box"><div class="metric-val">{d["busta"].get("giorni_pagati",0)}</div><div class="metric-lbl">Pagati</div></div>', unsafe_allow_html=True)
        with cols[2]: st.markdown(f'<div class="metric-box"><div class="metric-val">{d["cart"].get("giorni_reali",0)}</div><div class="metric-lbl">Lavorati</div></div>', unsafe_allow_html=True)
        ass = sum(ag["counts"].values())
        with cols[3]: st.markdown(f'<div class="metric-box"><div class="metric-val">{ass}</div><div class="metric-lbl">Giustificati</div></div>', unsafe_allow_html=True)
        
        if ag["events"]:
            st.markdown("### 📅 Dettaglio")
            gr = {}
            for e in ag["events"]:
                k = e["desc"]
                if k not in gr: gr[k] = []
                gr[k].append(e["date"])
            
            c_ev = st.columns(len(gr) if gr else 1)
            for i, (k, v) in enumerate(gr.items()):
                with c_ev[i%4]: st.info(f"**{k} ({len(v)})**\n\n" + ", ".join(v))
    else:
        with cols[1]: st.markdown(f'<div class="metric-box"><div class="metric-val">€ {d["busta"].get("lordo_totale",0)}</div><div class="metric-lbl">Lordo</div></div>', unsafe_allow_html=True)
