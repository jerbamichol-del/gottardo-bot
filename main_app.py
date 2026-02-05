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
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
try:
    locale.setlocale(locale.LC_TIME, 'it_IT.UTF-8')
except:
    pass

# --- CREDENZIALI DINAMICHE ---
def get_credentials():
    """✅ Sistema di login con credenziali utente"""
    if 'credentials_set' in st.session_state and st.session_state.get('credentials_set'):
        return st.session_state.get('username'), st.session_state.get('password')

    try:
        return st.secrets["ZK_USER"], st.secrets["ZK_PASS"]
    except:
        return None, None

# Google API Key
try:
    GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
except Exception:
    st.error("❌ Google API Key mancante in secrets")
    st.stop()

# ✅ AUTO-DISCOVERY MODELLI GEMINI
genai.configure(api_key=GOOGLE_API_KEY)

@st.cache_resource
def inizializza_modelli_gemini():
    """Auto-discovery: Scopre automaticamente tutti i modelli Gemini disponibili"""
    try:
        tutti_modelli = genai.list_models()
        modelli_validi = [m for m in tutti_modelli if 'generateContent' in m.supported_generation_methods]

        modelli_gemini = []
        for m in modelli_validi:
            nome_pulito = m.name.replace('models/', '')
            if 'gemini' in nome_pulito.lower() and 'embedding' not in nome_pulito.lower():
                try:
                    modello = genai.GenerativeModel(nome_pulito)
                    modelli_gemini.append((nome_pulito, modello))
                except:
                    continue

        if len(modelli_gemini) == 0:
            st.error("❌ Nessun modello Gemini disponibile")
            st.stop()

        def priorita(nome):
            if 'flash' in nome.lower() and 'lite' not in nome.lower():
                return 0
            elif 'lite' in nome.lower():
                return 1
            elif 'pro' in nome.lower():
                return 2
            else:
                return 3

        modelli_gemini.sort(key=lambda x: priorita(x[0]))
        return modelli_gemini

    except Exception as e:
        st.error(f"❌ Errore caricamento modelli: {e}")
        st.stop()

MODELLI_DISPONIBILI = inizializza_modelli_gemini()

if 'modelli_mostrati' not in st.session_state:
    st.sidebar.success(f"✅ {len(MODELLI_DISPONIBILI)} modelli AI pronti")
    st.session_state['modelli_mostrati'] = True

# --- PARSING AI ---
def clean_json_response(text):
    """Estrae JSON pulito da risposta AI"""
    try:
        text = re.sub(r"```json|```", "", text).strip()
        start = text.find('{')
        end = text.rfind('}') + 1
        return json.loads(text[start:end]) if start != -1 else json.loads(text)
    except:
        return None

def estrai_con_fallback(file_path, prompt, tipo="documento", validate_fn=None):
    """
    ✅ Prova multipli modelli Gemini con fallback automatico.
    Se validate_fn restituisce False, prova il modello successivo.
    """
    if not file_path or not os.path.exists(file_path):
        return None

    with open(file_path, "rb") as f:
        bytes_data = f.read()

    if not bytes_data[:4] == b'%PDF':
        st.error(f"❌ Il file {tipo} non è un PDF valido")
        return None

    progress_placeholder = st.empty()

    for idx, (nome_modello, modello) in enumerate(MODELLI_DISPONIBILI, 1):
        try:
            progress_placeholder.info(f"🔄 Analisi {tipo}: modello {idx}/{len(MODELLI_DISPONIBILI)}...")

            response = modello.generate_content([
                prompt,
                {"mime_type": "application/pdf", "data": bytes_data}
            ])

            result = clean_json_response(response.text)

            if result and isinstance(result, dict):
                if validate_fn is not None:
                    # Se la validazione fallisce, passa al modello successivo
                    if not validate_fn(result):
                        continue

                progress_placeholder.success(f"✅ {tipo.capitalize()} analizzato!")
                time.sleep(0.7)
                progress_placeholder.empty()
                return result

        except Exception as e:
            msg = str(e).lower()
            if "429" in msg or "quota" in msg or "resource_exhausted" in msg:
                continue
            else:
                continue

    progress_placeholder.error(f"❌ Analisi {tipo} fallita (nessun modello ha fornito dati validi)")
    return None

def estrai_dati_busta_dettagliata(file_path):
    """Estrae dati dalla busta paga"""

    prompt = """
    Questo è un CEDOLINO PAGA GOTTARDO S.p.A. italiano. Segui ESATTAMENTE queste istruzioni:

    **1. DATI GENERALI (PRIMA PAGINA, RIGA PROGRESSIVI):**
    - **NETTO:** Cerca la riga "PROGRESSIVI" in fondo. Il NETTO è nella colonna finale prima di "ESTREMI ELABORAZIONE"
    - **GIORNI PAGATI:** Cerca in alto la riga con "GG. INPS" (numero a sinistra della colonna, es. "26")
    - **ORE ORDINARIE:** Cerca "ORE INAIL" oppure calcola: giorni_pagati × 8

    **2. COMPETENZE (TABELLA CENTRALE):**
    - **RETRIBUZIONE ORDINARIA (voce 1000):** Colonna "COMPETENZE"
    - **STRAORDINARI:** Somma tutte le voci tipo "STRAORDINARIO", "SUPPLEMENTARI", "NOTTURNI"
    - **FESTIVITA:** Somma voci "MAGG. FESTIVE", "FESTIVITA GODUTA"
    - **ANZIANITA:** Se vedi voci "SCATTI", "EDR", "ANZ." usale, altrimenti 0
    - **LORDO TOTALE:** Cerca riga "TOTALE COMPETENZE" o "PROGRESSIVI" → colonna "TOTALE COMPETENZE"

    **3. TRATTENUTE (SEZIONE I.N.P.S. + IRPEF):**
    - **INPS:** Sezione "IMPONIBILE / TRATTENUTE" → riga sotto "I.N.P.S."
    - **IRPEF NETTA:** Sezione "FISCALI" → riga "TRATTENUTE" sotto "IRPEF CONG."
    - **ADDIZIONALI:** Cerca voci "ADD.REG." e "ADD.COM." (sono rateizzate, non trattenute subito)

    **4. FERIE / PAR (TABELLA IN ALTO A DESTRA):**
    - Compila FERIE e P.A.R. dalle righe RES. PREC., SPETTANTI, FRUITE, SALDO

    **5. TREDICESIMA:**
    - Se nel titolo o nella colonna "Mensilità" c'è "TREDICESIMA" o "13MA" → e_tredicesima = true

    **IMPORTANTE:**
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
    return estrai_con_fallback(file_path, prompt, tipo="busta paga")

def _validate_cartellino(result: dict) -> bool:
    """✅ Anti-hallucination migliorata (non scarta dati validi)"""
    # 1. Se abbiamo dati numerici chiari nel JSON, il risultato è valido
    giorni_reali = float(result.get('giorni_reali', 0) or 0)
    gg_presenza = float(result.get('gg_presenza', 0) or 0)
    ore_ord = float(result.get('ore_ordinarie_0251', 0) or 0)
    
    if giorni_reali > 0 or gg_presenza > 0 or ore_ord > 0:
        return True

    # 2. Controllo testo (Corretto Typo: TIMBRATURE)
    dbg = (result.get("debug_prime_righe") or "")[:4000].upper()
    note = (result.get("note") or "").upper()

    has_timbr = "TIMBRATURE" in dbg
    has_pres = ("GG PRESENZA" in dbg) or ("0265" in dbg)
    has_day = re.search(r"\b[LMGVSD]\d{2}\b", dbg) is not None
    says_empty = "NESSUN DATO" in note or "NESSUN DATO" in dbg

    # Se dice "vuoto" ma ci sono prove del contrario nel testo debug -> Accetta
    if says_empty and (has_timbr or has_pres or has_day):
        return True

    # Se non ha trovato numeri AND non ci sono prove nel testo -> Scarta (riprova con altro modello)
    return has_timbr or has_pres or has_day

def estrai_dati_cartellino(file_path):
    """Estrae dati dal cartellino con prompt accurato"""

    prompt = r"""
    Analizza questo cartellino presenze GOTTARDO S.p.A.

    REGOLE IMPORTANTI (NO ALLUCINAZIONI):
    - Se nel PDF compare "TIMBRATURE" o una tabella con giorni tipo L01/M02/... allora NON è vuoto.
    - Estrai "0265 GG PRESENZA" se presente (numero finale, es. 24,00) -> gg_presenza
    - Estrai i totali ore:
       - Riga "0251 ORE ORDINARIE" -> ore_ordinarie_0251
       - Riga "0253 ORE LAVORATE" -> ore_lavorate_0253

    OUTPUT (solo JSON):
    {
      "giorni_reali": float,
      "gg_presenza": float,
      "ore_ordinarie_riepilogo": float,
      "ore_ordinarie_0251": float,
      "ore_lavorate_0253": float,
      "giorni_senza_badge": float,
      "note": "string",
      "debug_prime_righe": "prime ~30 righe (testo) copiate dal PDF"
    }

    NOTE: Usa il punto come separatore decimale. Se un valore non esiste, metti 0.
    """

    return estrai_con_fallback(file_path, prompt, tipo="cartellino", validate_fn=_validate_cartellino)

# --- PULIZIA FILE ---
def pulisci_file(path_busta, path_cart):
    """Elimina i file PDF scaricati dopo l'analisi"""
    file_eliminati = []

    if path_busta and os.path.exists(path_busta):
        try:
            os.remove(path_busta)
            file_eliminati.append(os.path.basename(path_busta))
        except:
            pass

    if path_cart and os.path.exists(path_cart):
        try:
            os.remove(path_cart)
            file_eliminati.append(os.path.basename(path_cart))
        except:
            pass

    if file_eliminati:
        st.info(f"🗑️ File eliminati: {', '.join(file_eliminati)}")

# --- CORE BOT ---
def scarica_documenti_automatici(mese_nome, anno, username, password, tipo_documento="cedolino"):
    """✅ Bot completo per download documenti"""
    nomi_mesi_it = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
                    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
    try:
        mese_num = nomi_mesi_it.index(mese_nome) + 1
    except:
        return None, None, None

    if tipo_documento == "tredicesima":
        target_busta = f"Tredicesima {anno}"
    else:
        target_busta = f"{mese_nome} {anno}"

    last_day = calendar.monthrange(anno, mese_num)[1]
    d_from_vis = f"01/{mese_num:02d}/{anno}"
    d_to_vis = f"{last_day}/{mese_num:02d}/{anno}"

    work_dir = Path.cwd()
    suffix = "_13" if tipo_documento == "tredicesima" else ""
    path_busta = str(work_dir / f"busta_{mese_num}_{anno}{suffix}.pdf")
    path_cart = str(work_dir / f"cartellino_{mese_num}_{anno}.pdf")

    st_status = st.empty()
    nome_tipo = "Tredicesima" if tipo_documento == "tredicesima" else "Cedolino"
    st_status.info(f"🤖 Bot: {nome_tipo} {mese_nome} {anno}")

    busta_ok = False
    cart_ok = False

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, slow_mo=500, args=['--disable-gpu', '--no-sandbox', '--disable-dev-shm-usage'])
            context = browser.new_context(accept_downloads=True, user_agent="Mozilla/5.0")
            context.set_default_timeout(45000)
            page = context.new_page()

            # LOGIN
            st_status.info("🔐 Login...")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y", wait_until="domcontentloaded")
            page.wait_for_selector('input[type="text"]', timeout=10000)
            page.fill('input[type="text"]', username)
            page.fill('input[type="password"]', password)
            page.press('input[type="password"]', 'Enter')
            time.sleep(3)

            try:
                page.wait_for_selector("text=I miei dati", timeout=15000)
            except:
                st_status.error("❌ Login fallito")
                browser.close()
                return None, None, "LOGIN_FALLITO"

            # DOWNLOAD BUSTA
            st_status.info(f"💰 Download {nome_tipo}...")
            page.click("text=I miei dati")
            page.wait_for_selector("text=Documenti", timeout=10000).click()
            time.sleep(3)

            try:
                page.locator("tr", has=page.locator("text=Cedolino")).locator(".z-image").click(timeout=5000)
            except:
                page.click("text=Cedolino")
            time.sleep(5)

            if tipo_documento == "tredicesima":
                links = page.locator(f"a:has-text('Tredicesima {anno}')")
                if links.count() > 0:
                    with page.expect_download(timeout=20000) as dl: links.first.click()
                    dl.value.save_as(path_busta)
                    busta_ok = True
            else:
                all_links = page.locator("a")
                link_matches = []
                for i in range(all_links.count()):
                    txt = all_links.nth(i).inner_text().strip()
                    if target_busta.lower() in txt.lower() and "13" not in txt:
                        link_matches.append(i)
                if link_matches:
                    with page.expect_download(timeout=20000) as dl: all_links.nth(link_matches[-1]).click()
                    dl.value.save_as(path_busta)
                    busta_ok = True

            # DOWNLOAD CARTELLINO
            if tipo_documento != "tredicesima":
                st_status.info("📅 Download cartellino...")
                page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2", wait_until="domcontentloaded")
                time.sleep(2)
                page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
                time.sleep(2)
                page.evaluate("document.getElementById('lnktab_5_label')?.click()")
                time.sleep(4)
                
                try:
                    dal = page.locator("input[id*='CLRICHIE']").first
                    al = page.locator("input[id*='CLRICHI2']").first
                    dal.fill(d_from_vis); al.fill(d_to_vis)
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last.click(force=True)
                    time.sleep(6)
                    
                    target_cart_row = f"{mese_num:02d}/{anno}"
                    icona = page.locator(f"tr:has-text('{target_cart_row}')").locator("img[src*='search']").first
                    if icona.count() > 0:
                        with context.expect_page(timeout=20000) as popup_info:
                            icona.click()
                        popup = popup_info.value
                        popup_url = popup.url.replace("/js_rev//", "/js_rev/")
                        if "EMBED=y" not in popup_url: popup_url += "&EMBED=y"
                        resp = context.request.get(popup_url)
                        if resp.body()[:4] == b"%PDF":
                            Path(path_cart).write_bytes(resp.body())
                            cart_ok = True
                except: pass

            browser.close()
    except Exception as e:
        st.error(f"Errore bot: {e}")

    return (path_busta if busta_ok else None), (path_cart if cart_ok else None), None

# --- UI STREAMLIT ---
st.set_page_config(page_title="Gottardo Payroll", page_icon="💶", layout="wide")
st.title("💶 Analisi Stipendio & Presenze")

with st.sidebar:
    st.header("🔐 Credenziali")
    username, password = get_credentials()
    if not st.session_state.get('credentials_set'):
        user_in = st.text_input("Username", value=username if username else "")
        pass_in = st.text_input("Password", type="password")
        if st.button("💾 Salva"):
            st.session_state['username'], st.session_state['password'], st.session_state['credentials_set'] = user_in, pass_in, True
            st.rerun()
    else:
        st.success(f"✅ Utente: {st.session_state['username']}")
        if st.button("🔄 Cambia"):
            st.session_state['credentials_set'] = False
            st.rerun()
    
    st.divider()
    if st.session_state.get('credentials_set'):
        st.header("Parametri")
        sel_anno = st.selectbox("Anno", [2024, 2025, 2026], index=1)
        sel_mese = st.selectbox("Mese", ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"], index=11)
        tipo_doc = st.radio("Tipo", ["📄 Cedolino", "🎄 Tredicesima"])
        
        if st.button("🚀 AVVIA ANALISI", type="primary"):
            tipo = "tredicesima" if "Tredicesima" in tipo_doc else "cedolino"
            b, c, err = scarica_documenti_automatici(sel_mese, sel_anno, st.session_state['username'], st.session_state['password'], tipo)
            st.session_state['busta'], st.session_state['cart'], st.session_state['tipo'], st.session_state['done'] = b, c, tipo, False

if st.session_state.get('busta') or st.session_state.get('cart'):
    if not st.session_state.get('done'):
        with st.spinner("🧠 Analisi AI..."):
            st.session_state['db'] = estrai_dati_busta_dettagliata(st.session_state['busta'])
            st.session_state['dc'] = estrai_dati_cartellino(st.session_state['cart']) if st.session_state['cart'] else None
            st.session_state['done'] = True
            pulisci_file(st.session_state['busta'], st.session_state['cart'])

    db, dc = st.session_state.get('db'), st.session_state.get('dc')
    tab1, tab2, tab3 = st.tabs(["💰 Stipendio", "📅 Presenze", "📊 Analisi"])

    with tab1:
        if db:
            col1, col2, col3 = st.columns(3)
            col1.metric("💵 NETTO", f"€ {db['dati_generali']['netto']:.2f}")
            col2.metric("📊 LORDO", f"€ {db['competenze']['lordo_totale']:.2f}")
            col3.metric("📆 GG INPS", int(db['dati_generali']['giorni_pagati']))
            
            with st.expander("🏖️ Ferie e Permessi"):
                st.write(f"**Saldo Ferie:** {db['ferie']['saldo']:.2f} | **Saldo PAR:** {db['par']['saldo']:.2f}")
    
    with tab2:
        if dc:
            st.metric("📅 GG Presenza (Cartellino)", dc.get('gg_presenza', 0))
            st.info(f"**Note:** {dc.get('note', '')}")
    
    with tab3:
        if db and dc:
            pagati = float(db['dati_generali']['giorni_pagati'])
            reali = float(dc.get('gg_presenza') or dc.get('giorni_reali') or 0)
            st.subheader("🔍 Confronto Busta vs Cartellino")
            st.metric("Differenza Giorni", f"{reali - pagati:.1f}", delta=f"{reali-pagati:.1f}")
