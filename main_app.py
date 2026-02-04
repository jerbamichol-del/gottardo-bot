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

# --- PARSING AI ULTRA-SPECIFICO ---
def clean_json_response(text):
    try:
        text = re.sub(r"```json|```", "", text).strip()
        start = text.find('{')
        end = text.rfind('}') + 1
        return json.loads(text[start:end]) if start != -1 else json.loads(text)
    except: 
        return None

def estrai_dati_busta_dettagliata(file_path):
    """✅ PROMPT ULTRA-SPECIFICO BASATO SUL CEDOLINO REALE"""
    if not file_path or not os.path.exists(file_path):
        return None
    
    try:
        with open(file_path, "rb") as f: 
            bytes_data = f.read()
        
        prompt = """
        Questo è un CEDOLINO PAGA GOTTARDO S.p.A. italiano. Segui ESATTAMENTE queste istruzioni:

        **1. DATI GENERALI (PRIMA PAGINA, RIGA PROGRESSIVI):**
        - **NETTO:** Cerca la riga "PROGRESSIVI" in fondo. Il NETTO è nella colonna finale prima di "ESTREMI ELABORAZIONE"
        - **GIORNI PAGATI:** Cerca in alto la riga con "GG. INPS" (numero a sinistra della colonna, es. "26")
        - **ORE ORDINARIE:** Cerca "ORE INAIL" oppure calcola: giorni_pagati × 8

        **2. COMPETENZE (TABELLA CENTRALE):**
        - **RETRIBUZIONE ORDINARIA (voce 1000):** Colonna "COMPETENZE" (es. 1.783,75)
        - **STRAORDINARI:** Somma tutte le voci tipo "STRAORDINARIO", "SUPPLEMENTARI", "NOTTURNI" (es. voce 2050: 111,48)
        - **FESTIVITA:** Somma voci "MAGG. FESTIVE", "FESTIVITA GODUTA" (es. voce 2250: 37,16)
        - **ANZIANITA:** Se vedi voci "SCATTI", "EDR", "ANZ." usale, altrimenti 0
        - **LORDO TOTALE:** Cerca riga "TOTALE COMPETENZE" o "PROGRESSIVI" → colonna "TOTALE COMPETENZE" (es. 2.011,99)

        **3. TRATTENUTE (SEZIONE I.N.P.S. + IRPEF):**
        - **INPS:** Sezione "IMPONIBILE / TRATTENUTE" → riga sotto "I.N.P.S." (es. 188,50)
        - **IRPEF NETTA:** Sezione "FISCALI" → riga "TRATTENUTE" sotto "IRPEF CONG." (es. 58,90)
        - **ADDIZIONALI:** Cerca voci "ADD.REG." e "ADD.COM." (sono rateizzate, non trattenute subito)

        **4. FERIE (TABELLA IN ALTO A DESTRA):**
        - Ci sono DUE colonne: FERIE e P.A.R. (Permessi)
        - **Residue AP:** Riga "RES. PREC." colonna FERIE (es. -10,46)
        - **Maturate:** Riga "SPETTANTI" colonna FERIE (es. 173,00)
        - **Godute:** Riga "FRUITE" colonna FERIE (es. 162,67)
        - **Saldo:** Riga "SALDO" colonna FERIE (es. -0,13)
        
        **PAR (Permessi):**
        - **Residue:** Riga "RES. PREC." colonna P.A.R. (es. 5,30)
        - **Spettanti:** Riga "SPETTANTI" colonna P.A.R. (es. 38,00)
        - **Fruite:** Riga "FRUITE" colonna P.A.R. (es. 47,33)
        - **Saldo:** Riga "SALDO" colonna P.A.R. (es. -4,03)

        **5. TREDICESIMA:**
        - Se nel titolo o voci c'è "TREDICESIMA" o "13MA" → è_tredicesima = true
        - Altrimenti → è_tredicesima = false

        **IMPORTANTE:**
        - Usa SEMPRE i valori dalle colonne corrette
        - Se un valore non esiste scrivi 0
        - Usa il punto come separatore decimale
        
        Restituisci SOLO questo JSON:
        {
            "e_tredicesima": boolean,
            "dati_generali": {
                "netto": float,
                "giorni_pagati": float,
                "ore_ordinarie": float
            },
            "competenze": {
                "base": float,
                "anzianita": float,
                "straordinari": float,
                "festivita": float,
                "lordo_totale": float
            },
            "trattenute": {
                "inps": float,
                "irpef_netta": float,
                "addizionali_totali": float
            },
            "ferie": {
                "residue_ap": float,
                "maturate": float,
                "godute": float,
                "saldo": float
            },
            "par": {
                "residue_ap": float,
                "spettanti": float,
                "fruite": float,
                "saldo": float
            }
        }
        """
        
        response = model.generate_content([prompt, {"mime_type": "application/pdf", "data": bytes_data}])
        return clean_json_response(response.text)
    except Exception as e:
        st.error(f"❌ Err busta AI: {e}")
        return None


def estrai_dati_cartellino(file_path):
    """✅ PROMPT REALISTICO - Conta giorni dal periodo"""
    if not file_path or not os.path.exists(file_path):
        return None
    
    try:
        with open(file_path, "rb") as f: 
            bytes_data = f.read()
        
        prompt = """
        Questo PDF è un cartellino presenze o una ricerca.
        
        **CERCA:**
        1. Se vedi una TABELLA con timbrature dettagliate per giorno:
           - Conta quanti giorni hanno almeno UNA timbratura (entrata/uscita)
           - giorni_reali = quel numero
           - giorni_senza_badge = giorni con anomalie/badge mancante
        
        2. Se vedi SOLO "Periodo: XX/XX/XXXX - YY/YY/YYYY":
           - Calcola i giorni del periodo (es. 01/12-31/12 = 31 giorni)
           - giorni_reali = numero giorni nel periodo
           - giorni_senza_badge = 0
           - note = "Dati timbrature non disponibili, mostrato solo periodo"
        
        JSON:
        {
            "giorni_reali": int,
            "giorni_senza_badge": int,
            "note": "string"
        }
        """
        
        response = model.generate_content([prompt, {"mime_type": "application/pdf", "data": bytes_data}])
        return clean_json_response(response.text)
    except Exception as e:
        st.error(f"❌ Err cart AI: {e}")
        return None

# --- CORE BOT (INVARIATO) ---
def scarica_documenti_automatici(mese_nome, anno, scarica_tredicesima=False):
    """✅ Aggiunto flag per tredicesima"""
    nomi_mesi_it = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", 
                    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
    try: mese_num = nomi_mesi_it.index(mese_nome) + 1
    except: return None, None

    # ✅ Target diverso se tredicesima
    if scarica_tredicesima:
        target_busta = f"Tredicesima {anno}"
    else:
        target_busta = f"{mese_nome} {anno}"
    
    target_cart_row = f"{mese_num:02d}/{anno}"
    last_day = calendar.monthrange(anno, mese_num)[1]
    
    d_from_vis = f"01/{mese_num:02d}/{anno}"
    d_to_vis = f"{last_day}/{mese_num:02d}/{anno}"
    
    work_dir = Path.cwd()
    suffix = "_13" if scarica_tredicesima else ""
    path_busta = str(work_dir / f"busta_{mese_num}_{anno}{suffix}.pdf")
    path_cart = str(work_dir / f"cartellino_{mese_num}_{anno}.pdf")
    
    st_status = st.empty()
    tipo = "Tredicesima" if scarica_tredicesima else "Cedolino"
    st_status.info(f"🤖 Bot: {tipo} {mese_nome} {anno}")
    
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
            st_status.info(f"💰 {tipo}...")
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
                        st.info(f"✅ Trovato: {target_busta}")
                        with page.expect_download(timeout=20000) as dl:
                            links.first.click()
                        
                        dl.value.save_as(path_busta)
                        
                        if os.path.exists(path_busta):
                            busta_ok = True
                            st_status.success(f"✅ {tipo}: {os.path.getsize(path_busta)} bytes")
                    else:
                        st.warning(f"⚠️ {target_busta} non trovato")
                except Exception as e:
                    st.error(f"❌ Errore {tipo}: {e}")
                    
            except Exception as e: 
                st.error(f"Err {tipo}: {e}")

            # CARTELLINO (solo se NON è tredicesima)
            if not scarica_tredicesima:
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
    final_cart = path_cart if cart_ok and not scarica_tredicesima else None
    
    st.success(f"📦 {tipo}: {final_busta}, Cart: {final_cart}")
    
    return final_busta, final_cart

# --- UI AVANZATA ---
st.set_page_config(page_title="Gottardo Payroll Mobile", page_icon="💶", layout="wide")
st.title("💶 Analisi Stipendio & Presenze")

with st.sidebar:
    st.header("Parametri")
    sel_anno = st.selectbox("Anno", [2024, 2025, 2026], index=1)
    sel_mese = st.selectbox("Mese", ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", 
                                     "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"], index=11)
    
    # ✅ CHECKBOX TREDICESIMA
    scarica_13 = st.checkbox("📦 Scarica Tredicesima (se disponibile)", value=False)
    
    if st.button("🚀 AVVIA ANALISI", type="primary", use_container_width=True):
        st.session_state.clear()
        busta, cart = scarica_documenti_automatici(sel_mese, sel_anno, scarica_tredicesima=scarica_13)
        st.session_state['busta'] = busta
        st.session_state['cart'] = cart
        st.session_state['done'] = False
        st.session_state['is_13'] = scarica_13

if st.session_state.get('busta') or st.session_state.get('cart'):
    
    if not st.session_state.get('done'):
        with st.spinner("🧠 Analisi dettagliata AI in corso..."):
            db = estrai_dati_busta_dettagliata(st.session_state.get('busta'))
            dc = estrai_dati_cartellino(st.session_state.get('cart')) if st.session_state.get('cart') else None
            st.session_state['db'] = db
            st.session_state['dc'] = dc
            st.session_state['done'] = True

    db = st.session_state.get('db')
    dc = st.session_state.get('dc')
    is_13 = st.session_state.get('is_13', False)
    
    # ✅ ALERT SE TREDICESIMA
    if db and db.get('e_tredicesima'):
        st.success("🎄 **Questo è un cedolino TREDICESIMA**")
    
    st.divider()
    
    # --- 3 TAB ---
    tab1, tab2, tab3 = st.tabs(["💰 Dettaglio Stipendio", "📅 Cartellino & Presenze", "📊 Analisi & Confronto"])
    
    with tab1:
        if db:
            dg = db.get('dati_generali', {})
            comp = db.get('competenze', {})
            tratt = db.get('trattenute', {})
            ferie = db.get('ferie', {})
            par = db.get('par', {})

            # KPI CARDS
            k1, k2, k3 = st.columns(3)
            k1.metric("💵 NETTO IN BUSTA", f"€ {dg.get('netto', 0):.2f}", delta="Pagamento")
            k2.metric("📊 Lordo Totale", f"€ {comp.get('lordo_totale', 0):.2f}")
            k3.metric("📆 Giorni Pagati", int(dg.get('giorni_pagati', 0)))

            st.markdown("---")
            
            # DETTAGLIO ENTRATE/USCITE
            c_entr, c_usc = st.columns(2)
            with c_entr:
                st.subheader("➕ Competenze (Entrate)")
                st.write(f"**Paga Base:** € {comp.get('base', 0):.2f}")
                if comp.get('anzianita', 0) > 0:
                    st.write(f"**Anzianità:** € {comp.get('anzianita', 0):.2f}")
                if comp.get('straordinari', 0) > 0:
                    st.write(f"**Straordinari/Suppl.:** € {comp.get('straordinari', 0):.2f}")
                if comp.get('festivita', 0) > 0:
                    st.write(f"**Festività/Maggiorazioni:** € {comp.get('festivita', 0):.2f}")

            with c_usc:
                st.subheader("➖ Trattenute (Uscite)")
                st.write(f"**Contributi INPS:** € {tratt.get('inps', 0):.2f}")
                st.write(f"**IRPEF Netta:** € {tratt.get('irpef_netta', 0):.2f}")
                if tratt.get('addizionali_totali', 0) > 0:
                    st.write(f"**Addizionali (da rateizzare):** € {tratt.get('addizionali_totali', 0):.2f}")

            # FERIE ESPANDIBILI
            with st.expander("🏖️ Situazione Ferie"):
                f1, f2, f3, f4 = st.columns(4)
                f1.metric("Residue AP", f"{ferie.get('residue_ap', 0):.2f}")
                f2.metric("Maturate", f"{ferie.get('maturate', 0):.2f}")
                f3.metric("Godute", f"{ferie.get('godute', 0):.2f}")
                saldo_f = ferie.get('saldo', 0)
                f4.metric("✅ SALDO", f"{saldo_f:.2f}", delta="OK" if saldo_f >= 0 else "Negativo")
            
            with st.expander("⏱️ Situazione Permessi (P.A.R.)"):
                p1, p2, p3, p4 = st.columns(4)
                p1.metric("Residue AP", f"{par.get('residue_ap', 0):.2f}")
                p2.metric("Spettanti", f"{par.get('spettanti', 0):.2f}")
                p3.metric("Fruite", f"{par.get('fruite', 0):.2f}")
                saldo_p = par.get('saldo', 0)
                p4.metric("✅ SALDO", f"{saldo_p:.2f}", delta="OK" if saldo_p >= 0 else "Negativo")
        else:
            st.warning("⚠️ Dati busta non disponibili.")

    with tab2:
        if dc:
            c1, c2 = st.columns([1, 2])
            with c1:
                st.metric("📅 Giorni Lavorati", dc.get('giorni_reali', 0))
                anomalie = dc.get('giorni_senza_badge', 0)
                if anomalie > 0:
                    st.metric("⚠️ Anomalie Badge", anomalie, delta="Controlla")
                else:
                    st.metric("✅ Anomalie Badge", 0, delta="Perfetto")
            
            with c2:
                note = dc.get('note', 'Nessuna nota rilevante.')
                st.info(f"**📝 Note AI:** {note}")
        else:
            st.warning("⚠️ Dati cartellino non disponibili (normale per Tredicesima).")

    with tab3:
        if db and dc:
            pagati = float(db.get('dati_generali', {}).get('giorni_pagati', 0))
            reali = float(dc.get('giorni_reali', 0))
            diff = reali - pagati
            
            st.subheader("🔍 Analisi Discrepanze")
            
            col_a, col_b = st.columns(2)
            col_a.metric("Giorni Pagati (Busta)", pagati)
            col_b.metric("Giorni Lavorati (Cartellino)", reali, delta=f"{diff:.1f}")
            
            st.markdown("---")
            
            if abs(diff) < 0.5:  # Tolleranza per arrotondamenti
                st.success("✅ **Tutto perfetto!** I giorni lavorati corrispondono a quelli pagati.")
            elif diff > 0:
                st.info(f"ℹ️ Hai lavorato **{diff:.1f} giorni in più** rispetto a quelli pagati.\n\n"
                       f"Controlla che siano compensati come **Straordinari** (€ {db.get('competenze', {}).get('straordinari', 0):.2f}) "
                       f"o come **Festività** nella tab 'Dettaglio Stipendio'.")
            else:
                st.warning(f"⚠️ Risultano **{abs(diff):.1f} giorni pagati in più** rispetto alle timbrature.\n\n"
                          f"Possibili cause:\n"
                          f"- **Ferie godute:** {db.get('ferie', {}).get('godute', 0):.2f} giorni\n"
                          f"- **Permessi:** {db.get('par', {}).get('fruite', 0):.2f} ore\n"
                          f"- Controlla nella tab 'Dettaglio Stipendio' → Ferie/Permessi")
        elif is_13:
            st.info("ℹ️ Analisi comparativa non disponibile per cedolino Tredicesima.")
        else:
            st.warning("⚠️ Servono entrambi i documenti per l'analisi.")
