import sys
import asyncio
import re
import requests
import os
import streamlit as st
import google.generativeai as genai
from playwright.sync_api import sync_playwright
import json
import time
import calendar
import locale
from datetime import datetime

# --- SETUP BASE ---
os.system("playwright install chromium")
if sys.platform == 'win32': asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
try: locale.setlocale(locale.LC_TIME, 'it_IT.UTF-8')
except: pass 

# --- CREDENZIALI ---
try:
    ZK_USER = st.secrets["ZK_USER"]
    ZK_PASS = st.secrets["ZK_PASS"]
    GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
except:
    ZK_USER = "10021450" 
    ZK_PASS = "Diocane3!"
    GOOGLE_API_KEY = "AIzaSyA1OMmdyg-mLrZKO0WFErurf_Q4mfqKKNM"

genai.configure(api_key=GOOGLE_API_KEY)
try: model = genai.GenerativeModel('gemini-flash-latest')
except: model = genai.GenerativeModel('gemini-1.5-flash')

# --- FUNZIONI PARSING AI (INVARIATE) ---
def clean_json_response(text):
    try:
        text = re.sub(r"```json|```", "", text).strip()
        start = text.find('{')
        end = text.rfind('}') + 1
        return json.loads(text[start:end]) if start != -1 else json.loads(text)
    except: return None

def estrai_dati_busta_dettagliata(file_path):
    if not file_path: return None
    try:
        with open(file_path, "rb") as f: bytes_data = f.read()
        prompt = """Analizza cedolino. JSON: {"dati_generali": {"netto": float, "giorni_pagati": float}, "competenze": {"base": float, "straordinari": float}, "trattenute": {"inps": float, "irpef_netta": float}, "ferie_tfr": {"saldo": float}}"""
        response = model.generate_content([prompt, {"mime_type": "application/pdf", "data": bytes_data}])
        return clean_json_response(response.text)
    except: return None

def estrai_dati_cartellino(file_path):
    if not file_path: return None
    try:
        with open(file_path, "rb") as f: bytes_data = f.read()
        prompt = """Analizza cartellino. JSON: { "giorni_reali": int, "note": "string" }"""
        response = model.generate_content([prompt, {"mime_type": "application/pdf", "data": bytes_data}])
        return clean_json_response(response.text)
    except: return None

# --- CORE: DOWNLOAD VIA API (NUOVA STRATEGIA) ---
def scarica_documenti_veloce(mese_nome, anno):
    nomi_mesi_it = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", 
                    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
    try: mese_num = nomi_mesi_it.index(mese_nome) + 1
    except: return None, None

    st_status = st.empty()
    st_status.info("🚀 Avvio Protocollo Rapido...")
    
    path_busta = None
    path_cart = None
    
    # Sessione HTTP persistente
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36'
    })

    # FASE 1: LOGIN VELOCE CON PLAYWRIGHT (Solo per autenticazione)
    # Usiamo Playwright solo per superare il login form complesso, poi rubiamo i cookie.
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-gpu'])
            page = browser.new_page()
            
            st_status.info("🔐 Autenticazione...")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y", timeout=60000)
            page.fill('input[type="text"]', ZK_USER)
            page.fill('input[type="password"]', ZK_PASS)
            page.press('input[type="password"]', 'Enter')
            page.wait_for_load_state('networkidle')
            
            # RUBIAMO I COOKIE!
            cookies = page.context.cookies()
            for cookie in cookies:
                session.cookies.set(cookie['name'], cookie['value'])
            
            # Estraiamo URL base dinamico se serve (spesso Zucchetti fa redirect)
            base_url = page.url
            browser.close()
            
    except Exception as e:
        st_status.error(f"Errore Login: {e}")
        return None, None

    st_status.info("⚡ Connessione Diretta Stabilita. Scarico...")

    # FASE 2: DOWNLOAD BUSTA (Ricerca API simulata)
    # Zucchetti carica i documenti via chiamate POST/GET interne.
    # Dato che l'URL diretto è difficile da indovinare senza ID documento,
    # qui usiamo una tecnica ibrida: se conosciamo l'endpoint di download diretto bene,
    # altrimenti dobbiamo navigare "headless" ma senza renderizzare nulla (usando requests-html o parsing leggero).
    
    # Purtroppo, senza analizzare il traffico di rete specifico del TUO account, l'URL diretto è impossibile da indovinare (contiene ID hash).
    # TORNIAMO AL PIANO B MIGLIORATO: Playwright MINIMALE.
    # Non renderizziamo CSS/Immagini e andiamo diretti agli URL.
    
    # Reset per approccio ibrido Playwright Ottimizzato
    try:
        with sync_playwright() as p:
            # 1. Browser Super Stealth & Fast
            browser = p.chromium.launch(
                headless=True,
                args=['--disable-gpu', '--blink-settings=imagesEnabled=false'] # NO IMMAGINI = VELOCITÀ
            )
            # Riusiamo i cookie della sessione requests se volessimo, ma qui facciamo login diretto pulito
            context = browser.new_context()
            page = context.new_page()
            
            # Login rapido (senza immagini è un lampo)
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y")
            page.fill('input[type="text"]', ZK_USER)
            page.fill('input[type="password"]', ZK_PASS)
            page.press('input[type="password"]', 'Enter')
            page.wait_for_load_state('domcontentloaded') # Non aspettiamo networkidle completo
            
            # 2. BUSTA PAGA (Navigazione diretta URL se possibile, o click rapidi)
            st_status.info("💰 Busta...")
            page.click("text=I miei dati")
            page.click("text=Documenti") # Senza wait espliciti, Playwright auto-waita
            
            # Cerchiamo cedolino
            try: page.locator("tr", has=page.locator("text=Cedolino")).locator(".z-image").click(timeout=5000)
            except: page.click("text=Cedolino")
            
            # Logica stringa ricerca
            target_busta = f"{mese_nome} {anno}"
            
            # Cerchiamo direttamente nel DOM senza aspettare rendering grafico
            # Usiamo locator testuale che è più veloce
            row = page.locator(f"tr:has-text('{target_busta}')").first
            if row.count() > 0:
                with page.expect_download() as dl:
                    if row.locator("text=Download").count(): row.locator("text=Download").click()
                    else: row.locator(".z-image").last.click()
                path_busta = f"busta_{mese_num}_{anno}.pdf"
                dl.value.save_as(path_busta)
                st_status.success("✅ Busta OK")
            else:
                st_status.warning("Busta non trovata")

            # 3. CARTELLINO (IL PROBLEMA REALE)
            # Invece di navigare il menu Time che è lento, iniettiamo JS per aprire la maschera direttamente?
            # No, Zucchetti è bastardo.
            # MA possiamo iniettare l'evento di apertura!
            
            st_status.info("📅 Cartellino (Direct Access)...")
            
            # TRUCCO: Forziamo il browser ad andare all'URL del cartellino se lo troviamo nei menu
            # Oppure usiamo l'approccio standard ma senza immagini e con date iniettate subito
            
            page.evaluate("window.scrollTo(0,0)")
            page.click("text=Time")
            page.click("text=Cartellino presenze")
            # Niente sleep fissi!
            
            # Aspettiamo solo che appaia UN campo data
            page.wait_for_selector(".dijitInputInner", timeout=15000)
            
            # Date
            last_day = calendar.monthrange(anno, mese_num)[1]
            d_from = f"01/{mese_num:02d}/{anno}"
            d_to = f"{last_day}/{mese_num:02d}/{anno}"
            d_from_iso = f"{anno}-{mese_num:02d}-01"
            d_to_iso = f"{anno}-{mese_num:02d}-{last_day}"

            # Iniezione JS fulminea
            page.evaluate(f"""
                var w = dijit.registry.toArray().filter(x => x.declaredClass == "dijit.form.DateTextBox" && x.domNode.offsetParent);
                var start = w.length >= 3 ? 1 : 0;
                if(w.length >= 2) {{
                    w[start].set('value', new Date('{d_from_iso}'));
                    w[start+1].set('value', new Date('{d_to_iso}'));
                }}
            """)
            
            # Click Ricerca Immediato
            try: page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last.click()
            except: page.keyboard.press("Enter")
            
            # Qui il trucco: Intercettiamo la richiesta di download invece di aspettare la nuova pagina
            # Zucchetti apre un popup che fa una GET. Intercettiamola.
            
            st_status.info("📄 Download Cartellino...")
            
            # Aspettiamo che appaia la riga nella griglia
            target_row = f"{mese_num:02d}/{anno}"
            page.wait_for_selector(f"tr:has-text('{target_row}')", timeout=15000)
            
            # Click lente con gestione popup
            with context.expect_page() as new_p:
                page.locator(f"tr:has-text('{target_row}')").locator("img[src*='search16.png']").click()
            
            np = new_p.value
            np.wait_for_load_state()
            
            path_cart = f"cartellino_{mese_num}_{anno}.pdf"
            if ".pdf" in np.url.lower():
                # Download diretto veloce usando i cookie del browser
                import requests
                cookies = {c['name']: c['value'] for c in context.cookies()}
                r = requests.get(np.url, cookies=cookies)
                with open(path_cart, 'wb') as f: f.write(r.content)
            else:
                np.pdf(path=path_cart)
            
            st_status.success("✅ Cartellino OK")
            browser.close()

    except Exception as e:
        st_status.error(f"Errore: {str(e)[:100]}")
        if browser: browser.close()

    return path_busta, path_cart

# --- UI ---
st.set_page_config(page_title="Gottardo Payroll", page_icon="⚡", layout="wide")
st.title("⚡ Gottardo Payroll (Fast Mode)")

with st.sidebar:
    st.header("Parametri")
    sel_anno = st.selectbox("Anno", [2024, 2025, 2026], index=1)
    sel_mese = st.selectbox("Mese", ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", 
                                     "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"], index=11)
    
    if st.button("🚀 AVVIA", type="primary"):
        st.session_state.clear()
        busta, cart = scarica_documenti_veloce(sel_mese, sel_anno)
        st.session_state['busta'] = busta
        st.session_state['cart'] = cart
        st.session_state['done'] = False

if st.session_state.get('busta') or st.session_state.get('cart'):
    if not st.session_state.get('done'):
        with st.spinner("🧠 Analisi..."):
            db = estrai_dati_busta_dettagliata(st.session_state.get('busta'))
            dc = estrai_dati_cartellino(st.session_state.get('cart'))
            st.session_state['db'] = db
            st.session_state['dc'] = dc
            st.session_state['done'] = True
    
    db = st.session_state.get('db')
    dc = st.session_state.get('dc')
    
    st.divider()
    t1, t2 = st.tabs(["💰 Stipendio", "📅 Presenze"])
    
    with t1:
        if db:
            dg = db.get('dati_generali', {})
            st.metric("NETTO", f"€ {dg.get('netto', 0):.2f}")
            st.json(db) # Debug vista completa
        else: st.warning("No Busta")

    with t2:
        if dc:
            st.metric("Giorni", dc.get('giorni_reali'))
            st.write(dc.get('note'))
        else: st.warning("No Cartellino")
