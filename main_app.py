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

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# --- CONFIGURAZIONE ---
try: locale.setlocale(locale.LC_TIME, 'it_IT.UTF-8')
except: pass 

# --- CREDENZIALI ---
try:
    ZK_USER = st.secrets["ZK_USER"]
    ZK_PASS = st.secrets["ZK_PASS"]
    GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
except:
    # Fallback per test locale
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

# --- CORE: CODICE ORIGINALE ADATTATO ---
def scarica_documenti_automatici(mese_nome, anno):
    nomi_mesi_it = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", 
                    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
    try: mese_num = nomi_mesi_it.index(mese_nome) + 1
    except: return None, None

    target_busta = f"{mese_nome} {anno}" 
    target_cart_row = f"{mese_num:02d}/{anno}" 
    last_day = calendar.monthrange(anno, mese_num)[1]
    
    d_from_vis = f"01/{mese_num:02d}/{anno}"
    d_to_vis = f"{last_day}/{mese_num:02d}/{anno}"
    d_from_srv = f"{anno}-{mese_num:02d}-01"
    d_to_srv = f"{anno}-{mese_num:02d}-{last_day}"
    
    st_status = st.empty()
    st_status.info(f"🤖 Bot Cloud attivo: {mese_nome} {anno}")
    
    path_busta = None
    path_cart = None

    try:
        with sync_playwright() as p:
            # 1. BROWSER (Unica vera differenza col PC: headless=True)
            browser = p.chromium.launch(
                headless=True,
                slow_mo=500, # Un po' di calma aiuta sul cloud
                args=['--disable-gpu', '--no-sandbox', '--disable-dev-shm-usage']
            )
            context = browser.new_context(
                accept_downloads=True,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
            )
            # Timeout generoso per server lenti
            context.set_default_timeout(60000) 
            page = context.new_page()
            # Risoluzione grande per evitare layout mobile
            page.set_viewport_size({"width": 1920, "height": 1080})

            # 2. LOGIN (Classico)
            st_status.info("🔐 Login...")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y")
            page.fill('input[type="text"]', ZK_USER)
            page.fill('input[type="password"]', ZK_PASS)
            page.press('input[type="password"]', 'Enter')
            page.wait_for_load_state('networkidle')

            # 3. BUSTA PAGA (Dal codice originale)
            st_status.info("💰 Busta Paga...")
            try:
                page.click("text=I miei dati")
                page.wait_for_selector("text=Documenti").click()
                
                try: 
                    page.locator("tr", has=page.locator("text=Cedolino")).locator(".z-image").click(timeout=5000)
                except: 
                    page.click("text=Cedolino")
                
                # Attesa che si carichi la lista
                try:
                    page.wait_for_selector(f"tr:has-text('{target_busta}')", timeout=10000)
                except:
                    time.sleep(3)
                
                rows = page.locator(f"tr:has-text('{target_busta}')")
                found = False
                for i in range(rows.count()):
                    txt = rows.nth(i).inner_text()
                    if "Tredicesima" not in txt and "Quattordicesima" not in txt:
                        with page.expect_download(timeout=20000) as dl_info:
                            if rows.nth(i).locator("text=Download").count():
                                rows.nth(i).locator("text=Download").click()
                            else: 
                                rows.nth(i).locator(".z-image").last.click()
                        path_busta = f"busta_{mese_num}_{anno}.pdf"
                        dl_info.value.save_as(path_busta)
                        found = True
                        st_status.success("✅ Busta OK")
                        break
                if not found: st_status.warning("Busta non trovata.")

            except Exception as e:
                st_status.error(f"Err Busta: {e}")

            # 4. CARTELLINO (Dal codice originale, con fix Agenda)
            st_status.info("📅 Cartellino...")
            try:
                page.evaluate("window.scrollTo(0, 0)")
                
                # Gestione Menu Time (Spesso ostico su cloud)
                try:
                    page.click("text=Time", timeout=5000)
                except:
                    # Se il click fallisce, riproviamo o usiamo JS
                    page.evaluate("document.querySelector('span[title=\"Time\"]').click()")
                
                # Aspettiamo il sottomenu
                try:
                    page.wait_for_selector("text=Cartellino presenze", timeout=5000).click()
                except:
                     # Se non appare, forse è "Gestione cartoline"?
                     if page.locator("text=Gestione cartoline").is_visible():
                         page.locator("text=Gestione cartoline").click()
                     else:
                         # Riprova il click su Time
                         page.click("text=Time")
                         page.click("text=Cartellino presenze")
                
                # Attesa caricamento maschera
                time.sleep(5)
                
                # FIX AGENDA: Se siamo finiti sul calendario invece che sulla lista
                if page.locator("text=Permessi del").count() > 0 or page.locator("text=Filtri").count() > 0:
                    st_status.info("Fix Vista Agenda...")
                    # Cerchiamo l'icona stampa o lista per cambiare vista
                    try:
                        # Icona stampa generica Zucchetti
                        page.locator(".z-icon-print").first.click()
                    except:
                        # O cerca "Stampa" nel testo
                        if page.locator("text=Stampa").count() > 0:
                            page.locator("text=Stampa").click()
                
                # Scrittura Date (Originale + Iniezione JS per sicurezza)
                st_status.info("✍️ Ricerca...")
                
                # Tentativo scrittura mista (Robusto)
                try:
                    # Iniezione JS (più affidabile su headless)
                    page.evaluate(f"""
                        var widgets = dijit.registry.toArray().filter(w => w.declaredClass === "dijit.form.DateTextBox" && w.domNode.offsetParent !== null);
                        if (widgets.length >= 2) {{
                            // Spesso l'ordine è: Ricerca, Dal, Al. Quindi indice 1 e 2.
                            var idx = widgets.length >= 3 ? 1 : 0;
                            widgets[idx].set('displayedValue', '{d_from_vis}');
                            widgets[idx].set('value', new Date('{d_from_srv}'));
                            widgets[idx+1].set('displayedValue', '{d_to_vis}');
                            widgets[idx+1].set('value', new Date('{d_to_srv}'));
                        }}
                    """)
                except: pass
                
                time.sleep(2)

                # Click Ricerca
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                try: page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last.click()
                except: page.keyboard.press("Enter")
                
                time.sleep(5)
                
                # Download con gestione Nuova Pagina (Dal codice originale modificato)
                st_status.info("📄 Download...")
                with context.expect_page(timeout=30000) as new_page_info:
                    try:
                        # Cerchiamo la riga
                        row = page.locator(f"tr:has-text('{target_cart_row}')").first
                        if row.count() > 0:
                            row.scroll_into_view_if_needed()
                            # Cerchiamo la lente specifica
                            if row.locator("img[src*='search16.png']").count() > 0:
                                row.locator("img[src*='search16.png']").click()
                            else:
                                # Fallback generico lente
                                row.locator(".z-image").click()
                        else:
                            # Se il filtro non va, clicca la PRIMA lente disponibile
                            st_status.warning("Filtro lento, scarico l'ultimo disponibile...")
                            page.locator("img[src*='search16.png']").first.click()
                    except:
                        # Fallback JS estremo
                        page.evaluate("document.querySelector(\"img[src*='search16.png']\").click()")
                
                # Gestione PDF aperto in nuova scheda
                np = new_page_info.value
                np.wait_for_load_state()
                time.sleep(2)
                
                path_cart = f"cartellino_{mese_num}_{anno}.pdf"
                if ".pdf" in np.url.lower():
                     import requests
                     cs = {c['name']: c['value'] for c in context.cookies()}
                     with open(path_cart, 'wb') as f:
                         f.write(requests.get(np.url, cookies=cs).content)
                else:
                    np.pdf(path=path_cart)
                
                np.close()
                st_status.success("✅ Cartellino OK")

            except Exception as e:
                print(f"Err Cart: {e}")
                st_status.warning(f"Cartellino skip: {str(e)[:50]}")
                # FOTO ERRORE (Se siamo ancora vivi)
                try: 
                    st.image(page.screenshot(), caption="Errore Cartellino", use_container_width=True)
                except: pass

            browser.close()
            
    except Exception as e:
        st_status.error(f"Errore: {e}")
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
