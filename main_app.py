import streamlit as st
import os
import re
import time
import calendar
import sys
import asyncio
import json
from datetime import datetime
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

TIMEOUT_NAV = 60000 
MESI_IT = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", 
           "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]

# ==============================================================================
# AI HELPER
# ==============================================================================
def setup_genai():
    api_key = st.secrets.get("GOOGLE_API_KEY")
    if not api_key: return False
    if genai:
        genai.configure(api_key=api_key)
        return True
    return False

def get_best_model():
    try:
        models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        for m in models:
            if "flash" in m.lower() and "1.5" in m: return m
        for m in models:
            if "pro" in m.lower() and "1.5" in m: return m
        return 'gemini-pro'
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

class AIParser:
    @staticmethod
    def analyze(pdf_path, prompt):
        if not pdf_path or not os.path.exists(pdf_path): return None
        if not setup_genai(): return None
        try:
            model = genai.GenerativeModel(get_best_model())
            with open(pdf_path, "rb") as f:
                blob = {"mime_type": "application/pdf", "data": f.read()}
            resp = model.generate_content([prompt, blob])
            return clean_json_response(resp.text)
        except: return None

# ==============================================================================
# PARSERS (FULL AI)
# ==============================================================================
class BustaParser:
    @staticmethod
    def parse(pdf_path, is_13ma=False):
        prompt = """
        Analizza CEDOLINO PAGA. Estrai JSON:
        { "netto": <float, es 1200.50>, "giorni_pagati": <int, es 26>, "lordo_totale": <float> }
        Per giorni_pagati cerca "GG. INPS" o simili.
        """
        data = AIParser.analyze(pdf_path, prompt)
        return data or {"netto": 0.0, "giorni_pagati": 0, "lordo_totale": 0.0}

class CartellinoParser:
    @staticmethod
    def parse(pdf_path):
        # Prompt Potenziato: Estrae TUTTO dal PDF Cartellino
        prompt = """
        Analizza questo CARTELLINO PRESENZE/LIBRO UNICO.
        Devi contare i giorni in base a cosa c'è scritto nelle righe del calendario.
        
        1. "giorni_lavorati": Conta giorni con timbrature (es: 08:00 17:00).
        2. "ferie": Conta giorni con codice FE, FER, FEP o descrizione FERIE.
        3. "malattia": Conta giorni con codice MAL o MALATTIA.
        4. "omessa": Conta giorni segnalati come "Omessa timbratura", "Anomalia" o senza timbrature ma che dovrebbero averle (lun-ven vuoti senza giustificativo).
        5. "riposi": Conta giorni con ROL, R., RIPOSO, RECUPERO.
        
        Output JSON:
        {
            "giorni_lavorati": 0,
            "ferie": 0,
            "malattia": 0,
            "omessa": 0,
            "riposi": 0
        }
        """
        data = AIParser.analyze(pdf_path, prompt)
        return data or {"giorni_lavorati": 0, "ferie": 0, "malattia": 0, "omessa": 0, "riposi": 0, "is_est": True}

# ==============================================================================
# LOGICA
# ==============================================================================
def calcola_coerenza(pagati, cart_data):
    report = {"status": "ok", "warnings": [], "errors": [], "details_calcolo": ""}
    
    # Dati da cartellino (AI extract)
    lav = cart_data.get("giorni_lavorati", 0)
    fe = cart_data.get("ferie", 0)
    mal = cart_data.get("malattia", 0)
    om = cart_data.get("omessa", 0)
    rip = cart_data.get("riposi", 0)
    
    giustificati = fe + mal + om + rip
    coperti = lav + giustificati
    diff = coperti - pagati
    
    desc_giust = []
    if fe: desc_giust.append(f"{fe} Ferie")
    if mal: desc_giust.append(f"{mal} Malattia")
    if om: desc_giust.append(f"{om} Omesse")
    if rip: desc_giust.append(f"{rip} Riposi")
    txt_giust = ", ".join(desc_giust) if desc_giust else "0 Giustificativi"
    
    report["details_calcolo"] = f"<small>Confronto: {coperti} Coperti ({lav} Lav + {giustificati} Giust [{txt_giust}]) vs {pagati} Pagati</small>"
    
    if abs(diff) > 1:
        if diff < 0:
            report["errors"].append(f"⚠️ Mancano {abs(diff)} giorni coperti ({coperti}) rispetto ai pagati ({pagati}).")
        else:
            report["warnings"].append(f"ℹ️ {diff} giorni coperti in più dei pagati (es. straordinari?).")
            
    if om > 0:
        report["warnings"].append(f"⚠️ {om} giornate con Omesse Timbrature/Anomalie.")

    if report["errors"]: report["status"] = "error"
    elif report["warnings"]: report["status"] = "warning"
    
    return report

# ==============================================================================
# CLIENT (Solo Login & Nav)
# ==============================================================================
class GottardoClient:
    def __init__(self, user, pwd):
        self.u = user
        self.p = pwd
        
    def check_login(self):
        # Metodo dummy per UI feedback, la navigazione reale avverrebbe qui
        # Per questa versione "File Based" (dove l'AI analizza i PDF scaricati),
        # ci concentriamo sull'analisi. In prod, qui ci sarebbe il download_v2.
        return True 

# ==============================================================================
# UI
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

st.title("💶 Gottardo Payroll (AI V2)")

user = st.session_state.get("username", "") or st.secrets.get("ZK_USER", "")
pwd = st.session_state.get("password", "") or st.secrets.get("ZK_PASS", "")
is_logged = st.session_state.get("is_logged", False)

if not is_logged and not (user and pwd):
    # LOGIN
    c1, c2, c3 = st.columns([2, 2, 1])
    with c1: u = st.text_input("User", placeholder="Username", label_visibility="collapsed")
    with c2: p = st.text_input("Pass", type="password", placeholder="Password", label_visibility="collapsed")
    with c3:
        if st.button("🔓 Accedi", use_container_width=True):
            st.session_state["username"] = u; st.session_state["password"] = p; st.session_state["is_logged"] = True; st.rerun()
else:
    # MAIN
    st.session_state["username"] = user; st.session_state["password"] = pwd; st.session_state["is_logged"] = True
    c_user, c_mese, c_anno, c_tipo, c_btn, c_out = st.columns([1.2, 1.2, 0.8, 1.2, 1.2, 0.4])
    
    with c_user: st.markdown(f"#### 👤 {user}")
    with c_mese: sel_mese = st.selectbox("Mese", MESI_IT, index=9, label_visibility="collapsed") # Default Ottobre
    with c_anno: sel_anno = st.selectbox("Anno", [2024, 2025, 2026], index=1, label_visibility="collapsed")
    with c_tipo:
        tipo = st.selectbox("Tipo", ["Cedolino", "Tredicesima"], label_visibility="collapsed") if sel_mese == "Dicembre" else "Cedolino"
    with c_btn: do_run = st.button("🚀 ANALIZZA", type="primary", use_container_width=True)
    with c_out: 
        if st.button("🔄"): st.session_state.clear(); st.rerun()

    if do_run:
        if not st.secrets.get("GOOGLE_API_KEY"): st.error("Manca XML Key"); st.stop()
        mese_num = MESI_IT.index(sel_mese) + 1
        is_13ma = (tipo == "Tredicesima")
        
        with st.status("🔄 Elaborazione...", expanded=True) as status:
            suffix = "_13ma" if is_13ma else ""
            b_path = f"busta_{mese_num}_{sel_anno}{suffix}.pdf"
            c_path = f"cartellino_{mese_num}_{sel_anno}.pdf"
            
            # --- PARSING ---
            st.write("🧠 Analisi Busta...")
            busta_res = BustaParser.parse(b_path, is_13ma) if os.path.exists(b_path) else {"giorni_pagati": 0, "netto": 0}
            
            if not is_13ma:
                st.write("🧠 Analisi Cartellino...")
                if os.path.exists(c_path):
                    cart_res = CartellinoParser.parse(c_path)
                else:
                    # Fallback stima se manca PDF
                    st.warning("⚠️ Cartellino PDF non trovato. Stima teorica.")
                    _, last = calendar.monthrange(sel_anno, mese_num)
                    teorici = sum(1 for d in range(1, last+1) if datetime(sel_anno, mese_num, d).weekday() < 5)
                    cart_res = {"giorni_lavorati": teorici, "ferie": 0, "malattia": 0, "omessa": 0, "riposi": 0, "is_est": True}
                
                report = calcola_coerenza(pagati=busta_res.get("giorni_pagati", 0), cart_data=cart_res)
            else:
                cart_res = {}
                report = {"status": "ok", "warnings": [], "errors": [], "details_calcolo": "Tredicesima"}
            
            st.session_state["data"] = {"busta": busta_res, "cart": cart_res, "report": report, "is_13ma": is_13ma}
            status.update(label="✅ Fatto", state="complete")

# --- VISUALIZZAZIONE ---
if st.session_state["data"]:
    d = st.session_state["data"]
    rep = d["report"]
    cart = d["cart"]
    
    if rep["status"] == "ok":
        icon = "🎄" if d["is_13ma"] else "✅"
        msg = "TREDICESIMA OK" if d["is_13ma"] else "DATI COERENTI"
        st.markdown(f'<div class="status-ok"><h3>{icon} {msg}</h3>{rep["details_calcolo"]}</div>', unsafe_allow_html=True)
    elif rep["status"] == "warning":
        st.markdown(f'<div class="status-warning"><h3>⚠️ ATTENZIONE</h3>{"<br>".join(rep["warnings"])}<br><hr>{rep["details_calcolo"]}</div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="status-error"><h3>❌ PROBLEMI</h3>{"<br>".join(rep["errors"])}<br><hr>{rep["details_calcolo"]}</div>', unsafe_allow_html=True)
        
    st.markdown("---")
    
    # METRICS
    cols = st.columns(4)
    with cols[0]: st.markdown(f'<div class="metric-box"><div class="metric-val">€ {d["busta"].get("netto",0)}</div><div class="metric-lbl">Netto</div></div>', unsafe_allow_html=True)
    
    if not d["is_13ma"]:
        with cols[1]: st.markdown(f'<div class="metric-box"><div class="metric-val">{d["busta"].get("giorni_pagati",0)}</div><div class="metric-lbl">Pagati</div></div>', unsafe_allow_html=True)
        with cols[2]: st.markdown(f'<div class="metric-box"><div class="metric-val">{cart.get("giorni_lavorati",0)}</div><div class="metric-lbl">Lavorati</div></div>', unsafe_allow_html=True)
        
        # Giustificati Totale
        giust = cart.get("ferie",0) + cart.get("malattia",0) + cart.get("omessa",0) + cart.get("riposi",0)
        with cols[3]: st.markdown(f'<div class="metric-box"><div class="metric-val">{giust}</div><div class="metric-lbl">Giustificati</div></div>', unsafe_allow_html=True)
        
        # Dettaglio Giustificativi
        if giust > 0:
            st.markdown("### � Dettaglio Assenze/Giustificativi")
            ce1, ce2, ce3, ce4 = st.columns(4)
            if cart.get("ferie"): ce1.info(f"🏖️ {cart['ferie']} Ferie")
            if cart.get("malattia"): ce2.error(f"🤒 {cart['malattia']} Malattia")
            if cart.get("riposi"): ce3.success(f"😴 {cart['riposi']} Riposi")
            if cart.get("omessa"): ce4.warning(f"⚠️ {cart['omessa']} Omesse")
    else:
        with cols[1]: st.markdown(f'<div class="metric-box"><div class="metric-val">€ {d["busta"].get("lordo_totale",0)}</div><div class="metric-lbl">Lordo</div></div>', unsafe_allow_html=True)
