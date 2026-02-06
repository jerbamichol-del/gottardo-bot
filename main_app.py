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
    """Genera link per scaricare il PDF"""
    if not os.path.exists(file_path):
        return None
    with open(file_path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    return f'<a href="data:application/pdf;base64,{data}" download="{filename}">📥 Scarica {filename}</a>'


# -------------------------
# AGENDA (AGGIORNATA: LOGICA POPUP CORRETTA)
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
    Imposta mese/anno interagendo direttamente con le label nel popup calendario.
    """
    nomi_mesi_it = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
                    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
    mese_nome_target = nomi_mesi_it[mese_num - 1]

    # 1. Apri popup calendario (icona)
    try:
        # Cerca icona calendario generica (più robusto che ID fisso)
        # Prova classe .calendar16 o .dijitCalendarIcon
        icon = page.locator(".calendar16, .dijitCalendarIcon").first
        # Clicca il genitore (bottone) se esiste, altrimenti l'icona
        btn = icon.locator("xpath=./ancestor::span[@role='button' or contains(@class, 'dijitButton')]").first
        if btn.count() > 0:
            btn.click(timeout=6000)
        else:
            icon.click(timeout=6000)
            
        page.wait_for_selector("div.dijitCalendarMonthLabel", timeout=8000)
        time.sleep(0.5)
        debug_info.append("Agenda: popup calendario aperto")
    except Exception as e:
        debug_info.append(f"Agenda: popup non aperto ({e})")
        return

    # 2. Gestisci Mese e Anno nel popup
    # Le label cliccabili (Mese e Anno) hanno spesso la classe 'dijitCalendarMonthLabel'
    labels = page.locator("div.dijitCalendarMonthLabel")
    
    if labels.count() >= 2:
        # --- MESE (solitamente il primo, index 0) ---
        try:
            cur_month_txt = labels.nth(0).inner_text().strip()
            # Se il mese corrente non è quello target, cambialo
            if cur_month_txt.lower() != mese_nome_target.lower():
                labels.nth(0).click() # Apre menu a tendina
                time.sleep(0.3)
                # Clicca il mese target nel menu
                page.locator(".dijitMenuItemLabel, .dijitMenuItem").filter(has_text=re.compile(f"^{mese_nome_target}$", re.IGNORECASE)).first.click(timeout=3000)
                time.sleep(0.5)
                debug_info.append(f"Agenda: cambiato mese a {mese_nome_target}")
        except Exception as e:
            debug_info.append(f"Agenda: errore cambio mese ({e})")

        # --- ANNO (solitamente il secondo, index 1) ---
        try:
            cur_year_txt = labels.nth(1).inner_text().strip()
            # Se l'anno corrente non è quello target, cambialo
            if str(anno) not in cur_year_txt:
                labels.nth(1).click() # Apre menu o input
                time.sleep(0.3)
                
                # Prova a cliccare l'anno nel menu se appare
                yr_opt = page.locator(".dijitMenuItemLabel, .dijitMenuItem").filter(has_text=str(anno))
                if yr_opt.count() > 0:
                    yr_opt.first.click(timeout=3000)
                    debug_info.append(f"Agenda: cambiato anno a {anno} (menu)")
                else:
                    # Se non c'è nel menu (o è un input), prova a scriverlo
                    page.keyboard.type(str(anno), delay=50)
                    page.keyboard.press("Enter")
                    debug_info.append(f"Agenda: cambiato anno a {anno} (typing)")
                time.sleep(0.5)
        except Exception as e:
            debug_info.append(f"Agenda: errore cambio anno ({e})")

    else:
        debug_info.append("Agenda: labels Mese/Anno non trovate nel popup")

    # 3. Conferma / Chiudi Popup
    # Clicchiamo sul giorno 1 per essere sicuri che prenda la modifica e chiuda il popup
    try:
        page.locator("span.dijitCalendarDateLabel", has_text=re.compile(r"^1$")).first.click(timeout=2000)
        time.sleep(1.0)
        debug_info.append("Agenda: click giorno 1 per conferma")
    except:
        page.keyboard.press("Escape") # Chiudi se rimasto aperto


def agenda_extract_events_fast(page):
    """
    Estrae testo eventi dall'Agenda (best-effort) e conta keyword utili.
    """
    texts = []
    candidates = page.locator("[class*='event'], [class*='Event'], [class*='appointment'], [class*='Appunt']")
    try:
        n = candidates.count()
    except:
        n = 0

    if n > 0:
        for i in range(min(n, 300)):
            t = (candidates.nth(i).inner_text() or "").strip()
            if t:
                texts.append(t)

    if not texts:
        texts = [page.inner_text("body") or ""]

    blob = "\n".join(texts)
    up = blob.upper()

    counts = {k: up.count(k) for k in AGENDA_KEYWORDS}

    lines = []
    for ln in blob.splitlines():
        s = (ln or "").strip()
        if not s:
            continue
        su = s.upper()
        if any(k in su for k in AGENDA_KEYWORDS):
            lines.append(s)

    # dedup preservando ordine
    seen, uniq = set(), []
    for s in lines:
        if s in seen:
            continue
        seen.add(s)
        uniq.append(s)

    return {"counts": counts, "lines": uniq[:200], "raw_len": len(blob)}


def cartellino_parse_deterministico(file_path: str):
    """Parser deterministico per cartellino."""
    text = extract_text_from_pdf(file_path)
    if not text or len(text.strip()) < 20:
        return {
            "giorni_reali": 0.0,
            "gg_presenza": 0.0,
            "ore_ordinarie_riepilogo": 0.0,
            "ore_ordinarie_0251": 0.0,
            "ore_lavorate_0253": 0.0,
            "giorni_senza_badge": 0.0,
            "note": "PDF vuoto o illeggibile.",
            "debug_prime_righe": text[:500] if text else "Nessun testo estratto"
        }

    upper = text.upper()
    debug_text = "\n".join(text.splitlines()[:40])

    has_days = re.search(r"\b[LMGVSD]\d{2}\b", upper) is not None
    has_timbr = "TIMBRATURE" in upper or "TIMBRATURA" in upper

    # Se contiene "nessun dato" e non ha timbrature reali
    if ("NESSUN DATO" in upper or "NESSUNA" in upper) and (not has_days) and (not has_timbr):
        return {
            "giorni_reali": 0.0,
            "gg_presenza": 0.0,
            "ore_ordinarie_riepilogo": 0.0,
            "ore_ordinarie_0251": 0.0,
            "ore_lavorate_0253": 0.0,
            "giorni_senza_badge": 0.0,
            "note": "Cartellino vuoto (testo contiene 'Nessun dato' e nessuna timbratura).",
            "debug_prime_righe": debug_text
        }

    # Giorni timbrati (token unici)
    day_tokens = sorted(set(re.findall(r"\b[LMGVSD]\d{2}\b", upper)))
    giorni_reali = float(len(day_tokens))

    # GG PRESENZA (0265)
    m = re.search(r"0265\s+GG\s+PRESENZA.*?(\d{1,3}[.,]\d{2})", upper)
    gg_presenza = parse_it_number(m.group(1)) if m else 0.0

    # Ore 0251 / 0253
    m1 = re.search(r"0251\s+ORE\s+ORDINARIE.*?(\d{1,3}[.,]\d{2})", upper)
    ore_ord_0251 = parse_it_number(m1.group(1)) if m1 else 0.0

    m2 = re.search(r"0253\s+ORE\s+LAVORATE.*?(\d{1,3}[.,]\d{2})", upper)
    ore_lav_0253 = parse_it_number(m2.group(1)) if m2 else 0.0

    # Riepilogo ore
    ore_riep = 0.0
    for line in text.splitlines():
        ln = line.strip()
        if not ln:
            continue
        if re.search(r"\b02\d{2}\b", ln):
            continue
        if re.match(r"^\d{1,3}[.,]\d{2}(\s+\d{1,3}[.,]\d{2}){2,}$", ln):
            first_num = re.findall(r"\d{1,3}[.,]\d{2}", ln)
            if first_num:
                ore_riep = parse_it_number(first_num[0])
            break

    note_parts = []
    if gg_presenza > 0:
        note_parts.append(f"0265 GG PRESENZA={gg_presenza:.2f}.")
    if ore_ord_0251 > 0:
        note_parts.append(f"0251 ORE ORDINARIE={ore_ord_0251:.2f}.")
    if ore_lav_0253 > 0:
        note_parts.append(f"0253 ORE LAVORATE={ore_lav_0253:.2f}.")
    if ore_riep > 0:
        note_parts.append(f"Riepilogo ore={ore_riep:.2f}.")
    if giorni_reali > 0:
        note_parts.append(f"Token giorni={int(giorni_reali)}.")

    return {
        "giorni_reali": giorni_reali,
        "gg_presenza": gg_presenza,
        "ore_ordinarie_riepilogo": ore_riep,
        "ore_ordinarie_0251": ore_ord_0251,
        "ore_lavorate_0253": ore_lav_0253,
        "giorni_senza_badge": 0.0,
        "note": " ".join(note_parts) if note_parts else "Nessun dato numerico trovato.",
        "debug_prime_righe": debug_text
    }

@st.cache_resource
def get_gemini_models():
    if not HAS_GEMINI:
        return []
    try:
        models = genai.list_models()
        valid = [m for m in models if 'generateContent' in m.supported_generation_methods]
        gemini_list = []
        for m in valid:
            name = m.name.replace('models/', '')
            if 'gemini' in name.lower() and 'embedding' not in name.lower():
                try:
                    gemini_list.append((name, genai.GenerativeModel(name)))
                except:
                    pass

        def priority(n):
            n = n.lower()
            if 'flash' in n and 'lite' not in n:
                return 0
            if 'lite' in n:
                return 1
            if 'pro' in n:
                return 2
            return 3

        gemini_list.sort(key=lambda x: priority(x[0]))
        return gemini_list
    except:
        return []

def estrai_con_fallback(file_path, prompt, tipo, validate_fn=None):
    if not file_path or not os.path.exists(file_path):
        return None

    status = st.empty()

    # 1) GEMINI
    models = get_gemini_models()
    if models:
        try:
            with open(file_path, "rb") as f:
                pdf_bytes = f.read()
        except:
            pdf_bytes = None

        if pdf_bytes and pdf_bytes[:4] == b"%PDF":
            for name, model in models:
                try:
                    status.info(f"🤖 Analisi {tipo} (Gemini: {name})...")
                    resp = model.generate_content([prompt, {"mime_type": "application/pdf", "data": pdf_bytes}])
                    res = clean_json_response(resp.text)

                    if res and isinstance(res, dict):
                        if validate_fn and not validate_fn(res):
                            continue
                        status.success(f"✅ {tipo} analizzato (Gemini)")
                        time.sleep(0.3)
                        status.empty()
                        return res
                except Exception as e:
                    msg = str(e).lower()
                    if "429" in msg or "quota" in msg or "resource_exhausted" in msg:
                        continue
                    continue

    # 2) DEEPSEEK
    if HAS_DEEPSEEK and OpenAI:
        text = extract_text_from_pdf(file_path)
        if text and len(text) > 50:
            try:
                status.warning(f"⚠️ Gemini esausto. Analisi {tipo} con DeepSeek...")
                client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
                full_prompt = f"{prompt}\n\n--- TESTO PDF ---\n{text[:50000]}"

                resp = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[
                        {"role": "system", "content": "Sei un estrattore JSON. Rispondi solo con JSON valido."},
                        {"role": "user", "content": full_prompt}
                    ],
                    temperature=0.1
                )

                res = clean_json_response(resp.choices[0].message.content)
                if res and isinstance(res, dict):
                    if validate_fn and not validate_fn(res):
                        status.error("❌ Dati DeepSeek non validi")
                    else:
                        status.success(f"✅ {tipo} analizzato (DeepSeek)")
                        time.sleep(0.3)
                        status.empty()
                        return res
            except Exception as e:
                status.error(f"Errore DeepSeek: {e}")

    status.error(f"❌ Analisi {tipo} fallita")
    return None


# --- PROMPT AI ---
def get_busta_prompt():
    return """
Analizza CEDOLINO PAGA (Italia). Restituisci SOLO JSON valido.

Campi:
- e_tredicesima: true se è 13ma, altrimenti false.

dati_generali:
- netto: cerca PROGRESSIVI -> netto.
- giorni_pagati: "GG. INPS".

competenze:
- base, straordinari, festivita, anzianita, lordo_totale.

trattenute:
- inps, irpef_netta, addizionali_totali.

ferie: residue_ap, maturate, godute, saldo.
par: residue_ap, spettanti, fruite, saldo.

JSON:
{
    "e_tredicesima": boolean,
    "dati_generali": {"netto": float, "giorni_pagati": float},
    "competenze": {"base": float, "straordinari": float, "festivita": float, "anzianita": float, "lordo_totale": float},
    "trattenute": {"inps": float, "irpef_netta": float, "addizionali_totali": float},
    "ferie": {"residue_ap": float, "maturate": float, "godute": float, "saldo": float},
    "par": {"residue_ap": float, "spettanti": float, "fruite": float, "saldo": float}
}
Se manca -> 0.0.
""".strip()

def get_cartellino_prompt_ai_only():
    return r"""
Analizza CARTELLINO PRESENZE. SOLO JSON.

- giorni_reali: conta giorni con token \b[LMGVSD]\d{2}\b.
- gg_presenza: da "0265 GG PRESENZA".
- ore_ordinarie_0251, ore_lavorate_0253, ore_ordinarie_riepilogo.
- giorni_senza_badge: 0 se incerto.
- debug_prime_righe: prime 30 righe reali.
- note: breve.

JSON:
{
    "giorni_reali": float,
    "gg_presenza": float,
    "ore_ordinarie_riepilogo": float,
    "ore_ordinarie_0251": float,
    "ore_lavorate_0253": float,
    "giorni_senza_badge": float,
    "note": "string",
    "debug_prime_righe": "string"
}
""".strip()

def validate_cartellino_ai_fallback(res):
    try:
        if any(float(res.get(k, 0) or 0) > 0 for k in ["gg_presenza", "ore_ordinarie_0251", "ore_lavorate_0253", "ore_ordinarie_riepilogo", "giorni_reali"]):
            return True
    except:
        pass
    txt = (str(res.get("debug_prime_righe", "")) + " " + str(res.get("note", ""))).upper()
    return ("TIMBRATURE" in txt) or ("GG PRESENZA" in txt) or ("0265" in txt)


# --- BOT DOWNLOAD ---
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

    # Date per cartellino
    last_day = calendar.monthrange(anno, mese_num)[1]
    d_from_vis = f"01/{mese_num:02d}/{anno}"
    d_to_vis = f"{last_day}/{mese_num:02d}/{anno}"

    st_status = st.empty()
    st_status.info(f"🤖 Bot avviato: {mese_nome} {anno}")

    b_ok, c_ok = False, False
    debug_info = []

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
            page.wait_for_selector('input[type="text"]', timeout=10000)
            page.fill('input[type="text"]', username)
            page.fill('input[type="password"]', password)
            page.press('input[type="password"]', 'Enter')
            time.sleep(3)

            try:
                page.wait_for_selector("text=I miei dati", timeout=15000)
                st_status.info("✅ Login OK")
                debug_info.append("Login: OK")
            except:
                st_status.error("❌ Login fallito")
                browser.close()
                return None, None, "LOGIN_FALLITO"

            # -------------------------
            # AGENDA (NUOVO): prima lettura subito dopo login (landing page)
            # -------------------------
            try:
                st_status.info("🗓️ Lettura Agenda (mese)...")
                
                # Attesa che la pagina carichi
                try:
                    page.wait_for_selector(".dijitCalendar, .fc-view, .agenda-view", timeout=8000)
                except:
                    pass 
                
                agenda_set_month_enter(page, mese_num, anno, debug_info)
                agenda_data = agenda_extract_events_fast(page)
                st.session_state["agenda_data"] = agenda_data
                debug_info.append(f"Agenda: OK (raw_len={agenda_data.get('raw_len')})")
                st_status.success("✅ Agenda letta")
            except Exception as e:
                st.session_state["agenda_data"] = None
                debug_info.append(f"Agenda: errore ({e})")

            # BUSTA PAGA
            st_status.info(f"💰 Download busta...")
            try:
                page.click("text=I miei dati", force=True)
                page.wait_for_selector("text=Documenti", timeout=10000).click()
                time.sleep(3)

                try:
                    page.locator("tr", has=page.locator("text=Cedolino")).locator(".z-image").click(timeout=5000)
                except:
                    page.click("text=Cedolino", force=True)

                time.sleep(5)

                links = page.locator("a")
                total_links = links.count()
                link_matches = []

                for i in range(total_links):
                    try:
                        txt = links.nth(i).inner_text().strip()
                        if not txt or len(txt) < 3:
                            continue
                        low = txt.lower()
                        if target_busta.lower() not in low:
                            continue

                        is_13 = ("tredicesima" in low) or ("13ma" in low) or ("xiii" in low)
                        if tipo_documento == "tredicesima" and not is_13:
                            continue
                        if tipo_documento != "tredicesima" and is_13:
                            continue

                        link_matches.append((i, txt))
                    except:
                        continue

                debug_info.append(f"Busta links trovati: {len(link_matches)}")

                if len(link_matches) > 0:
                    link_index, link_txt = link_matches[-1]
                    debug_info.append(f"Busta click: {link_txt}")
                    with page.expect_download(timeout=20000) as download_info:
                        links.nth(link_index).click()
                    download = download_info.value
                    download.save_as(path_busta)
                    if os.path.exists(path_busta) and os.path.getsize(path_busta) > 5000:
                        b_ok = True
                        st_status.success("✅ Busta scaricata")
            except Exception as e:
                debug_info.append(f"Busta errore: {e}")
                st.error(f"Errore busta: {e}")

            # CARTELLINO
            if tipo_documento != "tredicesima":
                st_status.info("📅 Download cartellino...")
                try:
                    # Torna alla home
                    page.evaluate("window.scrollTo(0, 0)")
                    time.sleep(1)
                    try:
                        page.keyboard.press("Escape")
                        time.sleep(0.5)
                    except:
                        pass

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
                    debug_info.append("Cartellino: Menu Time cliccato")

                    # Vai su Cartellino presenze
                    page.evaluate("document.getElementById('lnktab_5_label')?.click()")
                    time.sleep(5)
                    debug_info.append("Cartellino: Tab Cartellino cliccato")

                    # Imposta date
                    try:
                        dal = page.locator("input[id*='CLRICHIE'][class*='dijitInputInner']").first
                        al = page.locator("input[id*='CLRICHI2'][class*='dijitInputInner']").first

                        if dal.count() > 0 and al.count() > 0:
                            dal.click(force=True)
                            page.keyboard.press("Control+A")
                            dal.fill("")
                            dal.type(d_from_vis, delay=80)
                            dal.press("Tab")
                            time.sleep(0.5)

                            al.click(force=True)
                            page.keyboard.press("Control+A")
                            al.fill("")
                            al.type(d_to_vis, delay=80)
                            al.press("Tab")
                            time.sleep(0.5)
                            debug_info.append(f"Cartellino: Date impostate {d_from_vis} - {d_to_vis}")
                    except Exception as e:
                        debug_info.append(f"Cartellino: Errore date: {e}")

                    # Esegui ricerca
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(0.5)
                    try:
                        page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last.click(force=True)
                        debug_info.append("Cartellino: Esegui ricerca cliccato")
                    except:
                        page.keyboard.press("Enter")
                        debug_info.append("Cartellino: Enter premuto")

                    # Attendi risultati
                    time.sleep(5)
                    try:
                        page.wait_for_selector("text=Risultati della ricerca", timeout=15000)
                        debug_info.append("Cartellino: Risultati trovati")
                    except:
                        debug_info.append("Cartellino: Nessun risultato visibile")

                    # Trova riga e lente
                    target_cart_row = f"{mese_num:02d}/{anno}"
                    riga_row = page.locator(f"tr:has-text('{target_cart_row}')").first

                    icona = None
                    if riga_row.count() > 0 and riga_row.locator("img[src*='search']").count() > 0:
                        icona = riga_row.locator("img[src*='search']").first
                        debug_info.append(f"Cartellino: Riga {target_cart_row} trovata con icona")
                    else:
                        icona = page.locator("img[src*='search']").first
                        debug_info.append(f"Cartellino: Fallback prima icona search")

                    if icona is None or icona.count() == 0:
                        debug_info.append("Cartellino: Nessuna icona trovata, salvo pagina")
                        page.pdf(path=path_cart)
                    else:
                        with context.expect_page(timeout=20000) as popup_info:
                            icona.click()
                        popup = popup_info.value
                        debug_info.append(f"Cartellino: Popup aperto")

                        popup_url = (popup.url or "").replace("/js_rev//", "/js_rev/")
                        if "EMBED=y" not in popup_url:
                            popup_url = popup_url + ("&" if "?" in popup_url else "?") + "EMBED=y"

                        debug_info.append(f"Cartellino: URL = {popup_url[:120]}...")

                        resp = context.request.get(popup_url, timeout=60000)
                        body = resp.body()

                        if body[:4] == b"%PDF":
                            Path(path_cart).write_bytes(body)
                            debug_info.append(f"Cartellino: PDF salvato ({len(body)} bytes)")
                        else:
                            debug_info.append(f"Cartellino: Risposta non PDF, provo stampa")
                            try:
                                popup.pdf(path=path_cart, format="A4")
                            except:
                                page.pdf(path=path_cart)

                        try:
                            popup.close()
                        except:
                            pass

                    if os.path.exists(path_cart) and os.path.getsize(path_cart) > 5000:
                        c_ok = True
                        st_status.success("✅ Cartellino OK")
                    else:
                        st.warning("⚠️ Cartellino scaricato ma sembra piccolo/vuoto")

                except Exception as e:
                    debug_info.append(f"Cartellino: Errore {e}")
                    st.error(f"❌ Errore cartellino: {e}")
                    try:
                        page.pdf(path=path_cart)
                    except:
                        pass

            browser.close()
            st_status.empty()

    except Exception as e:
        st_status.error(f"Errore: {e}")
        return None, None, str(e)

    # Salva debug info
    st.session_state['debug_info'] = debug_info

    return (path_busta if b_ok else None), (path_cart if c_ok else None), None


# --- UI APP ---
st.set_page_config(page_title="Gottardo Payroll", page_icon="💶", layout="wide")
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
            st.session_state.pop('username', None)
            st.session_state.pop('password', None)
            st.rerun()

    st.divider()

    if st.session_state.get('credentials_set'):
        sel_anno = st.selectbox("Anno", [2024, 2025, 2026], index=1)
        sel_mese = st.selectbox(
            "Mese",
            ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
             "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"],
            index=0  # Gennaio default
        )
        tipo_doc = st.radio("Tipo", ["📄 Cedolino Mensile", "🎄 Tredicesima"])

        if st.button("🚀 AVVIA ANALISI", type="primary"):
            for k in ["done", "busta", "cart", "db", "dc", "tipo", "debug_info",
                      "path_busta_temp", "path_cart_temp", "agenda_data"]:
                st.session_state.pop(k, None)

            tipo = "tredicesima" if "Tredicesima" in tipo_doc else "cedolino"
            pb, pc, err = scarica_documenti_automatici(sel_mese, sel_anno, st.session_state['username'], st.session_state['password'], tipo)

            if err == "LOGIN_FALLITO":
                st.error("LOGIN FALLITO")
            elif err:
                st.error(err)
            else:
                st.session_state.update({
                    'busta': pb,
                    'cart': pc,
                    'tipo': tipo,
                    'done': False,
                    'path_busta_temp': pb,
                    'path_cart_temp': pc
                })
    else:
        st.warning("⚠️ Inserisci le credenziali")

# RISULTATI
if st.session_state.get('busta') or st.session_state.get('cart'):
    if not st.session_state.get('done'):
        with st.spinner("🧠 Analisi..."):
            db = None
            if st.session_state.get('busta'):
                db = estrai_con_fallback(st.session_state.get('busta'), get_busta_prompt(), "Busta Paga")

            dc = None
            if st.session_state.get('cart'):
                dc = cartellino_parse_deterministico(st.session_state.get('cart'))
                # Se il parser deterministico non trova nulla di utile, prova AI
                if dc and dc.get('giorni_reali', 0) == 0 and dc.get('gg_presenza', 0) == 0:
                    dc_ai = estrai_con_fallback(
                        st.session_state.get('cart'),
                        get_cartellino_prompt_ai_only(),
                        "Cartellino",
                        validate_fn=validate_cartellino_ai_fallback
                    )
                    if dc_ai and (dc_ai.get('giorni_reali', 0) > 0 or dc_ai.get('gg_presenza', 0) > 0):
                        dc = dc_ai

            st.session_state.update({'db': db, 'dc': dc, 'done': True})

            # NON eliminiamo i file per debug

    db = st.session_state.get('db')
    dc = st.session_state.get('dc')
    tipo = st.session_state.get('tipo', 'cedolino')

    # Costante: settimana 6 giorni
    ORE_PER_GIORNO = 6.67

    if db and db.get('e_tredicesima'):
        st.success("🎄 **Cedolino TREDICESIMA**")

    st.divider()

    tab1, tab2, tab3, tab4 = st.tabs(["💰 Dettaglio Stipendio", "📅 Cartellino & Presenze", "📊 Analisi & Confronto", "🔧 Debug"])

    with tab1:
        if db:
            dg = db.get('dati_generali', {})
            comp = db.get('competenze', {})
            tratt = db.get('trattenute', {})
            ferie = db.get('ferie', {})
            par = db.get('par', {})

            k1, k2, k3 = st.columns(3)
            k1.metric("💵 NETTO IN BUSTA", f"€ {float(dg.get('netto', 0) or 0):.2f}", delta="Pagamento")
            k2.metric("📊 Lordo Totale", f"€ {float(comp.get('lordo_totale', 0) or 0):.2f}")
            k3.metric("📆 GG INPS (Busta)", int(float(dg.get('giorni_pagati', 0) or 0)))

            st.markdown("---")

            c_entr, c_usc = st.columns(2)
            with c_entr:
                st.subheader("➕ Competenze")
                st.write(f"**Paga Base:** € {float(comp.get('base', 0) or 0):.2f}")
                if float(comp.get('anzianita', 0) or 0) > 0:
                    st.write(f"**Anzianità:** € {float(comp.get('anzianita', 0) or 0):.2f}")
                if float(comp.get('straordinari', 0) or 0) > 0:
                    st.write(f"**Straordinari/Suppl.:** € {float(comp.get('straordinari', 0) or 0):.2f}")
                if float(comp.get('festivita', 0) or 0) > 0:
                    st.write(f"**Festività/Maggiorazioni:** € {float(comp.get('festivita', 0) or 0):.2f}")

            with c_usc:
                st.subheader("➖ Trattenute")
                st.write(f"**Contributi INPS:** € {float(tratt.get('inps', 0) or 0):.2f}")
                st.write(f"**IRPEF Netta:** € {float(tratt.get('irpef_netta', 0) or 0):.2f}")
                if float(tratt.get('addizionali_totali', 0) or 0) > 0:
                    st.write(f"**Addizionali:** € {float(tratt.get('addizionali_totali', 0) or 0):.2f}")

            with st.expander("🏖️ Ferie / Permessi"):
                f1, f2 = st.columns(2)
                with f1:
                    st.write("**FERIE**")
                    st.write(f"Residue AP: {float(ferie.get('residue_ap', 0) or 0):.2f}")
                    st.write(f"Maturate: {float(ferie.get('maturate', 0) or 0):.2f}")
                    st.write(f"Godute: {float(ferie.get('godute', 0) or 0):.2f}")
                    st.write(f"**Saldo: {float(ferie.get('saldo', 0) or 0):.2f}**")
                with f2:
                    st.write("**PAR**")
                    st.write(f"Residue AP: {float(par.get('residue_ap', 0) or 0):.2f}")
                    st.write(f"Spettanti: {float(par.get('spettanti', 0) or 0):.2f}")
                    st.write(f"Fruite: {float(par.get('fruite', 0) or 0):.2f}")
                    st.write(f"**Saldo: {float(par.get('saldo', 0) or 0):.2f}**")
        else:
            st.warning("⚠️ Dati busta non disponibili")

    with tab2:
        if dc:
            c1, c2 = st.columns([1, 2])
            with c1:
                gg_presenza = float(dc.get('gg_presenza', 0) or 0)
                giorni_reali = float(dc.get('giorni_reali', 0) or 0)

                if gg_presenza > 0:
                    st.metric("📅 GG Presenza (Cartellino)", gg_presenza)
                elif giorni_reali > 0:
                    st.metric("📅 Giorni timbrati (token)", giorni_reali)
                else:
                    st.metric("📅 Presenze", "N/D")

                anom = float(dc.get('giorni_senza_badge', 0) or 0)
                if anom > 0:
                    st.metric("⚠️ Anomalie Badge", anom, delta="Controlla")
                else:
                    st.metric("✅ Anomalie Badge", 0, delta="OK")

            with c2:
                st.info(f"**📝 Note:** {dc.get('note', '')}")

                ore_ord = float(dc.get("ore_ordinarie_0251", 0) or 0)
                ore_lav = float(dc.get("ore_lavorate_0253", 0) or 0)
                ore_riep = float(dc.get("ore_ordinarie_riepilogo", 0) or 0)

                if ore_ord > 0 or ore_lav > 0 or ore_riep > 0:
                    with st.expander("⏱️ Dettaglio Ore"):
                        st.write(f"**0251 Ore Ordinarie:** {ore_ord:.2f}")
                        st.write(f"**0253 Ore Lavorate:** {ore_lav:.2f}")
                        st.write(f"**Riepilogo Ore:** {ore_riep:.2f}")

                if dc.get('debug_prime_righe'):
                    with st.expander("🔍 Debug: Contenuto PDF"):
                        st.code(dc['debug_prime_righe'], language=None)
        else:
            if tipo == "tredicesima":
                st.warning("⚠️ Cartellino non disponibile (Tredicesima)")
            else:
                st.error("❌ Errore cartellino")

    with tab3:
        if db and dc:
            gg_inps = float(db.get('dati_generali', {}).get('giorni_pagati', 0) or 0)
            gg_presenza = float(dc.get('gg_presenza', 0) or 0)
            giorni_reali = float(dc.get('giorni_reali', 0) or 0)
            val_cart = gg_presenza if gg_presenza > 0 else giorni_reali

            st.subheader("🔍 Analisi Discrepanze")
            col_a, col_b = st.columns(2)
            col_a.metric("GG INPS (Busta)", gg_inps)
            col_b.metric("GG Cartellino", val_cart, delta=f"{(val_cart - gg_inps):.1f}")

            # Calcolo ore (settimana 6 giorni = 6.67h/gg)
            ore_attese = gg_inps * ORE_PER_GIORNO
            ore_effettive = float(
                dc.get("ore_ordinarie_0251", 0) or
                dc.get("ore_lavorate_0253", 0) or
                dc.get("ore_ordinarie_riepilogo", 0) or 0
            )

            if ore_effettive > 0:
                st.markdown("---")
                st.subheader("⏱️ Confronto Ore")
                c_ore1, c_ore2 = st.columns(2)
                c_ore1.metric("Ore Attese (6.67h/gg)", f"{ore_attese:.2f}")
                c_ore2.metric("Ore Effettive", f"{ore_effettive:.2f}", delta=f"{(ore_effettive - ore_attese):.2f}")
                st.caption("📌 Calcolo: 40h settimanali ÷ 6 giorni = 6.67h/giorno")

        elif tipo == "tredicesima":
            st.info("Analisi non disponibile per Tredicesima")
        else:
            st.warning("Servono entrambi i documenti")

    with tab4:
        st.subheader("🔧 Debug Bot")

        # AGENDA (NUOVO): mostra risultati lettura
        ad = st.session_state.get("agenda_data")
        st.markdown("### Agenda (mese)")
        if ad:
            cA1, cA2, cA3 = st.columns(3)
            cA1.metric("Omesse timbrature", ad["counts"].get("OMESSA TIMBRATURA", 0))
            cA2.metric("Malattia", ad["counts"].get("MALATTIA", 0))
            cA3.metric("Riposo", ad["counts"].get("RIPOSO", 0))

            with st.expander("Dettagli Agenda (righe trovate)"):
                for s in ad.get("lines", []):
                    st.text(s)
        else:
            st.info("Agenda non letta / non disponibile.")

        st.markdown("---")

        # Mostra log debug
        if st.session_state.get('debug_info'):
            st.write("**Log operazioni:**")
            for line in st.session_state['debug_info']:
                st.text(f"• {line}")

        st.markdown("---")

        # Download PDF per verifica manuale
        st.write("**Scarica PDF per verifica:**")
        col_d1, col_d2 = st.columns(2)

        with col_d1:
            pb = st.session_state.get('path_busta_temp')
            if pb and os.path.exists(pb):
                link = get_pdf_download_link(pb, "busta.pdf")
                if link:
                    st.markdown(link, unsafe_allow_html=True)
                    st.caption(f"Dimensione: {os.path.getsize(pb)} bytes")
            else:
                st.write("Busta non disponibile")

        with col_d2:
            pc = st.session_state.get('path_cart_temp')
            if pc and os.path.exists(pc):
                link = get_pdf_download_link(pc, "cartellino.pdf")
                if link:
                    st.markdown(link, unsafe_allow_html=True)
                    st.caption(f"Dimensione: {os.path.getsize(pc)} bytes")
            else:
                st.write("Cartellino non disponibile")
