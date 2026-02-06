import sys
import asyncio
import re
import os
import streamlit as st
import google.generativeai as genai
from playwright.sync_api import sync_playwright
import json
import time
import calendar
import locale
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

# --- OPTIONAL: DeepSeek (OpenAI-compatible) + PDF text extraction ---
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None


# ==============================================================================
# Setup
# ==============================================================================
os.system("playwright install chromium")

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

try:
    locale.setlocale(locale.LC_TIME, "it_IT.UTF-8")
except Exception:
    pass


def get_credentials():
    if "credentials_set" in st.session_state and st.session_state.get("credentials_set"):
        return st.session_state.get("username"), st.session_state.get("password")
    try:
        return st.secrets["ZK_USER"], st.secrets["ZK_PASS"]
    except Exception:
        return None, None


# --- Keys ---
try:
    GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
except Exception:
    st.error("❌ Google API Key mancante in secrets")
    st.stop()

try:
    DEEPSEEK_API_KEY = st.secrets["DEEPSEEK_API_KEY"]
except Exception:
    DEEPSEEK_API_KEY = None


# ==============================================================================
# Gemini model autodiscovery
# ==============================================================================
genai.configure(api_key=GOOGLE_API_KEY)


@st.cache_resource
def inizializza_modelli_gemini():
    try:
        tutti_modelli = genai.list_models()
        modelli_validi = [
            m for m in tutti_modelli if "generateContent" in m.supported_generation_methods
        ]

        modelli_gemini = []
        for m in modelli_validi:
            nome_pulito = m.name.replace("models/", "")
            if "gemini" in nome_pulito.lower() and "embedding" not in nome_pulito.lower():
                try:
                    modelli_gemini.append((nome_pulito, genai.GenerativeModel(nome_pulito)))
                except Exception:
                    continue

        if not modelli_gemini:
            st.error("❌ Nessun modello Gemini disponibile")
            st.stop()

        def priorita(nome: str) -> int:
            n = nome.lower()
            if "flash" in n and "lite" not in n:
                return 0
            if "lite" in n:
                return 1
            if "pro" in n:
                return 2
            return 3

        modelli_gemini.sort(key=lambda x: priorita(x[0]))
        return modelli_gemini
    except Exception as e:
        st.error(f"❌ Errore caricamento modelli: {e}")
        st.stop()


MODELLI_DISPONIBILI = inizializza_modelli_gemini()

if "modelli_mostrati" not in st.session_state:
    st.sidebar.success(f"✅ {len(MODELLI_DISPONIBILI)} modelli AI pronti")
    st.session_state["modelli_mostrati"] = True


# ==============================================================================
# AGENDA - VERSIONE CORRETTA CON API INTERCEPT
# ==============================================================================

# Codici evento del calendario Gottardo (scoperti via scraping)
CALENDAR_CODES = {
    "FEP": "FERIE PIANIFICATE",
    "OMT": "OMESSA TIMBRATURA",
    "RCS": "RIPOSO COMPENSATIVO SUCCESSIVO",
    "RIC": "RIPOSO COMPENSATIVO FORZATO",
    "MAL": "MALATTIA"
}

AGENDA_KEYWORDS = [
    "OMESSA TIMBRATURA",
    "MALATTIA",
    "RIPOSO",
    "FERIE",
    "PERMESS",
    "CHIUSURA",
    "INFORTUN",
    "ASSENZA",
    "ANOMALIA",
]

MONTH_ABBR_IT = {
    1: "gen", 2: "feb", 3: "mar", 4: "apr", 5: "mag", 6: "giu",
    7: "lug", 8: "ago", 9: "set", 10: "ott", 11: "nov", 12: "dic",
}
MONTH_FULL_IT = {
    1: "Gennaio", 2: "Febbraio", 3: "Marzo", 4: "Aprile",
    5: "Maggio", 6: "Giugno", 7: "Luglio", 8: "Agosto",
    9: "Settembre", 10: "Ottobre", 11: "Novembre", 12: "Dicembre",
}


def _get_calendar_frame(page):
    """
    Trova il frame che contiene il calendario (CalUIFrame.jsp).
    IMPORTANTE: Il calendario NON è nel contesto principale della pagina!
    """
    for frame in page.frames:
        url = frame.url or ""
        if "CalUIFrame" in url or "CalUI" in url or "calendar" in url.lower():
            return frame
    return None


def _iter_contexts_with_calendar(page):
    """Itera su tutti i contesti, dando priorità al frame del calendario."""
    cal_frame = _get_calendar_frame(page)
    if cal_frame:
        yield cal_frame
    for frame in page.frames:
        if frame != cal_frame:
            yield frame
    yield page


def agenda_read_via_api(page, context, mese_num, anno, debug_log):
    """
    METODO PRINCIPALE: Legge l'agenda tramite chiamate API dirette.
    
    Le API del portale Gottardo sono:
    - /api/time/v2/events?$filter_api=calendarCode=XXX,startTime=...,endTime=...
    - /api/time/v2/timeoffbalances?$filter_api=year=XXXX
    """
    debug_log.append("📡 Metodo API: chiamate dirette...")
    
    api_responses = {
        "events": {},
        "balances": None,
    }
    
    base_url = "https://selfservice.gottardospa.it/js_rev/JSipert2"
    
    # Chiamate API per ogni tipo di evento
    for code, name in CALENDAR_CODES.items():
        try:
            url = f"{base_url}/api/time/v2/events?$filter_api=calendarCode={code},startTime={anno}-01-01T00:00:00,endTime={anno}-12-31T00:00:00"
            resp = context.request.get(url, timeout=15000)
            
            if resp.ok:
                try:
                    data = resp.json()
                    if data:
                        api_responses["events"][code] = data
                        count = len(data) if isinstance(data, list) else 1
                        debug_log.append(f"  ✅ {code} ({name}): {count} eventi")
                except Exception:
                    pass
        except Exception as e:
            debug_log.append(f"  ⚠️ {code}: {type(e).__name__}")
    
    # Saldo ferie/permessi
    try:
        url = f"{base_url}/api/time/v2/timeoffbalances?$filter_api=year={anno}"
        resp = context.request.get(url, timeout=10000)
        if resp.ok:
            api_responses["balances"] = resp.json()
            debug_log.append("  ✅ Saldo ferie/permessi OK")
    except Exception:
        pass
    
    # Processa risultati
    return _process_api_responses(api_responses, mese_num, anno, debug_log)


def _process_api_responses(api_responses, mese_num, anno, debug_log):
    """Elabora le risposte API e costruisce il risultato finale."""
    result = {
        "counts": {k: 0 for k in AGENDA_KEYWORDS},
        "lines": [],
        "raw_len": 0,
        "items_found": 0,
        "api_data": api_responses,
        "events_by_type": {}
    }
    
    all_events = []
    
    for code, events in api_responses.get("events", {}).items():
        if not isinstance(events, list):
            events = [events] if events else []
        
        code_name = CALENDAR_CODES.get(code, code)
        events_this_month = []
        
        for event in events:
            try:
                # Filtra per mese
                start_time = event.get("startTime", "") or event.get("start", "") or ""
                if start_time and len(start_time) >= 7:
                    try:
                        event_month = int(start_time[5:7])
                        if event_month != mese_num:
                            continue
                    except ValueError:
                        pass
                
                summary = event.get("summary", "") or event.get("description", "") or code_name
                event_str = f"{code}: {summary}"
                all_events.append(event_str)
                events_this_month.append(event)
                
                # Conta keywords
                summary_upper = (summary + " " + code_name).upper()
                for kw in AGENDA_KEYWORDS:
                    if kw in summary_upper:
                        result["counts"][kw] += 1
                        
            except Exception:
                continue
        
        if events_this_month:
            result["events_by_type"][code_name] = len(events_this_month)
    
    result["items_found"] = len(all_events)
    result["lines"] = list(set(all_events))[:200]
    result["raw_len"] = sum(len(e) for e in all_events)
    
    debug_log.append(f"  📊 Totale: {result['items_found']} eventi nel mese {mese_num}")
    
    return result


def agenda_read_via_dom(page, mese_num, anno, debug_log):
    """
    Metodo DOM (fallback): cerca gli eventi nel frame del calendario.
    """
    debug_log.append("🔍 Metodo DOM (fallback)...")
    
    result = {
        "counts": {k: 0 for k in AGENDA_KEYWORDS},
        "lines": [],
        "raw_len": 0,
        "items_found": 0
    }
    
    # Trova il frame del calendario
    cal_frame = _get_calendar_frame(page)
    if cal_frame:
        debug_log.append(f"  Frame calendario: {cal_frame.url[:60]}...")
        result = _extract_events_from_context(cal_frame, debug_log)
    else:
        debug_log.append("  ⚠️ Frame calendario non trovato, cerco ovunque...")
        for ctx in _iter_contexts_with_calendar(page):
            temp_result = _extract_events_from_context(ctx, debug_log)
            if temp_result["items_found"] > result["items_found"]:
                result = temp_result
    
    return result


def _extract_events_from_context(ctx, debug_log):
    """Estrae eventi da un contesto (frame o page)."""
    result = {
        "counts": {k: 0 for k in AGENDA_KEYWORDS},
        "lines": [],
        "raw_len": 0,
        "items_found": 0
    }
    
    selectors = [
        ".dojoxCalendarEvent",
        ".dijitCalendarEvent",
        "[class*='CalendarEvent']",
    ]
    
    texts = []
    
    for sel in selectors:
        try:
            loc = ctx.locator(sel)
            count = loc.count()
            
            if count > 0:
                debug_log.append(f"  📌 {sel}: {count} elementi")
                
                for i in range(min(count, 500)):
                    try:
                        el = loc.nth(i)
                        text = ""
                        try:
                            text = (el.inner_text() or "").strip()
                        except Exception:
                            pass
                        
                        title = ""
                        try:
                            title = (el.get_attribute("title") or "").strip()
                        except Exception:
                            pass
                        
                        combo = " ".join([x for x in [text, title] if x]).strip()
                        if combo:
                            texts.append(combo)
                    except Exception:
                        continue
        except Exception:
            continue
    
    for text in texts:
        text_upper = text.upper()
        for kw in AGENDA_KEYWORDS:
            if kw in text_upper:
                result["counts"][kw] += 1
                if text not in result["lines"]:
                    result["lines"].append(text)
    
    result["items_found"] = len(texts)
    result["raw_len"] = sum(len(t) for t in texts)
    result["lines"] = result["lines"][:200]
    
    return result


def agenda_read_month(page, mese_num, anno, debug_log, context=None):
    """
    Funzione principale per leggere l'agenda.
    
    Strategia:
    1. Prova prima con le API dirette (più affidabile)
    2. Fallback sul DOM nel frame calendario
    """
    debug_log.append(f"=== AGENDA: Lettura {MONTH_FULL_IT.get(mese_num, mese_num)} {anno} ===")
    
    # Ottieni context se non passato
    if context is None:
        context = page.context if hasattr(page, 'context') else None
    
    # Metodo 1: API (preferito)
    if context:
        try:
            result = agenda_read_via_api(page, context, mese_num, anno, debug_log)
            if result["items_found"] > 0 or result.get("api_data", {}).get("events"):
                debug_log.append("✅ Dati ottenuti via API")
                return result
        except Exception as e:
            debug_log.append(f"⚠️ API fallita: {type(e).__name__}: {str(e)[:100]}")
    
    # Metodo 2: DOM (fallback)
    try:
        result = agenda_read_via_dom(page, mese_num, anno, debug_log)
        if result["items_found"] > 0:
            debug_log.append("✅ Dati ottenuti via DOM")
            return result
    except Exception as e:
        debug_log.append(f"⚠️ DOM fallito: {type(e).__name__}")
    
    debug_log.append("❌ Nessun dato agenda trovato")
    return {
        "counts": {k: 0 for k in AGENDA_KEYWORDS},
        "lines": [],
        "raw_len": 0,
        "items_found": 0
    }


# ==============================================================================
# AI parsing
# ==============================================================================
def clean_json_response(text):
    try:
        if not text:
            return None
        text = re.sub(r"```json|```", "", text).strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        payload = text[start:end] if start != -1 else text
        return json.loads(payload)
    except Exception:
        return None


def extract_text_from_pdf_any(file_path: str):
    if not file_path or not os.path.exists(file_path):
        return None

    if fitz is not None:
        try:
            doc = fitz.open(file_path)
            chunks = []
            for p in doc:
                chunks.append(p.get_text())
            txt = "\n".join(chunks).strip()
            return txt if txt else None
        except Exception:
            pass

    if PdfReader is not None:
        try:
            reader = PdfReader(file_path)
            chunks = []
            for page in reader.pages:
                chunks.append((page.extract_text() or ""))
            txt = "\n".join(chunks).strip()
            return txt if txt else None
        except Exception:
            pass

    return None


def estrai_con_fallback(file_path, prompt, tipo="documento"):
    if not file_path or not os.path.exists(file_path):
        return None

    with open(file_path, "rb") as f:
        bytes_data = f.read()

    if bytes_data[:4] != b"%PDF":
        st.error(f"❌ Il file {tipo} non è un PDF valido")
        return None

    progress = st.empty()
    last_err = None

    for idx, (nome, model) in enumerate(MODELLI_DISPONIBILI, 1):
        try:
            progress.info(f"🔄 Analisi {tipo}: modello {idx}/{len(MODELLI_DISPONIBILI)} ({nome})...")
            resp = model.generate_content([prompt, {"mime_type": "application/pdf", "data": bytes_data}])
            out = clean_json_response(getattr(resp, "text", ""))
            if out and isinstance(out, dict):
                progress.success(f"✅ {tipo.capitalize()} analizzato!")
                time.sleep(0.5)
                progress.empty()
                return out
        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if "429" in msg or "quota" in msg or "resource_exhausted" in msg:
                continue
            continue

    if DEEPSEEK_API_KEY and OpenAI is not None:
        try:
            progress.warning(f"⚠️ Quote Gemini esaurite. Fallback DeepSeek per {tipo}...")
            txt = extract_text_from_pdf_any(file_path)
            if not txt or len(txt.strip()) < 50:
                progress.error("❌ DeepSeek: testo PDF non estraibile (probabile PDF a immagini).")
                time.sleep(0.5)
                progress.empty()
                return None

            client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
            full_prompt = prompt + "\n\n--- TESTO ESTRATTO DAL PDF ---\n" + txt[:25000]
            r = client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "Rispondi solo con JSON valido, senza testo extra."},
                    {"role": "user", "content": full_prompt},
                ],
                temperature=0.1,
            )
            out = clean_json_response(r.choices[0].message.content)
            if out and isinstance(out, dict):
                progress.success(f"✅ {tipo.capitalize()} analizzato (DeepSeek)!")
                time.sleep(0.5)
                progress.empty()
                return out
        except Exception:
            pass

    progress.error(f"❌ Analisi {tipo} fallita.")
    if last_err:
        with st.expander("🔎 Ultimo errore AI"):
            st.code(str(last_err)[:2000])
    return None


def estrai_dati_busta_dettagliata(file_path):
    prompt = """
Questo è un CEDOLINO PAGA GOTTARDO S.p.A. italiano. Segui ESATTAMENTE queste istruzioni:

**1. DATI GENERALI (PRIMA PAGINA, RIGA PROGRESSIVI):**
- NETTO: riga "PROGRESSIVI" in fondo, colonna finale prima di "ESTREMI ELABORAZIONE"
- GIORNI PAGATI: riga con "GG. INPS"
- ORE ORDINARIE: "ORE INAIL" oppure giorni_pagati × 8

**2. COMPETENZE:**
- base: "RETRIBUZIONE ORDINARIA" (voce 1000) colonna COMPETENZE
- straordinari: somma voci "STRAORDINARIO"/"SUPPLEMENTARI"/"NOTTURNI"
- festivita: somma voci "MAGG. FESTIVE"/"FESTIVITA GODUTA"
- anzianita: voci "SCATTI"/"EDR"/"ANZ." altrimenti 0
- lordo_totale: "TOTALE COMPETENZE" o "PROGRESSIVI" colonna totale

**3. TRATTENUTE:**
- inps: sezione I.N.P.S.
- irpef_netta: sezione FISCALI
- addizionali_totali: add.reg + add.com (se presenti, altrimenti 0)

**4. FERIE/PAR:**
- tabella ferie in alto a destra (RES.PREC / SPETTANTI / FRUITE / SALDO)

**5. TREDICESIMA:**
- se trovi "TREDICESIMA"/"13MA" -> e_tredicesima=true

IMPORTANTE:
- valori mancanti = 0
- separatore decimale = punto
- Output SOLO JSON:

{
  "e_tredicesima": false,
  "dati_generali": {"netto": 0.0, "giorni_pagati": 0.0, "ore_ordinarie": 0.0},
  "competenze": {"base": 0.0, "anzianita": 0.0, "straordinari": 0.0, "festivita": 0.0, "lordo_totale": 0.0},
  "trattenute": {"inps": 0.0, "irpef_netta": 0.0, "addizionali_totali": 0.0},
  "ferie": {"residue_ap": 0.0, "maturate": 0.0, "godute": 0.0, "saldo": 0.0},
  "par": {"residue_ap": 0.0, "spettanti": 0.0, "fruite": 0.0, "saldo": 0.0}
}
""".strip()
    return estrai_con_fallback(file_path, prompt, tipo="busta paga")


def estrai_dati_cartellino(file_path):
    prompt = r"""
Analizza questo cartellino presenze GOTTARDO S.p.A.

- Se trovi tabella timbrature con righe tipo L01/M02/... e E/U: conta giorni con almeno una timbratura.
- Se il PDF è vuoto (solo ricerca): giorni_reali=0.

OUTPUT (solo JSON):
{
  "giorni_reali": 0,
  "giorni_senza_badge": 0,
  "note": "",
  "debug_prime_righe": ""
}
""".strip()
    return estrai_con_fallback(file_path, prompt, tipo="cartellino")


# ==============================================================================
# Cleanup
# ==============================================================================
def pulisci_file(path_busta, path_cart):
    deleted = []
    if path_busta and os.path.exists(path_busta):
        try:
            os.remove(path_busta)
            deleted.append(os.path.basename(path_busta))
        except Exception:
            pass
    if path_cart and os.path.exists(path_cart):
        try:
            os.remove(path_cart)
            deleted.append(os.path.basename(path_cart))
        except Exception:
            pass
    if deleted:
        st.info(f"🗑️ File eliminati: {', '.join(deleted)}")


# ==============================================================================
# Navigation helpers
# ==============================================================================
def safe_click_mydata(page, debug_log=None):
    try:
        page.keyboard.press("Escape")
        time.sleep(0.2)
    except Exception:
        pass

    try:
        page.evaluate(
            """
            (() => {
              const t = document.querySelector('#searchMenuTooltip');
              if (t) t.style.display = 'none';
              const p = document.querySelector('#popup_1');
              if (p) p.style.display = 'none';
            })();
            """
        )
    except Exception:
        pass

    try:
        page.evaluate("document.getElementById('revit_navigation_NavHoverItem_0_label')?.click()")
        if debug_log is not None:
            debug_log.append("MyData: click ok (js id)")
        time.sleep(1.0)
        return True
    except Exception:
        pass

    try:
        page.locator("text=I miei dati").first.click(force=True, timeout=8000)
        if debug_log is not None:
            debug_log.append("MyData: click ok (force text)")
        time.sleep(1.0)
        return True
    except Exception as e:
        if debug_log is not None:
            debug_log.append(f"MyData: click fail ({type(e).__name__})")
        return False


def open_documenti(page, debug_log=None):
    try:
        page.keyboard.press("Escape")
        time.sleep(0.2)
    except Exception:
        pass

    if not safe_click_mydata(page, debug_log or []):
        return False

    try:
        page.wait_for_selector("span[id^='lnktab_']", timeout=15000)
    except Exception:
        pass

    for js_id in ["lnktab_2_label", "lnktab_2"]:
        try:
            page.evaluate(f"document.getElementById('{js_id}')?.click()")
            if debug_log is not None:
                debug_log.append(f"Documenti: click js ok ({js_id})")
            time.sleep(1.0)
            break
        except Exception:
            continue

    try:
        page.locator("span", has_text=re.compile(r"\bDocumenti\b", re.I)).first.click(force=True, timeout=8000)
        if debug_log is not None:
            debug_log.append("Documenti: click force ok (regex)")
        time.sleep(1.0)
    except Exception as e:
        if debug_log is not None:
            debug_log.append(f"Documenti: click force fail ({type(e).__name__})")

    try:
        page.wait_for_selector("text=Cedolino", timeout=15000)
        if debug_log is not None:
            debug_log.append("Documenti: Cedolino found")
        return True
    except Exception:
        if debug_log is not None:
            debug_log.append("Documenti: Cedolino NOT found")
        return False


def _ensure_query(url: str, key: str, value: str) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q[key] = value
    new_q = urlencode(q, doseq=True)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, new_q, p.fragment))


# ==============================================================================
# Core bot
# ==============================================================================
def scarica_documenti_automatici(mese_nome, anno, username, password, tipo_documento="cedolino"):
    nomi_mesi_it = [
        "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
        "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"
    ]
    try:
        mese_num = nomi_mesi_it.index(mese_nome) + 1
    except Exception:
        return None, None, None

    target_busta = f"Tredicesima {anno}" if tipo_documento == "tredicesima" else f"{mese_nome} {anno}"

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

    st.session_state["agenda_data"] = None
    st.session_state["agenda_debug"] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                slow_mo=500,
                args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                accept_downloads=True,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
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
            page.press('input[type="password"]', "Enter")
            time.sleep(3)

            try:
                page.wait_for_selector("text=I miei dati", timeout=15000)
                st_status.info("✅ Login OK")
            except Exception:
                st_status.error("❌ Login fallito")
                browser.close()
                return None, None, "LOGIN_FALLITO"

            # AGENDA (METODO CORRETTO - API)
            try:
                st_status.info("🗓️ Lettura agenda (API)...")
                agenda_debug = []
                
                # Attendi che la pagina carichi completamente
                time.sleep(3)
                
                # Usa il nuovo metodo che passa il context per le chiamate API
                agenda_data = agenda_read_month(page, mese_num, anno, agenda_debug, context=context)
                
                st.session_state["agenda_data"] = agenda_data
                st.session_state["agenda_debug"] = agenda_debug
                
                if agenda_data.get("items_found", 0) > 0:
                    st_status.success(f"✅ Agenda: {agenda_data['items_found']} eventi trovati")
                else:
                    st_status.info("ℹ️ Agenda: nessun evento nel mese selezionato")
                    
            except Exception as e:
                st.session_state["agenda_data"] = None
                st.session_state["agenda_debug"] = [f"Agenda error: {type(e).__name__}: {str(e)}"]
                st_status.warning("⚠️ Agenda non disponibile")

            # BUSTA PAGA
            st_status.info(f"💰 Download {nome_tipo}...")
            try:
                nav_log = []
                if not open_documenti(page, nav_log):
                    st.error("❌ Non riesco ad aprire 'Documenti' (tab hidden/overlay).")
                    with st.expander("🔎 Debug navigazione Documenti"):
                        st.write(nav_log)
                    browser.close()
                    return None, None, "DOC_FAIL"

                time.sleep(1.5)

                try:
                    page.locator("tr", has=page.locator("text=Cedolino")).locator(".z-image").click(timeout=8000)
                except Exception:
                    page.locator("text=Cedolino").first.click(force=True, timeout=8000)

                time.sleep(4)

                if tipo_documento == "tredicesima":
                    links = page.locator(f"a:has-text('Tredicesima {anno}')")
                    if links.count() > 0:
                        with page.expect_download(timeout=20000) as dl:
                            links.first.click()
                        dl.value.save_as(path_busta)
                        if os.path.exists(path_busta):
                            busta_ok = True
                            st_status.success("✅ Tredicesima scaricata")
                else:
                    all_links = page.locator("a")
                    total_links = all_links.count()
                    link_matches = []

                    for i in range(total_links):
                        try:
                            txt = (all_links.nth(i).inner_text() or "").strip()
                            if not txt or len(txt) < 3:
                                continue
                            if any(m in txt for m in nomi_mesi_it) and str(anno) in txt:
                                ha_target = target_busta.lower() in txt.lower()
                                e_tredicesima = any(kw in txt for kw in ["Tredicesima", "13", "XIII"])
                                if ha_target and not e_tredicesima:
                                    link_matches.append((i, txt))
                        except Exception:
                            continue

                    if link_matches:
                        link_index, _ = link_matches[-1]
                        with page.expect_download(timeout=20000) as download_info:
                            all_links.nth(link_index).click()
                        download_info.value.save_as(path_busta)
                        if os.path.exists(path_busta):
                            busta_ok = True
                            st_status.success("✅ Cedolino scaricato")

            except Exception as e:
                st.error(f"❌ Errore busta: {e}")

            # CARTELLINO
            if tipo_documento != "tredicesima":
                st_status.info("📅 Download cartellino...")
                debug_log = []

                def _normalize_url(u: str) -> str:
                    return (u or "").replace("/js_rev//", "/js_rev/")

                def _save_pdf_via_request(url: str) -> bool:
                    try:
                        url = _normalize_url(url)
                        url = _ensure_query(url, "EMBED", "y")

                        resp = context.request.get(url, timeout=60000)
                        ct = (resp.headers.get("content-type") or "").lower()
                        body = resp.body()

                        debug_log.append(f"🌐 HTTP GET -> status={resp.status}, content-type={ct}, bytes={len(body)}")

                        if body[:4] == b"%PDF":
                            Path(path_cart).write_bytes(body)
                            debug_log.append("✅ Salvato PDF raw da HTTP")
                            return True

                        return False
                    except Exception as e:
                        debug_log.append(f"❌ Errore GET PDF: {str(e)[:220]}")
                        return False

                try:
                    page.evaluate("window.scrollTo(0, 0)")
                    time.sleep(1.0)
                    try:
                        page.keyboard.press("Escape")
                        time.sleep(0.2)
                    except Exception:
                        pass

                    debug_log.append("🏠 Tornando alla home...")
                    try:
                        logo = page.locator("img[src*='logo'], .logo").first
                        if logo.is_visible(timeout=2000):
                            logo.click()
                            time.sleep(2)
                    except Exception:
                        page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2", wait_until="domcontentloaded")
                        time.sleep(3)

                    debug_log.append("⏰ Navigazione a Time...")
                    page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
                    time.sleep(3)

                    debug_log.append("📋 Apertura Cartellino presenze...")
                    page.evaluate("document.getElementById('lnktab_5_label')?.click()")
                    time.sleep(5)

                    debug_log.append(f"📅 Impostazione date: {d_from_vis} - {d_to_vis}")
                    dal = page.locator("input[id*='CLRICHIE'][class*='dijitInputInner']").first
                    al = page.locator("input[id*='CLRICHI2'][class*='dijitInputInner']").first

                    if dal.count() > 0 and al.count() > 0:
                        dal.click(force=True)
                        page.keyboard.press("Control+A")
                        dal.fill("")
                        dal.type(d_from_vis, delay=80)
                        dal.press("Tab")
                        time.sleep(0.6)

                        al.click(force=True)
                        page.keyboard.press("Control+A")
                        al.fill("")
                        al.type(d_to_vis, delay=80)
                        al.press("Tab")
                        time.sleep(0.6)

                    debug_log.append("🔍 Esecuzione ricerca...")
                    page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last.click(force=True)
                    time.sleep(8)

                    try:
                        page.wait_for_selector("text=Risultati della ricerca", timeout=20000)
                    except Exception:
                        pass

                    pattern_da_provare = [
                        f"{mese_num:02d}/{anno}",
                        f"{mese_num}/{anno}",
                    ]

                    riga_target = None
                    for pattern in pattern_da_provare:
                        riga_test = page.locator(f"tr:has-text('{pattern}')").first
                        if riga_test.count() > 0 and riga_test.locator("img[src*='search']").count() > 0:
                            riga_target = riga_test
                            break

                    if not riga_target:
                        icona = page.locator("img[src*='search']").first
                    else:
                        icona = riga_target.locator("img[src*='search']").first

                    if icona.count() > 0:
                        with context.expect_page(timeout=20000) as popup_info:
                            icona.click()
                        popup = popup_info.value

                        t0 = time.time()
                        last_url = popup.url
                        while time.time() - t0 < 20:
                            u = popup.url
                            if u and u != "about:blank":
                                last_url = u
                                if ("SERVIZIO=JPSC" in u) and ("ATTIVITA=visualizza" in u):
                                    break
                            time.sleep(0.25)

                        popup_url = _normalize_url(last_url)
                        ok = _save_pdf_via_request(popup_url)
                        
                        if not ok:
                            try:
                                popup.pdf(path=path_cart, format="A4")
                            except Exception:
                                page.pdf(path=path_cart)

                        try:
                            popup.close()
                        except Exception:
                            pass

                    if os.path.exists(path_cart):
                        size = os.path.getsize(path_cart)
                        if size > 5000:
                            cart_ok = True
                            st_status.success(f"✅ Cartellino OK ({size:,} bytes)")

                except Exception as e:
                    debug_log.append(f"❌ ERRORE: {str(e)[:240]}")
                    st.error(f"❌ Errore cartellino: {e}")

                with st.expander("🔍 LOG DEBUG (cartellino)"):
                    for x in debug_log:
                        st.text(x)

            browser.close()

    except Exception as e:
        st.error(f"Errore generale: {e}")

    final_busta = path_busta if busta_ok else None
    final_cart = path_cart if cart_ok else None
    return final_busta, final_cart, None


# ==============================================================================
# UI
# ==============================================================================
st.set_page_config(page_title="Gottardo Payroll", page_icon="💶", layout="wide")
st.title("💶 Analisi Stipendio & Presenze")

with st.sidebar:
    st.header("🔐 Credenziali")

    username, password = get_credentials()

    if not st.session_state.get("credentials_set"):
        st.info("Inserisci le tue credenziali Gottardo SelfService")
        input_user = st.text_input("Username", value=username if username else "", key="input_user")
        input_pass = st.text_input("Password", type="password", value="", key="input_pass")

        if st.button("💾 Salva Credenziali"):
            if input_user and input_pass:
                st.session_state["username"] = input_user
                st.session_state["password"] = input_pass
                st.session_state["credentials_set"] = True
                st.success("✅ Credenziali salvate!")
                st.rerun()
            else:
                st.error("⚠️ Inserisci username e password")
    else:
        st.success(f"✅ Loggato: **{st.session_state['username']}**")
        if st.button("🔄 Cambia Credenziali"):
            st.session_state["credentials_set"] = False
            st.session_state.pop("username", None)
            st.session_state.pop("password", None)
            st.rerun()

    st.divider()

    if st.session_state.get("credentials_set"):
        st.header("Parametri")
        sel_anno = st.selectbox("Anno", [2024, 2025, 2026], index=1)
        sel_mese = st.selectbox(
            "Mese",
            [
                "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
                "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre",
            ],
            index=11,
        )

        tipo_doc = st.radio("Tipo documento", ["📄 Cedolino Mensile", "🎄 Tredicesima"], index=0)

        if st.button("🚀 AVVIA ANALISI", type="primary", use_container_width=True):
            for key in ["busta", "cart", "db", "dc", "done", "agenda_data", "agenda_debug", "tipo"]:
                st.session_state.pop(key, None)

            tipo = "tredicesima" if "Tredicesima" in tipo_doc else "cedolino"
            busta, cart, errore = scarica_documenti_automatici(
                sel_mese,
                sel_anno,
                st.session_state.get("username"),
                st.session_state.get("password"),
                tipo_documento=tipo,
            )

            if errore == "LOGIN_FALLITO":
                st.error("❌ LOGIN FALLITO")
                st.stop()

            st.session_state["busta"] = busta
            st.session_state["cart"] = cart
            st.session_state["tipo"] = tipo
            st.session_state["done"] = False
    else:
        st.warning("⚠️ Inserisci le credenziali")


# ==============================================================================
# Analysis + display
# ==============================================================================
if st.session_state.get("busta") or st.session_state.get("cart"):
    if not st.session_state.get("done"):
        with st.spinner("🧠 Analisi AI in corso..."):
            db = estrai_dati_busta_dettagliata(st.session_state.get("busta"))
            dc = estrai_dati_cartellino(st.session_state.get("cart")) if st.session_state.get("cart") else None

            st.session_state["db"] = db
            st.session_state["dc"] = dc
            st.session_state["done"] = True

            pulisci_file(st.session_state.get("busta"), st.session_state.get("cart"))
            st.session_state.pop("busta", None)
            st.session_state.pop("cart", None)

    db = st.session_state.get("db")
    dc = st.session_state.get("dc")
    tipo = st.session_state.get("tipo", "cedolino")

    if db and db.get("e_tredicesima"):
        st.success("🎄 **Cedolino TREDICESIMA**")

    st.divider()
    tab1, tab2, tab3, tab4 = st.tabs(
        ["💰 Dettaglio Stipendio", "📅 Cartellino & Presenze", "📊 Analisi & Confronto", "🗓️ Agenda"]
    )

    with tab1:
        if db:
            dg = db.get("dati_generali", {})
            comp = db.get("competenze", {})
            tratt = db.get("trattenute", {})
            ferie = db.get("ferie", {})
            par = db.get("par", {})

            k1, k2, k3 = st.columns(3)
            k1.metric("💵 NETTO IN BUSTA", f"€ {dg.get('netto', 0):.2f}")
            k2.metric("📊 Lordo Totale", f"€ {comp.get('lordo_totale', 0):.2f}")
            k3.metric("📆 Giorni Pagati", int(dg.get("giorni_pagati", 0)))

            st.markdown("---")
            c_entr, c_usc = st.columns(2)
            with c_entr:
                st.subheader("➕ Competenze")
                st.write(f"**Paga Base:** € {comp.get('base', 0):.2f}")
                if comp.get("anzianita", 0) > 0:
                    st.write(f"**Anzianità:** € {comp.get('anzianita', 0):.2f}")
                if comp.get("straordinari", 0) > 0:
                    st.write(f"**Straordinari/Suppl.:** € {comp.get('straordinari', 0):.2f}")
                if comp.get("festivita", 0) > 0:
                    st.write(f"**Festività/Maggiorazioni:** € {comp.get('festivita', 0):.2f}")

            with c_usc:
                st.subheader("➖ Trattenute")
                st.write(f"**Contributi INPS:** € {tratt.get('inps', 0):.2f}")
                st.write(f"**IRPEF Netta:** € {tratt.get('irpef_netta', 0):.2f}")
                if tratt.get("addizionali_totali", 0) > 0:
                    st.write(f"**Addizionali:** € {tratt.get('addizionali_totali', 0):.2f}")

            with st.expander("🏖️ Situazione Ferie"):
                f1, f2, f3, f4 = st.columns(4)
                f1.metric("Residue AP", f"{ferie.get('residue_ap', 0):.2f}")
                f2.metric("Maturate", f"{ferie.get('maturate', 0):.2f}")
                f3.metric("Godute", f"{ferie.get('godute', 0):.2f}")
                f4.metric("Saldo", f"{ferie.get('saldo', 0):.2f}")

            with st.expander("⏱️ Situazione Permessi (PAR)"):
                p1, p2, p3, p4 = st.columns(4)
                p1.metric("Residue AP", f"{par.get('residue_ap', 0):.2f}")
                p2.metric("Spettanti", f"{par.get('spettanti', 0):.2f}")
                p3.metric("Fruite", f"{par.get('fruite', 0):.2f}")
                p4.metric("Saldo", f"{par.get('saldo', 0):.2f}")
        else:
            st.warning("⚠️ Dati busta non disponibili")

    with tab2:
        if dc:
            c1, c2 = st.columns([1, 2])
            with c1:
                giorni_reali = dc.get("giorni_reali", 0)
                st.metric("📅 Giorni Lavorati", giorni_reali if giorni_reali > 0 else "N/D")
                anomalie = dc.get("giorni_senza_badge", 0)
                st.metric("⚠️ Anomalie Badge", anomalie)
            with c2:
                st.info(f"**📝 Note:** {dc.get('note', '')}")
        else:
            st.warning("⚠️ Dati cartellino non disponibili")

    with tab3:
        if db and dc:
            pagati = float(db.get("dati_generali", {}).get("giorni_pagati", 0))
            reali = float(dc.get("giorni_reali", 0))

            st.subheader("🔍 Analisi Discrepanze")
            if reali == 0:
                st.info("ℹ️ Cartellino senza timbrature: usa i giorni pagati.")
            else:
                diff = reali - pagati
                col_a, col_b = st.columns(2)
                col_a.metric("Giorni Pagati (Busta)", pagati)
                col_b.metric("Giorni Lavorati (Cartellino)", reali, delta=f"{diff:.1f}")
        elif tipo == "tredicesima":
            st.info("ℹ️ Analisi non disponibile per Tredicesima")
        else:
            st.warning("⚠️ Servono entrambi i documenti")

    with tab4:
        st.subheader("🗓️ Agenda - Eventi/Anomalie")
        ad = st.session_state.get("agenda_data")
        
        if isinstance(ad, dict) and (ad.get("items_found", 0) > 0 or ad.get("api_data")):
            # Mostra conteggi per keyword
            st.markdown("### Conteggi per tipo")
            cols = st.columns(4)
            for i, kw in enumerate(AGENDA_KEYWORDS):
                count = ad.get("counts", {}).get(kw, 0)
                if count > 0:
                    cols[i % 4].metric(kw, count)
            
            # Mostra eventi per tipo (dal nuovo formato API)
            events_by_type = ad.get("events_by_type", {})
            if events_by_type:
                st.markdown("### Eventi per categoria")
                for tipo_ev, count in events_by_type.items():
                    st.write(f"**{tipo_ev}:** {count} eventi")
            
            st.caption(f"📊 Totale eventi: {ad.get('items_found', 0)} | raw_len: {ad.get('raw_len', 0)}")
            
            with st.expander("📋 Dettaglio eventi"):
                lines = ad.get("lines", [])
                if lines:
                    for line in lines[:50]:
                        st.text(f"• {line}")
                else:
                    st.info("Nessun evento con keyword rilevanti")
            
            # Mostra dati API raw per debug
            api_data = ad.get("api_data", {})
            if api_data.get("balances"):
                with st.expander("🏖️ Saldo Ferie/Permessi (API)"):
                    st.json(api_data["balances"])
        else:
            st.info("ℹ️ Nessun evento agenda per questo mese.")

        with st.expander("🔍 Debug agenda"):
            debug = st.session_state.get("agenda_debug", [])
            for line in debug:
                st.text(line)
