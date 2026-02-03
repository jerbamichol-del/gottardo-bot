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
    ZK_USER = "10021450" 
    ZK_PASS = "Diocane3!"
    GOOGLE_API_KEY = "AIzaSyA1OMmdyg-mLrZKO0WFErurf_Q4mfqKKNM"

genai.configure(api_key=GOOGLE_API_KEY)
try: model = genai.GenerativeModel('gemini-flash-latest')
except: model = genai.GenerativeModel('gemini-1.5-flash')

# --- FUNZIONI DI ANALISI ---
def clean_json_response(text):
    try:
        text = re.sub(r"```json|```", "", text).strip()
        start = text.find('{')
        end = text.rfind('}') + 1
        if start != -1 and end != -1: return json.loads(text[start:end])
        return json.loads(text)
    except: return None

def estrai_dati_busta_dettagliata(file_path):
    if not file_path: return None
    try:
        with open(file_path, "rb") as f: bytes_data = f.read()
        prompt = """
        Analizza cedolino. Estrai dati numerici precisi.
        JSON richiesto:
        {
            "dati_generali": {"netto": float, "giorni_pagati": float, "ore_ordinarie": float},
            "competenze": {"base": float, "anzianita": float, "straordinari": float, "festivita": float, "lordo_totale": float},
            "trattenute": {"inps": float, "irpef_netta": float, "addizionali": float},
            "ferie_tfr": {"residue_ap": float, "maturate": float, "godute": float, "saldo": float}
        }
        """
        response = model.generate_content([prompt, {"mime_type": "application/pdf", "data": bytes_data}])
        return clean_json_response(response.text)
    except Exception as e: return None

def estrai_dati_cartellino(file_path):
    if not file_path: return None
    try:
        with open(file_path, "rb") as f: bytes_data = f.read()
        prompt = """Analizza cartellino presenze. JSON: { "giorni_reali": int, "giorni_senza_badge": int, "note": "string" }"""
        response = model.generate_content([prompt, {"mime_type": "application/pdf", "data": bytes_data}])
        return clean_json_response(response.text)
    except: return None

# --- BOT AUTOMAZIONE (CLOUD OPTIMIZED) ---
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
    
    path_busta, path_cart = None, None

    try:
        with sync_playwright() as p:
            # BROWSER LEGGERO PER CLOUD
            browser = p.chromium.launch(
                headless=True,
                slow_mo=300, 
                args=['--disable-gpu', '--disable-dev-shm-usage', '--no-sandbox']
            )
            context = browser.new_context(
                accept_downloads=True,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
            )
            context.set_default_timeout(150000) # 2.5 min timeout
            page = context.new_page()
            page.set_viewport_size({"width": 1920, "height": 1080})

            # 1. LOGIN
            st_status.info("🔐 Login...")
            try:
                page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y", timeout=60000)
                page.wait_for_selector('input[type="text"]', state="visible", timeout=30000)
                page.fill('input[type="text"]', ZK_USER)
                page.fill('input[type="password"]', ZK_PASS)
                page.press('input[type="password"]', 'Enter')
                page.wait_for_load_state('networkidle', timeout=60000)
            except Exception as e:
                st_status.error(f"Errore Login: {str(e)[:100]}")
                browser.close()
                return None, None
            
            # 2. BUSTA PAGA
            st_status.info("💰 Busta Paga...")
            try:
                page.click("text=I miei dati")
                page.wait_for_selector("text=Documenti").click()
                
                try: 
                    ced_lente = page.locator("tr", has=page.locator("text=Cedolino")).locator(".z-image")
                    if ced_lente.count() > 0: ced_lente.click(timeout=8000)
                    else: page.click("text=Cedolino")
                except: page.click("text=Cedolino")
                
                time.sleep(3)
                
                rows = page.locator(f"tr:has-text('{target_busta}')")
                found = False
                for i in range(rows.count()):
                    txt = rows.nth(i).inner_text()
                    if "Tredicesima" not in txt and "Quattordicesima" not in txt:
                        with page.expect_download(timeout=30000) as dl_info:
                            if rows.nth(i).locator("text=Download").count():
                                rows.nth(i).locator("text=Download").click()
                            else: rows.nth(i).locator(".z-image").last.click()
                        path_busta = f"busta_{mese_num}_{anno}.pdf"
                        dl_info.value.save_as(path_busta)
                        found = True
                        st_status.success("✅ Busta OK")
                        break
                if not found: st_status.warning("Busta non trovata.")

            except Exception as e:
                st_status.error(f"Err Busta: {e}")

            # 3. CARTELLINO (STRATEGIA ROBUSTA)
            st_status.info("📅 Cartellino...")
            try:
                page.evaluate("window.scrollTo(0, 0)")
                # Tentativi multipli per aprire il menu
                for _ in range(3):
                    try:
                        page.click("text=Time", timeout=3000)
                        if page.locator("text=Cartellino presenze").is_visible():
                            page.click("text=Cartellino presenze")
                            break
                    except: time.sleep(1)
                else:
                    # Fallback
                    page.click("text=Time")
                    page.wait_for_selector("text=Cartellino presenze", timeout=10000).click()

                time.sleep(6) # Attesa Dojo

                st_status.info("✍️ Ricerca...")
                # Iniezione JS Date
                page.evaluate(f"""
                    () => {{
                        try {{
                            var ws = dijit.registry.toArray().filter(w => w.declaredClass === "dijit.form.DateTextBox" && w.domNode.offsetParent !== null);
                            var idxDal = ws.length >= 3 ? 1 : 0;
                            if(ws.length >= 2) {{
                                ws[idxDal].set('displayedValue', '{d_from_vis}');
                                ws[idxDal].set('value', new Date('{d_from_srv}'));
                                ws[idxDal+1].set('displayedValue', '{d_to_vis}');
                                ws[idxDal+1].set('value', new Date('{d_to_srv}'));
                            }}
                        }} catch(e) {{}}
                    }}
                """)
                time.sleep(1)

                # Click Cerca
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                try: 
                    page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last.click(force=True, timeout=5000)
                except: 
                    page.keyboard.press("Enter")
                
                time.sleep(5)

                st_status.info("📄 Download...")
                with context.expect_page(timeout=30000) as new_page_info:
                    try:
                        # Clicca la riga specifica
                        row = page.locator(f"tr:has-text('{target_cart_row}')").first
                        if row.count() > 0:
                            row.scroll_into_view_if_needed()
                            row.locator("img[src*='search16.png']").click(timeout=5000)
                        else:
                            # Fallback: Clicca la PRIMA lente
                            page.locator("img[src*='search16.png']").first.click(timeout=5000)
                    except:
                        # Fallback JS estremo
                        page.evaluate("document.querySelector(\"img[src*='search16.png']\").click()")
                
                np = new_page_info.value
                np.wait_for_load_state()
                time.sleep(3)
                
                path_cart = f"cartellino_{mese_num}_{anno}.pdf"
                if ".pdf" in np.url.lower():
                     cookies = {c['name']: c['value'] for c in context.cookies()}
                     with open(path_cart, 'wb') as f:
                         f.write(requests.get(np.url, cookies=cookies).content)
                else:
                    np.pdf(path=path_cart)
                np.close()
                st_status.success("✅ Cartellino OK")

            except Exception as e:
                print(f"Err Cart: {e}")
                st_status.warning(f"Cartellino saltato (Timeout/Errore).")

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
            comp = db.get('competenze', {})
            tratt = db.get('trattenute', {})
            ferie = db.get('ferie_tfr', {})
            
            st.metric("NETTO", f"€ {dg.get('netto', 0):.2f}", delta="Mensile")
            
            c1, c2 = st.columns(2)
            c1.write("**+ Entrate**")
            c1.caption(f"Base: {comp.get('base')}")
            c1.caption(f"Straordinari: {comp.get('straordinari')}")
            
            c2.write("**- Uscite**")
            c2.caption(f"Tasse: {tratt.get('irpef_netta')}")
            c2.caption(f"INPS: {tratt.get('inps')}")
            
            st.info(f"🏖️ Ferie Saldo: **{ferie.get('saldo')}**")
        else: st.warning("No Busta")

    with t2:
        if dc:
            st.metric("Giorni Lavorati", dc.get('giorni_reali'))
            st.write(f"Note: {dc.get('note')}")
            
            if db:
                diff = float(dc.get('giorni_reali', 0)) - float(db.get('dati_generali', {}).get('giorni_pagati', 0))
                st.metric("Differenza", f"{diff:.1f}", delta_color="inverse")
        else: st.warning("No Cartellino")
