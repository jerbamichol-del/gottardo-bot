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
            context.set_default_timeout(45000)
            page = context.new_page()
            page.set_viewport_size({"width": 1920, "height": 1080})

            # LOGIN
            st_status.info("🔐 Login...")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y", wait_until="domcontentloaded")
            page.wait_for_selector('input[type="text"]', timeout=10000)
            page.fill('input[type="text"]', ZK_USER)
            page.fill('input[type="password"]', ZK_PASS)
            page.press('input[type="password"]', 'Enter')
            page.wait_for_selector("text=I miei dati", timeout=15000)
            st_status.info("✅ Login OK")

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
                
                st.image(page.screenshot(), caption="Pre Menu Time", use_container_width=True)
                
                # MENU TIME - Con hover
                st_status.info("📂 Apro Time...")
                menu_opened = False
                
                # Tentativo con hover + click
                try:
                    time_menu = page.locator("text=Time").first
                    time_menu.hover()  # ✅ HOVER prima di cliccare
                    time.sleep(1)  # Aspetta espansione menu
                    time_menu.click()
                    menu_opened = True
                    st_status.info("✅ Time aperto")
                except Exception as e:
                    st_status.warning(f"Time fallito: {str(e)[:50]}")
                
                if not menu_opened:
                    try:
                        time_span = page.locator('span[title="Time"]').first
                        time_span.hover()
                        time.sleep(1)
                        time_span.click()
                        menu_opened = True
                        st_status.info("✅ Time aperto (span)")
                    except: pass
                
                if not menu_opened:
                    st_status.error("❌ Menu Time non trovato!")
                    st.image(page.screenshot(), caption="Menu Time non trovato", use_container_width=True)
                    raise Exception("Menu Time non accessibile")
                
                # ✅ ATTESA ESPLICITA per sottomenu
                time.sleep(3)
                
                st.image(page.screenshot(), caption="Post Menu Time (sottomenu aperto?)", use_container_width=True)
                
                # CARTELLINO PRESENZE - Tentativi multipli
                st_status.info("📋 Cerco Cartellino presenze...")
                cart_opened = False
                
                # Tentativo 1: Link diretto con wait
                try:
                    cart_link = page.wait_for_selector("text=Cartellino presenze", timeout=5000)
                    cart_link.click()
                    cart_opened = True
                    st_status.info("✅ Cartellino aperto (text)")
                except Exception as e:
                    st_status.warning(f"Text Cartellino fallito: {str(e)[:50]}")
                
                # Tentativo 2: Span/a con testo
                if not cart_opened:
                    try:
                        cart_locators = page.locator("*:has-text('Cartellino presenze')").all()
                        for loc in cart_locators:
                            if loc.is_visible():
                                loc.click()
                                cart_opened = True
                                st_status.info("✅ Cartellino aperto (generic)")
                                break
                    except Exception as e:
                        st_status.warning(f"Generic Cartellino fallito: {str(e)[:50]}")
                
                # Tentativo 3: Gestione cartoline (alternativa)
                if not cart_opened:
                    try:
                        alt = page.locator("text=Gestione cartoline").first
                        if alt.is_visible(timeout=2000):
                            alt.click()
                            cart_opened = True
                            st_status.info("✅ Gestione cartoline aperta")
                    except: pass
                
                # Tentativo 4: Click su qualsiasi voce visibile del menu Time
                if not cart_opened:
                    st_status.warning("🔧 Tentativo click generico su menu...")
                    try:
                        # Cerca tutti i link/span visibili dopo aver aperto Time
                        menu_items = page.locator("span.z-menu-item-text, a:visible").all()
                        for item in menu_items:
                            text = item.inner_text().lower()
                            if "cartellino" in text or "presenze" in text:
                                item.click()
                                cart_opened = True
                                st_status.info(f"✅ Cliccato: {text}")
                                break
                    except Exception as e:
                        st_status.warning(f"Menu items fallito: {str(e)[:50]}")
                
                if not cart_opened:
                    st_status.error("❌ Cartellino presenze NON trovato!")
                    st.image(page.screenshot(), caption="Cartellino non trovato - Menu completo", use_container_width=True)
                    raise Exception("Voce Cartellino non accessibile")
                
                time.sleep(5)
                
                st.image(page.screenshot(), caption="Pagina Cartellini", use_container_width=True)
                
                # Fix Agenda
                if page.locator("text=Permessi del").count() > 0:
                    st_status.info("🔄 Fix Agenda...")
                    try: 
                        page.locator(".z-icon-print").first.click()
                        time.sleep(3)
                    except: pass
                
                # DATE
                st_status.info(f"✍️ Date: {d_from_vis} → {d_to_vis}")
                
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
                        st_status.info("✅ Date impostate")
                except Exception as e:
                    st_status.warning(f"Date fallite: {str(e)[:50]}")
                    try:
                        page.evaluate(f"""
                            () => {{
                                var ws = dijit.registry.toArray().filter(w => 
                                    w.declaredClass === "dijit.form.DateTextBox" && 
                                    w.domNode.offsetParent !== null
                                );
                                if (ws.length >= 2) {{
                                    var i1 = ws.length >= 3 ? 1 : 0;
                                    ws[i1].set('displayedValue', '{d_from_vis}');
                                    ws[i1+1].set('displayedValue', '{d_to_vis}');
                                }}
                            }}
                        """)
                        st_status.info("✅ Date JS OK")
                    except: pass
                
                st.image(page.screenshot(), caption=f"Pre-ricerca {d_from_vis}→{d_to_vis}", use_container_width=True)
                
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
                try: st.image(page.screenshot(), caption="Errore Cart Finale", use_container_width=True)
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
