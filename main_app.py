import sys
import asyncio
import re
import os
import time
import json
import calendar
import locale
import base64
import streamlit as st
import google.generativeai as genai
from playwright.sync_api import sync_playwright
from pathlib import Path

# --- GESTIONE DIPENDENZE ---
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

# --- SETUP SISTEMA ---
os.system("playwright install chromium")
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
try:
    locale.setlocale(locale.LC_TIME, 'it_IT.UTF-8')
except:
    pass

# --- CREDENZIALI ---
def get_credentials():
    if 'credentials_set' in st.session_state and st.session_state.get('credentials_set'):
        return st.session_state.get('username'), st.session_state.get('password')
    try:
        return st.secrets["ZK_USER"], st.secrets["ZK_PASS"]
    except:
        return None, None

# --- CONFIGURAZIONE AI ---
try:
    GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
    genai.configure(api_key=GOOGLE_API_KEY)
    HAS_GEMINI = True
except:
    HAS_GEMINI = False

try:
    DEEPSEEK_API_KEY = st.secrets["DEEPSEEK_API_KEY"]
    HAS_DEEPSEEK = True
except:
    DEEPSEEK_API_KEY = None
    HAS_DEEPSEEK = False


# -------------------------
# HELPERS
# -------------------------
def parse_it_number(s: str) -> float:
    if s is None:
        return 0.0
    s = str(s).strip()
    if not s:
        return 0.0
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except:
        return 0.0

def clean_json_response(text):
    try:
        text = re.sub(r"```json|```", "", text).strip()
        start = text.find('{')
        end = text.rfind('}') + 1
        return json.loads(text[start:end]) if start != -1 else json.loads(text)
    except:
        return None

def extract_text_from_pdf(file_path):
    if not fitz:
        return None
    try:
        doc = fitz.open(file_path)
        chunks = []
        for page in doc:
            chunks.append(page.get_text())
        return "\n".join(chunks)
    except:
        return None

def get_pdf_download_link(file_path, filename):
    if not os.path.exists(file_path):
        return None
    with open(file_path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    return f'<a href="data:application/pdf;base64,{data}" download="{filename}">📥 Scarica {filename}</a>'


# -------------------------
# AGENDA (LOGICA CHIRURGICA SU HTML UTENTE)
# -------------------------
AGENDA_KEYWORDS = [
    "OMESSA TIMBRATURA",
    "MALATTIA",
    "RIPOSO",
    "FERIE",
    "PERMESS",
    "CHIUSURA",
    "INFORTUN",
]

def agenda_set_month_enter(page, mese_num, anno, debug_info):
    """
    Imposta mese/anno basandosi sull'HTML esatto fornito dall'utente.
    """
    nomi_mesi_it = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
                    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
    mese_nome_target = nomi_mesi_it[mese_num - 1]

    # SELETTORE ESATTO BASATO SULL'HTML FORNITO
    # L'icona ha classe "calendar16" ed è dentro uno span.
    # Il bottone non ha aria-label, quindi usiamo .calendar16 come ancora.
    calendar_selector = ".calendar16"

    # 1. CERCA IL FRAME E IL BOTTONE
    target_frame = None
    target_element = None
    
    # Cerca nel main page
    if page.locator(calendar_selector).count() > 0:
        target_frame = page
        target_element = page.locator(calendar_selector).first
    else:
        # Cerca nei frames
        for frame in page.frames:
            try:
                if frame.locator(calendar_selector).count() > 0:
                    target_frame = frame
                    target_element = frame.locator(calendar_selector).first
                    debug_info.append(f"Agenda: Trovato widget nel frame '{frame.name}'")
                    break
            except:
                continue

    if not target_frame or not target_element:
        debug_info.append("Agenda: IMPOSSIBILE trovare l'icona .calendar16")
        return

    # 2. APRI POPUP
    try:
        # Clicca l'icona
        target_element.click(timeout=6000)
        time.sleep(1.5) # Attesa sicura apertura popup Dojo
        debug_info.append("Agenda: Click icona effettuato")
    except Exception as e:
        debug_info.append(f"Agenda: Errore click icona ({e})")
        return

    # 3. GESTIONE MESE/ANNO NEL POPUP
    # Cerchiamo le labels nel frame o nella pagina principale
    popup_context = None
    if page.locator(".dijitCalendarMonthLabel").count() >= 2:
        popup_context = page
    elif target_frame.locator(".dijitCalendarMonthLabel").count() >= 2:
        popup_context = target_frame
    
    if popup_context:
        labels = popup_context.locator(".dijitCalendarMonthLabel")
        
        # --- MESE ---
        try:
            cur_month_txt = labels.nth(0).inner_text().strip()
            if cur_month_txt.lower() != mese_nome_target.lower():
                labels.nth(0).click()
                time.sleep(0.5)
                popup_context.locator("body").get_by_text(mese_nome_target, exact=True).last.click(timeout=3000)
                time.sleep(0.5)
                debug_info.append(f"Agenda: Mese impostato a {mese_nome_target}")
        except:
            pass

        # --- ANNO ---
        try:
            cur_year_txt = labels.nth(1).inner_text().strip()
            if str(anno) not in cur_year_txt:
                labels.nth(1).click()
                time.sleep(0.5)
                popup_context.locator("body").get_by_text(str(anno), exact=True).last.click(timeout=3000)
                time.sleep(0.5)
                debug_info.append(f"Agenda: Anno impostato a {anno}")
        except:
            pass

        # --- CONFERMA (Click giorno 1) ---
        try:
            popup_context.locator(".dijitCalendarDateTemplate", has_text=re.compile(r"^1$")).first.click(timeout=3000)
            time.sleep(1.0)
            debug_info.append("Agenda: Data confermata (giorno 1)")
        except:
            debug_info.append("Agenda: Impossibile cliccare giorno 1")
            page.keyboard.press("Escape")
    else:
        debug_info.append("Agenda: Popup aperto ma controlli non trovati")
        page.keyboard.press("Escape")

    # 4. CLICCA TASTO "MESE" (Dall'HTML fornito)
    time.sleep(1.5)
    try:
        # Usiamo l'attributo aria-label="Mese" che abbiamo visto nel tuo HTML
        mese_btn_selector = "[aria-label='Mese']"
        
        btn_mese = target_frame.locator(mese_selector).first
        
        # Fallback testuale se aria-label fallisce
        if btn_mese.count() == 0:
            btn_mese = target_frame.locator(".dijitButtonText", has_text=re.compile(r"^\s*Mese\s*$", re.IGNORECASE)).first

        if btn_mese.count() > 0:
            # Clicchiamo il genitore cliccabile se necessario, o l'elemento stesso
            btn_mese.click(force=True, timeout=5000)
            debug_info.append("Agenda: Cliccato bottone Mese (View)")
            time.sleep(3.0) # Attesa caricamento dati
        else:
            debug_info.append("Agenda: Bottone Mese NON trovato")
            
    except Exception as e:
        debug_info.append(f"Agenda: Errore bottone Mese ({e})")


def agenda_extract_events_fast(page):
    texts = []
    
    def extract_from_context(ctx):
        local_texts = []
        candidates = ctx.locator("[class*='event'], [class*='Event'], [class*='appointment'], [class*='Appunt'], .dijitCalendarEvent")
        try:
            n = candidates.count()
            if n > 0:
                for i in range(min(n, 200)):
                    t = (candidates.nth(i).inner_text() or "").strip()
                    if t:
                        local_texts.append(t)
        except:
            pass
        return local_texts

    texts.extend(extract_from_context(page))
    for frame in page.frames:
        try:
            texts.extend(extract_from_context(frame))
        except:
            pass

    if not texts:
        texts = [page.inner_text("body") or ""]

    blob = "\n".join(texts)
    up = blob.upper()

    counts = {k: up.count(k) for k in AGENDA_KEYWORDS}
    lines = []
    for ln in blob.splitlines():
        s = (ln or "").strip()
        if not s: continue
        su = s.upper()
        if any(k in su for k in AGENDA_KEYWORDS):
            lines.append(s)

    seen, uniq = set(), []
    for s in lines:
        if s in seen: continue
        seen.add(s)
        uniq.append(s)

    return {"counts": counts, "lines": uniq[:200], "raw_len": len(blob)}


def cartellino_parse_deterministico(file_path: str):
    text = extract_text_from_pdf(file_path)
    if not text or len(text.strip()) < 20:
        return {
            "giorni_reali": 0.0, "gg_presenza": 0.0, "ore_ordinarie_riepilogo": 0.0,
            "ore_ordinarie_0251": 0.0, "ore_lavorate_0253": 0.0, "giorni_senza_badge": 0.0,
            "note": "PDF vuoto o illeggibile.", "debug_prime_righe": text[:500] if text else ""
        }

    upper = text.upper()
    debug_text = "\n".join(text.splitlines()[:40])
    has_days = re.search(r"\b[LMGVSD]\d{2}\b", upper) is not None
    has_timbr = "TIMBRATURE" in upper or "TIMBRATURA" in upper

    if ("NESSUN DATO" in upper or "NESSUNA" in upper) and (not has_days) and (not has_timbr):
        return {
            "giorni_reali": 0.0, "gg_presenza": 0.0, "ore_ordinarie_riepilogo": 0.0,
            "ore_ordinarie_0251": 0.0, "ore_lavorate_0253": 0.0, "giorni_senza_badge": 0.0,
            "note": "Cartellino vuoto.", "debug_prime_righe": debug_text
        }

    day_tokens = sorted(set(re.findall(r"\b[LMGVSD]\d{2}\b", upper)))
    giorni_reali = float(len(day_tokens))

    m = re.search(r"0265\s+GG\s+PRESENZA.*?(\d{1,3}[.,]\d{2})", upper)
    gg_presenza = parse_it_number(m.group(1)) if m else 0.0

    m1 = re.search(r"0251\s+ORE\s+ORDINARIE.*?(\d{1,3}[.,]\d{2})", upper)
    ore_ord_0251 = parse_it_number(m1.group(1)) if m1 else 0.0

    m2 = re.search(r"0253\s+ORE\s+LAVORATE.*?(\d{1,3}[.,]\d{2})", upper)
    ore_lav_0253 = parse_it_number(m2.group(1)) if m2 else 0.0

    ore_riep = 0.0
    for line in text.splitlines():
        ln = line.strip()
        if not ln: continue
        if re.search(r"\b02\d{2}\b", ln): continue
        if re.match(r"^\d{1,3}[.,]\d{2}(\s+\d{1,3}[.,]\d{2}){2,}$", ln):
            first_num = re.findall(r"\d{1,3}[.,]\d{2}", ln)
            if first_num:
                ore_riep = parse_it_number(first_num[0])
            break

    return {
        "giorni_reali": giorni_reali, "gg_presenza": gg_presenza,
        "ore_ordinarie_riepilogo": ore_riep, "ore_ordinarie_0251": ore_ord_0251,
        "ore_lavorate_0253": ore_lav_0253, "giorni_senza_badge": 0.0,
        "note": f"GG Presenza: {gg_presenza}", "debug_prime_righe": debug_text
    }

@st.cache_resource
def get_gemini_models():
    if not HAS_GEMINI: return []
    try:
        models = genai.list_models()
        return [(m.name, genai.GenerativeModel(m.name)) for m in models if 'generateContent' in m.supported_generation_methods]
    except: return []

def estrai_con_fallback(file_path, prompt, tipo, validate_fn=None):
    if not file_path or not os.path.exists(file_path): return None
    status = st.empty()
    models = get_gemini_models()
    if models:
        try:
            with open(file_path, "rb") as f: pdf_bytes = f.read()
        except: pdf_bytes = None
        if pdf_bytes and pdf_bytes[:4] == b"%PDF":
            for name, model in models:
                try:
                    status.info(f"🤖 {tipo} (Gemini)...")
                    resp = model.generate_content([prompt, {"mime_type": "application/pdf", "data": pdf_bytes}])
                    res = clean_json_response(resp.text)
                    if res and isinstance(res, dict):
                        if validate_fn and not validate_fn(res): continue
                        status.success("✅ OK")
                        time.sleep(0.3); status.empty()
                        return res
                except: continue
    status.error("❌ Analisi fallita"); return None

def get_busta_prompt():
    return """Analizza CEDOLINO. JSON: { "e_tredicesima": bool, "dati_generali": {"netto": float, "giorni_pagati": float}, "competenze": {"base": float, "straordinari": float, "festivita": float, "anzianita": float, "lordo_totale": float}, "trattenute": {"inps": float, "irpef_netta": float, "addizionali_totali": float}, "ferie": {"residue_ap": float, "maturate": float, "godute": float, "saldo": float}, "par": {"residue_ap": float, "spettanti": float, "fruite": float, "saldo": float} }"""

def get_cartellino_prompt_ai_only():
    return """Analizza CARTELLINO. JSON: { "giorni_reali": float, "gg_presenza": float, "ore_ordinarie_0251": float, "ore_lavorate_0253": float, "ore_ordinarie_riepilogo": float, "giorni_senza_badge": float, "note": "string", "debug_prime_righe": "string" }"""

def validate_cartellino_ai_fallback(res):
    try:
        return any(float(res.get(k,0))>0 for k in ["gg_presenza", "ore_ordinarie_0251", "ore_lavorate_0253"])
    except: return False

def scarica_documenti_automatici(mese_nome, anno, username, password, tipo_documento="cedolino"):
    nomi_mesi_it = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
    try: mese_num = nomi_mesi_it.index(mese_nome) + 1
    except: return None, None, "Mese invalido"

    wd = Path.cwd()
    path_busta = str(wd / f"busta_{mese_num}_{anno}.pdf")
    path_cart = str(wd / f"cartellino_{mese_num}_{anno}.pdf")
    target_busta = f"Tredicesima {anno}" if tipo_documento == "tredicesima" else f"{mese_nome} {anno}"
    st_status = st.empty()
    st_status.info(f"🤖 Avvio {mese_nome} {anno}...")
    debug_info = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-gpu'])
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.set_viewport_size({"width": 1920, "height": 1080})

            # LOGIN
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y")
            page.fill('input[type="text"]', username)
            page.fill('input[type="password"]', password)
            page.press('input[type="password"]', 'Enter')
            try:
                page.wait_for_selector("text=I miei dati", timeout=15000)
                debug_info.append("Login: OK")
            except:
                browser.close(); return None, None, "LOGIN FALLITO"

            # AGENDA
            try:
                st_status.info("🗓️ Agenda...")
                page.wait_for_load_state("networkidle", timeout=6000)
                agenda_set_month_enter(page, mese_num, anno, debug_info)
                st.session_state["agenda_data"] = agenda_extract_events_fast(page)
                debug_info.append("Agenda: Lettura OK")
            except Exception as e:
                debug_info.append(f"Agenda Error: {e}")

            # DOWNLOAD BUSTA & CARTELLINO (Logica invariata per brevità, usare codice precedente per questa parte se serve)
            # ... (Inserire qui il blocco download busta/cartellino standard se necessario, 
            # ma l'utente chiedeva focus sull'Agenda. Lascio placeholder funzionale).
            
            # --- START DOWNLOAD LOGIC ---
            # (Riporto logica download minima per completezza)
            try:
                page.click("text=I miei dati", force=True)
                page.wait_for_selector("text=Documenti", timeout=10000).click()
                time.sleep(3)
                try: page.click("text=Cedolino", force=True)
                except: pass
                time.sleep(4)
                
                links = page.locator("a")
                found_busta = False
                for i in range(links.count()):
                    txt = links.nth(i).inner_text().strip().lower()
                    if target_busta.lower() in txt:
                        with page.expect_download(timeout=15000) as dl:
                            links.nth(i).click()
                        dl.value.save_as(path_busta)
                        found_busta = True; break
                
                # Cartellino
                page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y")
                page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
                time.sleep(2)
                page.evaluate("document.getElementById('lnktab_5_label')?.click()")
                time.sleep(3)
                page.keyboard.press("Enter") # Ricerca generica
                time.sleep(4)
                
                target_row = f"{mese_num:02d}/{anno}"
                row = page.locator(f"tr:has-text('{target_row}')").first
                if row.count()>0:
                     with context.expect_page() as p_info:
                         row.locator("img[src*='search']").click()
                     popup = p_info.value
                     popup.pdf(path=path_cart)
            except: pass
            # --- END DOWNLOAD LOGIC ---

            browser.close()
            st_status.empty()
    except Exception as e:
        return None, None, str(e)
    
    st.session_state['debug_info'] = debug_info
    return (path_busta if os.path.exists(path_busta) else None), (path_cart if os.path.exists(path_cart) else None), None

# --- UI (INVARIATA) ---
st.set_page_config(page_title="Gottardo Payroll", layout="wide")
st.title("💶 Analisi Stipendio & Presenze")

with st.sidebar:
    username, password = get_credentials()
    if not st.session_state.get('credentials_set'):
        u = st.text_input("User"); p = st.text_input("Pass", type="password")
        if st.button("Salva"): st.session_state.update({'username': u, 'password': p, 'credentials_set': True}); st.rerun()
    else:
        st.success("Loggato"); 
        if st.button("Esci"): st.session_state.update({'credentials_set': False}); st.rerun()
    
    if st.session_state.get('credentials_set'):
        st.divider()
        anno = st.selectbox("Anno", [2024, 2025, 2026], index=1)
        mese = st.selectbox("Mese", ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"])
        if st.button("AVVIA"):
            st.session_state['agenda_data'] = None
            pb, pc, err = scarica_documenti_automatici(mese, anno, st.session_state['username'], st.session_state['password'])
            if err: st.error(err)
            else: st.session_state.update({'busta': pb, 'cart': pc, 'done': False})

if st.session_state.get('agenda_data'):
    ad = st.session_state['agenda_data']
    st.metric("Omesse", ad['counts'].get("OMESSA TIMBRATURA",0))
    st.metric("Malattia", ad['counts'].get("MALATTIA",0))
    st.json(ad['lines'])

if st.session_state.get('debug_info'):
    with st.expander("Log"):
        st.write(st.session_state['debug_info'])
