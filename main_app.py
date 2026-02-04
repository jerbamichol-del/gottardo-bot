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
import base64

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

# Groq API Key (fallback)
try:
    GROQ_API_KEY = st.secrets.get("GROQ_API_KEY", None)
except:
    GROQ_API_KEY = None

genai.configure(api_key=GOOGLE_API_KEY)
try: 
    model = genai.GenerativeModel('gemini-flash-latest')
except: 
    model = genai.GenerativeModel('gemini-1.5-flash')

# --- CONVERSIONE PDF -> IMMAGINI PER GROQ ---
def pdf_to_images(pdf_path):
    """Converte PDF in immagini per Groq"""
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(pdf_path)
        images = []
        
        for page_num in range(len(doc)):
            page = doc[page_num]
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # 2x zoom per qualità
            img_bytes = pix.tobytes("png")
            img_base64 = base64.b64encode(img_bytes).decode()
            images.append(img_base64)
        
        doc.close()
        return images
    except ImportError:
        st.error("❌ PyMuPDF non installato. Aggiungi 'pymupdf' a requirements.txt")
        return None
    except Exception as e:
        st.error(f"❌ Errore conversione PDF: {e}")
        return None

# --- PARSING AI ---
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
    
    # ✅ TENTATIVO 1: GEMINI
    try:
        with open(file_path, "rb") as f: 
            bytes_data = f.read()
        
        response = model.generate_content([prompt, {"mime_type": "application/pdf", "data": bytes_data}])
        return clean_json_response(response.text)
        
    except Exception as e:
        error_msg = str(e)
        
        # ✅ TENTATIVO 2: GROQ FALLBACK
        if ("429" in error_msg or "quota" in error_msg.lower()) and GROQ_API_KEY:
            st.warning("⚠️ Quota Gemini esaurita per busta paga, uso Groq...")
            return estrai_dati_busta_groq(file_path, prompt)
        else:
            st.error(f"❌ Err busta AI: {e}")
            return None

def estrai_dati_busta_groq(file_path, prompt):
    """Fallback Groq per busta paga"""
    try:
        from groq import Groq
        
        # Converti PDF in immagini
        images = pdf_to_images(file_path)
        if not images:
            return None
        
        client = Groq(api_key=GROQ_API_KEY)
        
        # Usa solo prima pagina (busta paga di solito è 1 pagina)
        content = [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{images[0]}"
                }
            }
        ]
        
        response = client.chat.completions.create(
            model="llama-3.2-90b-vision-preview",
            messages=[{"role": "user", "content": content}],
            temperature=0.1,
            max_tokens=2048
        )
        
        result = clean_json_response(response.choices[0].message.content)
        if result:
            st.success("✅ Analisi busta completata con Groq")
        return result
        
    except ImportError:
        st.error("❌ Libreria 'groq' non installata. Aggiungi a requirements.txt")
        return None
    except Exception as e:
        st.error(f"❌ Errore Groq busta: {e}")
        return None

def estrai_dati_cartellino(file_path):
    if not file_path or not os.path.exists(file_path):
        return None
    
    prompt = """
    Questo PDF è un cartellino presenze o una ricerca.
    
    **ANALISI RICHIESTA:**
    
    1. **SE vedi una TABELLA DETTAGLIATA con timbrature giornaliere:**
       - Conta SOLO i giorni che hanno almeno UNA timbratura valida (entrata/uscita)
       - NON contare sabati/domeniche/festività se non hanno timbrature
       - giorni_reali = numero di giorni con timbrature
       - giorni_senza_badge = giorni con anomalie (badge mancante, errori)
       - note = "Analisi da timbrature dettagliate"
    
    2. **SE vedi SOLO "Periodo: XX/XX/XXXX - YY/YY/YYYY" SENZA tabella timbrature:**
       - NON contare tutti i giorni del calendario
       - giorni_reali = 0
       - giorni_senza_badge = 0
       - note = "Cartellino senza timbrature dettagliate. Impossibile determinare i giorni lavorati effettivi. Usa i giorni pagati dalla busta paga come riferimento."
    
    **IMPORTANTE:**
    - Non inventare dati se non ci sono timbrature
    - Considera che un mese lavorativo tipico ha circa 20-23 giorni (esclusi weekend/festività)
    - Se il documento è vuoto o non contiene dati utili, metti giorni_reali = 0
    
    Restituisci SOLO questo JSON:
    {
        "giorni_reali": int,
        "giorni_senza_badge": int,
        "note": "string"
    }
    """
    
    # ✅ TENTATIVO 1: GEMINI
    try:
        with open(file_path, "rb") as f: 
            bytes_data = f.read()
        
        response = model.generate_content([prompt, {"mime_type": "application/pdf", "data": bytes_data}])
        return clean_json_response(response.text)
        
    except Exception as e:
        error_msg = str(e)
        
        # ✅ TENTATIVO 2: GROQ FALLBACK
        if ("429" in error_msg or "quota" in error_msg.lower()) and GROQ_API_KEY:
            st.warning("⚠️ Quota Gemini esaurita per cartellino, uso Groq...")
            return estrai_dati_cartellino_groq(file_path, prompt)
        else:
            st.error(f"❌ Err cart AI: {e}")
            return None

def estrai_dati_cartellino_groq(file_path, prompt):
    """Fallback Groq per cartellino"""
    try:
        from groq import Groq
        
        # Converti PDF in immagini
        images = pdf_to_images(file_path)
        if not images:
            return None
        
        client = Groq(api_key=GROQ_API_KEY)
        
        # Cartellino può avere più pagine, uso tutte le immagini
        content = [{"type": "text", "text": prompt}]
        
        for img_base64 in images[:3]:  # Max 3 pagine per non superare token limit
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img_base64}"}
            })
        
        response = client.chat.completions.create(
            model="llama-3.2-90b-vision-preview",
            messages=[{"role": "user", "content": content}],
            temperature=0.1,
            max_tokens=1024
        )
        
        result = clean_json_response(response.choices[0].message.content)
        if result:
            st.success("✅ Analisi cartellino completata con Groq")
        return result
        
    except ImportError:
        st.error("❌ Libreria 'groq' non installata. Aggiungi a requirements.txt")
        return None
    except Exception as e:
        st.error(f"❌ Errore Groq cartellino: {e}")
        return None

# --- PULIZIA FILE ---
def pulisci_file(path_busta, path_cart):
    """Elimina i file PDF scaricati dopo l'analisi"""
    file_eliminati = []
    
    if path_busta and os.path.exists(path_busta):
        try:
            os.remove(path_busta)
            file_eliminati.append(os.path.basename(path_busta))
        except Exception as e:
            st.warning(f"⚠️ Non riesco a eliminare {path_busta}: {e}")
    
    if path_cart and os.path.exists(path_cart):
        try:
            os.remove(path_cart)
            file_eliminati.append(os.path.basename(path_cart))
        except Exception as e:
            st.warning(f"⚠️ Non riesco a eliminare {path_cart}: {e}")
    
    if file_eliminati:
        st.info(f"🗑️ File eliminati: {', '.join(file_eliminati)}")

# --- CORE BOT ---
def scarica_documenti_automatici(mese_nome, anno, username, password, tipo_documento="cedolino"):
    """✅ Bot con gestione duplicati Dicembre"""
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
            
            # Verifica login riuscito
            try:
                page.wait_for_selector("text=I miei dati", timeout=15000)
                st_status.info("✅ Login OK")
            except:
                st_status.error("❌ Login fallito - Verifica credenziali o stato del sito")
                screenshot = page.screenshot()
                st.image(screenshot, caption="Errore login", use_container_width=True)
                browser.close()
                return None, None, "LOGIN_FALLITO"

            # BUSTA PAGA / TREDICESIMA
            st_status.info(f"💰 {nome_tipo}...")
            try:
                page.click("text=I miei dati")
                page.wait_for_selector("text=Documenti", timeout=10000).click()
                time.sleep(3)
                
                try: 
                    page.locator("tr", has=page.locator("text=Cedolino")).locator(".z-image").click(timeout=5000)
                except: 
                    page.click("text=Cedolino")
                
                time.sleep(5)
                
                # ✅ DOWNLOAD CON GESTIONE DUPLICATI
                try:
                    if tipo_documento == "tredicesima":
                        links = page.locator(f"a:has-text('Tredicesima {anno}')")
                        if links.count() > 0:
                            st.info(f"✅ Trovata: Tredicesima {anno}")
                            with page.expect_download(timeout=20000) as dl:
                                links.first.click()
                            
                            dl.value.save_as(path_busta)
                            
                            if os.path.exists(path_busta):
                                busta_ok = True
                                st_status.success(f"✅ Tredicesima: {os.path.getsize(path_busta)} bytes")
                        else:
                            st.warning(f"⚠️ Tredicesima {anno} non trovata")
                    else:
                        st.info("🔍 Ricerca cedolino...")
                        time.sleep(2)
                        
                        all_links = page.locator("a")
                        total_links = all_links.count()
                        
                        # ✅ RACCOGLI TUTTI I MATCH
                        link_matches = []
                        
                        for i in range(total_links):
                            try:
                                txt = all_links.nth(i).inner_text().strip()
                                
                                if not txt or len(txt) < 3:
                                    continue
                                
                                if any(mese in txt for mese in nomi_mesi_it) and str(anno) in txt:
                                    ha_target = target_busta.lower() in txt.lower()
                                    e_tredicesima = any(kw in txt for kw in ["Tredicesima", "13", "XIII", "13ma", "13MA"])
                                    
                                    if ha_target and not e_tredicesima:
                                        link_matches.append((i, txt))
                            except:
                                continue
                        
                        # ✅ SE CI SONO DUPLICATI, PRENDI L'ULTIMO
                        if len(link_matches) > 0:
                            if len(link_matches) > 1:
                                st.warning(f"⚠️ Trovati {len(link_matches)} match per '{target_busta}'")
                                st.info(f"📌 Seleziono l'ULTIMO (probabilmente il cedolino, non la tredicesima)")
                            
                            link_index, link_txt = link_matches[-1]  # ULTIMO
                            st.success(f"✅ Selezionato: '{link_txt}'")
                            
                            st.info(f"📥 Download in corso...")
                            
                            try:
                                with page.expect_download(timeout=20000) as download_info:
                                    all_links.nth(link_index).click()
                                
                                download = download_info.value
                                download.save_as(path_busta)
                                
                                if os.path.exists(path_busta):
                                    busta_ok = True
                                    st_status.success(f"✅ Cedolino: {os.path.getsize(path_busta)} bytes")
                                else:
                                    st.error(f"❌ File non salvato")
                                    
                            except Exception as dl_err:
                                st.error(f"❌ Errore download: {dl_err}")
                                screenshot = page.screenshot()
                                st.image(screenshot, caption="Errore download", use_container_width=True)
                        else:
                            st.warning(f"⚠️ '{target_busta}' non trovato")
                            screenshot = page.screenshot()
                            st.image(screenshot, caption="Lista documenti", use_container_width=True)
                            
                except Exception as e:
                    st.error(f"❌ Errore: {e}")
                    import traceback
                    st.code(traceback.format_exc())
                    
            except Exception as e: 
                st.error(f"Err: {e}")

            # CARTELLINO (solo se NON è tredicesima)
            if tipo_documento != "tredicesima":
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
    
    return final_busta, final_cart, None

# --- UI ---
st.set_page_config(page_title="Gottardo Payroll Mobile", page_icon="💶", layout="wide")
st.title("💶 Analisi Stipendio & Presenze")

# SIDEBAR CON LOGIN
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
        st.success(f"✅ Loggato come: **{st.session_state['username']}**")
        if st.button("🔄 Cambia Credenziali"):
            st.session_state['credentials_set'] = False
            st.session_state.pop('username', None)
            st.session_state.pop('password', None)
            st.rerun()
    
    st.divider()
    
    # PARAMETRI ANALISI
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
                st.error("❌ **LOGIN FALLITO**")
                st.warning("Verifica username e password oppure controlla lo stato del sito.")
                st.stop()
            
            st.session_state['busta'] = busta
            st.session_state['cart'] = cart
            st.session_state['tipo'] = tipo
            st.session_state['done'] = False
    else:
        st.warning("⚠️ Inserisci le credenziali per continuare")

# ANALISI CON PULIZIA AUTOMATICA
if st.session_state.get('busta') or st.session_state.get('cart'):
    
    if not st.session_state.get('done'):
        with st.spinner("🧠 Analisi dettagliata AI in corso..."):
            db = estrai_dati_busta_dettagliata(st.session_state.get('busta'))
            dc = estrai_dati_cartellino(st.session_state.get('cart')) if st.session_state.get('cart') else None
            st.session_state['db'] = db
            st.session_state['dc'] = dc
            st.session_state['done'] = True
            
            # PULIZIA AUTOMATICA FILE
            with st.spinner("🗑️ Pulizia file temporanei..."):
                pulisci_file(st.session_state.get('busta'), st.session_state.get('cart'))
            
            st.session_state.pop('busta', None)
            st.session_state.pop('cart', None)

    db = st.session_state.get('db')
    dc = st.session_state.get('dc')
    tipo = st.session_state.get('tipo', 'cedolino')
    
    if db and db.get('e_tredicesima'):
        st.success("🎄 **Questo è un cedolino TREDICESIMA**")
    
    st.divider()
    
    # 3 TAB
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
                giorni_reali = dc.get('giorni_reali', 0)
                if giorni_reali > 0:
                    st.metric("📅 Giorni Lavorati", giorni_reali)
                else:
                    st.metric("📅 Giorni Lavorati", "N/D", help="Timbrature dettagliate non disponibili")
                
                anomalie = dc.get('giorni_senza_badge', 0)
                if anomalie > 0:
                    st.metric("⚠️ Anomalie Badge", anomalie, delta="Controlla")
                else:
                    st.metric("✅ Anomalie Badge", 0, delta="Perfetto")
            
            with c2:
                note = dc.get('note', 'Nessuna nota rilevante.')
                st.info(f"**📝 Note AI:** {note}")
        else:
            if tipo == "tredicesima":
                st.warning("⚠️ Dati cartellino non disponibili (normale per Tredicesima).")
            else:
                st.error("❌ Errore nel download o analisi del cartellino.")

    with tab3:
        if db and dc:
            pagati = float(db.get('dati_generali', {}).get('giorni_pagati', 0))
            reali = float(dc.get('giorni_reali', 0))
            
            st.subheader("🔍 Analisi Discrepanze")
            
            # ✅ GESTIONE CARTELLINO SENZA TIMBRATURE
            if reali == 0:
                st.info("ℹ️ **Cartellino senza timbrature dettagliate**")
                st.write(f"📋 Giorni pagati in busta: **{int(pagati)}**")
                st.write(f"📝 {dc.get('note', '')}")
                st.success("✅ Usa i **giorni pagati dalla busta** come riferimento per il mese.")
            else:
                diff = reali - pagati
                
                col_a, col_b = st.columns(2)
                col_a.metric("Giorni Pagati (Busta)", pagati)
                col_b.metric("Giorni Lavorati (Cartellino)", reali, delta=f"{diff:.1f}")
                
                st.markdown("---")
                
                if abs(diff) < 0.5:
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
        elif tipo == "tredicesima":
            st.info("ℹ️ Analisi comparativa non disponibile per Tredicesima.")
        else:
            st.warning("⚠️ Servono entrambi i documenti per l'analisi.")
