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
except Exception as e:
    st.error(f"❌ Google API Key mancante in secrets")
    st.stop()

# ✅ AUTO-DISCOVERY MODELLI GEMINI
genai.configure(api_key=GOOGLE_API_KEY)

@st.cache_resource
def inizializza_modelli_gemini():
    """Auto-discovery: Scopre automaticamente tutti i modelli Gemini disponibili"""
    try:
        tutti_modelli = genai.list_models()
        
        modelli_validi = [
            m for m in tutti_modelli 
            if 'generateContent' in m.supported_generation_methods
        ]
        
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
        
        # Ordina per preferenza
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

def estrai_con_fallback(file_path, prompt, tipo="documento"):
    """✅ Prova multipli modelli Gemini con fallback automatico"""
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
                progress_placeholder.success(f"✅ {tipo.capitalize()} analizzato!")
                time.sleep(1)
                progress_placeholder.empty()
                return result
                
        except Exception as e:
            error_msg = str(e)
            
            if "429" in error_msg or "quota" in error_msg.lower() or "resource_exhausted" in error_msg.lower():
                continue
            else:
                continue
    
    progress_placeholder.error(f"❌ Analisi {tipo} fallita (quote esaurite)")
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
    - Se nel titolo o nella colonna "Mensilità" c'è "TREDICESIMA" o "13MA" → è_tredicesima = true
    - Altrimenti → è_tredicesima = false

    **IMPORTANTE:**
    - Usa SEMPRE i valori dalle colonne corrette
    - Se un valore non esiste scrivi 0
    - Usa il punto come separatore decimale
    
    Restituisci SOLO questo JSON (niente testo aggiuntivo):
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

def estrai_dati_cartellino(file_path):
    """Estrae dati dal cartellino - CON DEBUG INTEGRATO"""
    
    prompt = """
    Sei un esperto nell'analisi di cartellini presenze GOTTARDO S.p.A.

    **IL TUO COMPITO:**
    Analizza questo PDF e conta QUANTI GIORNI sono stati lavorati guardando le TIMBRATURE o le DATE presenti.

    **FORMATO TIPICO GOTTARDO:**
    
    Il PDF può avere:
    
    TIPO A - TABELLA DETTAGLIATA:
    ```
    Data        Giorno    Entrata  Uscita   Ore    Note
    01/12/2025  Lunedì    08:00    17:00    8:00   
    02/12/2025  Martedì   08:05    16:58    7:53   
    03/12/2025  Mercoledì 08:02    17:01    8:01   
    ...
    ```
    → Se vedi questo formato, conta TUTTE le righe con una data
    
    TIPO B - LISTA DATE:
    ```
    Periodo: 01/12/2025 - 31/12/2025
    
    Timbrature rilevate:
    01/12/2025 - 8h
    02/12/2025 - 8h
    03/12/2025 - 8h
    ...
    ```
    → Conta quante date/giorni vedi elencati
    
    TIPO C - VUOTO (SOLO INTESTAZIONE):
    ```
    Periodo: 01/12/2025 - 31/12/2025
    Nessun dato disponibile
    ```
    → giorni_reali = 0

    **REGOLE:**
    1. Cerca qualsiasi pattern di date tipo DD/MM/YYYY o DD-MM-YYYY
    2. Se trovi 20+ date → è un cartellino completo normale
    3. Se trovi 0-5 date → probabilmente è vuoto
    4. Conta come anomalie solo giorni con scritte tipo "badge non rilevato", "errore", "anomalia"

    **OUTPUT RICHIESTO:**
    {
        "giorni_reali": <numero di giorni lavorati rilevati>,
        "giorni_senza_badge": <numero anomalie>,
        "note": "<descrizione di cosa hai trovato>",
        "debug_prime_righe": "<copia le prime 15 righe del PDF qui per debug>"
    }

    **ESEMPI DI NOTE:**
    - "Rilevate 26 timbrature complete da 01/12 a 30/12"
    - "Trovate 22 date nel periodo, 2 con badge mancante"
    - "Il PDF mostra solo la schermata dei parametri di ricerca e un raggruppamento per il mese 12/2025. Non è presente l'elenco dettagliato delle timbrature giornaliere (Tipo C)."
    
    **IMPORTANTE PER DEBUG:**
    Nel campo "debug_prime_righe" copia ESATTAMENTE le prime 10-15 righe di testo che vedi nel PDF,
    anche se sembrano header o intestazioni. Questo ci serve per capire il formato.
    """
    
    result = estrai_con_fallback(file_path, prompt, tipo="cartellino")
    
    # Mostra debug se disponibile
    if result and 'debug_prime_righe' in result:
        with st.expander("🔍 DEBUG: Prime righe estratte dall'AI"):
            st.text(result['debug_prime_righe'])
            
            # Conta date nel testo debug
            date_trovate = re.findall(r'\d{2}/\d{2}/\d{4}', result['debug_prime_righe'])
            st.info(f"📊 Date trovate nel testo: **{len(date_trovate)}**")
            
            if len(date_trovate) > 0:
                st.write("**Prime 5 date:**")
                for data in date_trovate[:5]:
                    st.code(data)
    
    return result

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
    """✅ Bot con gestione duplicati Dicembre + fix download cartellino"""
    nomi_mesi_it = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", 
                    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
    try: mese_num = nomi_mesi_it.index(mese_nome) + 1
    except: return None, None, None

    if tipo_documento == "tredicesima":
        target_busta = f"Tredicesima {anno}"
    else:
        target_busta = f"{mese_nome} {anno}"
    
    target_cart_row = f"{mese_num:02d}/{anno}"
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
            page.fill('input[type="text"]', username)
            page.fill('input[type="password"]', password)
            page.press('input[type="password"]', 'Enter')
            
            time.sleep(3)
            
            try:
                page.wait_for_selector("text=I miei dati", timeout=15000)
                st_status.info("✅ Login OK")
            except:
                st_status.error("❌ Login fallito")
                browser.close()
                return None, None, "LOGIN_FALLITO"

            # BUSTA PAGA
            st_status.info(f"💰 Download {nome_tipo}...")
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
                    if tipo_documento == "tredicesima":
                        links = page.locator(f"a:has-text('Tredicesima {anno}')")
                        if links.count() > 0:
                            with page.expect_download(timeout=20000) as dl:
                                links.first.click()
                            dl.value.save_as(path_busta)
                            if os.path.exists(path_busta):
                                busta_ok = True
                                st_status.success(f"✅ Tredicesima scaricata")
                    else:
                        all_links = page.locator("a")
                        total_links = all_links.count()
                        link_matches = []
                        
                        for i in range(total_links):
                            try:
                                txt = all_links.nth(i).inner_text().strip()
                                if not txt or len(txt) < 3:
                                    continue
                                if any(mese in txt for mese in nomi_mesi_it) and str(anno) in txt:
                                    ha_target = target_busta.lower() in txt.lower()
                                    e_tredicesima = any(kw in txt for kw in ["Tredicesima", "13", "XIII"])
                                    if ha_target and not e_tredicesima:
                                        link_matches.append((i, txt))
                            except:
                                continue
                        
                        if len(link_matches) > 0:
                            link_index, link_txt = link_matches[-1]
                            
                            try:
                                with page.expect_download(timeout=20000) as download_info:
                                    all_links.nth(link_index).click()
                                download = download_info.value
                                download.save_as(path_busta)
                                if os.path.exists(path_busta):
                                    busta_ok = True
                                    st_status.success(f"✅ Cedolino scaricato")
                            except Exception as dl_err:
                                st.error(f"❌ Errore download: {dl_err}")
                except Exception as e:
                    st.error(f"❌ Errore: {e}")
                    
            except Exception as e: 
                st.error(f"Errore: {e}")

            # CARTELLINO - ✅ FIX COMPLETO
            if tipo_documento != "tredicesima":
                st_status.info("📅 Download cartellino...")
                try:
                    page.evaluate("window.scrollTo(0, 0)")
                    time.sleep(2)
                    try: 
                        page.keyboard.press("Escape")
                        time.sleep(1)
                    except: 
                        pass
                    
                    # Torna alla home
                    try:
                        logo = page.locator("img[src*='logo'], .logo").first
                        if logo.is_visible(timeout=2000): 
                            logo.click()
                            time.sleep(2)
                    except:
                        page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2", wait_until="domcontentloaded")
                        time.sleep(3)
                    
                    # Vai su Time
                    page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
                    time.sleep(3)
                    
                    # Vai su Cartellino presenze
                    page.evaluate("document.getElementById('lnktab_5_label')?.click()")
                    time.sleep(5)
                    
                    # Chiudi eventuali popup
                    if page.locator("text=Permessi del").count() > 0:
                        try: 
                            page.locator(".z-icon-print").first.click()
                            time.sleep(3)
                        except: 
                            pass
                    
                    # Imposta date
                    try:
                        dal = page.locator("input[id*='CLRICHIE'][class*='dijitInputInner']").first
                        al = page.locator("input[id*='CLRICHI2'][class*='dijitInputInner']").first
                        
                        if dal.count() > 0 and al.count() > 0:
                            dal.click(force=True)
                            page.keyboard.press("Control+A")
                            dal.fill("")
                            dal.type(d_from_vis, delay=100)
                            dal.press("Tab")
                            time.sleep(1)
                            
                            al.click(force=True)
                            page.keyboard.press("Control+A")
                            al.fill("")
                            al.type(d_to_vis, delay=100)
                            al.press("Tab")
                            time.sleep(1)
                    except Exception as e:
                        st.warning(f"⚠️ Errore date: {e}")
                    
                    # Esegui ricerca
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(1)
                    
                    try: 
                        page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last.click(force=True)
                    except: 
                        page.keyboard.press("Enter")
                    
                    # Aspetta risultati
                    time.sleep(5)
                    
                    try:
                        page.wait_for_selector("text=Risultati della ricerca", timeout=10000)
                        st.info("✅ Risultati ricerca trovati")
                    except:
                        st.warning("⚠️ Timeout risultati")
                    
                    # ✅ CERCA LA RIGA CON IL MESE CORRETTO
                    target_cart_text = f"{mese_num}/{str(anno)[-2:]}"  # Es: "12/25"
                    st.info(f"🔍 Ricerca riga: {target_cart_text}")
                    
                    try:
                        riga_target = page.locator(f"tr:has-text('{target_cart_text}')").first
                        
                        if riga_target.count() > 0:
                            st.success(f"✅ Riga trovata")
                            
                            # Clicca sull'icona search nella riga
                            try:
                                icona_search = riga_target.locator("img[src*='search']").first
                                
                                if icona_search.count() > 0:
                                    st.info("📥 Click su dettaglio...")
                                    old_url = page.url
                                    
                                    icona_search.click()
                                    time.sleep(5)
                                    
                                    new_url = page.url
                                    
                                    if new_url != old_url:
                                        st.success(f"✅ Nuova pagina aperta")
                                        
                                        # Aspetta caricamento
                                        try:
                                            page.wait_for_selector("text=Caricamento in corso", state="hidden", timeout=15000)
                                        except:
                                            time.sleep(3)
                                        
                                        # Scarica PDF
                                        try:
                                            cs = {c['name']: c['value'] for c in context.cookies()}
                                            response = requests.get(new_url, cookies=cs, timeout=30)
                                            
                                            if b'%PDF' in response.content[:10]:
                                                with open(path_cart, 'wb') as f:
                                                    f.write(response.content)
                                                st.success("✅ PDF via HTTP")
                                            else:
                                                page.pdf(path=path_cart)
                                                st.success("✅ PDF generato")
                                        except Exception as e:
                                            st.warning(f"⚠️ Tentativo 1 fallito: {e}")
                                            page.pdf(path=path_cart)
                                            st.success("✅ PDF fallback")
                                    else:
                                        st.warning("⚠️ URL non cambiato")
                                        time.sleep(3)
                                        page.pdf(path=path_cart)
                                else:
                                    st.error("❌ Icona search non trovata")
                            except Exception as e:
                                st.error(f"❌ Errore click: {e}")
                        else:
                            st.warning(f"⚠️ Riga '{target_cart_text}' non trovata")
                            # Fallback: prima icona
                            try:
                                page.locator("img[src*='search16.png']").first.click()
                                time.sleep(5)
                                page.pdf(path=path_cart)
                            except:
                                pass
                    except Exception as e:
                        st.error(f"❌ Errore ricerca riga: {e}")
                    
                    # Verifica file
                    if os.path.exists(path_cart):
                        size = os.path.getsize(path_cart)
                        
                        if size > 5000:  # Almeno 5KB
                            cart_ok = True
                            st_status.success(f"✅ Cartellino OK ({size} bytes)")
                        else:
                            st.warning(f"⚠️ PDF piccolo ({size} bytes)")
                    else:
                        st.error("❌ File non trovato")

                except Exception as e:
                    st.error(f"❌ Errore cartellino: {e}")

            browser.close()
            
    except Exception as e:
        st.error(f"Errore generale: {e}")
    
    final_busta = path_busta if busta_ok else None
    final_cart = path_cart if cart_ok else None
    
    return final_busta, final_cart, None

# --- UI ---
st.set_page_config(page_title="Gottardo Payroll", page_icon="💶", layout="wide")
st.title("💶 Analisi Stipendio & Presenze")

# SIDEBAR
with st.sidebar:
    st.header("🔐 Credenziali")
    
    username, password = get_credentials()
    
    if not st.session_state.get('credentials_set'):
        st.info("Inserisci le tue credenziali Gottardo SelfService")
        
        input_user = st.text_input("Username", value=username if username else "", key="input_user")
        input_pass = st.text_input("Password", type="password", value="", key="input_pass")
        
        if st.button("💾 Salva Credenziali"):
            if input_user and input_pass:
                st.session_state['username'] = input_user
                st.session_state['password'] = input_pass
                st.session_state['credentials_set'] = True
                st.success("✅ Credenziali salvate!")
                st.rerun()
            else:
                st.error("⚠️ Inserisci username e password")
    else:
        st.success(f"✅ Loggato: **{st.session_state['username']}**")
        if st.button("🔄 Cambia Credenziali"):
            st.session_state['credentials_set'] = False
            st.session_state.pop('username', None)
            st.session_state.pop('password', None)
            st.rerun()
    
    st.divider()
    
    if st.session_state.get('credentials_set'):
        st.header("Parametri")
        sel_anno = st.selectbox("Anno", [2024, 2025, 2026], index=1)
        sel_mese = st.selectbox("Mese", ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", 
                                         "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"], index=11)
        
        tipo_doc = st.radio(
            "Tipo documento",
            ["📄 Cedolino Mensile", "🎄 Tredicesima"],
            index=0
        )
        
        if st.button("🚀 AVVIA ANALISI", type="primary", use_container_width=True):
            for key in ['busta', 'cart', 'db', 'dc', 'done']:
                st.session_state.pop(key, None)
            
            tipo = "tredicesima" if "Tredicesima" in tipo_doc else "cedolino"
            username = st.session_state.get('username')
            password = st.session_state.get('password')
            
            busta, cart, errore = scarica_documenti_automatici(sel_mese, sel_anno, username, password, tipo_documento=tipo)
            
            if errore == "LOGIN_FALLITO":
                st.error("❌ LOGIN FALLITO")
                st.stop()
            
            st.session_state['busta'] = busta
            st.session_state['cart'] = cart
            st.session_state['tipo'] = tipo
            st.session_state['done'] = False
    else:
        st.warning("⚠️ Inserisci le credenziali")

# ANALISI
if st.session_state.get('busta') or st.session_state.get('cart'):
    
    if not st.session_state.get('done'):
        with st.spinner("🧠 Analisi AI in corso..."):
            db = estrai_dati_busta_dettagliata(st.session_state.get('busta'))
            dc = estrai_dati_cartellino(st.session_state.get('cart')) if st.session_state.get('cart') else None
            st.session_state['db'] = db
            st.session_state['dc'] = dc
            st.session_state['done'] = True
            
            pulisci_file(st.session_state.get('busta'), st.session_state.get('cart'))
            st.session_state.pop('busta', None)
            st.session_state.pop('cart', None)

    db = st.session_state.get('db')
    dc = st.session_state.get('dc')
    tipo = st.session_state.get('tipo', 'cedolino')
    
    if db and db.get('e_tredicesima'):
        st.success("🎄 **Cedolino TREDICESIMA**")
    
    st.divider()
    
    tab1, tab2, tab3 = st.tabs(["💰 Dettaglio Stipendio", "📅 Cartellino & Presenze", "📊 Analisi & Confronto"])
    
    with tab1:
        if db:
            dg = db.get('dati_generali', {})
            comp = db.get('competenze', {})
            tratt = db.get('trattenute', {})
            ferie = db.get('ferie', {})
            par = db.get('par', {})

            k1, k2, k3 = st.columns(3)
            k1.metric("💵 NETTO IN BUSTA", f"€ {dg.get('netto', 0):.2f}", delta="Pagamento")
            k2.metric("📊 Lordo Totale", f"€ {comp.get('lordo_totale', 0):.2f}")
            k3.metric("📆 Giorni Pagati", int(dg.get('giorni_pagati', 0)))

            st.markdown("---")
            
            c_entr, c_usc = st.columns(2)
            with c_entr:
                st.subheader("➕ Competenze")
                st.write(f"**Paga Base:** € {comp.get('base', 0):.2f}")
                if comp.get('anzianita', 0) > 0:
                    st.write(f"**Anzianità:** € {comp.get('anzianita', 0):.2f}")
                if comp.get('straordinari', 0) > 0:
                    st.write(f"**Straordinari/Suppl.:** € {comp.get('straordinari', 0):.2f}")
                if comp.get('festivita', 0) > 0:
                    st.write(f"**Festività/Maggiorazioni:** € {comp.get('festivita', 0):.2f}")

            with c_usc:
                st.subheader("➖ Trattenute")
                st.write(f"**Contributi INPS:** € {tratt.get('inps', 0):.2f}")
                st.write(f"**IRPEF Netta:** € {tratt.get('irpef_netta', 0):.2f}")
                if tratt.get('addizionali_totali', 0) > 0:
                    st.write(f"**Addizionali:** € {tratt.get('addizionali_totali', 0):.2f}")

            with st.expander("🏖️ Situazione Ferie"):
                f1, f2, f3, f4 = st.columns(4)
                f1.metric("Residue AP", f"{ferie.get('residue_ap', 0):.2f}")
                f2.metric("Maturate", f"{ferie.get('maturate', 0):.2f}")
                f3.metric("Godute", f"{ferie.get('godute', 0):.2f}")
                saldo_f = ferie.get('saldo', 0)
                f4.metric("✅ SALDO", f"{saldo_f:.2f}", delta="OK" if saldo_f >= 0 else "Negativo")
            
            with st.expander("⏱️ Situazione Permessi"):
                p1, p2, p3, p4 = st.columns(4)
                p1.metric("Residue AP", f"{par.get('residue_ap', 0):.2f}")
                p2.metric("Spettanti", f"{par.get('spettanti', 0):.2f}")
                p3.metric("Fruite", f"{par.get('fruite', 0):.2f}")
                saldo_p = par.get('saldo', 0)
                p4.metric("✅ SALDO", f"{saldo_p:.2f}", delta="OK" if saldo_p >= 0 else "Negativo")
        else:
            st.warning("⚠️ Dati busta non disponibili")

    with tab2:
        if dc:
            c1, c2 = st.columns([1, 2])
            with c1:
                giorni_reali = dc.get('giorni_reali', 0)
                if giorni_reali > 0:
                    st.metric("📅 Giorni Lavorati", giorni_reali)
                else:
                    st.metric("📅 Giorni Lavorati", "N/D", help="Timbrature non disponibili")
                
                anomalie = dc.get('giorni_senza_badge', 0)
                if anomalie > 0:
                    st.metric("⚠️ Anomalie Badge", anomalie, delta="Controlla")
                else:
                    st.metric("✅ Anomalie Badge", 0, delta="OK")
            
            with c2:
                note = dc.get('note', '')
                st.info(f"**📝 Note:** {note}")
        else:
            if tipo == "tredicesima":
                st.warning("⚠️ Cartellino non disponibile (Tredicesima)")
            else:
                st.error("❌ Errore cartellino")

    with tab3:
        if db and dc:
            pagati = float(db.get('dati_generali', {}).get('giorni_pagati', 0))
            reali = float(dc.get('giorni_reali', 0))
            
            st.subheader("🔍 Analisi Discrepanze")
            
            if reali == 0:
                st.info("ℹ️ **Cartellino senza timbrature dettagliate**")
                st.write(f"📋 Giorni pagati in busta: **{int(pagati)}**")
                st.success("✅ Usa i giorni pagati dalla busta come riferimento")
            else:
                diff = reali - pagati
                
                col_a, col_b = st.columns(2)
                col_a.metric("Giorni Pagati (Busta)", pagati)
                col_b.metric("Giorni Lavorati (Cartellino)", reali, delta=f"{diff:.1f}")
                
                st.markdown("---")
                
                if abs(diff) < 0.5:
                    st.success("✅ **Perfetto!** Giorni lavorati = giorni pagati")
                elif diff > 0:
                    st.info(f"ℹ️ Hai lavorato **{diff:.1f} giorni in più**\n\n"
                           f"Controlla Straordinari: € {db.get('competenze', {}).get('straordinari', 0):.2f}")
                else:
                    st.warning(f"⚠️ **{abs(diff):.1f} giorni pagati in più**\n\n"
                              f"Possibili cause:\n"
                              f"- Ferie godute: {db.get('ferie', {}).get('godute', 0):.2f} giorni\n"
                              f"- Permessi: {db.get('par', {}).get('fruite', 0):.2f} ore")
        elif tipo == "tredicesima":
            st.info("ℹ️ Analisi non disponibile per Tredicesima")
        else:
            st.warning("⚠️ Servono entrambi i documenti")
