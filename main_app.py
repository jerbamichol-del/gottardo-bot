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


# --- SETUP CLOUD ---
os.system("playwright install chromium")
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
try:
    locale.setlocale(locale.LC_TIME, 'it_IT.UTF-8')
except Exception:
    pass


# --- CREDENZIALI DINAMICHE ---
def get_credentials():
    """Sistema di login con credenziali utente"""
    if 'credentials_set' in st.session_state and st.session_state.get('credentials_set'):
        return st.session_state.get('username'), st.session_state.get('password')

    try:
        return st.secrets["ZK_USER"], st.secrets["ZK_PASS"]
    except Exception:
        return None, None


# --- KEYS ---
try:
    GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
except Exception:
    st.error("Google API Key mancante in secrets")
    st.stop()

try:
    DEEPSEEK_API_KEY = st.secrets["DEEPSEEK_API_KEY"]
except Exception:
    DEEPSEEK_API_KEY = None


# --- GEMINI ---
genai.configure(api_key=GOOGLE_API_KEY)

@st.cache_resource
def inizializza_modelli_gemini():
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
                except Exception:
                    continue

        if not modelli_gemini:
            st.error("Nessun modello Gemini disponibile")
            st.stop()

        def priorita(nome):
            n = nome.lower()
            if 'flash' in n and 'lite' not in n:
                return 0
            if 'lite' in n:
                return 1
            if 'pro' in n:
                return 2
            return 3

        modelli_gemini.sort(key=lambda x: priorita(x[0]))
        return modelli_gemini
    except Exception as e:
        st.error(f"Errore caricamento modelli: {e}")
        st.stop()

MODELLI_DISPONIBILI = inizializza_modelli_gemini()

if 'modelli_mostrati' not in st.session_state:
    st.sidebar.success(f"{len(MODELLI_DISPONIBILI)} modelli AI pronti")
    st.session_state['modelli_mostrati'] = True


# ==============================================================================
# AGENDA
# ==============================================================================
AGENDA_KEYWORDS = [
    "OMESSA TIMBRATURA", "MALATTIA", "RIPOSO", "FERIE", "PERMESS",
    "CHIUSURA", "INFORTUN", "ASSENZA", "ANOMALIA"
]

MONTH_ABBR_IT = {
    1: "gen", 2: "feb", 3: "mar", 4: "apr", 5: "mag", 6: "giu",
    7: "lug", 8: "ago", 9: "set", 10: "ott", 11: "nov", 12: "dic"
}
MONTH_FULL_IT = {
    1: "Gennaio", 2: "Febbraio", 3: "Marzo", 4: "Aprile", 5: "Maggio", 6: "Giugno",
    7: "Luglio", 8: "Agosto", 9: "Settembre", 10: "Ottobre", 11: "Novembre", 12: "Dicembre"
}

def _find_ctx_with_selector(page, selector, min_count=1):
    try:
        if page.locator(selector).count() >= min_count:
            return page
    except Exception:
        pass
    for fr in list(page.frames):
        try:
            if fr.locator(selector).count() >= min_count:
                return fr
        except Exception:
            continue
    return None

def _safe_click(page, selector, debug_log, label=None, timeout=8000):
    try:
        loc = page.locator(selector).first
        if loc.count() == 0:
            return False
        loc.click(force=True, timeout=timeout)
        if label:
            debug_log.append(f"Agenda: click_ok {label} ({selector})")
        return True
    except Exception as e:
        if label:
            debug_log.append(f"Agenda: click_fail {label} ({selector}) {type(e).__name__}")
        return False

def agenda_open_time_and_agenda_view(page, debug_log):
    try:
        page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
        debug_log.append("Agenda: Time click")
        time.sleep(3)
    except Exception as e:
        debug_log.append(f"Agenda: Time click fail {type(e).__name__}")

    ok = False
    for sel in ["#dijit_form_Button_13", "span[aria-label='Agenda']", "button[aria-label='Agenda']"]:
        if _safe_click(page, sel, debug_log, label="agenda_view"):
            ok = True
            time.sleep(2)
            break
    if not ok:
        debug_log.append("Agenda: agenda_view_button_not_found_or_not_clickable")

    t0 = time.time()
    while time.time() - t0 < 10:
        if (
            page.locator(".dojoxCalendar").count() > 0
            or page.locator("#calendarContainer").count() > 0
            or page.locator("#dijit_form_Button_10").count() > 0
        ):
            debug_log.append("Agenda: calendar_dom_present")
            return True
        time.sleep(0.2)

    debug_log.append("Agenda: calendar_dom_not_found")
    return False

def agenda_try_click_month_view(page, debug_log):
    for sel in ["#dijit_form_Button_10", "span[aria-label='Mese']", "button[aria-label='Mese']"]:
        if _safe_click(page, sel, debug_log, label="month_view"):
            time.sleep(1.2)
            return True
    debug_log.append("Agenda: month_view_not_set")
    return False

def agenda_get_ref_label(page):
    for sel in ["label.teamToolbarRefPeriod", "label[data-dojo-attach-point='referencePeriod']"]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                t = (loc.inner_text() or "").strip().lower()
                if t:
                    return t
        except Exception:
            pass
    return ""

def agenda_navigate_to_month(page, mese_num, anno, debug_log):
    target_month_abbr = MONTH_ABBR_IT[mese_num]
    target_year = str(anno)

    prev_candidates = ["#revit_form_Button_1", "#revit_form_Button_7"]
    next_candidates = ["#revit_form_Button_2", "#revit_form_Button_8"]

    def click_first(selectors, tag):
        for sel in selectors:
            if _safe_click(page, sel, debug_log, label=tag):
                return sel
        return None

    t = agenda_get_ref_label(page)
    if target_year in t and target_month_abbr in t:
        debug_log.append(f"Agenda: ref_ok ({t})")
        return True

    for i in range(36):
        t = agenda_get_ref_label(page)
        if target_year in t and target_month_abbr in t:
            debug_log.append(f"Agenda: ref_ok step={i} ({t})")
            return True

        direction = "next"
        m_year = re.search(r"\b(20\d{2})\b", t)
        if m_year:
            cur_y = int(m_year.group(1))
            if cur_y > anno:
                direction = "prev"
            elif cur_y < anno:
                direction = "next"
            else:
                cur_m = None
                for k, ab in MONTH_ABBR_IT.items():
                    if ab in t:
                        cur_m = k
                        break
                if cur_m is not None:
                    direction = "prev" if cur_m > mese_num else "next"

        sel = click_first(prev_candidates if direction == "prev" else next_candidates, tag=f"nav_{direction}")
        if not sel:
            debug_log.append("Agenda: nav_buttons_not_found")
            return False
        time.sleep(0.9)

    debug_log.append("Agenda: nav_giveup")
    return False

def agenda_set_month_via_datepicker(page, mese_num, anno, debug_log):
    opener = None
    for sel in ["#revit_form_Button_0", "#revit_form_Button_6", "span.popup-trigger:has(.calendar16)"]:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                opener = loc
                break
        except Exception:
            continue

    if opener is None:
        debug_log.append("Agenda: datepicker_opener_not_found")
        return False

    try:
        opener.click(force=True, timeout=8000)
        debug_log.append("Agenda: datepicker_click_ok")
    except Exception:
        try:
            opener.evaluate("el => el.click()")
            debug_log.append("Agenda: datepicker_click_ok_js")
        except Exception:
            debug_log.append("Agenda: datepicker_click_fail")
            return False

    popup_ctx = None
    t0 = time.time()
    while time.time() - t0 < 8:
        popup_ctx = _find_ctx_with_selector(page, ".dijitCalendarMonthLabel", min_count=2)
        if popup_ctx:
            break
        time.sleep(0.2)

    if not popup_ctx:
        debug_log.append("Agenda: datepicker_popup_not_found")
        return False

    labels = popup_ctx.locator(".dijitCalendarMonthLabel")
    if labels.count() < 2:
        debug_log.append("Agenda: datepicker_labels_missing")
        return False

    try:
        labels.nth(0).click()
        time.sleep(0.2)
        popup_ctx.locator("body").get_by_text(MONTH_FULL_IT[mese_num], exact=True).last.click(timeout=8000)
        debug_log.append("Agenda: datepicker_month_ok")
    except Exception as e:
        debug_log.append(f"Agenda: datepicker_month_fail {type(e).__name__}")

    try:
        labels.nth(1).click()
        time.sleep(0.2)
        popup_ctx.locator("body").get_by_text(str(anno), exact=True).last.click(timeout=8000)
        debug_log.append("Agenda: datepicker_year_ok")
    except Exception as e:
        debug_log.append(f"Agenda: datepicker_year_fail {type(e).__name__}")

    try:
        popup_ctx.locator(".dijitCalendarDateTemplate", has_text=re.compile(r"^1$")).first.click(timeout=8000)
        debug_log.append("Agenda: datepicker_day1_ok")
        time.sleep(1.0)
        return True
    except Exception as e:
        debug_log.append(f"Agenda: datepicker_day1_fail {type(e).__name__}")
        return False

def agenda_extract_events(page):
    texts = []
    selectors = [
        ".dojoxCalendarEvent",
        ".dijitCalendarEvent",
        "[class*='CalendarEvent']",
        "[class*='event']",
    ]

    for sel in selectors:
        try:
            loc = page.locator(sel)
            n = min(loc.count(), 500)
            for i in range(n):
                el = loc.nth(i)
                t = ""
                title = ""
                try:
                    t = (el.inner_text() or "").strip()
                except Exception:
                    t = ""
                try:
                    title = (el.get_attribute("title") or "").strip()
                except Exception:
                    title = ""
                combo = " ".join([x for x in [t, title] if x]).strip()
                if combo:
                    texts.append(combo)
        except Exception:
            continue

    blob = "\n".join(texts)
    up = blob.upper()
    counts = {k: up.count(k) for k in AGENDA_KEYWORDS}
    lines = sorted(list(set([t for t in texts if any(k in t.upper() for k in AGENDA_KEYWORDS)])))
    return {"counts": counts, "lines": lines[:200], "raw_len": len(blob), "items_found": len(texts)}

def agenda_read_month(page, mese_num, anno, debug_log):
    agenda_open_time_and_agenda_view(page, debug_log)
    agenda_try_click_month_view(page, debug_log)

    ok = agenda_navigate_to_month(page, mese_num, anno, debug_log)
    if not ok:
        debug_log.append("Agenda: nav_failed_try_datepicker")
        agenda_set_month_via_datepicker(page, mese_num, anno, debug_log)

    data = agenda_extract_events(page)
    debug_log.append(f"Agenda: extracted items={data.get('items_found', 0)} raw_len={data.get('raw_len', 0)}")
    return data


# ==============================================================================
# AI helpers
# ==============================================================================
def clean_json_response(text):
    try:
        if not text:
            return None
        text = re.sub(r"```json|```", "", text).strip()
        start = text.find('{')
        end = text.rfind('}') + 1
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

    if not bytes_data[:4] == b'%PDF':
        st.error(f"Il file {tipo} non è un PDF valido")
        return None

    progress_placeholder = st.empty()
    last_err = None

    # GEMINI
    for idx, (nome_modello, modello) in enumerate(MODELLI_DISPONIBILI, 1):
        try:
            progress_placeholder.info(f"Analisi {tipo}: modello {idx}/{len(MODELLI_DISPONIBILI)}...")

            response = modello.generate_content([
                prompt,
                {"mime_type": "application/pdf", "data": bytes_data}
            ])

            result = clean_json_response(getattr(response, "text", ""))
            if result and isinstance(result, dict):
                progress_placeholder.success(f"{tipo.capitalize()} analizzato")
                time.sleep(0.7)
                progress_placeholder.empty()
                return result

        except Exception as e:
            last_err = e
            msg = str(e).lower()
            if "429" in msg or "quota" in msg or "resource_exhausted" in msg:
                continue
            continue

    # DEEPSEEK (opzionale)
    if DEEPSEEK_API_KEY and OpenAI is not None:
        try:
            progress_placeholder.warning(f"Gemini non disponibile/quote. Fallback DeepSeek per {tipo}...")
            txt = extract_text_from_pdf_any(file_path)
            if not txt or len(txt.strip()) < 50:
                progress_placeholder.error("DeepSeek: testo PDF non estraibile (probabile PDF a immagini)")
                time.sleep(0.7)
                progress_placeholder.empty()
                return None

            client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
            full_prompt = prompt + "\n\n--- TESTO ESTRATTO DAL PDF ---\n" + txt[:25000]

            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "Rispondi solo con JSON valido, senza testo extra."},
                    {"role": "user", "content": full_prompt},
                ],
                temperature=0.1,
            )

            out = resp.choices[0].message.content
            result = clean_json_response(out)
            if result and isinstance(result, dict):
                progress_placeholder.success(f"{tipo.capitalize()} analizzato (DeepSeek)")
                time.sleep(0.7)
                progress_placeholder.empty()
                return result
        except Exception:
            pass

    progress_placeholder.error(f"Analisi {tipo} fallita")
    if last_err:
        with st.expander("Dettaglio ultimo errore AI"):
            st.code(str(last_err)[:2000])
    return None


def estrai_dati_busta_dettagliata(file_path):
    prompt = """
Questo è un CEDOLINO PAGA GOTTARDO S.p.A. italiano. Segui ESATTAMENTE queste istruzioni:

1) DATI GENERALI:
- NETTO: riga "PROGRESSIVI" (colonna finale prima di "ESTREMI ELABORAZIONE")
- GIORNI PAGATI: riga con "GG. INPS"
- ORE ORDINARIE: "ORE INAIL" oppure giorni_pagati × 8

2) COMPETENZE:
- base: "RETRIBUZIONE ORDINARIA" (voce 1000) colonna COMPETENZE
- straordinari: somma voci "STRAORDINARIO"/"SUPPLEMENTARI"/"NOTTURNI"
- festivita: somma voci "MAGG. FESTIVE"/"FESTIVITA GODUTA"
- anzianita: voci "SCATTI"/"EDR"/"ANZ." altrimenti 0
- lordo_totale: "TOTALE COMPETENZE" o "PROGRESSIVI" colonna totale

3) TRATTENUTE:
- inps: sezione I.N.P.S.
- irpef_netta: sezione FISCALI
- addizionali_totali: add.reg + add.com (se presenti, altrimenti 0)

4) FERIE/PAR: tabella ferie in alto a destra (RES.PREC / SPETTANTI / FRUITE / SALDO)

5) TREDICESIMA:
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

    result = estrai_con_fallback(file_path, prompt, tipo="cartellino")

    if result and 'debug_prime_righe' in result:
        with st.expander("DEBUG: Prime righe estratte dall'AI"):
            st.text(result.get('debug_prime_righe', ''))
            timbrature = re.findall(r'[LMGVSD]\d{2}', result.get('debug_prime_righe', ''))
            st.info(f"Timbrature trovate (pattern): {len(timbrature)}")
            if timbrature:
                st.success("Cartellino con timbrature dettagliate")

    return result


# ==============================================================================
# File cleanup
# ==============================================================================
def pulisci_file(path_busta, path_cart):
    file_eliminati = []
    if path_busta and os.path.exists(path_busta):
        try:
            os.remove(path_busta)
            file_eliminati.append(os.path.basename(path_busta))
        except Exception:
            pass
    if path_cart and os.path.exists(path_cart):
        try:
            os.remove(path_cart)
            file_eliminati.append(os.path.basename(path_cart))
        except Exception:
            pass
    if file_eliminati:
        st.info(f"File eliminati: {', '.join(file_eliminati)}")


# ==============================================================================
# Navigation fixes
# ==============================================================================
def safe_click_mydata(page, debug_log=None):
    # chiudi overlay/tooltip che intercetta pointer events
    try:
        page.keyboard.press("Escape")
        time.sleep(0.2)
    except Exception:
        pass

    try:
        page.evaluate("""
        (() => {
          const t = document.querySelector('#searchMenuTooltip');
          if (t) t.style.display = 'none';
          const p = document.querySelector('#popup_1');
          if (p) p.style.display = 'none';
        })();
        """)
    except Exception:
        pass

    # click JS sul label nav
    try:
        page.evaluate("document.getElementById('revit_navigation_NavHoverItem_0_label')?.click()")
        if debug_log is not None:
            debug_log.append("MyData: click ok (js id)")
        return True
    except Exception:
        pass

    # fallback: locator force
    try:
        page.locator("text=I miei dati").first.click(force=True, timeout=8000)
        if debug_log is not None:
            debug_log.append("MyData: click ok (force text)")
        return True
    except Exception as e:
        if debug_log is not None:
            debug_log.append(f"MyData: click fail ({type(e).__name__})")
        return False


def _ensure_query(url: str, key: str, value: str) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q[key] = value
    new_q = urlencode(q, doseq=True)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, new_q, p.fragment))


# ==============================================================================
# CORE BOT (busta + cartellino + agenda)
# ==============================================================================
def scarica_documenti_automatici(mese_nome, anno, username, password, tipo_documento="cedolino"):
    nomi_mesi_it = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
                    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
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
    st_status.info(f"Bot: {nome_tipo} {mese_nome} {anno}")

    busta_ok = False
    cart_ok = False

    st.session_state["agenda_data"] = None
    st.session_state["agenda_debug"] = []

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
            st_status.info("Login...")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y", wait_until="domcontentloaded")
            page.wait_for_selector('input[type="text"]', timeout=10000)
            page.fill('input[type="text"]', username)
            page.fill('input[type="password"]', password)
            page.press('input[type="password"]', 'Enter')
            time.sleep(3)

            try:
                page.wait_for_selector("text=I miei dati", timeout=15000)
                st_status.info("Login OK")
            except Exception:
                st_status.error("Login fallito")
                browser.close()
                return None, None, "LOGIN_FALLITO"

            # AGENDA (best effort)
            try:
                st_status.info("Lettura agenda...")
                agenda_debug = []
                agenda_data = agenda_read_month(page, mese_num, anno, agenda_debug)
                st.session_state["agenda_data"] = agenda_data
                st.session_state["agenda_debug"] = agenda_debug
                st_status.success("Agenda letta")
            except Exception as e:
                st.session_state["agenda_data"] = None
                st.session_state["agenda_debug"] = [f"Agenda error: {type(e).__name__}"]
                st_status.warning("Agenda non disponibile")

            # BUSTA PAGA
            st_status.info(f"Download {nome_tipo}...")
            try:
                nav_log = []
                if not safe_click_mydata(page, nav_log):
                    st.error("Non riesco a cliccare 'I miei dati' (overlay attivo)")
                    browser.close()
                    return None, None, "NAV_FAIL"

                page.wait_for_selector("text=Documenti", timeout=12000).click()
                time.sleep(3)

                try:
                    page.locator("tr", has=page.locator("text=Cedolino")).locator(".z-image").click(timeout=5000)
                except Exception:
                    page.click("text=Cedolino")

                time.sleep(5)

                if tipo_documento == "tredicesima":
                    links = page.locator(f"a:has-text('Tredicesima {anno}')")
                    if links.count() > 0:
                        with page.expect_download(timeout=20000) as dl:
                            links.first.click()
                        dl.value.save_as(path_busta)
                        if os.path.exists(path_busta):
                            busta_ok = True
                            st_status.success("Tredicesima scaricata")
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
                            st_status.success("Cedolino scaricato")

            except Exception as e:
                st.error(f"Errore busta: {e}")

            # CARTELLINO
            if tipo_documento != "tredicesima":
                st_status.info("Download cartellino...")
                debug_log = []

                def _normalize_url(u: str) -> str:
                    return (u or "").replace("/js_rev//", "/js_rev/")

                def _save_pdf_via_request(url: str) -> bool:
                    # ✅ CORREZIONE: sempre EMBED=y (salta la risposta HTML)
                    try:
                        url = _normalize_url(url)
                        url = _ensure_query(url, "EMBED", "y")

                        resp = context.request.get(url, timeout=60000)
                        ct = (resp.headers.get("content-type") or "").lower()
                        body = resp.body()

                        debug_log.append(f"HTTP GET -> status={resp.status}, content-type={ct}, bytes={len(body)}")
                        debug_log.append(f"First bytes: {body[:8]!r}")

                        if body[:4] == b"%PDF":
                            Path(path_cart).write_bytes(body)
                            debug_log.append("Salvato PDF raw via HTTP (firma %PDF ok)")
                            return True

                        debug_log.append("Response non sembra un PDF (%PDF mancante)")
                        return False
                    except Exception as e:
                        debug_log.append(f"Errore GET PDF: {str(e)[:220]}")
                        return False

                try:
                    page.evaluate("window.scrollTo(0, 0)")
                    time.sleep(1.5)
                    try:
                        page.keyboard.press("Escape")
                        time.sleep(0.2)
                    except Exception:
                        pass

                    debug_log.append("Tornando alla home...")
                    try:
                        logo = page.locator("img[src*='logo'], .logo").first
                        if logo.is_visible(timeout=2000):
                            logo.click()
                            time.sleep(2)
                    except Exception:
                        page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2", wait_until="domcontentloaded")
                        time.sleep(3)

                    debug_log.append("Navigazione a Time...")
                    page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
                    time.sleep(3)
                    debug_log.append("Time aperto")

                    debug_log.append("Apertura Cartellino presenze...")
                    page.evaluate("document.getElementById('lnktab_5_label')?.click()")
                    time.sleep(5)
                    debug_log.append("Cartellino presenze aperto")

                    debug_log.append(f"Impostazione date: {d_from_vis} - {d_to_vis}")
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
                        debug_log.append("Date impostate")

                    debug_log.append("Esecuzione ricerca...")
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(0.6)
                    page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last.click(force=True)
                    debug_log.append("Click 'Esegui ricerca' OK")
                    time.sleep(8)

                    try:
                        page.wait_for_selector("text=Risultati della ricerca", timeout=20000)
                        debug_log.append("Risultati caricati")
                    except Exception:
                        debug_log.append("Timeout risultati")

                    debug_log.append("Ricerca riga cartellino...")
                    pattern_da_provare = [
                        f"{mese_num:02d}/{anno}",
                        f"{mese_num}/{anno}",
                        f"{mese_num}/{str(anno)[-2:]}",
                    ]

                    riga_target = None
                    for pattern in pattern_da_provare:
                        debug_log.append(f"Cerco pattern: '{pattern}'")
                        riga_test = page.locator(f"tr:has-text('{pattern}')").first
                        if riga_test.count() > 0 and riga_test.locator("img[src*='search']").count() > 0:
                            riga_target = riga_test
                            debug_log.append(f"Riga trovata con pattern: '{pattern}'")
                            break

                    if not riga_target:
                        debug_log.append("Riga non trovata, fallback: prima icona search")
                        icona = page.locator("img[src*='search']").first
                    else:
                        icona = riga_target.locator("img[src*='search']").first

                    if icona.count() == 0:
                        debug_log.append("Icona lente non trovata")
                    else:
                        debug_log.append("Click lente: attendo popup...")
                        with context.expect_page(timeout=20000) as popup_info:
                            icona.click()
                        popup = popup_info.value

                        t0 = time.time()
                        last_url = popup.url
                        while time.time() - t0 < 20:
                            u = popup.url
                            if u and u != "about:blank":
                                last_url = u
                                if ("SERVIZIO=JPSC" in u) and ("ATTIVITA=visualizza" in u) and ("DOPDF=y" in u):
                                    break
                            time.sleep(0.25)

                        popup_url = _normalize_url(last_url)
                        debug_log.append(f"Popup catturato: {popup_url}")

                        ok = _save_pdf_via_request(popup_url)
                        if not ok:
                            debug_log.append("Fallback: popup.pdf()")
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
                        debug_log.append(f"File trovato: {size} bytes")
                        if size > 5000:
                            cart_ok = True
                            st_status.success(f"Cartellino OK ({size} bytes)")
                        else:
                            st.warning(f"PDF piccolo ({size} bytes) - potrebbe essere vuoto")
                    else:
                        st.error("File cartellino non trovato")
                        debug_log.append("FILE NON TROVATO")

                except Exception as e:
                    debug_log.append(f"ERRORE GENERALE CARTELLINO: {str(e)[:240]}")
                    st.error(f"Errore cartellino: {e}")

                with st.expander("LOG DEBUG COMPLETO (cartellino)"):
                    for x in debug_log:
                        st.text(x)
                    log_path = work_dir / f"debug_cartellino_{mese_num}_{anno}.txt"
                    try:
                        log_path.write_text("\n".join(debug_log), encoding="utf-8")
                        st.info(f"Log salvato: {log_path}")
                    except Exception:
                        pass

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
st.title("Analisi Stipendio & Presenze")

with st.sidebar:
    st.header("Credenziali")

    username, password = get_credentials()

    if not st.session_state.get('credentials_set'):
        st.info("Inserisci le tue credenziali Gottardo SelfService")
        input_user = st.text_input("Username", value=username if username else "", key="input_user")
        input_pass = st.text_input("Password", type="password", value="", key="input_pass")

        if st.button("Salva Credenziali"):
            if input_user and input_pass:
                st.session_state['username'] = input_user
                st.session_state['password'] = input_pass
                st.session_state['credentials_set'] = True
                st.success("Credenziali salvate")
                st.rerun()
            else:
                st.error("Inserisci username e password")
    else:
        st.success(f"Loggato: {st.session_state['username']}")
        if st.button("Cambia Credenziali"):
            st.session_state['credentials_set'] = False
            st.session_state.pop('username', None)
            st.session_state.pop('password', None)
            st.rerun()

    st.divider()

    if st.session_state.get('credentials_set'):
        st.header("Parametri")
        sel_anno = st.selectbox("Anno", [2024, 2025, 2026], index=1)
        sel_mese = st.selectbox(
            "Mese",
            ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
             "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"],
            index=11
        )

        tipo_doc = st.radio("Tipo documento", ["Cedolino Mensile", "Tredicesima"], index=0)

        if st.button("AVVIA ANALISI", type="primary", use_container_width=True):
            for key in ['busta', 'cart', 'db', 'dc', 'done', 'agenda_data', 'agenda_debug', 'tipo']:
                st.session_state.pop(key, None)

            tipo = "tredicesima" if "Tredicesima" in tipo_doc else "cedolino"
            username = st.session_state.get('username')
            password = st.session_state.get('password')

            busta, cart, errore = scarica_documenti_automatici(sel_mese, sel_anno, username, password, tipo_documento=tipo)

            if errore == "LOGIN_FALLITO":
                st.error("LOGIN FALLITO")
                st.stop()

            st.session_state['busta'] = busta
            st.session_state['cart'] = cart
            st.session_state['tipo'] = tipo
            st.session_state['done'] = False
    else:
        st.warning("Inserisci le credenziali")


# ANALISI
if st.session_state.get('busta') or st.session_state.get('cart'):
    if not st.session_state.get('done'):
        with st.spinner("Analisi AI in corso..."):
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
        st.success("Cedolino TREDICESIMA")

    st.divider()
    tab1, tab2, tab3, tab4 = st.tabs([
        "Dettaglio Stipendio",
        "Cartellino & Presenze",
        "Analisi & Confronto",
        "Agenda"
    ])

    with tab1:
        if db:
            dg = db.get('dati_generali', {})
            comp = db.get('competenze', {})
            tratt = db.get('trattenute', {})
            ferie = db.get('ferie', {})
            par = db.get('par', {})

            k1, k2, k3 = st.columns(3)
            k1.metric("NETTO", f"€ {dg.get('netto', 0):.2f}")
            k2.metric("Lordo Totale", f"€ {comp.get('lordo_totale', 0):.2f}")
            k3.metric("Giorni Pagati", int(dg.get('giorni_pagati', 0)))

            st.markdown("---")
            c_entr, c_usc = st.columns(2)
            with c_entr:
                st.subheader("Competenze")
                st.write(f"Paga Base: € {comp.get('base', 0):.2f}")
                if comp.get('anzianita', 0) > 0:
                    st.write(f"Anzianità: € {comp.get('anzianita', 0):.2f}")
                if comp.get('straordinari', 0) > 0:
                    st.write(f"Straordinari/Suppl.: € {comp.get('straordinari', 0):.2f}")
                if comp.get('festivita', 0) > 0:
                    st.write(f"Festività/Magg.: € {comp.get('festivita', 0):.2f}")

            with c_usc:
                st.subheader("Trattenute")
                st.write(f"INPS: € {tratt.get('inps', 0):.2f}")
                st.write(f"IRPEF Netta: € {tratt.get('irpef_netta', 0):.2f}")
                if tratt.get('addizionali_totali', 0) > 0:
                    st.write(f"Addizionali: € {tratt.get('addizionali_totali', 0):.2f}")

            with st.expander("Ferie"):
                f1, f2, f3, f4 = st.columns(4)
                f1.metric("Residue AP", f"{ferie.get('residue_ap', 0):.2f}")
                f2.metric("Maturate", f"{ferie.get('maturate', 0):.2f}")
                f3.metric("Godute", f"{ferie.get('godute', 0):.2f}")
                f4.metric("Saldo", f"{ferie.get('saldo', 0):.2f}")

            with st.expander("Permessi (PAR)"):
                p1, p2, p3, p4 = st.columns(4)
                p1.metric("Residue AP", f"{par.get('residue_ap', 0):.2f}")
                p2.metric("Spettanti", f"{par.get('spettanti', 0):.2f}")
                p3.metric("Fruite", f"{par.get('fruite', 0):.2f}")
                p4.metric("Saldo", f"{par.get('saldo', 0):.2f}")
        else:
            st.warning("Dati busta non disponibili")

    with tab2:
        if dc:
            c1, c2 = st.columns([1, 2])
            with c1:
                giorni_reali = dc.get('giorni_reali', 0)
                st.metric("Giorni lavorati", giorni_reali if giorni_reali > 0 else "N/D")
                anomalie = dc.get('giorni_senza_badge', 0)
                st.metric("Anomalie badge", anomalie)
            with c2:
                st.info(f"Note: {dc.get('note', '')}")
        else:
            if tipo == "tredicesima":
                st.warning("Cartellino non disponibile (Tredicesima)")
            else:
                st.error("Errore cartellino")

    with tab3:
        if db and dc:
            pagati = float(db.get('dati_generali', {}).get('giorni_pagati', 0))
            reali = float(dc.get('giorni_reali', 0))

            if reali == 0:
                st.info("Cartellino senza timbrature dettagliate: usa i giorni pagati in busta come riferimento")
                st.write(f"Giorni pagati in busta: {int(pagati)}")
            else:
                diff = reali - pagati
                col_a, col_b = st.columns(2)
                col_a.metric("Giorni Pagati (Busta)", pagati)
                col_b.metric("Giorni Lavorati (Cartellino)", reali, delta=f"{diff:.1f}")

                if abs(diff) < 0.5:
                    st.success("Giorni lavorati = giorni pagati")
                elif diff > 0:
                    st.info(f"Hai lavorato {diff:.1f} giorni in più")
                else:
                    st.warning(f"{abs(diff):.1f} giorni pagati in più")
        elif tipo == "tredicesima":
            st.info("Analisi non disponibile per Tredicesima")
        else:
            st.warning("Servono entrambi i documenti")

    with tab4:
        st.subheader("Agenda - Eventi/Anomalie")
        ad = st.session_state.get("agenda_data")
        if isinstance(ad, dict):
            cols = st.columns(4)
            for i, k in enumerate(AGENDA_KEYWORDS):
                cols[i % 4].metric(k, int(ad.get("counts", {}).get(k, 0)))
            st.caption(f"Eventi trovati: {ad.get('items_found', 0)} | raw_len: {ad.get('raw_len', 0)}")
            with st.expander("Righe trovate (match keyword)"):
                st.write(ad.get("lines", []))
        else:
            st.info("Agenda non disponibile per questo mese (o selettori non agganciati).")

        with st.expander("Debug agenda"):
            st.write(st.session_state.get("agenda_debug", []))
