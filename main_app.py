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
from pathlib import Path

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
except Exception as e:
    st.error(f"❌ Secrets mancanti: {e}")
    st.stop()

try:
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')
except Exception as e:
    st.error(f"❌ Errore configurazione Google AI: {e}")
    st.stop()

# --- PARSING ---
def clean_json_response(text):
    try:
        text = re.sub(r"```json|```", "", text).strip()
        start = text.find('{')
        end = text.rfind('}') + 1
        return json.loads(text[start:end]) if start != -1 else json.loads(text)
    except: 
        return None

def estrai_dati_busta_dettagliata(file_path):
    if not file_path:
        st.warning("⚠️ Path busta vuoto")
        return None
    if not os.path.exists(file_path):
        st.warning(f"⚠️ File busta non esiste: {file_path}")
        return None
    
    try:
        file_size = os.path.getsize(file_path)
        st.info(f"📄 Busta: {file_size} bytes")
        
        with open(file_path, "rb") as f: 
            bytes_data = f.read()
        
        prompt = """Analizza cedolino. JSON: {"dati_generali": {"netto": float, "giorni_pagati": float}, "competenze": {"base": float, "straordinari": float}, "trattenute": {"inps": float, "irpef_netta": float}, "ferie_tfr": {"saldo": float}}"""
        response = model.generate_content([prompt, {"mime_type": "application/pdf", "data": bytes_data}])
        return clean_json_response(response.text)
    except Exception as e:
        st.error(f"❌ Errore estrazione busta: {e}")
        return None

def estrai_dati_cartellino(file_path):
    if not file_path:
        st.warning("⚠️ Path cartellino vuoto")
        return None
    if not os.path.exists(file_path):
        st.warning(f"⚠️ File cartellino non esiste: {file_path}")
        return None
    
    try:
        file_size = os.path.getsize(file_path)
        st.info(f"📄 Cartellino: {file_size} bytes")
        
        with open(file_path, "rb") as f: 
            bytes_data = f.read()
        
        prompt = """Analizza cartellino. JSON: { "giorni_reali": int, "note": "string" }"""
        response = model.generate_content([prompt, {"mime_type": "application/pdf", "data": bytes_data}])
        return clean_json_response(response.text)
    except Exception as e:
        st.error(f"❌ Errore estrazione cartellino: {e}")
        return None

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
    
    # Path assoluti
    work_dir = Path.cwd()
    path_busta = str(work_dir / f"busta_{mese_num}_{anno}.pdf")
    path_cart = str(work_dir / f"cartellino_{mese_num}_{anno}.pdf")
    
    st_status = st.empty()
    st_status.info(f"🤖 Bot: {mese_nome} {anno}")
    
    busta_ok = False
    cart_ok = False

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
            context.set_default_timeout(30000)
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
            st_status.info("💰 Busta...")
            try:
                page.click("text=I miei dati")
                page.wait_for_selector("text=Documenti", timeout=10000).click()
                time.sleep(2)
                
                # Apri Cedolino
                try: 
                    page.locator("tr", has=page.locator("text=Cedolino")).locator(".z-image").click(timeout=5000)
                except: 
                    page.click("text=Cedolino")
                
                page.wait_for_selector(".dgrid-row", timeout=15000)
                time.sleep(3)
                
                st.image(page.screenshot(), caption="Sezione Cedolini", use_container_width=True)
                
                # Cerca righe
                st_status.info(f"🔍 Cerco: '{target_busta}'")
                
                # Prova diversi pattern
                patterns = [
                    f"{mese_nome} {anno}",
                    f"{mese_nome.upper()} {anno}",
                    f"{mese_num:02d}/{anno}",
                ]
                
                rows = None
                for pattern in patterns:
                    rows = page.locator(f"tr:has-text('{pattern}')")
                    count = rows.count()
                    st_status.info(f"Pattern '{pattern}': {count} righe")
                    if count > 0:
                        break
                
                if rows and rows.count() > 0:
                    for i in range(rows.count()):
                        txt = rows.nth(i).inner_text()
                        st_status.info(f"Riga {i}: {txt[:80]}")
                        
                        if "Tredicesima" not in txt and "13" not in txt:
                            try:
                                with page.expect_download(timeout=20000) as dl:
                                    # Prova tutti i possibili click
                                    if rows.nth(i).locator("text=Download").count(): 
                                        rows.nth(i).locator("text=Download").click()
                                        st_status.info("Click Download")
                                    elif rows.nth(i).locator(".z-image").count():
                                        rows.nth(i).locator(".z-image").last.click()
                                        st_status.info("Click z-image")
                                    else:
                                        rows.nth(i).click()
                                        st_status.info("Click riga")
                                
                                dl.value.save_as(path_busta)
                                
                                if os.path.exists(path_busta):
                                    size = os.path.getsize(path_busta)
                                    busta_ok = True
                                    st_status.success(f"✅ Busta: {size} bytes")
                                break
                            except Exception as e:
                                st_status.warning(f"Download riga {i} fallito: {str(e)[:50]}")
                else:
                    st_status.error(f"❌ Nessuna riga trovata per '{target_busta}'")
                    
                if not busta_ok: 
                    st_status.warning("⚠️ Busta non scaricata")
                    
            except Exception as e: 
                st_status.error(f"Err Busta: {e}")

            # CARTELLINO (uguale a prima)
            st_status.info("📅 Cartellino...")
            try:
                page.evaluate("window.scrollTo(0, 0)")
                time.sleep(2)
                try: page.keyboard.press("Escape"); time.sleep(1)
                except: pass
                
                try:
                    logo = page.locator("img[src*='logo'], .logo").first
                    if logo.is_visible(timeout=2000): logo.click(); time.sleep(2)
                except:
                    page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2", wait_until="domcontentloaded")
                    time.sleep(3)
                
                # TIME
                time_clicked = False
                try:
                    result = page.evaluate("""() => {
                        const e = document.getElementById('revit_navigation_NavHoverItem_2_label');
                        if (e) { e.click(); return 'OK'; }
                        return 'NOT_FOUND';
                    }""")
                    if result == 'OK': time_clicked = True
                except: pass
                
                if not time_clicked:
                    try: page.locator("#revit_navigation_NavHoverItem_2_label").click(timeout=3000); time_clicked = True
                    except: pass
                
                if not time_clicked:
                    try: page.locator("text=Time").first.click(timeout=3000); time_clicked = True
                    except: pass
                
                if not time_clicked: raise Exception("Menu Time non trovato")
                time.sleep(3)
                
                # CARTELLINO
                cart_opened = False
                try:
                    page.wait_for_selector("#lnktab_5_label", state="visible", timeout=10000)
                    result = page.evaluate("""() => {
                        const e = document.getElementById('lnktab_5_label');
                        if (e) { e.click(); return 'OK'; }
                        return 'NOT_FOUND';
                    }""")
                    if result == 'OK': cart_opened = True
                except: pass
                
                if not cart_opened:
                    try: page.locator("#lnktab_5_label").click(force=True, timeout=5000); cart_opened = True
                    except: pass
                
                if not cart_opened:
                    try:
                        result = page.evaluate("""() => {
                            const spans = document.querySelectorAll('span.dijitButtonText');
                            for (let s of spans) {
                                if (s.textContent.trim().includes('Cartellino presenze')) {
                                    s.click(); return 'OK';
                                }
                            }
                            return 'NOT_FOUND';
                        }""")
                        if result == 'OK': cart_opened = True
                    except: pass
                
                if not cart_opened: raise Exception("Cartellino non accessibile")
                time.sleep(5)
                
                if page.locator("text=Permessi del").count() > 0:
                    try: page.locator(".z-icon-print").first.click(); time.sleep(3)
                    except: pass
                
                # DATE
                try:
                    dal = page.locator("input[id*='CLRICHIE'][class*='dijitInputInner']").first
                    al = page.locator("input[id*='CLRICHI2'][class*='dijitInputInner']").first
                    
                    if dal.count() > 0 and al.count() > 0:
                        dal.click(force=True); page.keyboard.press("Control+A"); dal.fill(""); time.sleep(0.3)
                        dal.type(d_from_vis, delay=100); dal.press("Tab"); time.sleep(1)
                        
                        al.click(force=True); page.keyboard.press("Control+A"); al.fill(""); time.sleep(0.3)
                        al.type(d_to_vis, delay=100); al.press("Tab"); time.sleep(1)
                except: pass
                
                # Ricerca
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)"); time.sleep(1)
                try: 
                    btn = page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last
                    btn.scroll_into_view_if_needed(); btn.click(force=True)
                except: page.keyboard.press("Enter")
                time.sleep(5)
                
                # Download
                old_url = page.url
                try:
                    row = page.locator(f"tr:has-text('{target_cart_row}')").first
                    row.scroll_into_view_if_needed()
                    row.locator("img[src*='search16.png']").click()
                except:
                    page.locator("img[src*='search16.png']").first.click()
                
                try:
                    page.wait_for_selector("text=Caricamento in corso", state="attached", timeout=5000)
                    page.wait_for_selector("text=Caricamento in corso", state="hidden", timeout=30000)
                except: time.sleep(3)
                
                time.sleep(2)
                new_url = page.url
                
                if new_url != old_url:
                    try:
                        cs = {c['name']: c['value'] for c in context.cookies()}
                        response = requests.get(new_url, cookies=cs, timeout=30)
                        if b'%PDF' in response.content[:10]:
                            with open(path_cart, 'wb') as f: f.write(response.content)
                        else:
                            page.pdf(path=path_cart)
                    except: page.pdf(path=path_cart)
                else:
                    page.pdf(path=path_cart)
                
                if os.path.exists(path_cart):
                    size = os.path.getsize(path_cart)
                    cart_ok = True
                    st_status.success(f"✅ Cartellino: {size} bytes")

            except Exception as e:
                st_status.error(f"Err Cart: {e}")

            browser.close()
            
    except Exception as e:
        st_status.error(f"Errore Gen: {e}")
    
    final_busta = path_busta if busta_ok and os.path.exists(path_busta) else None
    final_cart = path_cart if cart_ok and os.path.exists(path_cart) else None
    
    st.success(f"📦 Ritorno → Busta: {final_busta}, Cart: {final_cart}")
    
    return final_busta, final_cart

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
