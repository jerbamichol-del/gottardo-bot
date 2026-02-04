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

# --- PARSING AI AVANZATO (COME PC) ---
def clean_json_response(text):
    try:
        text = re.sub(r"```json|```", "", text).strip()
        start = text.find('{')
        end = text.rfind('}') + 1
        return json.loads(text[start:end]) if start != -1 else json.loads(text)
    except: 
        return None

def estrai_dati_busta_dettagliata(file_path):
    """✅ PROMPT AVANZATO come bot PC"""
    if not file_path or not os.path.exists(file_path):
        return None
    
    try:
        with open(file_path, "rb") as f: 
            bytes_data = f.read()
        
        prompt = """
        Analizza questo cedolino PDF in dettaglio. Estrai le seguenti sezioni:

        1. **DATI GENERALI**:
           - Netto del mese
           - Giorni Lavorati/Pagati
           - Ore Lavorate Ordinarie

        2. **VOCI RETRIBUTIVE (Competenze)**:
           - Minimo Tabellare / Paga Base
           - Scatti Anzianità (se presenti)
           - Superminimo (se presente)
           - Totale Straordinari/Supplementari (somma importi)
           - Totale Festività/Permessi goduti
           - Totale Lordo (Imponibile Previdenziale)

        3. **TRATTENUTE (Dati Fiscali/Previdenziali)**:
           - Contributi IVS/INPS (c/dipendente)
           - Totale Trattenute IRPEF (Lorda - Detrazioni)
           - Addizionali Regionali/Comunali

        4. **FERIE E TFR**:
           - Ferie Residue Anno Prec.
           - Ferie Maturate
           - Ferie Godute
           - Ferie Saldo Attuale
           - Ratei 13ma/14ma Maturati
        
        Restituisci un JSON strutturato così:
        {
            "dati_generali": {"netto": float, "giorni_pagati": float, "ore_ordinarie": float},
            "competenze": {"base": float, "anzianita": float, "straordinari": float, "festivita": float, "lordo_totale": float},
            "trattenute": {"inps": float, "irpef_netta": float, "addizionali": float},
            "ferie_tfr": {"residue_ap": float, "maturate": float, "godute": float, "saldo": float, "ratei_extra": "string"}
        }
        """
        response = model.generate_content([prompt, {"mime_type": "application/pdf", "data": bytes_data}])
        return clean_json_response(response.text)
    except Exception as e:
        st.error(f"❌ Err busta AI: {e}")
        return None

def estrai_dati_cartellino(file_path):
    """✅ PROMPT AVANZATO con anomalie badge"""
    if not file_path or not os.path.exists(file_path):
        return None
    
    try:
        with open(file_path, "rb") as f: 
            bytes_data = f.read()
        
        prompt = """
        Analizza cartellino presenze. Estrai:
        - giorni_reali: numero totale giorni lavorati
        - giorni_senza_badge: giorni con anomalie/mancate timbrature
        - note: breve descrizione situazione (max 2 righe)
        
        JSON: { "giorni_reali": int, "giorni_senza_badge": int, "note": "string" }
        """
        response = model.generate_content([prompt, {"mime_type": "application/pdf", "data": bytes_data}])
        return clean_json_response(response.text)
    except Exception as e:
        st.error(f"❌ Err cart AI: {e}")
        return None

# --- CORE BOT (FUNZIONANTE, NON TOCCO!) ---
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
            st_status.info("💰 Busta...")
            try:
                page.click("text=I miei dati")
                page.wait_for_selector("text=Documenti", timeout=10000).click()
                time.sleep(3)
                
                try: 
                    page.locator("tr", has=page.locator("text=Cedolino")).locator(".z-image").click(timeout=5000)
                except: 
                    page.click("text=Cedolino")
                
                time.sleep(5)
                
                try:
                    links = page.locator(f"a:has-text('{target_busta}')")
                    
                    if links.count() > 0:
                        for i in range(links.count()):
                            txt = links.nth(i).inner_text()
                            
                            if "Tredicesima" not in txt and "13" not in txt:
                                with page.expect_download(timeout=20000) as dl:
                                    links.nth(i).click()
                                
                                dl.value.save_as(path_busta)
                                
                                if os.path.exists(path_busta):
                                    busta_ok = True
                                    st_status.success(f"✅ Busta: {os.path.getsize(path_busta)} bytes")
                                break
                except Exception as e:
                    st.error(f"❌ Errore busta: {e}")
                    
            except Exception as e: 
                st.error(f"Err Busta: {e}")

            # CARTELLINO
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

# --- UI AVANZATA (COME PC) ---
st.set_page_config(page_title="Gottardo Payroll Mobile", page_icon="💶", layout="wide")
st.title("💶 Analisi Stipendio & Presenze")

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
        with st.spinner("🧠 Analisi dettagliata AI in corso..."):
            db = estrai_dati_busta_dettagliata(st.session_state.get('busta'))
            dc = estrai_dati_cartellino(st.session_state.get('cart'))
            st.session_state['db'] = db
            st.session_state['dc'] = dc
            st.session_state['done'] = True

    db = st.session_state.get('db')
    dc = st.session_state.get('dc')
    
    st.divider()
    
    # --- ✅ 3 TAB COME PC ---
    tab1, tab2, tab3 = st.tabs(["💰 Dettaglio Stipendio", "📅 Cartellino & Presenze", "📊 Analisi & Confronto"])
    
    with tab1:
        if db:
            dg = db.get('dati_generali', {})
            comp = db.get('competenze', {})
            tratt = db.get('trattenute', {})
            ferie = db.get('ferie_tfr', {})

            # ✅ KPI CARDS
            k1, k2, k3 = st.columns(3)
            k1.metric("💵 NETTO IN BUSTA", f"€ {dg.get('netto', 0):.2f}", delta="Pagamento")
            k2.metric("📊 Lordo Totale", f"€ {comp.get('lordo_totale', 0):.2f}")
            k3.metric("📆 Giorni Pagati", dg.get('giorni_pagati', 0))

            st.markdown("---")
            
            # ✅ DETTAGLIO ENTRATE/USCITE
            c_entr, c_usc = st.columns(2)
            with c_entr:
                st.subheader("➕ Competenze (Entrate)")
                st.write(f"**Paga Base:** € {comp.get('base', 0):.2f}")
                if comp.get('anzianita', 0) > 0:
                    st.write(f"**Anzianità:** € {comp.get('anzianita', 0):.2f}")
                if comp.get('straordinari', 0) > 0:
                    st.write(f"**Straordinari/Extra:** € {comp.get('straordinari', 0):.2f}")
                if comp.get('festivita', 0) > 0:
                    st.write(f"**Festività/Permessi:** € {comp.get('festivita', 0):.2f}")

            with c_usc:
                st.subheader("➖ Trattenute (Uscite)")
                st.write(f"**Contributi INPS:** € {tratt.get('inps', 0):.2f}")
                st.write(f"**IRPEF Netta:** € {tratt.get('irpef_netta', 0):.2f}")
                if tratt.get('addizionali', 0) > 0:
                    st.write(f"**Addizionali:** € {tratt.get('addizionali', 0):.2f}")

            # ✅ FERIE ESPANDIBILI
            with st.expander("🏖️ Situazione Ferie & TFR"):
                f1, f2, f3, f4 = st.columns(4)
                f1.metric("Residue AP", ferie.get('residue_ap', 0))
                f2.metric("Maturate", ferie.get('maturate', 0))
                f3.metric("Godute", ferie.get('godute', 0))
                f4.metric("✅ SALDO", ferie.get('saldo', 0))
                if ferie.get('ratei_extra'):
                    st.info(f"**Ratei Extra:** {ferie.get('ratei_extra')}")
        else:
            st.warning("⚠️ Dati busta non disponibili.")

    with tab2:
        if dc:
            # ✅ METRICHE CARTELLINO
            c1, c2 = st.columns([1, 2])
            with c1:
                st.metric("📅 Giorni Lavorati", dc.get('giorni_reali', 0))
                anomalie = dc.get('giorni_senza_badge', 0)
                if anomalie > 0:
                    st.metric("⚠️ Anomalie Badge", anomalie, delta="Controlla")
                else:
                    st.metric("✅ Anomalie Badge", 0, delta="Perfetto")
            
            with c2:
                # ✅ NOTE AI
                note = dc.get('note', 'Nessuna nota rilevante.')
                st.info(f"**📝 Note AI:** {note}")
        else:
            st.warning("⚠️ Dati cartellino non disponibili.")

    with tab3:
        # ✅ ANALISI DISCREPANZE (COME PC)
        if db and dc:
            pagati = float(db.get('dati_generali', {}).get('giorni_pagati', 0))
            reali = float(dc.get('giorni_reali', 0))
            diff = reali - pagati
            
            st.subheader("🔍 Analisi Discrepanze")
            
            col_a, col_b = st.columns(2)
            col_a.metric("Giorni Pagati (Busta)", pagati)
            col_b.metric("Giorni Lavorati (Cartellino)", reali, delta=f"{diff:.1f}")
            
            st.markdown("---")
            
            if diff == 0:
                st.success("✅ **Tutto perfetto!** I giorni lavorati corrispondono esattamente a quelli pagati.")
            elif diff > 0:
                st.info(f"ℹ️ Hai lavorato **{diff:.1f} giorni in più** rispetto al tabellare base.\n\n"
                       f"Controlla che siano stati pagati come **Straordinari** nella tab 'Dettaglio Stipendio' → Competenze.")
            else:
                st.warning(f"⚠️ Risultano **{abs(diff):.1f} giorni pagati in più** rispetto alle timbrature reali.\n\n"
                          f"Potrebbero essere: **Ferie godute**, **Permessi**, o **ROL**. Verifica nella tab 'Dettaglio Stipendio' → Ferie.")
        else:
            st.warning("⚠️ Servono entrambi i documenti per l'analisi comparativa.")
