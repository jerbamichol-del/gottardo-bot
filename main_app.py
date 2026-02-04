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

    target_busta = f"{mese_nome} {anno}"
    target_cart_row = f"{mese_num:02d}/{anno}"
    last_day = calendar.monthrange(anno, mese_num)[1]
    
    d_from_vis = f"01/{mese_num:02d}/{anno}"
    d_to_vis = f"{last_day}/{mese_num:02d}/{anno}"
    
    st_status = st.empty()
    st_status.info(f"🤖 Bot Cloud: {mese_nome} {anno}")
    
    path_busta = None
    path_cart = None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                slow_mo=500,
                args=['--disable-gpu', '--no-sandbox', '--disable-dev-shm-usage']
            )
            context = browser.new_context(
                accept_downloads=True, 
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
            )
            context.set_default_timeout(45000)  # Ridotto a 45s
            page = context.new_page()
            page.set_viewport_size({"width": 1920, "height": 1080})

            # LOGIN ROBUSTO
            st_status.info("🔐 Login...")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y", wait_until="domcontentloaded")
            
            # Aspetta che il form sia pronto
            page.wait_for_selector('input[type="text"]', timeout=10000)
            page.fill('input[type="text"]', ZK_USER)
            page.fill('input[type="password"]', ZK_PASS)
            page.press('input[type="password"]', 'Enter')
            
            # ✅ FIX: Aspetta elemento specifico menu invece di networkidle
            try:
                page.wait_for_selector("text=I miei dati", timeout=15000)
                st_status.info("✅ Login OK")
            except:
                st_status.error("Login fallito - Menu non apparso")
                st.image(page.screenshot(), caption="Errore Login")
                browser.close()
                return None, None

            # BUSTA PAGA
            st_status.info("💰 Busta Paga...")
            try:
                page.click("text=I miei dati")
                page.wait_for_selector("text=Documenti", timeout=10000).click()
                
                try: 
                    page.locator("tr", has=page.locator("text=Cedolino")).locator(".z-image").click(timeout=5000)
                except: 
                    page.click("text=Cedolino")
                
                page.wait_for_selector(".dgrid-row", timeout=15000)
                time.sleep(2)
                
                rows = page.locator(f"tr:has-text('{target_busta}')")
                found = False
                for i in range(rows.count()):
                    txt = rows.nth(i).inner_text()
                    if "Tredicesima" not in txt:
                        with page.expect_download(timeout=20000) as dl:
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
            except Exception as e: 
                st_status.error(f"Err Busta: {e}")

            # CARTELLINO
            st_status.info("📅 Cartellino...")
            try:
                page.evaluate("window.scrollTo(0, 0)")
                time.sleep(1)
                
                # Menu Time
                st_status.info("📂 Menu Time...")
                try: 
                    page.click("text=Time", timeout=5000)
                except: 
                    page.evaluate("document.querySelector('span[title=\"Time\"]').click()")
                
                time.sleep(2)
                
                try: 
                    page.wait_for_selector("text=Cartellino presenze", timeout=5000).click()
                except:
                    if page.locator("text=Gestione cartoline").is_visible(): 
                        page.locator("text=Gestione cartoline").click()
                    else: 
                        page.click("text=Time")
                        page.click("text=Cartellino presenze")
                
                time.sleep(5)
                
                # Fix Agenda
                if page.locator("text=Permessi del").count() > 0:
                    st_status.info("🔄 Fix Agenda...")
                    try: 
                        page.locator(".z-icon-print").first.click()
                        time.sleep(3)
                    except: 
                        if page.locator("text=Stampa").count() > 0: 
                            page.locator("text=Stampa").click()
                            time.sleep(3)
                
                # DATE - Triplo tentativo
                st_status.info(f"✍️ Date: {d_from_vis} → {d_to_vis}")
                date_ok = False
                
                # Tentativo 1: Input diretti
                try:
                    dal = page.locator("input[id*='CLRICHIE'][class*='dijitInputInner']").first
                    al = page.locator("input[id*='CLRICHI2'][class*='dijitInputInner']").first
                    
                    if dal.count() > 0 and al.count() > 0:
                        dal.click(force=True)
                        dal.fill("")
                        dal.type(d_from_vis, delay=80)
                        dal.press("Tab")
                        time.sleep(0.5)
                        
                        al.click(force=True)
                        al.fill("")
                        al.type(d_to_vis, delay=80)
                        al.press("Tab")
                        time.sleep(0.5)
                        
                        date_ok = True
                        st_status.info("✅ Date OK (input)")
                except Exception as e:
                    st_status.warning(f"Input date fallito: {str(e)[:50]}")
                
                # Tentativo 2: JS Dojo
                if not date_ok:
                    try:
                        result = page.evaluate(f"""
                            () => {{
                                try {{
                                    var ws = dijit.registry.toArray().filter(w => 
                                        w.declaredClass === "dijit.form.DateTextBox" && 
                                        w.domNode.offsetParent !== null
                                    );
                                    if (ws.length >= 2) {{
                                        var i1 = ws.length >= 3 ? 1 : 0;
                                        ws[i1].set('displayedValue', '{d_from_vis}');
                                        ws[i1+1].set('displayedValue', '{d_to_vis}');
                                        return 'OK';
                                    }}
                                    return 'NO_WIDGETS';
                                }} catch(e) {{
                                    return 'ERROR: ' + e.message;
                                }}
                            }}
                        """)
                        if "OK" in str(result):
                            date_ok = True
                            st_status.info("✅ Date OK (JS)")
                    except Exception as e:
                        st_status.warning(f"JS Dojo fallito: {str(e)[:50]}")
                
                # Screenshot pre-ricerca
                st.image(page.screenshot(), caption=f"Pre-ricerca ({d_from_vis} → {d_to_vis})", use_container_width=True)
                
                # Ricerca
                st_status.info("🔍 Ricerca...")
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(1)
                
                try: 
                    btn = page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last
                    btn.scroll_into_view_if_needed()
                    btn.click(force=True)
                except: 
                    page.keyboard.press("Enter")
                
                time.sleep(5)
                
                # Screenshot post-ricerca
                st.image(page.screenshot(), caption=f"Risultati {mese_nome}", use_container_width=True)
                
                # Download
                st_status.info(f"📄 Download {target_cart_row}...")
                
                with context.expect_page(timeout=30000) as new_page_info:
                    try:
                        row = page.locator(f"tr:has-text('{target_cart_row}')").first
                        row.scroll_into_view_if_needed()
                        row.locator("img[src*='search16.png']").click()
                    except:
                        page.locator("img[src*='search16.png']").first.click()
                
                np = new_page_info.value
                np.wait_for_load_state("domcontentloaded")
                time.sleep(2)
                
                path_cart = f"cartellino_{mese_num}_{anno}.pdf"
                
                if ".pdf" in np.url.lower():
                    cs = {c['name']: c['value'] for c in context.cookies()}
                    with open(path_cart, 'wb') as f:
                        f.write(requests.get(np.url, cookies=cs).content)
                else:
                    np.pdf(path=path_cart)
                
                np.close()
                st_status.success("✅ Cartellino OK")

            except Exception as e:
                st_status.warning(f"Err Cart: {e}")
                try: st.image(page.screenshot(), caption="Errore Cartellino", use_container_width=True)
                except: pass

            browser.close()
            
    except Exception as e:
        st_status.error(f"Errore Gen: {e}")
        return None, None

    return path_busta, path_cart

# --- UI ---
st.set_page_config(page_title="Gottardo Payroll", page_icon="📱", layout="wide")
st.title("📱 Gottardo Payroll Mobile")

with st.sidebar:
    st.header("Parametri")
    sel_anno = st.selectbox("Anno", [2024, 2025, 2026], index=1)
    sel_mese = st.selectbox("Mese", ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", 
                                     "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"], index=11)
    
    if st.button("🚀 AVVIA", type="primary", use_container_width=True):
        st.session_state.clear()
        busta, cart = scarica_documenti_automatici(sel_mese, sel_anno)
        st.session_state['busta'] = busta
        st.session_state['cart'] = cart
        st.session_state['done'] = False

if st.session_state.get('busta') or st.session_state.get('cart'):
    if not st.session_state.get('done'):
        with st.spinner("🧠 AI..."):
            db = estrai_dati_busta_dettagliata(st.session_state.get('busta'))
            dc = estrai_dati_cartellino(st.session_state.get('cart'))
            st.session_state['db'] = db
            st.session_state['dc'] = dc
            st.session_state['done'] = True

    db = st.session_state.get('db')
    dc = st.session_state.get('dc')
    
    st.divider()
    t1, t2 = st.tabs(["💰 Busta", "📅 Cartellino"])
    
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
