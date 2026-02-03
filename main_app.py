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

# --- FUNZIONI PARSING AI ---
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

# --- CORE: DOWNLOAD CON SCREENSHOT GARANTITO ---
def scarica_documenti_veloce(mese_nome, anno):
    nomi_mesi_it = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", 
                    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
    try: mese_num = nomi_mesi_it.index(mese_nome) + 1
    except: return None, None

    st_status = st.empty()
    st_status.info("🚀 Avvio Protocollo Ibrido...")
    
    path_busta = None
    path_cart = None
    
    with sync_playwright() as p:
        # SPOSTIAMO IL TRY DENTRO IL WITH, COSÌ IL BROWSER È VIVO SE C'È ERRORE
        try:
            # 1. Browser Super Stealth & Fast
            browser = p.chromium.launch(
                headless=True,
                args=['--disable-gpu', '--blink-settings=imagesEnabled=false', '--no-sandbox', '--disable-dev-shm-usage'] 
            )
            context = browser.new_context()
            page = context.new_page()
            page.set_viewport_size({"width": 1920, "height": 1080})
            
            # LOGIN
            st_status.info("🔐 Login...")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y", timeout=60000)
            page.fill('input[type="text"]', ZK_USER)
            page.fill('input[type="password"]', ZK_PASS)
            page.press('input[type="password"]', 'Enter')
            page.wait_for_load_state('domcontentloaded') 
            
            # 2. BUSTA PAGA
            st_status.info("💰 Busta...")
            try:
                page.click("text=I miei dati")
                page.click("text=Documenti") 
                try: page.locator("tr", has=page.locator("text=Cedolino")).locator(".z-image").click(timeout=5000)
                except: page.click("text=Cedolino")
                
                target_busta = f"{mese_nome} {anno}"
                row = page.locator(f"tr:has-text('{target_busta}')").first
                if row.count() > 0:
                    with page.expect_download(timeout=30000) as dl:
                        if row.locator("text=Download").count(): row.locator("text=Download").click()
                        else: row.locator(".z-image").last.click()
                    path_busta = f"busta_{mese_num}_{anno}.pdf"
                    dl.value.save_as(path_busta)
                    st_status.success("✅ Busta OK")
                else:
                    st_status.warning("Busta non trovata")
            except Exception as e:
                st_status.error(f"Err Busta: {e}")

            # 3. CARTELLINO (DEBUG MODE)
            st_status.info("📅 Cartellino...")
            
            page.evaluate("window.scrollTo(0,0)")
            
            # Menu
            menu_ok = False
            try:
                page.locator("text=Time").click(timeout=5000)
                if page.locator("text=Cartellino presenze").is_visible():
                    page.locator("text=Cartellino presenze").click()
                    menu_ok = True
            except: pass
            
            if not menu_ok:
                st_status.warning("Menu Time difficile, provo forza bruta...")
                page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y") # Ricarica soft
                page.wait_for_load_state('domcontentloaded')
                page.locator("text=Time").click(force=True)
                page.locator("text=Cartellino presenze").click(force=True)

            st_status.info("✍️ Ricerca...")
            page.wait_for_selector(".dijitInputInner", timeout=20000)
            
            # Date Injection
            last_day = calendar.monthrange(anno, mese_num)[1]
            d_from_iso = f"{anno}-{mese_num:02d}-01"
            d_to_iso = f"{anno}-{mese_num:02d}-{last_day}"

            page.evaluate(f"""
                var w = dijit.registry.toArray().filter(x => x.declaredClass == "dijit.form.DateTextBox" && x.domNode.offsetParent);
                var start = w.length >= 3 ? 1 : 0;
                if(w.length >= 2) {{
                    w[start].set('value', new Date('{d_from_iso}'));
                    w[start+1].set('value', new Date('{d_to_iso}'));
                }}
            """)
            
            # Click Ricerca
            try: page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last.click()
            except: page.keyboard.press("Enter")
            
            st_status.info("📄 Download (Race Condition)...")
            time.sleep(5) 
            
            # TRUCCO DOPPIO: Aspetta O il download O la pagina
            # Clicchiamo la lente via JS
            page.evaluate("document.querySelectorAll('img[src*=\"search16.png\"]')[0].click()")

            # Ora aspettiamo: o parte un download, o si apre una pagina
            try:
                # Definiamo due eventi
                with context.expect_page(timeout=10000) as p_info:
                    # Riprova click se il primo non è andato
                    page.evaluate("document.querySelectorAll('img[src*=\"search16.png\"]')[0].click()")
                
                # Se siamo qui, si è aperta una pagina
                np = p_info.value
                np.wait_for_load_state()
                path_cart = f"cartellino_{mese_num}_{anno}.pdf"
                if ".pdf" in np.url.lower():
                    cookies = {c['name']: c['value'] for c in context.cookies()}
                    r = requests.get(np.url, cookies=cookies)
                    with open(path_cart, 'wb') as f: f.write(r.content)
                else:
                    np.pdf(path=path_cart)
            
            except:
                # Se expect_page fallisce (timeout), forse è partito un download diretto?
                st_status.warning("Nessuna nuova pagina, controllo download...")
                # In realtà Zucchetti su download diretto è difficile da intercettare post-click se non hai wrappato l'evento.
                # Ma spesso il timeout è perché la pagina ci mette >10s.
                # Diamo per perso se fallisce qui.
                raise Exception("Popup/Download fallito")
            
            st_status.success("✅ Cartellino OK")

        except Exception as e:
            st_status.error(f"❌ ERRORE: {str(e)[:100]}")
            # FOTO DEBUG GARANTITA PERCHÉ SIAMO ANCORA NEL CONTEXT
            try:
                st.warning("📸 SCATTO FOTO ERRORE...")
                st.image(page.screenshot(), caption="Schermata al momento dell'errore", use_container_width=True)
            except Exception as img_err:
                st.error(f"Impossibile scattare foto: {img_err}")
    
    return path_busta, path_cart

# --- UI ---
st.set_page_config(page_title="Gottardo Payroll", page_icon="⚡", layout="wide")
st.title("⚡ Gottardo Payroll (Photo Debug)")

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
            st.json(db) 
        else: st.warning("No Busta")

    with t2:
        if dc:
            st.metric("Giorni", dc.get('giorni_reali'))
            st.write(dc.get('note'))
        else: st.warning("No Cartellino")
