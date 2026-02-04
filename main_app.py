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

# --- SETUP CLOUD ---
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

# --- PARSING ---
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

# --- CORE ---
def scarica_documenti_automatici(mese_nome, anno):
    nomi_mesi_it = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", 
                    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
    try: mese_num = nomi_mesi_it.index(mese_nome) + 1
    except: return None, None

    # Target: Es. "Dicembre 2025"
    target_text = f"{mese_nome} {anno}"
    
    st_status = st.empty()
    st_status.info(f"🤖 Bot Cloud: {target_text}")
    
    path_busta = None
    path_cart = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                slow_mo=1000, # Rallentiamo per dare tempo a Zucchetti
                args=['--disable-gpu', '--no-sandbox', '--disable-dev-shm-usage']
            )
            context = browser.new_context(accept_downloads=True, user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36")
            context.set_default_timeout(60000)
            page = context.new_page()
            page.set_viewport_size({"width": 1920, "height": 1080})

            # LOGIN
            st_status.info("🔐 Login...")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y")
            page.fill('input[type="text"]', ZK_USER)
            page.fill('input[type="password"]', ZK_PASS)
            page.press('input[type="password"]', 'Enter')
            page.wait_for_load_state('networkidle')

            # BUSTA PAGA
            st_status.info("💰 Busta Paga...")
            try:
                page.click("text=I miei dati")
                page.wait_for_selector("text=Documenti").click()
                try: page.locator("tr", has=page.locator("text=Cedolino")).locator(".z-image").click(timeout=5000)
                except: page.click("text=Cedolino")
                
                # Attesa tabella
                page.wait_for_selector(".dgrid-row", timeout=15000)
                
                # Cerca riga giusta (Escludendo Tredicesima se non richiesta)
                rows = page.locator(f"tr:has-text('{target_text}')")
                found = False
                for i in range(rows.count()):
                    txt = rows.nth(i).inner_text()
                    if "Tredicesima" not in txt:
                        with page.expect_download(timeout=30000) as dl:
                            # Prova vari selettori di download
                            if rows.nth(i).locator("text=Download").count(): 
                                rows.nth(i).locator("text=Download").click()
                            else: 
                                rows.nth(i).locator(".z-image").last.click()
                        path_busta = f"busta_{mese_num}_{anno}.pdf"
                        dl.value.save_as(path_busta)
                        found = True
                        st_status.success("✅ Busta OK")
                        break
                if not found: st_status.warning("Busta non trovata")
            except Exception as e: st_status.error(f"Err Busta: {e}")

            # CARTELLINO
            st_status.info("📅 Cartellino...")
            try:
                page.evaluate("window.scrollTo(0, 0)")
                # Navigazione Menu
                try: page.click("text=Time", timeout=5000)
                except: page.evaluate("document.querySelector('span[title=\"Time\"]').click()")
                
                try: page.wait_for_selector("text=Cartellino presenze", timeout=5000).click()
                except:
                     if page.locator("text=Gestione cartoline").is_visible(): page.locator("text=Gestione cartoline").click()
                     else: page.click("text=Time"); page.click("text=Cartellino presenze")
                
                time.sleep(5)
                
                # FIX AGENDA
                if page.locator("text=Permessi del").count() > 0 or page.locator("text=Filtri").count() > 0:
                    st_status.info("Fix Vista Agenda...")
                    try: page.locator(".z-icon-print").first.click()
                    except: 
                        if page.locator("text=Stampa").count() > 0: page.locator("text=Stampa").click()
                
                st_status.info("✍️ Ricerca...")
                
                # SKIP DATA ENTRY (La tabella c'è già!)
                # Clicchiamo solo Esegui ricerca per refreshare, se possibile
                try: 
                    page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last.click()
                    time.sleep(3)
                except: pass
                
                st_status.info(f"📄 Cerco riga '{target_text}'...")
                
                # Strategia Selettiva: Cerchiamo la riga che ha ESATTAMENTE "Dicembre 2025" nella colonna Mensilità
                # Per evitare la Tredicesima.
                
                # Prendiamo tutte le righe che contengono "Dicembre 2025"
                rows = page.locator(f"tr:has-text('{target_text}')")
                
                target_row = None
                
                for i in range(rows.count()):
                    r_text = rows.nth(i).inner_text()
                    # Logica: Se cerchiamo Dicembre, vogliamo evitare "Tredicesima"
                    # Se il mese non è Dicembre, la Tredicesima non dovrebbe esserci comunque
                    if "Tredicesima" not in r_text:
                        target_row = rows.nth(i)
                        break
                
                # Se non abbiamo trovato una riga "pulita", prendiamo la prima che capita col mese
                if not target_row and rows.count() > 0:
                    target_row = rows.first
                
                if target_row:
                    with context.expect_page(timeout=30000) as new_page_info:
                        target_row.scroll_into_view_if_needed()
                        # Clicchiamo "Download"
                        if target_row.locator("text=Download").count() > 0:
                            target_row.locator("text=Download").click()
                        elif target_row.locator(".z-image").count() > 0:
                             target_row.locator(".z-image").last.click() # Spesso l'ultima icona è il download
                        else:
                             # Click generico sulla riga
                             target_row.click()

                    # Gestione PDF
                    np = new_page_info.value
                    np.wait_for_load_state()
                    path_cart = f"cartellino_{mese_num}_{anno}.pdf"
                    
                    if ".pdf" in np.url.lower():
                         import requests
                         cs = {c['name']: c['value'] for c in context.cookies()}
                         with open(path_cart, 'wb') as f: f.write(requests.get(np.url, cookies=cs).content)
                    else:
                        np.pdf(path=path_cart)
                    
                    np.close()
                    st_status.success("✅ Cartellino OK")
                else:
                    st_status.error("Riga non trovata in tabella.")
                    st.image(page.screenshot(), caption="Tabella senza mese cercato", use_container_width=True)

            except Exception as e:
                st_status.warning(f"Errore Cart: {e}")
                try: st.image(page.screenshot(), caption="Errore Finale", use_container_width=True)
                except: pass

            browser.close()
            
    except Exception as e:
        st_status.error(f"Errore Gen: {e}")
        return None, None

    return path_busta, path_cart

# --- UI STREAMLIT ---
st.set_page_config(page_title="Gottardo Payroll", page_icon="📱", layout="wide")
st.title("📱 Gottardo Payroll Mobile")

with st.sidebar:
    st.header("Parametri")
    sel_anno = st.selectbox("Anno", [2024, 2025, 2026], index=1)
    sel_mese = st.selectbox("Mese", ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", 
                                     "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"], index=11)
    
    if st.button("🚀 AVVIA ANALISI", type="primary", use_container_width=True):
        st.session_state.clear()
        busta, cart = scarica_documenti_automatici(sel_mese, sel_anno)
        st.session_state['busta'] = busta
        st.session_state['cart'] = cart
        st.session_state['done'] = False

if st.session_state.get('busta') or st.session_state.get('cart'):
    if not st.session_state.get('done'):
        with st.spinner("🧠 Analisi AI..."):
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
