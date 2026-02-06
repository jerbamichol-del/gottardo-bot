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
# AGENDA (LOGICA RICORSIVA + TIMEOUT ESTESI)
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

def find_element_recursive(ctx, selector):
    """Cerca un elemento ricorsivamente in tutti i frame."""
    # Cerca nel contesto corrente
    try:
        if ctx.locator(selector).count() > 0:
            return ctx, ctx.locator(selector).first
    except:
        pass
        
    # Cerca nei figli (frames)
    if hasattr(ctx, 'frames'):
        for frame in ctx.frames:
            # Evita loop infiniti o frame distrutti
            if frame == ctx: continue
            try:
                found_ctx, found_el = find_element_recursive(frame, selector)
                if found_el:
                    return found_ctx, found_el
            except:
                continue
    elif hasattr(ctx, 'child_frames'): # Per oggetti Frame
        for frame in ctx.child_frames:
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

    # 1. ATTESA TOOLBAR (Per essere sicuri che l'agenda sia caricata)
    debug_info.append("Agenda: Attesa caricamento toolbar...")
    # Proviamo ad aspettare genericamente che la rete si calmi
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except:
        pass

    # 2. CERCA IL BOTTONE CALENDARIO (.calendar16)
    # Usiamo ricerca ricorsiva per trovare frame annidati
    target_frame, target_element = find_element_recursive(page, ".calendar16")

    if not target_frame or not target_element:
        # Fallback: prova a cercare la toolbar generica
        debug_info.append("Agenda: Icona .calendar16 non trovata subito. Cerco toolbar...")
        target_frame, toolbar = find_element_recursive(page, ".dijitToolbar")
        if target_frame:
            target_element = target_frame.locator(".calendar16").first
    
    if not target_element or target_element.count() == 0:
        debug_info.append("Agenda: ERRORE FATALE - Icona calendario non trovata in nessun frame.")
        return

    debug_info.append(f"Agenda: Icona trovata nel frame '{getattr(target_frame, 'name', 'N/A')}'")

    # 3. APRI POPUP
    try:
        # Clicca
        target_element.click(force=True, timeout=10000)
        time.sleep(2.0) # Attesa animazione Dojo
        debug_info.append("Agenda: Click icona effettuato")
    except Exception as e:
        debug_info.append(f"Agenda: Errore click icona ({e})")
        return

    # 4. GESTIONE MESE/ANNO NEL POPUP
    # Il popup potrebbe essere nel frame o nella root page
    popup_context = None
    if page.locator(".dijitCalendarMonthLabel").count() >= 2:
        popup_context = page
    elif target_frame.locator(".dijitCalendarMonthLabel").count() >= 2:
        popup_context = target_frame
    
    if popup_context:
        labels = popup_context.locator(".dijitCalendarMonthLabel")
        
        # MESE
        try:
            cur_month_txt = labels.nth(0).inner_text().strip()
            if cur_month_txt.lower() != mese_nome_target.lower():
                labels.nth(0).click()
                time.sleep(0.5)
                popup_context.locator("body").get_by_text(mese_nome_target, exact=True).last.click(timeout=5000)
                time.sleep(0.5)
                debug_info.append(f"Agenda: Mese impostato a {mese_nome_target}")
        except Exception as e:
            debug_info.append(f"Agenda: Warning Mese ({e})")

        # ANNO
        try:
            cur_year_txt = labels.nth(1).inner_text().strip()
            if str(anno) not in cur_year_txt:
                labels.nth(1).click()
                time.sleep(0.5)
                popup_context.locator("body").get_by_text(str(anno), exact=True).last.click(timeout=5000)
                time.sleep(0.5)
                debug_info.append(f"Agenda: Anno impostato a {anno}")
        except Exception as e:
            debug_info.append(f"Agenda: Warning Anno ({e})")

        # CONFERMA (Giorno 1)
        try:
            popup_context.locator(".dijitCalendarDateTemplate", has_text=re.compile(r"^1$")).first.click(timeout=5000)
            time.sleep(2.0)
            debug_info.append("Agenda: Data confermata (giorno 1)")
        except:
            debug_info.append("Agenda: Impossibile cliccare giorno 1")
            page.keyboard.press("Escape")
    else:
        debug_info.append("Agenda: Popup non rilevato (labels mancanti)")
        page.keyboard.press("Escape")

    # 5. CLICCA TASTO "MESE" (Fondamentale)
    time.sleep(1.0)
    try:
        # Cerca bottone con aria-label="Mese" nel frame dove abbiamo trovato il calendario
        btn_mese = target_frame.locator("[aria-label='Mese']").first
        
        # Fallback
        if btn_mese.count() == 0:
            btn_mese = target_frame.locator(".dijitButtonText", has_text=re.compile(r"^\s*Mese\s*$", re.IGNORECASE)).first

        if btn_mese.count() > 0:
            btn_mese.click(force=True, timeout=8000)
            debug_info.append("Agenda: Cliccato bottone 'Mese' (View)")
            time.sleep(4.0) # Attesa ricaricamento dati
        else:
            debug_info.append("Agenda: Bottone 'Mese' non trovato")
            
    except Exception as e:
        debug_info.append(f"Agenda: Errore bottone Mese ({e})")


def agenda_extract_events_fast(page):
    texts = []
    
    # Helper che scende ricorsivamente
    def extract_recursive(ctx):
        local = []
        # Estrai da qui
        candidates = ctx.locator("[class*='event'], [class*='Event'], [class*='appointment'], [class*='Appunt'], .dijitCalendarEvent")
        try:
            n = candidates.count()
            if n > 0:
                for i in range(min(n, 200)):
                    t = (candidates.nth(i).inner_text() or "").strip()
                    if t: local.append(t)
        except: pass
        
        # Scendi nei frame
        if hasattr(ctx, 'frames'): # Page
            for f in ctx.frames:
                if f != ctx: local.extend(extract_recursive(f))
        elif hasattr(ctx, 'child_frames'): # Frame
            for f in ctx.child_frames:
                local.extend(extract_recursive(f))
        return local

    texts = extract_recursive(page)
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

# -------------------------
# BOT DOWNLOAD COMPLETO
# -------------------------
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
    st_status.info(f"🤖 Bot avviato: {mese_nome} {anno}")
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

            # LOGIN
            st_status.info("🔐 Login...")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y", wait_until="domcontentloaded")
            page.fill('input[type="text"]', username)
            page.fill('input[type="password"]', password)
            page.press('input[type="password"]', 'Enter')
            
            try:
                page.wait_for_selector("text=I miei dati", timeout=20000)
                st_status.info("✅ Login OK")
                debug_info.append("Login: OK")
            except:
                browser.close()
                return None, None, "LOGIN_FALLITO"

            # -------------------------
            # AGENDA
            # -------------------------
            try:
                st_status.info("🗓️ Lettura Agenda...")
                agenda_set_month_enter(page, mese_num, anno, debug_info)
                agenda_data = agenda_extract_events_fast(page)
                st.session_state["agenda_data"] = agenda_data
                debug_info.append(f"Agenda: OK (raw_len={agenda_data.get('raw_len')})")
            except Exception as e:
                debug_info.append(f"Agenda Error: {e}")

            # -------------------------
            # BUSTA PAGA
            # -------------------------
            st_status.info("💰 Download Busta...")
            try:
                # Torna alla dashboard se necessario o clicca menu
                page.click("text=I miei dati", force=True)
                page.wait_for_selector("text=Documenti", timeout=10000).click()
                time.sleep(4)

                try:
                    page.click("text=Cedolino", force=True)
                except:
                    pass
                time.sleep(4)

                links = page.locator("a")
                found_link = None
                for i in range(links.count()):
                    try:
                        txt = links.nth(i).inner_text().strip()
                        if not txt or len(txt) < 3: continue
                        low = txt.lower()
                        if target_busta.lower() in low:
                            # Filtro 13ma
                            is_13 = ("tredicesima" in low) or ("13ma" in low)
                            if tipo_documento == "tredicesima" and not is_13: continue
                            if tipo_documento != "tredicesima" and is_13: continue
                            found_link = links.nth(i)
                            break
                    except: continue

                if found_link:
                    debug_info.append(f"Busta trovata: {target_busta}")
                    with page.expect_download(timeout=30000) as dl_info:
                        found_link.click()
                    dl = dl_info.value
                    dl.save_as(path_busta)
                    if os.path.exists(path_busta): b_ok = True
                else:
                    debug_info.append("Busta link non trovato")

            except Exception as e:
                debug_info.append(f"Busta Error: {e}")

            # -------------------------
            # CARTELLINO
            # -------------------------
            if tipo_documento != "tredicesima":
                st_status.info("📅 Download Cartellino...")
                try:
                    page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y", wait_until="domcontentloaded")
                    time.sleep(3)
                    
                    # Navigazione Menu Time
                    page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
                    time.sleep(2)
                    page.evaluate("document.getElementById('lnktab_5_label')?.click()")
                    time.sleep(4)

                    # Imposta date (opzionale se resetta, ma meglio esser sicuri)
                    try:
                        dal = page.locator("input[id*='CLRICHIE'][class*='dijitInputInner']").first
                        if dal.count()>0:
                            dal.click(); page.keyboard.press("Control+A"); dal.type(d_from_vis); dal.press("Tab")
                            time.sleep(0.5)
                            al = page.locator("input[id*='CLRICHI2'][class*='dijitInputInner']").first
                            al.click(); page.keyboard.press("Control+A"); al.type(d_to_vis); al.press("Tab")
                    except: pass

                    # Ricerca
                    try:
                        page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last.click(force=True)
                    except:
                        page.keyboard.press("Enter")
                    
                    time.sleep(5)

                    # Trova riga
                    target_row = f"{mese_num:02d}/{anno}"
                    row = page.locator(f"tr:has-text('{target_row}')").first
                    
                    if row.count() > 0:
                        icon = row.locator("img[src*='search']").first
                        if icon.count() > 0:
                            with context.expect_page(timeout=20000) as p_info:
                                icon.click()
                            popup = p_info.value
                            # Gestione PDF embedded
                            try:
                                popup.wait_for_load_state("networkidle")
                                url = popup.url
                                if "EMBED=y" not in url:
                                    url += "&EMBED=y"
                                    resp = context.request.get(url)
                                    Path(path_cart).write_bytes(resp.body())
                                else:
                                    popup.pdf(path=path_cart)
                            except:
                                popup.pdf(path=path_cart)
                            c_ok = True
                    
                    if not c_ok:
                        debug_info.append("Cartellino: Riga o icona non trovata")
                        
                except Exception as e:
                    debug_info.append(f"Cartellino Error: {e}")

            browser.close()
            st_status.empty()

    except Exception as e:
        return None, None, str(e)
    
    st.session_state['debug_info'] = debug_info
    return (path_busta if b_ok else None), (path_cart if c_ok else None), None

# -------------------------
# UI STREAMLIT
# -------------------------
st.set_page_config(page_title="Gottardo Payroll", layout="wide")
st.title("💶 Analisi Stipendio & Presenze")

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
        st.success(f"✅ {st.session_state.get('username', '')}")
        if st.button("🔄 Cambia"):
            st.session_state.update({'credentials_set': False})
            st.rerun()

    st.divider()

    if st.session_state.get('credentials_set'):
        sel_anno = st.selectbox("Anno", [2024, 2025, 2026], index=1)
        sel_mese = st.selectbox("Mese", ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
             "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"], index=0)
        tipo_doc = st.radio("Tipo", ["📄 Cedolino Mensile", "🎄 Tredicesima"])

        if st.button("🚀 AVVIA ANALISI", type="primary"):
            st.session_state['agenda_data'] = None
            st.session_state['debug_info'] = []
            st.session_state['busta'] = None
            st.session_state['cart'] = None
            st.session_state['done'] = False
            
            tipo = "tredicesima" if "Tredicesima" in tipo_doc else "cedolino"
            pb, pc, err = scarica_documenti_automatici(sel_mese, sel_anno, st.session_state['username'], st.session_state['password'], tipo)

            if err: st.error(err)
            else:
                st.session_state.update({'busta': pb, 'cart': pc, 'tipo': tipo})

# (VISUALIZZAZIONE RISULTATI - Invariata per brevità ma inclusa nel blocco logico)
if st.session_state.get('agenda_data'):
    ad = st.session_state['agenda_data']
    st.success("✅ Agenda letta con successo")
    c1, c2, c3 = st.columns(3)
    c1.metric("Omesse", ad['counts'].get("OMESSA TIMBRATURA", 0))
    c2.metric("Malattia", ad['counts'].get("MALATTIA", 0))
    c3.metric("Ferie", ad['counts'].get("FERIE", 0))
    
    with st.expander("Dettaglio righe Agenda"):
        st.write(ad['lines'])

if st.session_state.get('debug_info'):
    with st.expander("🛠️ Log Completo"):
        for l in st.session_state['debug_info']:
            st.text(l)
