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

genai.configure(api_key=GOOGLE_API_KEY)
try: 
    model = genai.GenerativeModel('gemini-flash-latest')
except: 
    model = genai.GenerativeModel('gemini-1.5-flash')

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
    if not file_path or not os.path.exists(file_path):
        return None
    
    try:
        with open(file_path, "rb") as f: 
            bytes_data = f.read()
        
        prompt = """Analizza cedolino. JSON: {"dati_generali": {"netto": float, "giorni_pagati": float}, "competenze": {"base": float, "straordinari": float}, "trattenute": {"inps": float, "irpef_netta": float}, "ferie_tfr": {"saldo": float}}"""
        response = model.generate_content([prompt, {"mime_type": "application/pdf", "data": bytes_data}])
        return clean_json_response(response.text)
    except Exception as e:
        st.error(f"❌ Err busta AI: {e}")
        return None

def estrai_dati_cartellino(file_path):
    if not file_path or not os.path.exists(file_path):
        return None
    
    try:
        with open(file_path, "rb") as f: 
            bytes_data = f.read()
        
        prompt = """Analizza cartellino. JSON: { "giorni_reali": int, "note": "string" }"""
        response = model.generate_content([prompt, {"mime_type": "application/pdf", "data": bytes_data}])
        return clean_json_response(response.text)
    except Exception as e:
        st.error(f"❌ Err cart AI: {e}")
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
            context.set_default_timeout(45000)  # ✅ 45s invece di 30s
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
                time.sleep(3)
                
                st.image(page.screenshot(), caption="Dopo click Documenti", use_container_width=True)
                
                # Apri Cedolino
                st_status.info("📂 Apro Cedolino...")
                try: 
                    page.locator("tr", has=page.locator("text=Cedolino")).locator(".z-image").click(timeout=5000)
                    st_status.info("✅ Click z-image")
                except: 
                    page.click("text=Cedolino")
                    st_status.info("✅ Click text")
                
                time.sleep(5)  # ✅ Attesa più lunga
                
                st.image(page.screenshot(), caption="Dopo apertura Cedolino", use_container_width=True)
                
                # ✅ Prova selettori alternativi
                st_status.info("⏳ Attendo griglia...")
                grid_ready = False
                
                selectors = [
                    ".dgrid-row",
                    "tr.dgrid-row",
                    ".dgrid-content tr",
                    "table tr",
                    "[role='row']"
                ]
                
                for selector in selectors:
                    try:
                        page.wait_for_selector(selector, timeout=10000)
                        st_status.info(f"✅ Griglia trovata: {selector}")
                        grid_ready = True
                        break
                    except:
                        st.warning(f"⏭️ {selector} non trovato")
                
                if not grid_ready:
                    st.error("❌ Nessuna griglia trovata!")
                    st.image(page.screenshot(), caption="Griglia non trovata", use_container_width=True)
                    raise Exception("Griglia cedolini non caricata")
                
                time.sleep(2)
                
                # Cerca righe
                st_status.info(f"🔍 Cerco: '{target_busta}'")
                
                patterns = [
                    target_busta,
                    target_busta.upper(),
                    f"{mese_num:02d}/{anno}",
                    str(anno),
                ]
                
                found_rows = None
                found_pattern = None
                
                for pattern in patterns:
                    # ✅ Prova diversi selettori di riga
                    for row_selector in ["tr.dgrid-row", "tr", "[role='row']"]:
                        rows = page.locator(f"{row_selector}:has-text('{pattern}')")
                        count = rows.count()
                        if count > 0:
                            st.info(f"✅ '{pattern}' → {count} righe ({row_selector})")
                            found_rows = rows
                            found_pattern = pattern
                            break
                    if found_rows:
                        break
                
                if found_rows and found_rows.count() > 0:
                    st.success(f"✅ {found_rows.count()} righe con '{found_pattern}'")
                    
                    for i in range(found_rows.count()):
                        txt = found_rows.nth(i).inner_text()
                        st.info(f"📝 Riga {i}: {txt[:100]}")
                        
                        if any(x in txt.lower() for x in ["tredicesima", "13", "quattordicesima", "14"]):
                            st.warning(f"⏭️ Skip riga {i}")
                            continue
                        
                        st.info(f"✅ Tento download riga {i}...")
                        try:
                            with page.expect_download(timeout=20000) as dl:
                                if found_rows.nth(i).locator("text=Download").count(): 
                                    found_rows.nth(i).locator("text=Download").click()
                                elif found_rows.nth(i).locator(".z-image").count():
                                    found_rows.nth(i).locator(".z-image").last.click()
                                elif found_rows.nth(i).locator("img").count():
                                    found_rows.nth(i).locator("img").last.click()
                                else:
                                    found_rows.nth(i).click()
                            
                            dl.value.save_as(path_busta)
                            
                            if os.path.exists(path_busta):
                                size = os.path.getsize(path_busta)
                                busta_ok = True
                                st_status.success(f"✅ Busta: {size} bytes")
                            break
                        except Exception as e:
                            st.warning(f"⚠️ Riga {i} fallita: {str(e)[:80]}")
                else:
                    st.error(f"❌ NESSUNA riga trovata!")
                    
                    # Mostra tutto
                    for row_selector in ["tr.dgrid-row", "tr", "[role='row']"]:
                        all_rows = page.locator(row_selector)
                        count = all_rows.count()
                        if count > 0:
                            st.warning(f"📊 {count} righe totali ({row_selector})")
                            for i in range(min(count, 3)):
                                st.info(f"{i}: {all_rows.nth(i).inner_text()[:100]}")
                            break
                    
                if not busta_ok: 
                    st.warning("⚠️ Busta non scaricata")
                    
            except Exception as e: 
                st.error(f"Err Busta: {e}")
                try:
                    st.image(page.screenshot(), caption="Errore Busta", use_container_width=True)
                except: pass

            # CARTELLINO (invariato)
            st_status.info("📅 Cartellino...")
            try:
                page.evaluate("window.scrollTo(0, 0)"); time.sleep(2)
                try: page.keyboard.press("Escape"); time.sleep(1)
                except: pass
                
                try:
                    logo = page.locator("img[src*='logo'], .logo").first
                    if logo.is_visible(timeout=2000): logo.click(); time.sleep(2)
                except:
                    page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2", wait_until="domcontentloaded")
                    time.sleep(3)
                
                page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
                time.sleep(3)
                page.evaluate("document.getElementById('lnktab_5_label')?.click()")
                time.sleep(5)
                
                if page.locator("text=Permessi del").count() > 0:
                    try: page.locator(".z-icon-print").first.click(); time.sleep(3)
                    except: pass
                
                try:
                    dal = page.locator("input[id*='CLRICHIE'][class*='dijitInputInner']").first
                    al = page.locator("input[id*='CLRICHI2'][class*='dijitInputInner']").first
                    
                    if dal.count() > 0 and al.count() > 0:
                        dal.click(force=True); page.keyboard.press("Control+A"); dal.fill("")
                        dal.type(d_from_vis, delay=100); dal.press("Tab"); time.sleep(1)
                        al.click(force=True); page.keyboard.press("Control+A"); al.fill("")
                        al.type(d_to_vis, delay=100); al.press("Tab"); time.sleep(1)
                except: pass
                
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)"); time.sleep(1)
                try: 
                    page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last.click(force=True)
                except: page.keyboard.press("Enter")
                time.sleep(5)
                
                old_url = page.url
                try:
                    page.locator(f"tr:has-text('{target_cart_row}')").first.locator("img[src*='search16.png']").click()
                except:
                    page.locator("img[src*='search16.png']").first.click()
                
                try:
                    page.wait_for_selector("text=Caricamento in corso", state="hidden", timeout=30000)
                except: time.sleep(3)
                
                time.sleep(2)
                new_url = page.url
                
                if new_url != old_url:
                    try:
                        cs = {c['name']: c['value'] for c in context.cookies()}
                        response = requests.get(new_url, cookies=cs, timeout=30)
                        with open(path_cart, 'wb') as f: 
                            f.write(response.content if b'%PDF' in response.content[:10] else page.pdf())
                    except: 
                        page.pdf(path=path_cart)
                else:
                    page.pdf(path=path_cart)
                
                if os.path.exists(path_cart):
                    cart_ok = True
                    st_status.success(f"✅ Cartellino: {os.path.getsize(path_cart)} bytes")

            except Exception as e:
                st.error(f"Err Cart: {e}")

            browser.close()
            
    except Exception as e:
        st.error(f"Errore Gen: {e}")
    
    final_busta = path_busta if busta_ok else None
    final_cart = path_cart if cart_ok else None
    
    st.success(f"📦 Busta: {final_busta}, Cart: {final_cart}")
    
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
