import sys
import asyncio
import re
import os
import streamlit as st
import google.generativeai as genai
from playwright.sync_api import sync_playwright
import json
import time
import calendar
import locale
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

# --- OPTIONAL ---
try:
    import fitz  # PyMuPDF
except Exception: fitz = None

try:
    from pypdf import PdfReader
except Exception: PdfReader = None


# ==============================================================================
# CONFIG
# ==============================================================================
st.set_page_config(page_title="Gottardo Payroll", page_icon="💶", layout="wide", initial_sidebar_state="collapsed")
os.system("playwright install chromium")
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

try:
    locale.setlocale(locale.LC_TIME, "it_IT.UTF-8")
except Exception: pass

CALENDAR_CODES = { "FEP": "FERIE", "OMT": "OMESSA TIMBRATURA", "RCS": "RIPOSO", "RIC": "RIPOSO", "MAL": "MALATTIA" }
AGENDA_KEYWORDS = ["OMESSA", "MALATTIA", "RIPOSO", "FERIE", "PERMESS", "ANOMALIA"]
MESI_IT = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]

# ==============================================================================
# AI
# ==============================================================================
def setup_genai():
    api_key = st.secrets.get("GOOGLE_API_KEY")
    if not api_key: return False
    genai.configure(api_key=api_key)
    return True

def get_best_model():
    try:
        models = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        for m in models: 
            if "flash" in m.lower() and "1.5" in m: return m
        return 'gemini-pro'
    except: return 'gemini-pro'

def clean_json(text):
    try:
        text = re.sub(r"```json|```", "", text).strip()
        s, e = text.find("{"), text.rfind("}") + 1
        return json.loads(text[s:e]) if s != -1 else None
    except: return None

def analyze_pdf(path, prompt):
    if not path or not os.path.exists(path): return None
    if not setup_genai(): return None
    try:
        model = genai.GenerativeModel(get_best_model())
        with open(path, "rb") as f:
            blob = {"mime_type": "application/pdf", "data": f.read()}
        resp = model.generate_content([prompt, blob])
        return clean_json(resp.text)
    except: return None

# ==============================================================================
# PARSERS AI
# ==============================================================================
def parse_busta(path):
    prompt = """
    Analizza CEDOLINO. Estrai JSON:
    { "netto": <float>, "giorni_pagati": <int, cercare GG. INPS>, "lordo_totale": <float> }
    """
    return analyze_pdf(path, prompt) or {"netto": 0, "giorni_pagati": 0, "lordo": 0}

def parse_cartellino(path):
    prompt = """
    Analizza CARTELLINO.
    Conta:
    1. giorni_lavorati: giorni con timbrature (es 08:00).
    2. ferie: FE, FERIE.
    3. malattia: MAL, MALATTIA.
    4. omessa: ANOMALIA, OMESSA.
    5. riposi: RIPOSO, ROL, RECUPERO.
    
    Output JSON: { "giorni_lavorati": 0, "ferie": 0, "malattia": 0, "omessa": 0, "riposi": 0 }
    """
    return analyze_pdf(path, prompt) or {"giorni_lavorati": 0, "ferie": 0, "malattia": 0, "omessa": 0, "riposi": 0}

# ==============================================================================
# SCRAPER CORE (Logica Originale Funzionante)
# ==============================================================================
def execute_download(mese_nome, anno, user, pwd, is_13ma):
    paths = {"busta": None, "cart": None}
    
    try:
        idx = MESI_IT.index(mese_nome) + 1
    except: return paths
    
    suffix = "_13" if is_13ma else ""
    local_busta = os.path.abspath(f"busta_{idx}_{anno}{suffix}.pdf")
    local_cart = os.path.abspath(f"cartellino_{idx}_{anno}.pdf")
    target_busta = f"Tredicesima {anno}" if is_13ma else f"{mese_nome} {anno}"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(accept_downloads=True)
        page = ctx.new_page()
        
        try:
            # LOGIN
            st.toast("Login...", icon="🔐")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y")
            page.fill('input[type="text"]', user)
            page.fill('input[type="password"]', pwd)
            page.press('input[type="password"]', "Enter")
            try: page.wait_for_selector("text=I miei dati", timeout=20000)
            except: 
                st.error("Login fallito")
                return paths

            # BUSTA
            st.toast("Scarico Busta...", icon="💰")
            try:
                # Navigazione robusta Documenti
                try: page.locator("text=Documenti").first.click()
                except: page.evaluate("document.getElementById('revit_navigation_NavHoverItem_0_label')?.click()")
                time.sleep(2)
                
                try: page.locator("text=Cedolino").first.click()
                except: pass
                time.sleep(3)
                
                # Cerca link
                with page.expect_download(timeout=20000) as dl_info:
                    # Cerca link per testo parziale (case insensitive)
                    if is_13ma:
                        page.get_by_text(re.compile(f"Tredicesima.*{anno}", re.I)).first.click()
                    else:
                        # Cerca Mese e Anno
                        links = page.locator("a")
                        found = False
                        for i in range(links.count()):
                            txt = links.nth(i).inner_text()
                            if mese_nome in txt and str(anno) in txt and "Tredicesima" not in txt:
                                links.nth(i).click()
                                found = True
                                break
                        if not found: raise Exception("Link non trovato")
                
                dl_info.value.save_as(local_busta)
                if os.path.exists(local_busta): paths["busta"] = local_busta
                
            except Exception as e:
                st.warning(f"Busta non scaricata: {e}")

            # CARTELLINO
            if not is_13ma:
                st.toast("Scarico Cartellino...", icon="📅")
                try:
                    # Navigazione Presenze
                    try: page.locator("text=Presenze").first.click()
                    except: page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
                    time.sleep(2)
                    
                    try: page.locator("text=Cartellino").first.click()
                    except: pass
                    time.sleep(2)
                    
                    # Date
                    last = calendar.monthrange(anno, idx)[1]
                    d1, d2 = f"01/{idx:02d}/{anno}", f"{last}/{idx:02d}/{anno}"
                    
                    ins = page.locator(".dijitInputInner")
                    if ins.count() >= 2:
                        ins.nth(0).fill(d1); ins.nth(0).press("Tab")
                        time.sleep(0.5)
                        ins.nth(1).fill(d2); ins.nth(1).press("Tab")
                    
                    # Search
                    page.get_by_role("button", name=re.compile("ricerca|esegui", re.I)).last.click()
                    time.sleep(5)
                    
                    # PDF Icon
                    icon = page.locator("img[src*='search'], .z-icon-search").first
                    if icon.count() > 0:
                        with ctx.expect_page() as pop:
                            icon.click()
                        popup = pop.value
                        popup.wait_for_load_state()
                        
                        # Save
                        url = popup.url + ("&EMBED=y" if "EMBED" not in popup.url else "")
                        resp = ctx.request.get(url)
                        if resp.body()[:4] == b"%PDF":
                            with open(local_cart, "wb") as f: f.write(resp.body())
                            paths["cart"] = local_cart
                            
                except Exception as e:
                    st.warning(f"Cartellino non scaricato: {e}")

        except Exception as e:
            st.error(f"Errore Navigazione: {e}")
        finally:
            browser.close()
            
    return paths

# ==============================================================================
# UI CLEAN
# ==============================================================================
st.title("💶 Gottardo Payroll (V3 Restore)")

# CREDENZIALI
u = st.session_state.get("u", st.secrets.get("ZK_USER", ""))
p = st.session_state.get("p", st.secrets.get("ZK_PASS", ""))

if not u or not p:
    c1, c2, c3 = st.columns([2,2,1])
    u_in = c1.text_input("User"); p_in = c2.text_input("Pass", type="password")
    if c3.button("Login"):
        st.session_state["u"] = u_in; st.session_state["p"] = p_in; st.rerun()
else:
    # BARRA AZIONI
    col_u, col_m, col_a, col_btn, col_rst = st.columns([1, 1.5, 1, 1.5, 0.5])
    col_u.markdown(f"**👤 {u}**")
    m = col_m.selectbox("Mese", MESI_IT, index=11)
    a = col_a.selectbox("Anno", [2024, 2025, 2026], index=1)
    
    tipo = "Cedolino"
    if m == "Dicembre":
        tipo = col_m.radio("Tipo", ["Cedolino", "Tredicesima"], horizontal=True)

    if col_btn.button("🚀 ANALIZZA", type="primary"):
        is_13 = (tipo == "Tredicesima")
        with st.status("🔄 Elaborazione...", expanded=True):
            paths = execute_download(m, a, u, p, is_13)
            
            st.write("🧠 Analisi AI...")
            res_b = parse_busta(paths["busta"])
            res_c = parse_cartellino(paths["cart"]) if not is_13 else {}
            
            st.session_state["res"] = {"b": res_b, "c": res_c, "is_13": is_13, "paths": paths}

    if col_rst.button("🔄"): st.session_state.clear(); st.rerun()

# RISULTATI
if "res" in st.session_state:
    data = st.session_state["res"]
    b = data["b"]
    c = data["c"]
    is_13 = data["is_13"]
    
    # STATUS CHECK
    if not is_13:
        lav = c.get("giorni_lavorati", 0)
        giust = c.get("ferie",0)+c.get("malattia",0)+c.get("riposi",0)+c.get("omessa",0)
        tot = lav + giust
        pag = b.get("giorni_pagati", 0)
        diff = tot - pag
        
        status_html = ""
        if abs(diff) <= 1:
            status_html = f'<div style="background:#d4edda;padding:15px;border-radius:5px;border-left:5px solid #28a745"><h3>✅ DATI COERENTI</h3>Coperti: {tot} (Lav {lav} + Giust {giust}) vs Pagati: {pag}</div>'
        else:
            status_html = f'<div style="background:#f8d7da;padding:15px;border-radius:5px;border-left:5px solid #dc3545"><h3>❌ ATTENZIONE</h3>Mancano {abs(diff)} giorni! Coperti: {tot} vs Pagati: {pag}</div>'
        st.markdown(status_html, unsafe_allow_html=True)
    else:
        st.success("🎄 TREDICESIMA ANALIZZATA")

    st.markdown("---")
    
    # KIPS
    k1, k2, k3, k4 = st.columns(4)
    k1.metric("Netto", f"€ {b.get('netto', 0)}")
    k2.metric("Pagati", b.get("giorni_pagati", 0))
    if not is_13:
        k3.metric("Lavorati", c.get("giorni_lavorati", 0))
        k4.metric("Giustificati", c.get("ferie",0)+c.get("malattia",0)+c.get("riposi",0))
    else:
        k3.metric("Lordo", f"€ {b.get('lordo_totale', 0)}")
        
    # DETTAGLI
    if not is_13 and (c.get("ferie") or c.get("malattia") or c.get("omessa")):
        st.caption("📋 Dettaglio Giustificativi rilevati dal Cartellino:")
        c_d = st.columns(4)
        if c.get("ferie"): c_d[0].info(f"🏖️ {c['ferie']} Ferie")
        if c.get("malattia"): c_d[1].error(f"🤒 {c['malattia']} Malattia")
        if c.get("omessa"): c_d[2].warning(f"⚠️ {c['omessa']} Omesse")
