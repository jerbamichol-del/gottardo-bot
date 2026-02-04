import sys
import asyncio
import re
import requests
import os
import streamlit as st
import google.generativeai as genai  # ✅ CORRETTO
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
    except: 
        return None

def estrai_dati_busta_dettagliata(file_path):
    if not file_path: return None
    try:
        with open(file_path, "rb") as f: bytes_data = f.read()
        prompt = """Analizza cedolino. JSON: {"dati_generali": {"netto": float, "giorni_pagati": float}, "competenze": {"base": float, "straordinari": float}, "trattenute": {"inps": float, "irpef_netta": float}, "ferie_tfr": {"saldo": float}}"""
        response = model.generate_content([prompt, {"mime_type": "application/pdf", "data": bytes_data}])
        return clean_json_response(response.text)
    except: 
        return None

def estrai_dati_cartellino(file_path):
    if not file_path: return None
    try:
        with open(file_path, "rb") as f: bytes_data = f.read()
        prompt = """Analizza cartellino. JSON: { "giorni_reali": int, "note": "string" }"""
        response = model.generate_content([prompt, {"mime_type": "application/pdf", "data": bytes_data}])
        return clean_json_response(response.text)
    except: 
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
    
    st_status = st.empty()
    st_status.info(f"🤖 Bot Cloud: {mese_nome} {anno} → {d_from_vis} / {d_to_vis}")
    
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
                st_status.info("🏠 Reset...")
                page.evaluate("window.scrollTo(0, 0)")
                time.sleep(2)
                
                try:
                    page.keyboard.press("Escape")
                    time.sleep(1)
                except: pass
                
                try:
                    logo = page.locator("img[src*='logo'], .logo").first
                    if logo.is_visible(timeout=2000):
                        logo.click()
                        time.sleep(2)
                except:
                    page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2", wait_until="domcontentloaded")
                    time.sleep(3)
                
                # MENU TIME
                st_status.info("📂 Time...")
                time_clicked = False
                try:
                    result = page.evaluate("""
                        () => {
                            const time_elem = document.getElementById('revit_navigation_NavHoverItem_2_label');
                            if (time_elem) {
                                time_elem.click();
                                return 'OK';
                            }
                            return 'NOT_FOUND';
                        }
                    """)
                    if result == 'OK': time_clicked = True
                except: pass
                
                if not time_clicked:
                    try:
                        page.locator("#revit_navigation_NavHoverItem_2_label").click(timeout=3000)
                        time_clicked = True
                    except: pass
                
                if not time_clicked:
                    try:
                        page.locator("text=Time").first.click(timeout=3000)
                        time_clicked = True
                    except: pass
                
                if not time_clicked:
                    raise Exception("Menu Time non trovato")
                
                time.sleep(3)
                
                # CARTELLINO PRESENZE
                st_status.info("📋 Cartellino...")
                cart_opened = False
                
                try:
                    page.wait_for_selector("#lnktab_5_label", state="visible", timeout=10000)
                    result = page.evaluate("""
                        () => {
                            const elem = document.getElementById('lnktab_5_label');
                            if (elem) {
                                elem.click();
                                return 'OK';
                            }
                            return 'NOT_FOUND';
                        }
                    """)
                    if result == 'OK': cart_opened = True
                except: pass
                
                if not cart_opened:
                    try:
                        page.locator("#lnktab_5_label").click(force=True, timeout=5000)
                        cart_opened = True
                    except: pass
                
                if not cart_opened:
                    try:
                        result = page.evaluate("""
                            () => {
                                const spans = document.querySelectorAll('span.dijitButtonText');
                                for (let span of spans) {
                                    if (span.textContent.trim().includes('Cartellino presenze')) {
                                        span.click();
                                        return 'OK';
                                    }
                                }
                                return 'NOT_FOUND';
                            }
                        """)
                        if result == 'OK': cart_opened = True
                    except: pass
                
                if not cart_opened:
                    raise Exception("Cartellino non accessibile")
                
                time.sleep(5)
                
                st.image(page.screenshot(), caption="Pagina Cartellini (pre-date)", use_container_width=True)
                
                # Fix Agenda
                if page.locator("text=Permessi del").count() > 0:
                    st_status.info("🔄 Fix Agenda...")
                    try: 
                        page.locator(".z-icon-print").first.click()
                        time.sleep(3)
                    except: pass
                
                # DATE - ROBUSTE CON VERIFICA
                st_status.info(f"✍️ DATE: {d_from_vis} → {d_to_vis}")
                
                date_ok = False
                
                # TENTATIVO 1: Input diretti
                try:
                    dal = page.locator("input[id*='CLRICHIE'][class*='dijitInputInner']").first
                    al = page.locator("input[id*='CLRICHI2'][class*='dijitInputInner']").first
                    
                    if dal.count() > 0 and al.count() > 0:
                        dal.click(force=True)
                        page.keyboard.press("Control+A")
                        dal.fill("")
                        time.sleep(0.3)
                        dal.type(d_from_vis, delay=100)
                        time.sleep(0.5)
                        dal.press("Tab")
                        time.sleep(1)
                        
                        val_dal = dal.input_value()
                        st_status.info(f"DAL: '{val_dal}' (atteso: '{d_from_vis}')")
                        
                        al.click(force=True)
                        page.keyboard.press("Control+A")
                        al.fill("")
                        time.sleep(0.3)
                        al.type(d_to_vis, delay=100)
                        time.sleep(0.5)
                        al.press("Tab")
                        time.sleep(1)
                        
                        val_al = al.input_value()
                        st_status.info(f"AL: '{val_al}' (atteso: '{d_to_vis}')")
                        
                        if val_dal == d_from_vis and val_al == d_to_vis:
                            date_ok = True
                            st_status.success("✅ Date OK (input)")
                except Exception as e:
                    st_status.warning(f"Input date fallito: {str(e)[:50]}")
                
                # TENTATIVO 2: JS Dojo
                if not date_ok:
                    st_status.info("🔧 Dojo JS...")
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
                                        return 'OK: ' + ws[i1].get('displayedValue') + ' / ' + ws[i1+1].get('displayedValue');
                                    }}
                                    return 'NO_WIDGETS';
                                }} catch(e) {{
                                    return 'ERROR: ' + e.message;
                                }}
                            }}
                        """)
                        st_status.info(f"JS result: {result}")
                        if "OK" in str(result):
                            date_ok = True
                            st_status.success("✅ Date OK (JS)")
                    except Exception as e:
                        st_status.warning(f"JS fallito: {str(e)[:50]}")
                
                st.image(page.screenshot(), caption=f"Dopo date", use_container_width=True)
                
                if not date_ok:
                    st_status.error(f"❌ DATE NON IMPOSTATE!")
                
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
                
                st.image(page.screenshot(), caption=f"Risultati", use_container_width=True)
                
                # Download - 3 metodi
                st_status.info(f"📄 Download {target_cart_row}...")
                
                pdf_downloaded = False
                
                # Metodo 1: Nuova pagina
                try:
                    with context.expect_page(timeout=10000) as new_page_info:
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
                    pdf_downloaded = True
                    st_status.success("✅ Cartellino OK")
                except Exception as e:
                    st_status.warning(f"Nuova pagina fallita: {str(e)[:50]}")
                
                # Metodo 2: Download diretto
                if not pdf_downloaded:
                    try:
                        with page.expect_download(timeout=10000) as download_info:
                            try:
                                row = page.locator(f"tr:has-text('{target_cart_row}')").first
                                row.locator("img[src*='search16.png']").click()
                            except:
                                page.locator("img[src*='search16.png']").first.click()
                        
                        path_cart = f"cartellino_{mese_num}_{anno}.pdf"
                        download_info.value.save_as(path_cart)
                        pdf_downloaded = True
                        st_status.success("✅ Cartellino OK (download)")
                    except Exception as e:
                        st_status.warning(f"Download fallito: {str(e)[:50]}")
                
                # Metodo 3: Same tab
                if not pdf_downloaded:
                    try:
                        old_url = page.url
                        try:
                            row = page.locator(f"tr:has-text('{target_cart_row}')").first
                            row.locator("img[src*='search16.png']").click()
                        except:
                            page.locator("img[src*='search16.png']").first.click()
                        
                        page.wait_for_url(lambda url: url != old_url, timeout=10000)
                        time.sleep(3)
                        
                        path_cart = f"cartellino_{mese_num}_{anno}.pdf"
                        page.pdf(path=path_cart)
                        pdf_downloaded = True
                        st_status.success("✅ Cartellino OK (same tab)")
                    except Exception as e:
                        st_status.warning(f"Same tab fallito: {str(e)[:50]}")
                
                if not pdf_downloaded:
                    st_status.error("❌ Cartellino NON scaricato!")

            except Exception as e:
                st_status.warning(f"Err Cart: {e}")
                try: st.image(page.screenshot(), caption="Errore", use_container_width=True)
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
