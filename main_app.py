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


# ==============================================================================
# 1. HELPERS & PARSING
# ==============================================================================

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

# --- LOGICA AGENDA (FRAME RICORSIVI) ---

AGENDA_KEYWORDS = [
    "OMESSA TIMBRATURA", "MALATTIA", "RIPOSO", "FERIE", "PERMESS", "CHIUSURA", "INFORTUN"
]

def find_element_recursive(ctx, selector):
    """Cerca un elemento ricorsivamente in tutti i frame."""
    # 1. Cerca nel contesto corrente
    try:
        if ctx.locator(selector).count() > 0:
            return ctx, ctx.locator(selector).first
    except:
        pass
        
    # 2. Cerca nei figli (frames)
    frames_to_check = []
    if hasattr(ctx, 'frames'): # Oggetto Page
        frames_to_check = ctx.frames
    elif hasattr(ctx, 'child_frames'): # Oggetto Frame
        frames_to_check = ctx.child_frames

    for frame in frames_to_check:
        if frame == ctx: continue # Evita loop
        try:
            found_ctx, found_el = find_element_recursive(frame, selector)
            if found_el:
                return found_ctx, found_el
        except:
            continue
                
    return None, None

def agenda_set_month_enter(page, mese_num, anno, debug_info):
    nomi_mesi_it = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
                    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
    mese_nome_target = nomi_mesi_it[mese_num - 1]

    # A. ATTESA CARICAMENTO PAGINA/TOOLBAR
    # Aspettiamo che appaia la toolbar Dojo, segno che l'agenda è carica
    debug_info.append("Agenda: Ricerca toolbar...")
    _, toolbar = find_element_recursive(page, ".dijitToolbar")
    if not toolbar:
        # Fallback: aspetta network idle se toolbar non trovata subito
        try: page.wait_for_load_state("networkidle", timeout=6000)
        except: pass

    # B. CERCA L'ICONA DEL CALENDARIO (.calendar16)
    # Usiamo la ricerca ricorsiva perché spesso è dentro un iframe
    target_frame, target_element = find_element_recursive(page, ".calendar16")

    if not target_frame or not target_element:
        debug_info.append("Agenda: ERRORE - Icona .calendar16 non trovata in nessun frame.")
        return

    debug_info.append(f"Agenda: Icona trovata nel frame '{getattr(target_frame, 'name', 'N/A')}'")

    # C. APRI POPUP
    try:
        # Clicca l'icona
        target_element.click(force=True, timeout=8000)
        time.sleep(2.0) # Attesa animazione Dojo
        debug_info.append("Agenda: Click icona effettuato")
    except Exception as e:
        debug_info.append(f"Agenda: Errore click icona ({e})")
        return

    # D. IMPOSTA DATA NEL POPUP
    # Le label potrebbero essere nel frame o nella pagina principale
    popup_context = None
    if page.locator(".dijitCalendarMonthLabel").count() >= 2:
        popup_context = page
    elif target_frame.locator(".dijitCalendarMonthLabel").count() >= 2:
        popup_context = target_frame
    
    if popup_context:
        labels = popup_context.locator(".dijitCalendarMonthLabel")
        
        # Mese
        try:
            cur_month = labels.nth(0).inner_text().strip()
            if cur_month.lower() != mese_nome_target.lower():
                labels.nth(0).click()
                time.sleep(0.5)
                popup_context.locator("body").get_by_text(mese_nome_target, exact=True).last.click(timeout=4000)
                time.sleep(0.5)
        except: pass

        # Anno
        try:
            cur_year = labels.nth(1).inner_text().strip()
            if str(anno) not in cur_year:
                labels.nth(1).click()
                time.sleep(0.5)
                popup_context.locator("body").get_by_text(str(anno), exact=True).last.click(timeout=4000)
                time.sleep(0.5)
        except: pass

        # Conferma (Click giorno 1)
        try:
            popup_context.locator(".dijitCalendarDateTemplate", has_text=re.compile(r"^1$")).first.click(timeout=4000)
            time.sleep(1.5)
            debug_info.append("Agenda: Data impostata")
        except:
            debug_info.append("Agenda: Errore click giorno 1")
            page.keyboard.press("Escape")
    else:
        debug_info.append("Agenda: Popup aperto ma labels non trovate")
        page.keyboard.press("Escape")

    # E. CLICCA IL TASTO "MESE" (Fondamentale per la vista)
    time.sleep(1.0)
    try:
        # 1. Cerca per aria-label="Mese" (il metodo più sicuro dal tuo screenshot)
        btn_mese = target_frame.locator("[aria-label='Mese']").first
        
        # 2. Fallback per testo
        if btn_mese.count() == 0:
            btn_mese = target_frame.locator(".dijitButtonText", has_text=re.compile(r"^\s*Mese\s*$", re.IGNORECASE)).first

        if btn_mese.count() > 0:
            btn_mese.click(force=True, timeout=5000)
            debug_info.append("Agenda: Cliccato bottone 'Mese'")
            time.sleep(3.0) # Attesa caricamento dati griglia
        else:
            debug_info.append("Agenda: Bottone 'Mese' NON trovato")
            
    except Exception as e:
        debug_info.append(f"Agenda: Errore click tasto Mese ({e})")


def agenda_extract_events_fast(page):
    texts = []
    
    # Helper per estrarre testo ricorsivamente da tutti i frame
    def extract_recursive(ctx):
        local = []
        candidates = ctx.locator("[class*='event'], [class*='Event'], [class*='appointment'], [class*='Appunt'], .dijitCalendarEvent")
        try:
            n = candidates.count()
            if n > 0:
                for i in range(min(n, 200)):
                    t = (candidates.nth(i).inner_text() or "").strip()
                    if t: local.append(t)
        except: pass
        
        # Scendi nei frame
        if hasattr(ctx, 'frames'):
            for f in ctx.frames:
                if f != ctx: local.extend(extract_recursive(f))
        elif hasattr(ctx, 'child_frames'):
            for f in ctx.child_frames:
                local.extend(extract_recursive(f))
        return local

    texts = extract_recursive(page)
    # Fallback se non trova nulla di strutturato
    if not texts:
        texts = [page.inner_text("body") or ""]

    blob = "\n".join(texts)
    up = blob.upper()
    counts = {k: up.count(k) for k in AGENDA_KEYWORDS}
    
    # Estrai righe rilevanti
    lines = []
    for ln in blob.splitlines():
        s = (ln or "").strip()
        if not s: continue
        su = s.upper()
        if any(k in su for k in AGENDA_KEYWORDS):
            lines.append(s)

    # Dedup
    seen, uniq = set(), []
    for s in lines:
        if s in seen: continue
        seen.add(s); uniq.append(s)

    return {"counts": counts, "lines": uniq[:200], "raw_len": len(blob)}

# --- LOGICA PARSING CARTACEI ---

def get_busta_prompt():
    return """Analizza CEDOLINO PAGA. JSON valido:
{
    "e_tredicesima": boolean,
    "dati_generali": {"netto": float, "giorni_pagati": float},
    "competenze": {"base": float, "straordinari": float, "festivita": float, "anzianita": float, "lordo_totale": float},
    "trattenute": {"inps": float, "irpef_netta": float, "addizionali_totali": float},
    "ferie": {"residue_ap": float, "maturate": float, "godute": float, "saldo": float},
    "par": {"residue_ap": float, "spettanti": float, "fruite": float, "saldo": float}
}"""

def get_cartellino_prompt():
    return """Analizza CARTELLINO PRESENZE. JSON valido:
{
    "giorni_reali": float, "gg_presenza": float, 
    "ore_ordinarie_0251": float, "ore_lavorate_0253": float, 
    "ore_ordinarie_riepilogo": float, "giorni_senza_badge": float,
    "note": "string", "debug_prime_righe": "string"
}"""

@st.cache_resource
def get_gemini_models():
    if not HAS_GEMINI: return []
    try:
        models = genai.list_models()
        return [(m.name, genai.GenerativeModel(m.name)) for m in models if 'generateContent' in m.supported_generation_methods]
    except: return []

def estrai_con_ai(file_path, prompt, tipo):
    if not file_path or not os.path.exists(file_path): return None
    status = st.empty()
    models = get_gemini_models()
    if models:
        try:
            with open(file_path, "rb") as f: pdf_bytes = f.read()
        except: pdf_bytes = None
        
        if pdf_bytes:
            for name, model in models:
                try:
                    status.info(f"🤖 Analisi {tipo} con AI...")
                    resp = model.generate_content([prompt, {"mime_type": "application/pdf", "data": pdf_bytes}])
                    res = clean_json_response(resp.text)
                    if res and isinstance(res, dict):
                        status.empty()
                        return res
                except: continue
    status.empty()
    return None

def cartellino_parse_deterministico(file_path: str):
    text = extract_text_from_pdf(file_path)
    if not text: return None
    upper = text.upper()
    
    # Logica base regex
    m_gg = re.search(r"0265\s+GG\s+PRESENZA.*?(\d{1,3}[.,]\d{2})", upper)
    gg_presenza = parse_it_number(m_gg.group(1)) if m_gg else 0.0
    
    m_ore = re.search(r"0253\s+ORE\s+LAVORATE.*?(\d{1,3}[.,]\d{2})", upper)
    ore_lav = parse_it_number(m_ore.group(1)) if m_ore else 0.0
    
    return {
        "gg_presenza": gg_presenza,
        "ore_lavorate_0253": ore_lav,
        "debug_prime_righe": text[:200]
    }


# ==============================================================================
# 2. BOT PRINCIPALE (ORCHESTRATORE)
# ==============================================================================

def scarica_documenti_automatici(mese_nome, anno, username, password, tipo_documento="cedolino"):
    nomi_mesi_it = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
                    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
    try:
        mese_num = nomi_mesi_it.index(mese_nome) + 1
    except:
        return None, None, "Mese invalido"

    wd = Path.cwd()
    suffix = "_13" if tipo_documento == "tredicesima" else ""
    path_busta = str(wd / f"busta_{mese_num}_{anno}{suffix}.pdf")
    path_cart = str(wd / f"cartellino_{mese_num}_{anno}.pdf")
    target_busta = f"Tredicesima {anno}" if tipo_documento == "tredicesima" else f"{mese_nome} {anno}"

    last_day = calendar.monthrange(anno, mese_num)[1]
    d_from_vis = f"01/{mese_num:02d}/{anno}"
    d_to_vis = f"{last_day}/{mese_num:02d}/{anno}"

    st_status = st.empty()
    st_status.info(f"🤖 Bot avviato per {mese_nome} {anno}")
    debug_info = []
    b_ok, c_ok = False, False

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                slow_mo=300,
                args=['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage']
            )
            context = browser.new_context(
                accept_downloads=True,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
            )
            context.set_default_timeout(45000)
            page = context.new_page()
            page.set_viewport_size({"width": 1920, "height": 1080})

            # --- LOGIN ---
            st_status.info("🔐 Login in corso...")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y", wait_until="domcontentloaded")
            page.fill('input[type="text"]', username)
            page.fill('input[type="password"]', password)
            page.press('input[type="password"]', 'Enter')
            
            try:
                page.wait_for_selector("text=I miei dati", timeout=20000)
                st_status.success("✅ Login effettuato")
                debug_info.append("Login: OK")
            except:
                browser.close()
                return None, None, "LOGIN_FALLITO"

            # --- AGENDA ---
            try:
                st_status.info("🗓️ Lettura Agenda...")
                agenda_set_month_enter(page, mese_num, anno, debug_info)
                agenda_data = agenda_extract_events_fast(page)
                st.session_state["agenda_data"] = agenda_data
                debug_info.append(f"Agenda: OK (len={agenda_data.get('raw_len')})")
            except Exception as e:
                debug_info.append(f"Agenda Error: {e}")

            # --- BUSTA PAGA ---
            st_status.info("💰 Scarico Busta Paga...")
            try:
                page.click("text=I miei dati", force=True)
                page.wait_for_selector("text=Documenti", timeout=10000).click()
                time.sleep(3)
                try: page.click("text=Cedolino", force=True)
                except: pass
                time.sleep(4)

                links = page.locator("a")
                found_link = None
                for i in range(links.count()):
                    try:
                        txt = links.nth(i).inner_text().strip().lower()
                        if target_busta.lower() in txt:
                            found_link = links.nth(i)
                            break
                    except: continue

                if found_link:
                    with page.expect_download(timeout=30000) as dl_info:
                        found_link.click()
                    dl_info.value.save_as(path_busta)
                    if os.path.exists(path_busta): 
                        b_ok = True
                        debug_info.append("Busta: Scaricata")
                else:
                    debug_info.append("Busta: Link non trovato")
            except Exception as e:
                debug_info.append(f"Busta Error: {e}")

            # --- CARTELLINO ---
            if tipo_documento != "tredicesima":
                st_status.info("📅 Scarico Cartellino...")
                try:
                    page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y")
                    time.sleep(3)
                    # Navigazione Menu
                    page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
                    time.sleep(2)
                    page.evaluate("document.getElementById('lnktab_5_label')?.click()")
                    time.sleep(4)

                    # Imposta date (opzionale se resetta)
                    try:
                        dal = page.locator("input[id*='CLRICHIE'][class*='dijitInputInner']").first
                        if dal.count()>0:
                            dal.click(); page.keyboard.press("Control+A"); dal.type(d_from_vis); dal.press("Tab")
                            time.sleep(0.5)
                            al = page.locator("input[id*='CLRICHI2'][class*='dijitInputInner']").first
                            al.click(); page.keyboard.press("Control+A"); al.type(d_to_vis); al.press("Tab")
                    except: pass

                    # Ricerca
                    try: page.locator("//span[contains(text(),'Esegui ricerca')]").first.click(force=True)
                    except: page.keyboard.press("Enter")
                    time.sleep(5)

                    target_row = f"{mese_num:02d}/{anno}"
                    row = page.locator(f"tr:has-text('{target_row}')").first
                    if row.count() > 0:
                        icon = row.locator("img[src*='search']").first
                        if icon.count() > 0:
                            with context.expect_page() as p_info:
                                icon.click()
                            popup = p_info.value
                            try: popup.wait_for_load_state("networkidle")
                            except: pass
                            popup.pdf(path=path_cart)
                            c_ok = True
                            debug_info.append("Cartellino: Scaricato")
                except Exception as e:
                    debug_info.append(f"Cartellino Error: {e}")

            browser.close()
            st_status.empty()

    except Exception as e:
        return None, None, str(e)
    
    st.session_state['debug_info'] = debug_info
    return (path_busta if b_ok else None), (path_cart if c_ok else None), None


# ==============================================================================
# 3. INTERFACCIA UTENTE (STREAMLIT)
# ==============================================================================

st.set_page_config(page_title="Gottardo Payroll", page_icon="💶", layout="wide")
st.title("💶 Analisi Stipendio & Presenze")

# --- SIDEBAR ---
with st.sidebar:
    st.header("🔐 Credenziali")
    username, password = get_credentials()

    if not st.session_state.get('credentials_set'):
        u = st.text_input("Username", value=username if username else "")
        p = st.text_input("Password", type="password")
        if st.button("💾 Salva"):
            st.session_state.update({'username': u, 'password': p, 'credentials_set': True})
            st.rerun()
    else:
        st.success(f"Utente: {st.session_state.get('username', '')}")
        if st.button("🔄 Disconnetti"):
            st.session_state.update({'credentials_set': False})
            st.rerun()

    st.divider()

    if st.session_state.get('credentials_set'):
        sel_anno = st.selectbox("Anno", [2024, 2025, 2026], index=1)
        sel_mese = st.selectbox("Mese", ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
             "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"], index=0)
        tipo_doc = st.radio("Tipo", ["📄 Cedolino Mensile", "🎄 Tredicesima"])

        if st.button("🚀 AVVIA ANALISI COMPLETA", type="primary"):
            # Reset stati
            for k in ['agenda_data', 'debug_info', 'busta', 'cart', 'db', 'dc', 'done']:
                st.session_state.pop(k, None)
            
            tipo = "tredicesima" if "Tredicesima" in tipo_doc else "cedolino"
            pb, pc, err = scarica_documenti_automatici(sel_mese, sel_anno, st.session_state['username'], st.session_state['password'], tipo)

            if err: st.error(err)
            else:
                st.session_state.update({'busta': pb, 'cart': pc, 'done': False})

# --- ELABORAZIONE DATI (AI) ---
if (st.session_state.get('busta') or st.session_state.get('cart')) and not st.session_state.get('done'):
    with st.spinner("🧠 Analisi AI in corso..."):
        db = estrai_con_ai(st.session_state.get('busta'), get_busta_prompt(), "Busta Paga")
        
        # Tentativo misto per cartellino
        path_c = st.session_state.get('cart')
        dc = cartellino_parse_deterministico(path_c) # Prova regex
        # Se regex fallisce o dà 0, usa AI
        if not dc or (dc.get('gg_presenza', 0) == 0):
             dc_ai = estrai_con_ai(path_c, get_cartellino_prompt(), "Cartellino")
             if dc_ai: dc = dc_ai
        
        st.session_state.update({'db': db, 'dc': dc, 'done': True})

# --- VISUALIZZAZIONE TABS ---
db = st.session_state.get('db')
dc = st.session_state.get('dc')

if st.session_state.get('done'):
    tab1, tab2, tab3, tab4 = st.tabs(["💰 Stipendio", "📅 Cartellino", "📊 Confronto", "🔧 Debug"])

    with tab1:
        if db:
            dg = db.get('dati_generali', {})
            comp = db.get('competenze', {})
            tratt = db.get('trattenute', {})
            c1, c2 = st.columns(2)
            c1.metric("Netto", f"€ {dg.get('netto', 0)}")
            c2.metric("Lordo", f"€ {comp.get('lordo_totale', 0)}")
            st.json(db)
        else: st.warning("Dati busta non disponibili")

    with tab2:
        if dc:
            st.metric("GG Presenza", dc.get('gg_presenza', 0))
            st.write(f"Note: {dc.get('note', '')}")
            st.json(dc)
        else: st.warning("Dati cartellino non disponibili")

    with tab3:
        if db and dc:
            gg_busta = db.get('dati_generali', {}).get('giorni_pagati', 0)
            gg_cart = dc.get('gg_presenza', 0)
            st.metric("Discrepanza GG", f"{gg_cart - gg_busta:.2f}")
        else: st.info("Necessari entrambi i documenti")

    with tab4:
        st.subheader("Agenda & Log")
        if st.session_state.get('agenda_data'):
            ad = st.session_state['agenda_data']
            c1, c2, c3 = st.columns(3)
            c1.metric("Omesse", ad['counts'].get("OMESSA TIMBRATURA", 0))
            c2.metric("Malattia", ad['counts'].get("MALATTIA", 0))
            st.write(ad.get('lines', []))
        
        st.write("LOG OPERAZIONI:")
        st.write(st.session_state.get('debug_info', []))
        
        # Link download
        if st.session_state.get('busta'):
            st.markdown(get_pdf_download_link(st.session_state['busta'], "busta.pdf"), unsafe_allow_html=True)
        if st.session_state.get('cart'):
            st.markdown(get_pdf_download_link(st.session_state['cart'], "cartellino.pdf"), unsafe_allow_html=True)
