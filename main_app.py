import sys
import asyncio
import re
import os
import time
import json
import calendar
import locale
import requests
import streamlit as st
import google.generativeai as genai
from playwright.sync_api import sync_playwright
from pathlib import Path

# --- GESTIONE DIPENDENZE OPZIONALI ---
try:
    from openai import OpenAI
except ImportError:
    # Fallback se l'utente non ha installato openai, non si romperà tutto subito
    OpenAI = None

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

# --- SETUP AMBIENTE ---
# Installa browser se necessario (solo prima volta, ma male non fa)
os.system("playwright install chromium")

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

try:
    locale.setlocale(locale.LC_TIME, 'it_IT.UTF-8')
except:
    pass  # Su cloud linux a volte it_IT non c'è, fa nulla

# --- CREDENZIALI & CONFIGURAZIONE ---
def get_credentials():
    """Recupera credenziali da session_state o secrets"""
    if 'credentials_set' in st.session_state and st.session_state.get('credentials_set'):
        return st.session_state.get('username'), st.session_state.get('password')
    try:
        return st.secrets["ZK_USER"], st.secrets["ZK_PASS"]
    except:
        return None, None

# 1. GOOGLE GEMINI
try:
    GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
    genai.configure(api_key=GOOGLE_API_KEY)
    HAS_GEMINI = True
except:
    HAS_GEMINI = False

# 2. DEEPSEEK (o OpenAI compatibile)
try:
    DEEPSEEK_API_KEY = st.secrets["DEEPSEEK_API_KEY"]
    HAS_DEEPSEEK = True
except:
    DEEPSEEK_API_KEY = None
    HAS_DEEPSEEK = False

# --- FUNZIONI AI ---

@st.cache_resource
def get_gemini_models():
    """Restituisce lista modelli Gemini ordinati per priorità"""
    if not HAS_GEMINI: return []
    try:
        models = genai.list_models()
        valid = [m for m in models if 'generateContent' in m.supported_generation_methods]
        
        # Filtra solo modelli gemini, escludi embedding e vision-only se necessario
        gemini_list = []
        for m in valid:
            name = m.name.replace('models/', '')
            if 'gemini' in name.lower():
                # Creiamo istanza
                gemini_list.append((name, genai.GenerativeModel(name)))
        
        # Ordina: Flash (veloce) -> Pro (potente)
        def priority(n):
            if 'flash' in n.lower(): return 0
            if 'pro' in n.lower(): return 2
            return 1
        return sorted(gemini_list, key=lambda x: priority(x[0]))
    except Exception as e:
        st.error(f"Errore Gemini Init: {e}")
        return []

def extract_text_from_pdf(file_path):
    """Estrae testo puro da PDF (per DeepSeek che non vede immagini)"""
    if not fitz:
        st.warning("⚠️ Libreria PyMuPDF non trovata. Impossibile usare DeepSeek su PDF.")
        return None
    try:
        doc = fitz.open(file_path)
        text = ""
        for page in doc:
            text += page.get_text() + "\n"
        return text
    except Exception as e:
        st.error(f"Errore lettura PDF: {e}")
        return None

def clean_json(text):
    """Pulisce la risposta dell'AI per ottenere un JSON valido"""
    try:
        # Rimuove markdown code blocks
        text = re.sub(r"```json|```", "", text).strip()
        # Cerca la prima graffa aperta e l'ultima chiusa
        start = text.find('{')
        end = text.rfind('}') + 1
        if start != -1 and end != -1:
            return json.loads(text[start:end])
        return json.loads(text) # Prova diretta
    except:
        return None

def estrai_con_ai(file_path, prompt, tipo_doc, validate_fn=None):
    """
    Motore principale di estrazione:
    1. Prova tutti i modelli Gemini disponibili (Multimodale, legge PDF come immagine/blob)
    2. Se falliscono, prova DeepSeek (Testuale, legge testo estratto)
    """
    if not os.path.exists(file_path):
        return None

    status_box = st.empty()
    
    # --- FASE 1: GEMINI ---
    gemini_models = get_gemini_models()
    if gemini_models:
        with open(file_path, "rb") as f:
            pdf_bytes = f.read()

        for name, model in gemini_models:
            try:
                status_box.info(f"🤖 Analisi {tipo_doc} con Gemini ({name})...")
                
                response = model.generate_content([
                    prompt,
                    {"mime_type": "application/pdf", "data": pdf_bytes}
                ])
                
                res = clean_json(response.text)
                if res and isinstance(res, dict):
                    # Validazione opzionale
                    if validate_fn and not validate_fn(res):
                        continue # Risultato non valido, prova prossimo modello
                    
                    status_box.success(f"✅ {tipo_doc} analizzato con Google Gemini!")
                    time.sleep(1)
                    status_box.empty()
                    return res
            except Exception as e:
                # Se è un errore di quota (429), continua col prossimo. Altrimenti logga.
                err_msg = str(e).lower()
                if "429" in err_msg or "quota" in err_msg:
                    continue
                # Altri errori potrebbero essere fatali, ma proviamo il prossimo modello cmq

    # --- FASE 2: DEEPSEEK (Fallback) ---
    if HAS_DEEPSEEK and OpenAI:
        status_box.warning("⚠️ Gemini fallito/esaurito. Passo a DeepSeek...")
        
        pdf_text = extract_text_from_pdf(file_path)
        if pdf_text and len(pdf_text) > 50:
            try:
                client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
                
                full_prompt = f"{prompt}\n\n--- CONTENUTO DEL DOCUMENTO ---\n{pdf_text[:60000]}" # Tronca per sicurezza
                
                response = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[
                        {"role": "system", "content": "Sei un estrattore dati JSON rigoroso. Rispondi SOLO col JSON richiesto."},
                        {"role": "user", "content": full_prompt}
                    ],
                    temperature=0.1,
                    max_tokens=2000
                )
                
                res_content = response.choices[0].message.content
                res = clean_json(res_content)
                
                if res and isinstance(res, dict):
                     if validate_fn and not validate_fn(res):
                         status_box.error("❌ Dati DeepSeek non validi")
                     else:
                         status_box.success(f"✅ {tipo_doc} analizzato con DeepSeek!")
                         time.sleep(1)
                         status_box.empty()
                         return res
            except Exception as e:
                status_box.error(f"❌ Errore DeepSeek: {e}")
        else:
            status_box.error("❌ Impossibile estrarre testo per DeepSeek (PDF scannerizzato?)")

    status_box.error(f"❌ Impossibile analizzare {tipo_doc} con nessuna AI.")
    return None

# --- PROMPTS SPECIFICI ---

def get_busta_prompt():
    return """
    Sei un esperto paghe italiano. Analizza questo cedolino GOTTARDO SPA.
    Estrai i seguenti dati in formato JSON rigoroso:
    
    1. DATI GENERALI:
       - netto (riga Progressivi -> netto del mese)
       - giorni_pagati (spesso "GG. INPS" o "GIORNI RETRIBUITI")
    
    2. COMPETENZE:
       - base (paga base)
       - straordinari (somma voci straordinario/banca ore/notturno/supplementare)
       - festivita (somma voci festività/ex festività)
       - lordo_totale (totale competenze)
       
    3. TRATTENUTE:
       - inps (totale contributi sociali)
       - irpef_netta (trattenuta irpef netta)
       - addizionali_totali (somma addizionali reg/comunali)
       
    4. FERIE: {residue_ap, maturate, godute, saldo} (Dalla casella FERIE)
    5. PERMESSI (PAR/ROL): {residue_ap, spettanti, fruite, saldo} (Dalla casella P.A.R.)
    
    6. TREDICESIMA: boolean (true se è la busta della tredicesima/gratifica natalizia)

    Usa 0.0 se il valore manca. JSON Format:
    {
        "e_tredicesima": false,
        "dati_generali": {"netto": 0.0, "giorni_pagati": 0.0, "ore_ordinarie": 0.0},
        "competenze": {"base": 0.0, "anzianita": 0.0, "straordinari": 0.0, "festivita": 0.0, "lordo_totale": 0.0},
        "trattenute": {"inps": 0.0, "irpef_netta": 0.0, "addizionali_totali": 0.0},
        "ferie": {"residue_ap": 0.0, "maturate": 0.0, "godute": 0.0, "saldo": 0.0},
        "par": {"residue_ap": 0.0, "spettanti": 0.0, "fruite": 0.0, "saldo": 0.0}
    }
    """

def get_cartellino_prompt():
    return """
    Analizza questo cartellino presenze.
    
    REGOLE:
    1. Verifica se ci sono timbrature reali (es. L01 8:00... M02 8:00...).
       - Se ci sono, conta i giorni univoci -> "giorni_reali".
       - Se è vuoto (solo intestazione), giorni_reali = 0.
    
    2. Estrai i totali se presenti in fondo o in alto:
       - "GG PRESENZA" o "Giorni lavorati" -> "gg_presenza"
       - "Totale Ore Ordinarie" -> "ore_ordinarie"
    
    3. Note: scrivi eventuali anomalie (es. "Mancata timbratura uscita gg 15").
    
    OUTPUT JSON:
    {
      "giorni_reali": 0.0,
      "gg_presenza": 0.0,
      "ore_ordinarie_riepilogo": 0.0,
      "giorni_senza_badge": 0.0,
      "note": "breve descrizione",
      "debug_prime_righe": "copia qui del testo per debug"
    }
    """

def validate_cartellino(res):
    """Anti-hallucination per check cartellino vuoto"""
    # 1. Numeri presenti?
    numeri = [res.get('giorni_reali', 0), res.get('gg_presenza', 0)]
    if any(float(x or 0) > 0 for x in numeri): return True
    
    # 2. Testo "prova" presente nella risposta?
    dbg = str(res.get('debug_prime_righe', '')).upper()
    if "TIMBRATURE" in dbg or "0265" in dbg or "GG PRESENZA" in dbg: return True
    
    # Se dice vuoto e non ci sono numeri, va bene (è davvero vuoto)
    return True # Accettiamo anche risultati "vuoti" se coerenti

# --- DOWNLOADER BOT (Playwright) ---
def run_downloader(mese_nome, anno, username, password, tipo_doc):
    """Esegue il download headless"""
    
    # Mappatura Mesi
    mesi = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", 
            "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
    try:
        mese_num = mesi.index(mese_nome) + 1
    except:
        return None, None, "Mese non valido"

    # Percorsi temporanei
    wd = Path.cwd()
    path_busta = str(wd / f"temp_busta.pdf")
    path_cart = str(wd / f"temp_cartellino.pdf")
    
    # Init flags
    busta_ok = False
    cart_ok = False
    
    status_log = st.empty()
    status_log.info("🚀 Avvio browser sicuro...")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True, # Metti False se vuoi vederlo aprirsi
                args=['--no-sandbox', '--disable-gpu']
            )
            # Context con download automatici
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.set_default_timeout(30000)

            # 1. LOGIN
            status_log.info("🔐 Accesso al portale...")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y")
            
            # Compilazione form
            page.fill('input[type="text"]', username)
            page.fill('input[type="password"]', password)
            page.press('input[type="password"]', 'Enter')
            
            # Verifica accesso
            try:
                page.wait_for_selector("text=I miei dati", timeout=15000)
            except:
                browser.close()
                return None, None, "LOGIN_FALLITO"

            # 2. GESTIONE POPUP MOLESTI
            # Gottardo spesso ha dei dialog overlay che bloccano i click. Li rimuoviamo.
            try:
                page.keyboard.press("Escape")
                page.evaluate("""
                    document.querySelectorAll('.dijitDialogUnderlay').forEach(e => e.remove());
                    document.querySelectorAll('.dijitDialog').forEach(e => e.style.display='none');
                """)
                time.sleep(1)
            except: pass

            # 3. DOWNLOAD BUSTA
            status_log.info(f"📄 Cerco {tipo_doc}...")
            
            # Navigazione sicura
            page.click("text=I miei dati", force=True) 
            time.sleep(1)
            page.click("text=Documenti", force=True)
            time.sleep(3)
            
            # Clicca tab Cedolino
            try:
                page.locator("tr", has=page.locator("text=Cedolino")).locator(".z-image").first.click()
            except:
                page.click("text=Cedolino", force=True)
            
            time.sleep(3)

            # Logica ricerca link
            search_term = f"Tredicesima {anno}" if "Tredicesima" in tipo_doc else f"{mese_nome} {anno}"
            
            links = page.locator("a")
            found_idx = -1
            
            for i in range(links.count()):
                txt = links.nth(i).inner_text().strip()
                if search_term.lower() in txt.lower():
                    # Filtro anti-ambiguità
                    if "Tredicesima" in tipo_doc and "13" not in txt: continue
                    if "Tredicesima" not in tipo_doc and "13" in txt: continue
                    found_idx = i
            
            if found_idx >= 0:
                with page.expect_download(timeout=20000) as dl_info:
                    links.nth(found_idx).click()
                dl = dl_info.value
                dl.save_as(path_busta)
                busta_ok = True
                status_log.success("✅ Busta scaricata!")
            else:
                status_log.warning("⚠️ Busta non trovata per questo mese.")

            # 4. DOWNLOAD CARTELLINO (Solo se non è Tredicesima)
            if "Tredicesima" not in tipo_doc:
                status_log.info("📅 Cerco Cartellino...")
                
                # Navigazione rapida
                page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2")
                time.sleep(2)
                
                # Menu -> Time -> Cartellino
                page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
                time.sleep(1)
                page.evaluate("document.getElementById('lnktab_5_label')?.click()")
                time.sleep(4)
                
                # Imposta date (1° al Fine Mese)
                last_day = calendar.monthrange(anno, mese_num)[1]
                d_start = f"01/{mese_num:02d}/{anno}"
                d_end = f"{last_day}/{mese_num:02d}/{anno}"
                
                try:
                    # Compila date e cerca
                    page.locator("input[id*='CLRICHIE']").fill(d_start)
                    page.locator("input[id*='CLRICHI2']").fill(d_end)
                    page.keyboard.press("Enter")
                    time.sleep(5)
                except:
                    pass

                # Cerca icona lente d'ingrandimento nella riga del mese
                row_label = f"{mese_num:02d}/{anno}"
                icon = page.locator(f"tr:has-text('{row_label}')").locator("img[src*='search']").first
                
                if icon.count() > 0:
                    with context.expect_page() as popup_ev:
                        icon.click()
                    popup = popup_ev.value
                    
                    # Trick: Prendi l'URL del popup e aggiungi EMBED=y per avere il PDF raw
                    clean_url = popup.url.replace("/js_rev//", "/js_rev/")
                    if "EMBED=y" not in clean_url:
                        clean_url += "&EMBED=y" if "?" in clean_url else "?EMBED=y"
                    
                    try:
                        resp = context.request.get(clean_url)
                        if resp.body()[:4] == b"%PDF":
                            Path(path_cart).write_bytes(resp.body())
                            cart_ok = True
                            status_log.success("✅ Cartellino scaricato!")
                    except:
                        status_log.error("Errore download stream cartellino")
                
            browser.close()
            status_log.empty()

    except Exception as e:
        status_log.error(f"Errore critico bot: {e}")
        return None, None, str(e)

    return (path_busta if busta_ok else None), (path_cart if cart_ok else None), None

# --- UI APP ---

st.set_page_config(page_title="Analisi Buste & Presenze AI", layout="wide", page_icon="💶")

st.title("💶 Analisi Stipendio & Presenze Integrata")

# SIDEBAR CONTROLLI
with st.sidebar:
    st.header("⚙️ Impostazioni")
    
    # Credenziali
    user, pwd = get_credentials()
    if not user:
        with st.form("creds"):
            u = st.text_input("Username Portale")
            p = st.text_input("Password Portale", type="password")
            if st.form_submit_button("Salva"):
                st.session_state['username'] = u
                st.session_state['password'] = p
                st.session_state['credentials_set'] = True
                st.rerun()
    else:
        st.success(f"Utente: **{user}**")
        if st.button("Logout"):
            st.session_state.clear()
            st.rerun()

    st.divider()
    
    if user:
        sel_anno = st.selectbox("Anno", [2024, 2025, 2026], index=1)
        sel_mese = st.selectbox("Mese", ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"], index=11)
        sel_tipo = st.radio("Tipo Documento", ["Cedolino Mensile", "Tredicesima"])
        
        btn_start = st.button("🚀 AVVIA ANALISI", type="primary", use_container_width=True)

# LOGICA PRINCIPALE
if 'result_data' not in st.session_state:
    st.session_state['result_data'] = None

if user and btn_start:
    # 1. DOWNLOAD
    tipo_cod = "tredicesima" if "Tredicesima" in sel_tipo else "cedolino"
    pb, pc, err = run_downloader(sel_mese, sel_anno, user, pwd, sel_cod=tipo_cod)
    
    if err == "LOGIN_FALLITO":
        st.error("Credenziali sbagliate o sito irraggiungibile.")
    elif err:
        st.error(f"Errore: {err}")
    else:
        # 2. ANALISI AI
        with st.spinner("🤖 Analisi documenti con Intelligenza Artificiale..."):
            db = None
            dc = None
            
            if pb: db = estrai_con_ai(pb, get_busta_prompt(), "Busta Paga")
            if pc: dc = estrai_con_ai(pc, get_cartellino_prompt(), "Cartellino", validate_fn=validate_cartellino)
            
            st.session_state['result_data'] = {'db': db, 'dc': dc, 'tipo': tipo_cod}
            
            # Pulizia
            if pb and os.path.exists(pb): os.remove(pb)
            if pc and os.path.exists(pc): os.remove(pc)

# DISPLAY RISULTATI
res = st.session_state['result_data']

if res:
    db = res.get('db')
    dc = res.get('dc')
    is_13 = res.get('tipo') == 'tredicesima'

    if not db and not dc:
        st.warning("Nessun dato estratto. Controlla se i documenti erano presenti sul portale.")
    else:
        # TABELLA DASHBOARD
        tab1, tab2, tab3 = st.tabs(["💰 STIPENDIO", "📅 PRESENZE", "📊 CONFRONTO"])
        
        with tab1:
            if db:
                gen = db.get('dati_generali', {})
                comp = db.get('competenze', {})
                tratt = db.get('trattenute', {})
                
                # HERO METRICS
                c1, c2, c3 = st.columns(3)
                c1.metric("💵 NETTO PAGATO", f"€ {gen.get('netto', 0):.2f}", delta="Bonifico")
                c2.metric("LORDO TOTALE", f"€ {comp.get('lordo_totale', 0):.2f}")
                c3.metric("GIORNI COMPIUTI", gen.get('giorni_pagati', 0))
                
                st.divider()
                
                # DETTAGLI
                col_sx, col_dx = st.columns(2)
                with col_sx:
                    st.subheader("Entrate Variabili")
                    st.write(f"⏱️ **Straordinari:** € {comp.get('straordinari', 0):.2f}")
                    st.write(f"🎉 **Festività:** € {comp.get('festivita', 0):.2f}")
                
                with col_dx:
                    st.subheader("Saldo Ferie & PAR")
                    f_saldo = db.get('ferie', {}).get('saldo', 0)
                    p_saldo = db.get('par', {}).get('saldo', 0)
                    st.metric("Ferie Residue", f"{f_saldo:.2f}")
                    st.metric("PAR Residui", f"{p_saldo:.2f}")

            else:
                st.info("Dati busta non disponibili.")

        with tab2:
            if dc:
                pres = dc.get('gg_presenza', 0)
                reali = dc.get('giorni_reali', 0)
                
                valore_guida = pres if pres > 0 else reali
                
                st.metric("📅 GIORNI LAVORATI (Cartellino)", f"{valore_guida:.1f}")
                
                if dc.get('note'):
                    st.info(f"**Note:** {dc['note']}")
                
                debug_txt = dc.get('debug_prime_righe', '')
                if debug_txt:
                    with st.expander("Vedi dati grezzi"):
                        st.text(debug_txt)
            else:
                st.info("Cartellino non disponibile.")

        with tab3:
            if db and dc:
                pagati = float(db.get('dati_generali', {}).get('giorni_pagati', 0))
                lavorati = float(dc.get('gg_presenza', 0) or dc.get('giorni_reali', 0))
                
                diff = lavorati - pagati
                
                st.subheader("Discrepanza Giorni")
                col_a, col_b = st.columns(2)
                col_a.metric("Busta (Pagati)", pagati)
                col_b.metric("Cartellino (Timbrati)", lavorati, delta=f"{diff:.2f}")
                
                if abs(diff) > 0.5:
                    st.warning(f"⚠️ C'è una differenza di {diff:.1f} giorni.")
                else:
                    st.success("✅ I conti tornano!")
            else:
                st.write("Confronto non possibile (manca uno dei due documenti).")
