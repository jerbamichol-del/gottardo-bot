# ==============================================================================
# GOTTARDO PAYROLL ANALYZER - VERSIONE COMPLETA (CORRETTA)
# ==============================================================================
# Features:
# - Download Busta Paga + Cartellino
# - Lettura Agenda via Navigazione + fallback API
# - Parsing AI dettagliato con fallback DeepSeek
# - Controllo incrociato (Busta + Cartellino + Agenda informativa)
# ==============================================================================

import sys
import asyncio
import re
import os
import json
import time
import calendar
import locale
import subprocess
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse  # (tenuto, utile per debug URL)

import streamlit as st
import google.generativeai as genai
from playwright.sync_api import sync_playwright

# --- OPTIONAL: DeepSeek + PDF extraction ---
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
# CONFIG
# ==============================================================================
st.set_page_config(page_title="Gottardo Payroll", page_icon="💶", layout="wide")

# NOTA: evitare install Playwright ad ogni rerun.
# Se vuoi abilitarlo runtime (solo quando serve), setta env RUN_PLAYWRIGHT_INSTALL=1
@st.cache_resource
def ensure_playwright():
    if os.environ.get("RUN_PLAYWRIGHT_INSTALL", "0") == "1":
        try:
            subprocess.run(["playwright", "install", "chromium"], check=False)
        except Exception:
            pass
    return True

ensure_playwright()

LOGOPATH = Path(__file__).resolve().parent / "assets" / "logo.jpg"

c_logo, c_title = st.columns([0.75, 9.25], gap="small", vertical_alignment="center")
with c_logo:
    if LOGOPATH.exists():
        st.image(str(LOGOPATH), width=100)
with c_title:
    st.markdown(
        '<h1 style="margin:0;padding:0">Gottardo Payroll Analyzer</h1>',
        unsafe_allow_html=True,
    )

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

try:
    locale.setlocale(locale.LC_TIME, "it_IT.UTF-8")
except Exception:
    pass


# ==============================================================================
# COSTANTI
# ==============================================================================
MESI_IT = [
    "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre",
]

CALENDAR_CODES = {
    "FEP": "FERIE PIANIFICATE",
    "OMT": "OMESSA TIMBRATURA",
    "RCS": "RIPOSO COMPENSATIVO SUCC",
    "RIC": "RIPOSO COMPENSATIVO FORZ",
    "MAL": "MALATTIA",
}

AGENDA_KEYWORDS = [
    "OMESSA TIMBRATURA", "OMESSA", "OMT",
    "MALATTIA", "MAL",
    "RIPOSO COMPENSATIVO", "RCS", "RIC",
    "FERIE PIANIFICATE", "FERIE", "FEP",
    "PERMESSO", "PAR",
    "ANOMALIA", "ASSENZA",
]

# Conversione ore->giorni (nel tuo output 80h ≈ 11gg, quindi 7 è coerente con quel calcolo)
ORE_PER_GIORNO = 7.0


# ==============================================================================
# HELPERS
# ==============================================================================
def safe_float(val) -> float:
    try:
        if isinstance(val, str):
            val = val.replace("€", "").replace(",", ".").strip()
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def safe_int(val) -> int:
    try:
        if isinstance(val, str):
            val = val.strip()
        return int(float(val))
    except (ValueError, TypeError):
        return 0


# ==============================================================================
# AI SETUP
# ==============================================================================
def get_api_keys():
    google_key = st.secrets.get("GOOGLE_API_KEY")
    deepseek_key = st.secrets.get("DEEPSEEK_API_KEY")
    return google_key, deepseek_key


@st.cache_resource
def init_gemini_models():
    google_key, _ = get_api_keys()
    if not google_key:
        return []

    genai.configure(api_key=google_key)

    try:
        all_models = genai.list_models()
        valid = [m for m in all_models if "generateContent" in m.supported_generation_methods]

        gemini_models = []
        for m in valid:
            name = m.name.replace("models/", "")
            if "gemini" in name.lower() and "embedding" not in name.lower():
                try:
                    gemini_models.append((name, genai.GenerativeModel(name)))
                except Exception:
                    continue

        def priority(n: str) -> int:
            n = n.lower()
            if "flash" in n and "lite" not in n:
                return 0
            if "lite" in n:
                return 1
            if "pro" in n:
                return 2
            return 3

        gemini_models.sort(key=lambda x: priority(x[0]))
        return gemini_models
    except Exception as e:
        st.warning(f"Errore init modelli: {e}")
        return []


def clean_json_response(text: str):
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


def extract_text_from_pdf(file_path: str):
    if not file_path or not os.path.exists(file_path):
        return None

    if fitz:
        try:
            doc = fitz.open(file_path)
            text = "\n".join([p.get_text() for p in doc])
            if text.strip():
                return text.strip()
        except Exception:
            pass

    if PdfReader:
        try:
            reader = PdfReader(file_path)
            text = "\n".join([p.extract_text() or "" for p in reader.pages])
            if text.strip():
                return text.strip()
        except Exception:
            pass

    return None


def analyze_with_fallback(file_path, prompt, tipo="documento"):
    if not file_path or not os.path.exists(file_path):
        return None

    with open(file_path, "rb") as f:
        pdf_bytes = f.read()

    if pdf_bytes[:4] != b"%PDF":
        st.error(f"❌ {tipo} non è un PDF valido")
        return None

    models = init_gemini_models()
    _, deepseek_key = get_api_keys()

    progress = st.empty()
    last_error = None

    for idx, (name, model) in enumerate(models, 1):
        try:
            progress.info(f"🔄 {tipo}: modello {idx}/{len(models)} ({name})...")
            resp = model.generate_content([prompt, {"mime_type": "application/pdf", "data": pdf_bytes}])
            result = clean_json_response(getattr(resp, "text", ""))
            if result and isinstance(result, dict):
                progress.success(f"✅ {tipo} analizzato!")
                time.sleep(0.25)
                progress.empty()
                return result
        except Exception as e:
            last_error = e
            continue

    if deepseek_key and OpenAI:
        try:
            progress.warning(f"⚠️ Gemini esaurito. Fallback DeepSeek per {tipo}...")
            text = extract_text_from_pdf(file_path)
            if not text or len(text) < 50:
                progress.error("❌ PDF non leggibile per DeepSeek")
                return None

            client = OpenAI(api_key=deepseek_key, base_url="https://api.deepseek.com")
            full_prompt = prompt + "\n\n--- TESTO PDF ---\n" + text[:25000]

            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "Rispondi solo JSON valido."},
                    {"role": "user", "content": full_prompt},
                ],
                temperature=0.1,
            )
            result = clean_json_response(resp.choices[0].message.content)
            if result:
                progress.success(f"✅ {tipo} analizzato (DeepSeek)!")
                time.sleep(0.25)
                progress.empty()
                return result
        except Exception as e:
            last_error = e

    progress.error(f"❌ Analisi {tipo} fallita")
    if last_error:
        with st.expander("🔎 Errore"):
            st.code(str(last_error)[:800])
    return None


# ==============================================================================
# PARSERS AI DETTAGLIATI
# ==============================================================================
def parse_busta_dettagliata(path):
    prompt = """
Questo è un CEDOLINO PAGA GOTTARDO S.p.A. italiano. Estrai ESATTAMENTE:

1) DATI GENERALI:
- NETTO: riga "PROGRESSIVI" colonna finale (es. 788,61)
- GIORNI PAGATI: riga "GG. INPS" (es. 26)
- ORE ORDINARIE: "ORE INAIL" o giorni×8

2) COMPETENZE:
- base: "RETRIBUZIONE ORDINARIA" o "PAGA BASE" (voce 1000) -> colonna Competenze
- straordinari: somma STRAORDINARIO/SUPPLEMENTARI/NOTTURNI
- festivita: MAGG. FESTIVE/FESTIVITA GODUTA
- anzianita: SCATTI/EDR/ANZ.
- lordo_totale: "TOTALE COMPETENZE"

3) TRATTENUTE:
- inps: sezione I.N.P.S.
- irpef_netta: sezione FISCALI
- addizionali: add.reg + add.com

4) FERIE/PAR (tabella in alto a destra):
- RES.PREC / SPETTANTI / FRUITE / SALDO

5) ASSENZE DEL MESE (IMPORTANTE!):
- ore_ferie: "FERIE GODUTE" (spesso voce 4521) -> colonna ORE
- ore_permessi: "PERMESSI GODUTI"/"ROL GODUTI" (spesso voce 4529) -> colonna ORE
- ore_malattia: righe "MALATTIA" -> colonna ORE

6) TREDICESIMA:
- e_tredicesima=true se trovi "TREDICESIMA"/"13MA"

Output SOLO JSON:
{
  "e_tredicesima": false,
  "dati_generali": {"netto": 0.00, "giorni_pagati": 0, "ore_ordinarie": 0.00},
  "competenze": {"base": 0.00, "anzianita": 0.00, "straordinari": 0.00, "festivita": 0.00, "lordo_totale": 0.00},
  "trattenute": {"inps": 0.00, "irpef_netta": 0.00, "addizionali": 0.00},
  "ferie": {"residue_ap": 0.00, "maturate": 0.00, "godute": 0.00, "saldo": 0.00},
  "par": {"residue_ap": 0.00, "spettanti": 0.00, "fruite": 0.00, "saldo": 0.00},
  "assenze_mese": {"ore_ferie": 0.00, "ore_permessi": 0.00, "ore_malattia": 0.00}
}
""".strip()

    result = analyze_with_fallback(path, prompt, "Busta Paga")
    if not result:
        return {
            "e_tredicesima": False,
            "dati_generali": {"netto": 0, "giorni_pagati": 0, "ore_ordinarie": 0},
            "competenze": {"base": 0, "anzianita": 0, "straordinari": 0, "festivita": 0, "lordo_totale": 0},
            "trattenute": {"inps": 0, "irpef_netta": 0, "addizionali": 0},
            "ferie": {"residue_ap": 0, "maturate": 0, "godute": 0, "saldo": 0},
            "par": {"residue_ap": 0, "spettanti": 0, "fruite": 0, "saldo": 0},
            "assenze_mese": {"ore_ferie": 0, "ore_permessi": 0, "ore_malattia": 0},
        }
    return result


def parse_cartellino_dettagliato(path):
    prompt = """
Analizza questo CARTELLINO PRESENZE GOTTARDO S.p.A.

1) DATI DAL FOOTER (UFFICIALI):
- "GG PRESENZA" o codice 0265 -> giorni_footer
- "ORE LAVORATE" o codice 0253 -> ore_lavorate

2) CONTEGGIO RIGHE (VERIFICA):
Conta righe di presenza/lavoro:
- Codici che iniziano con 'V' (V70, V50, V29, V01, ecc.)
- Righe con orari di timbratura
- Righe "ORD" o "STR"
NON contare righe che hanno SOLO assenze (F70, FER, MAL, RCO/RDD) senza timbrature.
-> giorni_righe

3) ALTRI CODICI:
- festivita: F70, FST, FES (1 per giorno)
- ferie: FER, FE, FEP
- permessi: PAR, PER, ROL
- malattia: MAL
- omesse_timbrature: SOLO se testo contiene OMESSA/ANOMALIA/MANCATA TIMBRATURA (NON Vxx senza orari)

Output JSON:
{
  "giorni_lavorati": 0,
  "giorni_footer": 0,
  "giorni_righe": 0,
  "ore_lavorate": 0.00,
  "ferie": 0,
  "malattia": 0,
  "permessi": 0,
  "riposi": 0,
  "omesse_timbrature": 0,
  "festivita": 0,
  "note": ""
}
""".strip()

    result = analyze_with_fallback(path, prompt, "Cartellino")
    if not result:
        return {
            "giorni_lavorati": 0,
            "giorni_footer": 0,
            "giorni_righe": 0,
            "ore_lavorate": 0,
            "ferie": 0,
            "malattia": 0,
            "permessi": 0,
            "riposi": 0,
            "omesse_timbrature": 0,
            "festivita": 0,
            "note": "",
        }

    if safe_int(result.get("giorni_footer", 0)) > 0:
        result["giorni_lavorati"] = safe_int(result.get("giorni_footer", 0))
    elif safe_int(result.get("giorni_righe", 0)) > 0:
        result["giorni_lavorati"] = safe_int(result.get("giorni_righe", 0))

    return result


# ==============================================================================
# AGENDA - NAVIGAZIONE + INTERCETTAZIONE RETE
# ==============================================================================
def read_agenda_with_navigation(page, context, mese_num, anno):
    result = {"events_by_type": {}, "total_events": 0, "items": [], "debug": [], "success": False}
    captured_events = []

    def capture_calendar_response(response):
        try:
            url = response.url
            if ("events" in url.lower() or "calendar" in url.lower() or "time" in url.lower() or "anomalies" in url.lower()):
                if response.status == 200:
                    try:
                        data = response.json()
                        if data:
                            result["debug"].append(f"📡 Catturato: {url[:70]}...")
                            if isinstance(data, list):
                                captured_events.extend(data)
                            elif isinstance(data, dict) and "items" in data:
                                captured_events.extend(data["items"])
                            elif isinstance(data, dict):
                                captured_events.append(data)
                    except Exception:
                        pass
        except Exception:
            pass

    page.on("response", capture_calendar_response)

    try:
        result["debug"].append("🗓️ Navigazione al calendario...")

        # Time menu
        try:
            page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
            result["debug"].append("  Menu Time cliccato (JS)")
        except Exception:
            try:
                page.locator("text=Time").first.click(force=True)
                result["debug"].append("  Menu Time cliccato (locator)")
            except Exception:
                result["debug"].append("  ⚠️ Menu Time non trovato")
        time.sleep(3)

        # Tab calendario
        calendar_tabs = ["Mese", "Calendario", "Agenda", "Calendar", "Month"]
        tab_clicked = False
        for tab_name in calendar_tabs:
            try:
                tab = page.locator(f"text={tab_name}").first
                if tab.is_visible(timeout=2000):
                    tab.click(force=True)
                    result["debug"].append(f"  ✅ Tab '{tab_name}' cliccato")
                    tab_clicked = True
                    break
            except Exception:
                continue

        if not tab_clicked:
            for tab_id in ["lnktab_0_label", "lnktab_1_label", "lnktab_2_label"]:
                try:
                    if page.evaluate(f"!!document.getElementById('{tab_id}')"):
                        page.evaluate(f"document.getElementById('{tab_id}')?.click()")
                        result["debug"].append(f"  ✅ Tab {tab_id} cliccato")
                        break
                except Exception:
                    pass

        time.sleep(4)

        # Frame calendario
        result["debug"].append("🔍 Ricerca eventi nell'IFRAME del calendario...")
        calendar_frame = None
        for frame in page.frames:
            if "CalUI" in frame.name or "calendar" in frame.url:
                calendar_frame = frame
                result["debug"].append(f"  ✅ Frame calendario trovato: {frame.name}")
                break

        # Scraping DOM (come nel tuo codice: lo lasciamo, è lungo ma funzionale)
        dom_events = []
        found_any = False

        if calendar_frame:
            try:
                calendar_frame.locator("body").wait_for(timeout=2000)
                time.sleep(2)

                grid = calendar_frame.locator("#calendarContainer, #calendarUI_ExtendedCalendar_0").first
                search_area = grid if grid.is_visible() else calendar_frame.locator("body")
                src_name = "Griglia" if grid.is_visible() else "BODY (Fallback)"
                result["debug"].append(f"  Target scraping: {src_name}")

                allowed_boxes = []
                try:
                    cell_selectors = [
                        ".dijitCalendarCurrentMonth",
                        "td:not(.dijitCalendarPreviousMonth):not(.dijitCalendarNextMonth)",
                    ]
                    for sel in cell_selectors:
                        try:
                            cells = search_area.locator(sel).all()
                            for c in cells:
                                if c.is_visible():
                                    b = c.bounding_box()
                                    if b:
                                        allowed_boxes.append(b)
                            if len(allowed_boxes) >= 28:
                                break
                        except Exception:
                            continue
                    result["debug"].append(f"  ✅ Mappate {len(allowed_boxes)} celle giorni mese corrente")
                except Exception:
                    pass

                mese_nome_corrente = MESI_IT[mese_num - 1]
                altri_mesi = [m.lower()[:3] for m in MESI_IT if m.lower()[:3] != mese_nome_corrente.lower()[:3]]

                all_kws = ["OMESSA", "OMT", "MANCATA", "ANOMALIA", "FERIE", "FEP", "MALATTIA", "MAL", "RIPOSO", "RCS", "RIC", "RPS", "REC"]

                for kw in all_kws:
                    matches = search_area.locator(f"text={kw}")
                    count = matches.count()
                    real_matches = 0

                    for i in range(count):
                        try:
                            el = matches.nth(i)
                            if not el.is_visible():
                                continue

                            txt_upper = el.inner_text().upper()
                            txt_lower = el.inner_text().lower()

                            if "SALDO" in txt_upper or "RESIDUO" in txt_upper:
                                continue
                            if "TOTALE" in txt_upper or "PERMESSI DEL" in txt_upper:
                                continue

                            # filtro altri mesi (testuale)
                            if any(am in txt_lower for am in altri_mesi):
                                continue

                            box = el.bounding_box()
                            if not box:
                                continue

                            # sidebar a sinistra
                            if box["x"] < 300:
                                continue

                            # whitelist box
                            if allowed_boxes:
                                cx = box["x"] + box["width"] / 2
                                cy = box["y"] + box["height"] / 2
                                ok = False
                                for g in allowed_boxes:
                                    if (g["x"] <= cx <= g["x"] + g["width"]) and (g["y"] <= cy <= g["y"] + g["height"]):
                                        ok = True
                                        break
                                if not ok:
                                    continue

                            real_matches += 1
                            if kw in ["OMESSA", "OMT", "MANCATA", "ANOMALIA"]:
                                dom_events.append("OMESSA TIMBRATURA")
                            elif kw in ["FERIE", "FEP"]:
                                dom_events.append("FERIE")
                            elif kw in ["MALATTIA", "MAL"]:
                                dom_events.append("MALATTIA")
                            elif kw in ["RIPOSO", "RCS", "RIC", "RPS", "REC"]:
                                dom_events.append("RIPOSO")

                        except Exception:
                            pass

                    if real_matches > 0:
                        result["debug"].append(f"  📝 Trovati {real_matches} x '{kw}' validi")
                        found_any = True

                if not found_any:
                    result["debug"].append("  ⚠️ Nessun evento valido trovato (possibile filtro geometrico troppo stretto)")

            except Exception as e:
                result["debug"].append(f"  ❌ Errore scraping: {e}")

        result["debug"].append(f"📋 Totale eventi DOM estratti: {len(dom_events)}")

    except Exception as e:
        result["debug"].append(f"❌ Errore navigazione: {type(e).__name__}: {e}")
    finally:
        try:
            page.remove_listener("response", capture_calendar_response)
        except Exception:
            pass

    # Processa eventi catturati + DOM
    all_events = captured_events + [{"summary": e} for e in dom_events]

    for ev in all_events:
        summary = str(ev.get("summary", "") or ev.get("title", "") or ev.get("description", "")).upper()

        if "SALDO" in summary or "RESIDUO" in summary or "TOTALE" in summary:
            continue
        if "PERMESSI DEL" in summary:
            continue

        start = ev.get("startTime", "") or ev.get("start", "") or ev.get("date", "")
        if start and len(str(start)) >= 7:
            try:
                ev_month = int(str(start)[5:7])
                if ev_month != mese_num:
                    continue
            except Exception:
                pass

        is_omessa = (
            any(k in summary for k in ["OMESSA", "OMT", "MANCATA", "ANOMALIA"])
            or ev.get("isAnomaly") is True
            or ev.get("warning")
            or ev.get("type") == "Anomaly"
        )

        if is_omessa:
            result["events_by_type"]["OMESSA TIMBRATURA"] = result["events_by_type"].get("OMESSA TIMBRATURA", 0) + 1
        elif "FERIE" in summary or "FEP" in summary:
            result["events_by_type"]["FERIE"] = result["events_by_type"].get("FERIE", 0) + 1
        elif "MALATTIA" in summary or "MAL" in summary:
            result["events_by_type"]["MALATTIA"] = result["events_by_type"].get("MALATTIA", 0) + 1
        elif any(k in summary for k in ["RIPOSO", "RCS", "RIC", "RPS", "REC"]):
            result["events_by_type"]["RIPOSO"] = result["events_by_type"].get("RIPOSO", 0) + 1

    result["total_events"] = sum(result["events_by_type"].values())
    result["debug"].append(f"📊 Totale categorizzati: {result['total_events']}")
    result["success"] = True
    return result


def read_agenda_api(context, mese_num, anno):
    result = {"events_by_type": {}, "total_events": 0, "items": [], "debug": ["📡 Tentativo API dirette..."], "success": False}
    base_url = "https://selfservice.gottardospa.it/js_rev/JSipert2"

    code_to_norm = {
        "FEP": "FERIE",
        "OMT": "OMESSA TIMBRATURA",
        "RCS": "RIPOSO",
        "RIC": "RIPOSO",
        "MAL": "MALATTIA",
    }

    for code, name in CALENDAR_CODES.items():
        try:
            url = f"{base_url}/api/time/v2/events?$filter_api=calendarCode={code},startTime={anno}-01-01T00:00:00,endTime={anno}-12-31T00:00:00"
            resp = context.request.get(url, timeout=10000)
            result["debug"].append(f"  {code}: status={resp.status}")

            if resp.ok:
                try:
                    data = resp.json()
                    if data:
                        events = data if isinstance(data, list) else [data]
                        month_events = []
                        for ev in events:
                            start = ev.get("startTime", "") or ev.get("start", "")
                            if start and len(start) >= 7:
                                try:
                                    ev_month = int(start[5:7])
                                    if ev_month == mese_num:
                                        month_events.append(ev)
                                except Exception:
                                    pass

                        if month_events:
                            key = code_to_norm.get(code, name)
                            result["events_by_type"][key] = result["events_by_type"].get(key, 0) + len(month_events)
                            result["total_events"] += len(month_events)
                except Exception as e:
                    result["debug"].append(f"  ❌ {code} parse error: {e}")
        except Exception as e:
            result["debug"].append(f"  ⚠️ {code}: {type(e).__name__}: {e}")

    if result["total_events"] > 0:
        result["success"] = True
    return result


# ==============================================================================
# SCRAPER CORE
# ==============================================================================
def execute_download(mese_nome, anno, user, pwd, is_13ma):
    results = {"busta": None, "cart": None, "agenda": None, "login_ok": None, "login_error": None}

    try:
        idx = MESI_IT.index(mese_nome) + 1
    except Exception:
        return results

    suffix = "_13" if is_13ma else ""
    local_busta = os.path.abspath(f"busta_{idx}_{anno}{suffix}.pdf")
    local_cart = os.path.abspath(f"cartellino_{idx}_{anno}.pdf")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context(accept_downloads=True, user_agent="Mozilla/5.0 Chrome/120.0.0.0")
        ctx.set_default_timeout(45000)
        page = ctx.new_page()
        page.set_viewport_size({"width": 1920, "height": 1080})

        try:
            # === LOGIN ===
            st.toast("🔐 Login...", icon="🔐")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y", wait_until="domcontentloaded")
            page.wait_for_selector("#ParametriLogin input[name='username']", timeout=20000)

            u_in = page.locator("#ParametriLogin input[name='username']").first
            p_in = page.locator("#ParametriLogin input[name='password']").first
            u_in.fill(user)
            p_in.fill(pwd)

            submitted = False
            try:
                btn = page.get_by_role("button", name="Accedi")
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click()
                    submitted = True
            except Exception:
                pass

            if not submitted:
                try:
                    p_in.press("Enter")
                except Exception:
                    pass

            try:
                page.wait_for_load_state("networkidle", timeout=25000)
            except Exception:
                time.sleep(3)

            login_ok = False
            for sel in ["text=I miei dati", "text=Time", "text=Documenti", "#revit_navigation_NavHoverItem_0_label", "#revit_navigation_NavHoverItem_2_label"]:
                try:
                    if page.locator(sel).first.is_visible(timeout=5000):
                        login_ok = True
                        break
                except Exception:
                    continue

            if not login_ok:
                results["login_ok"] = False
                results["login_error"] = "Login fallito: elementi post-login non trovati."
                st.error("❌ Login fallito")
                try:
                    st.caption(f"URL: {page.url}")
                except Exception:
                    pass
                return results

            results["login_ok"] = True

            # === AGENDA ===
            st.toast("🗓️ Lettura Agenda...", icon="🗓️")
            try:
                results["agenda"] = read_agenda_with_navigation(page, ctx, idx, anno)
                if results["agenda"].get("total_events", 0) == 0:
                    results["agenda"] = read_agenda_api(ctx, idx, anno)

                if results["agenda"].get("total_events", 0) > 0:
                    st.toast(f"✅ Agenda: {results['agenda']['total_events']} eventi", icon="📅")
            except Exception as e:
                results["agenda"] = {"events_by_type": {}, "total_events": 0, "debug": [str(e)], "success": False}

            # === BUSTA ===
            st.toast("💰 Scarico Busta...", icon="💰")
            try:
                try:
                    page.keyboard.press("Escape")
                    time.sleep(0.2)
                except Exception:
                    pass

                try:
                    page.evaluate("document.getElementById('revit_navigation_NavHoverItem_0_label')?.click()")
                except Exception:
                    page.locator("text=I miei dati").first.click(force=True)
                time.sleep(2)

                for js_id in ["lnktab_2_label", "lnktab_2"]:
                    try:
                        page.evaluate(f"document.getElementById('{js_id}')?.click()")
                        break
                    except Exception:
                        continue

                try:
                    page.wait_for_selector("text=Cedolino", timeout=10000)
                except Exception:
                    pass

                try:
                    page.locator("tr", has=page.locator("text=Cedolino")).locator(".z-image").click(timeout=5000)
                except Exception:
                    page.locator("text=Cedolino").first.click(force=True)
                time.sleep(3)

                with page.expect_download(timeout=25000) as dl_info:
                    if is_13ma:
                        page.get_by_text(re.compile(f"Tredicesima.*{anno}", re.I)).first.click()
                    else:
                        links = page.locator("a")
                        total = links.count()
                        found = False
                        patterns = [f"{mese_nome} {anno}", f"{idx:02d}/{anno}", f"{idx:02d}-{anno}"]

                        for i in range(total):
                            try:
                                txt = (links.nth(i).inner_text() or "").strip()
                                if not txt or len(txt) < 4:
                                    continue
                                if "Tredicesima" in txt or "13" in txt:
                                    continue
                                if any(pat.lower() in txt.lower() for pat in patterns):
                                    links.nth(i).click()
                                    found = True
                                    break
                            except Exception:
                                continue

                        if not found:
                            raise Exception("Link busta non trovato")

                dl_info.value.save_as(local_busta)
                if os.path.exists(local_busta) and os.path.getsize(local_busta) > 1000:
                    results["busta"] = local_busta
                    st.toast(f"✅ Busta: {os.path.getsize(local_busta):,} bytes", icon="📄")
            except Exception as e:
                st.warning(f"⚠️ Busta: {e}")

            # === CARTELLINO ===
            if not is_13ma:
                st.toast("📅 Scarico Cartellino...", icon="📅")
                try:
                    try:
                        page.keyboard.press("Escape")
                        time.sleep(0.2)
                    except Exception:
                        pass

                    try:
                        page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2", wait_until="domcontentloaded")
                        time.sleep(2)
                    except Exception:
                        pass

                    try:
                        page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
                    except Exception:
                        page.locator("text=Time").first.click(force=True)
                    time.sleep(2)

                    try:
                        page.evaluate("document.getElementById('lnktab_5_label')?.click()")
                    except Exception:
                        page.locator("text=Cartellino").first.click(force=True)
                    time.sleep(4)

                    last_day = calendar.monthrange(anno, idx)[1]
                    d1, d2 = f"01/{idx:02d}/{anno}", f"{last_day}/{idx:02d}/{anno}"

                    dal = page.locator("input[id*='CLRICHIE'][class*='dijitInputInner']").first
                    al = page.locator("input[id*='CLRICHI2'][class*='dijitInputInner']").first

                    if dal.count() > 0 and al.count() > 0:
                        dal.click(force=True)
                        page.keyboard.press("Control+A")
                        dal.fill("")
                        dal.type(d1, delay=60)
                        dal.press("Tab")
                        time.sleep(0.4)

                        al.click(force=True)
                        page.keyboard.press("Control+A")
                        al.fill("")
                        al.type(d2, delay=60)
                        al.press("Tab")
                        time.sleep(0.4)

                    try:
                        page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last.click(force=True)
                    except Exception:
                        page.get_by_role("button", name=re.compile("ricerca|esegui", re.I)).last.click()
                    time.sleep(6)

                    pattern_cart = f"{idx:02d}/{anno}"
                    riga = page.locator(f"tr:has-text('{pattern_cart}')").first

                    if riga.count() > 0 and riga.locator("img[src*='search']").count() > 0:
                        icona = riga.locator("img[src*='search']").first
                    else:
                        icona = page.locator("img[src*='search']").first

                    if icona.count() > 0:
                        with ctx.expect_page(timeout=20000) as popup_info:
                            icona.click()
                        popup = popup_info.value

                        t0 = time.time()
                        last_url = popup.url
                        while time.time() - t0 < 15:
                            u = popup.url
                            if u and u != "about:blank":
                                last_url = u
                                if "SERVIZIO=JPSC" in u:
                                    break
                            time.sleep(0.25)

                        popup_url = last_url.replace("/js_rev//", "/js_rev/")
                        if "EMBED" not in popup_url:
                            popup_url += "&EMBED=y"

                        resp = ctx.request.get(popup_url, timeout=60000)
                        body = resp.body()

                        if body[:4] == b"%PDF":
                            with open(local_cart, "wb") as f:
                                f.write(body)
                            results["cart"] = local_cart
                            st.toast(f"✅ Cartellino: {len(body):,} bytes", icon="📋")

                        try:
                            popup.close()
                        except Exception:
                            pass

                except Exception as e:
                    st.warning(f"⚠️ Cartellino: {e}")

        except Exception as e:
            st.error(f"❌ Errore: {e}")
        finally:
            browser.close()

    return results


# ==============================================================================
# PULIZIA FILE
# ==============================================================================
def cleanup_files(*paths):
    deleted = []
    for p in paths:
        if p and os.path.exists(p):
            try:
                os.remove(p)
                deleted.append(os.path.basename(p))
            except Exception:
                pass
    if deleted:
        st.caption(f"🗑️ Eliminati: {', '.join(deleted)}")


# ==============================================================================
# UI
# ==============================================================================
u = st.session_state.get("u", st.secrets.get("ZK_USER", ""))
pw = st.session_state.get("p", st.secrets.get("ZK_PASS", ""))

if not u or not pw:
    c1, c2, c3 = st.columns([2, 2, 1])
    u_in = c1.text_input("👤 Username")
    p_in = c2.text_input("🔒 Password", type="password")
    if c3.button("Login", type="primary"):
        st.session_state["u"] = u_in
        st.session_state["p"] = p_in
        st.rerun()
else:
    col_u, col_m, col_a, col_btn, col_rst = st.columns([1, 1.5, 1, 1.5, 0.5])
    col_u.markdown(f"**👤 {u}**")
    m = col_m.selectbox("Mese", MESI_IT, index=9)
    a = col_a.selectbox("Anno", [2024, 2025, 2026], index=1)

    tipo = "Cedolino"
    if m == "Dicembre":
        tipo = col_m.radio("Tipo", ["Cedolino", "Tredicesima"], horizontal=True)

    if col_btn.button("🚀 ANALIZZA", type="primary"):
        is_13 = tipo == "Tredicesima"

        with st.status("🔄 Elaborazione...", expanded=True):
            paths = execute_download(m, a, u, pw, is_13)

            if isinstance(paths, dict) and paths.get("login_ok") is False:
                st.error(paths.get("login_error", "❌ Login fallito"))
                st.stop()

            st.write("🧠 Analisi AI...")
            res_b = parse_busta_dettagliata(paths.get("busta"))
            res_c = parse_cartellino_dettagliato(paths.get("cart")) if (not is_13 and paths.get("cart")) else {}

            st.session_state["res"] = {
                "busta": res_b,
                "cart": res_c,
                "agenda": paths.get("agenda", {}) or {},
                "is_13": is_13,
                "mese": m,
                "anno": a,
            }

            cleanup_files(paths.get("busta"), paths.get("cart"))

    if col_rst.button("🔄"):
        st.session_state.clear()
        st.rerun()


# ==============================================================================
# RISULTATI
# ==============================================================================
if "res" in st.session_state:
    data = st.session_state["res"]
    b = data.get("busta", {}) or {}
    c = data.get("cart", {}) or {}
    agenda = data.get("agenda", {}) or {}
    is_13 = bool(data.get("is_13", False))

    dg = b.get("dati_generali", {}) or {}
    comp = b.get("competenze", {}) or {}
    tratt = b.get("trattenute", {}) or {}
    ferie = b.get("ferie", {}) or {}
    par = b.get("par", {}) or {}

    anno = safe_int(data.get("anno", 2025))
    mese_nome = data.get("mese", "Ottobre")
    mese_num = MESI_IT.index(mese_nome) + 1
    nome_mese = calendar.month_name[mese_num].capitalize()

    # Agenda (chiavi coerenti: events_by_type)
    a_evs = agenda.get("events_by_type", {}) if isinstance(agenda, dict) else {}
    a_omesse = safe_int(a_evs.get("OMESSA TIMBRATURA", 0))
    a_ferie = safe_int(a_evs.get("FERIE", 0))
    a_malattia = safe_int(a_evs.get("MALATTIA", 0))
    a_riposi = safe_int(a_evs.get("RIPOSO", 0))

    # Tabs
    tab1, tab2, tab4 = st.tabs(["💰 Stipendio", "📅 Cartellino", "🏖️ Ferie/PAR"])

    # --------------------
    # Tab verifica (top)
    # --------------------
    if not is_13:
        c_lavorati = safe_float(c.get("giorni_lavorati", 0))
        c_ore_lavorate = safe_float(c.get("ore_lavorate", 0))
        c_riposi = safe_int(c.get("riposi", 0))
        c_festivita = safe_int(c.get("festivita", 0))
        c_malattia = safe_int(c.get("malattia", 0))
        c_ferie = safe_int(c.get("ferie", 0))

        assenze_busta = b.get("assenze_mese", {}) or {}
        ore_ferie_busta = safe_float(assenze_busta.get("ore_ferie", 0))
        ore_permessi_busta = safe_float(assenze_busta.get("ore_permessi", 0))
        ore_malattia_busta = safe_float(assenze_busta.get("ore_malattia", 0))

        ore_assenze_busta = ore_ferie_busta + ore_permessi_busta
        gg_assenze_busta = round(ore_assenze_busta / ORE_PER_GIORNO) if ore_assenze_busta > 0 else 0
        gg_malattia = round(ore_malattia_busta / ORE_PER_GIORNO) if ore_malattia_busta > 0 else c_malattia
        gg_permessi = round(ore_permessi_busta / ORE_PER_GIORNO) if ore_permessi_busta > 0 else 0

        # Ferie: priorità Busta > Cartellino > Agenda (agenda informativa/di supporto)
        gg_ferie_effettive = 0
        use_source_ferie = "Busta"
        if gg_assenze_busta > 0:
            gg_ferie_effettive = gg_assenze_busta
            if c_ferie != gg_ferie_effettive and c_ferie > 0:
                st.info(f"ℹ️ Ferie prese dalla Busta ({gg_ferie_effettive} gg). Cartellino indica {c_ferie}.")
        elif c_ferie > 0:
            gg_ferie_effettive = c_ferie
            use_source_ferie = "Cartellino"
        elif a_ferie > 0:
            gg_ferie_effettive = a_ferie
            use_source_ferie = "Agenda"

        final_omesse = a_omesse  # SOLO agenda

        gg_pagati_busta = safe_int(dg.get("giorni_pagati", 0))
        tot_calcolato = c_lavorati + gg_ferie_effettive + gg_malattia + c_festivita
        diff_gg = tot_calcolato - gg_pagati_busta

        st.markdown("---")
        st.subheader(f"📊 Verifica {nome_mese} {anno}")

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("📅 GG INPS (Busta)", gg_pagati_busta)
        col2.metric(
            "📋 GG Calcolati",
            f"{tot_calcolato:.0f}",
            delta=f"{diff_gg:+.0f}" if diff_gg != 0 else None,
            help="Lavorati + Ferie + Malattia + Festività",
        )
        col3.metric("👔 Lavorati (Cartellino)", c_lavorati)
        col4.metric("⚠️ Omesse (Agenda)", final_omesse, help="Solo informativo")

        col5, col6, col7, col8 = st.columns(4)
        if use_source_ferie == "Agenda":
            lbl_ferie, help_ferie = "🏖️ Ferie (Agenda)", "Dati rilevati dal calendario"
        elif use_source_ferie == "Cartellino":
            lbl_ferie, help_ferie = "🏖️ Ferie (Cartellino)", "Giorni 'FER' contati dal cartellino"
        else:
            lbl_ferie, help_ferie = "🏖️ Ferie (Busta)", "Calcolato dalle ore in busta (documento ufficiale)"

        col5.metric(lbl_ferie, gg_ferie_effettive, help=help_ferie)
        col6.metric("🤒 Malattia", gg_malattia)
        col7.metric("💤 Riposi", c_riposi)
        col8.metric("🎉 Festività", c_festivita)

        if ore_ferie_busta > 0 or ore_permessi_busta > 0 or ore_malattia_busta > 0:
            st.caption(
                f"📋 Dettaglio Busta: {ore_ferie_busta:.0f}h ferie + {ore_permessi_busta:.0f}h permessi"
                f"{(' + ' + str(int(ore_malattia_busta)) + 'h malattia') if ore_malattia_busta > 0 else ''}"
                f" = {(ore_assenze_busta + ore_malattia_busta):.0f}h (assenze ferie/permessi: {gg_assenze_busta} gg)"
            )

        st.markdown("---")

        if gg_pagati_busta > 0:
            if diff_gg == 0:
                st.success("✅ **DATI COERENTI**")
            elif diff_gg > 0:
                overlap = min(final_omesse, int(diff_gg)) if final_omesse > 0 else 0
                if overlap > 0:
                    st.success(
                        f"✅ **DATI COERENTI CON SOVRAPPOSIZIONE**: totale calcolato ({tot_calcolato:.0f}) "
                        f"supera la busta di {diff_gg:.0f} gg; una parte è probabile doppio conteggio "
                        f"legato a {final_omesse} omesse (giorni lavorati) e assenze ferie/permessi."
                    )
                else:
                    st.warning(
                        f"⚠️ **DISCREPANZA (ECCESSO)**: +{diff_gg:.0f} gg. "
                        f"Verifica sovrapposizione tra Lavorati ({c_lavorati}) e Ferie ({gg_ferie_effettive})."
                    )
            else:
                st.error(
                    f"❌ **DISCREPANZA (DIFETTO)**: {diff_gg:.0f} gg. "
                    f"Busta {gg_pagati_busta} vs Calcolato {tot_calcolato:.0f} "
                    f"(Lavorati {c_lavorati} + Ferie {gg_ferie_effettive} + Malattia {gg_malattia} + Fest {c_festivita})"
                )

        if final_omesse > 0:
            st.info(f"ℹ️ Nota: {final_omesse} giorni con omessa timbratura (Agenda). Non cambiano i GG INPS, sono informativi.")

        if c_riposi > 0 or a_riposi > 0:
            st.caption(f"💤 {max(c_riposi, a_riposi)} riposi (domeniche + compensativi) — non contano come GG INPS")

    else:
        if b.get("e_tredicesima"):
            st.success("🎄 **TREDICESIMA ANALIZZATA**")
        else:
            st.info("📄 Cedolino analizzato")

    st.divider()

    # ==============================================================================
    # TAB 1 - Stipendio
    # ==============================================================================
    with tab1:
        netto = safe_float(dg.get("netto", 0))
        lordo = safe_float(comp.get("lordo_totale", 0))
        base = safe_float(comp.get("base", 0))
        anzianita = safe_float(comp.get("anzianita", 0))
        straordinari = safe_float(comp.get("straordinari", 0))
        festivita_val = safe_float(comp.get("festivita", 0))
        inps = safe_float(tratt.get("inps", 0))
        irpef = safe_float(tratt.get("irpef_netta", 0))
        addizionali = safe_float(tratt.get("addizionali", 0))

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("💵 NETTO", f"€ {netto:,.2f}")
        k2.metric("📊 Lordo", f"€ {lordo:,.2f}")
        k3.metric("📆 Giorni Pagati", safe_int(dg.get("giorni_pagati", 0)))
        k4.metric("⏱️ Ore Lavorate", safe_float(dg.get("ore_ordinarie", 0)))

        st.markdown("---")
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("➕ Competenze")
            st.write(f"**Paga Base:** € {base:,.2f}")
            if anzianita > 0:
                st.write(f"**Anzianità:** € {anzianita:,.2f}")
            if straordinari > 0:
                st.write(f"**Straordinari:** € {straordinari:,.2f}")
            if festivita_val > 0:
                st.write(f"**Festività:** € {festivita_val:,.2f}")

        with c2:
            st.subheader("➖ Trattenute")
            st.write(f"**INPS:** € {inps:,.2f}")
            st.write(f"**IRPEF:** € {irpef:,.2f}")
            if addizionali > 0:
                st.write(f"**Addizionali:** € {addizionali:,.2f}")

    # ==============================================================================
    # TAB 2 - Cartellino (riepilogo consolidato)
    # ==============================================================================
    with tab2:
        if c:
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("👔 Lavorati", safe_float(c.get("giorni_lavorati", 0)), help=f"Ore Totali: {safe_float(c.get('ore_lavorate', 0))}")

            # Flag coerenti (niente NameError)
            use_agenda = (locals().get("use_source_ferie", "Busta") == "Agenda")
            use_cartellino = (locals().get("use_source_ferie", "Busta") == "Cartellino")

            if use_agenda:
                label_ferie_tab = "🏖️ Ferie (Agenda)"
            elif use_cartellino:
                label_ferie_tab = "🏖️ Ferie (Cartellino)"
            else:
                label_ferie_tab = "🏖️ Ferie (Busta)"

            # Queste variabili esistono solo se non è tredicesima
            gg_ferie_effettive = locals().get("gg_ferie_effettive", 0)
            gg_malattia = locals().get("gg_malattia", 0)
            final_omesse = locals().get("final_omesse", 0)
            gg_permessi = locals().get("gg_permessi", 0)
            a_ferie = locals().get("a_ferie", 0)
            c_riposi = locals().get("c_riposi", 0)
            c_festivita = locals().get("c_festivita", 0)

            k2.metric(label_ferie_tab, gg_ferie_effettive)
            k3.metric("🤒 Malattia", gg_malattia)
            k4.metric("⚠️ Omesse", final_omesse)

            st.markdown("---")
            k5, k6, k7 = st.columns(3)
            val_permessi = gg_permessi if not (use_agenda and a_ferie > 0) else 0
            k5.metric("📋 Permessi", val_permessi, help="Inclusi nelle Ferie se da Agenda")
            k6.metric("💤 Riposi", c_riposi)
            k7.metric("🎉 Festività", c_festivita)

            if c.get("note"):
                st.info(f"📝 {c['note']}")
        else:
            st.info("Cartellino non disponibile" if not is_13 else "Non applicabile per Tredicesima")

    # ==============================================================================
    # TAB 4 - Ferie/PAR
    # ==============================================================================
    with tab4:
        # Mostra SEMPRE anche il dettaglio ore (se disponibile), così il tab non è “vuoto”
        assenze_busta = b.get("assenze_mese", {}) or {}
        ore_ferie_busta = safe_float(assenze_busta.get("ore_ferie", 0))
        ore_permessi_busta = safe_float(assenze_busta.get("ore_permessi", 0))
        ore_malattia_busta = safe_float(assenze_busta.get("ore_malattia", 0))

        if ore_ferie_busta > 0 or ore_permessi_busta > 0 or ore_malattia_busta > 0:
            st.caption(
                f"📋 Dettaglio busta (ore): {ore_ferie_busta:.0f}h ferie + {ore_permessi_busta:.0f}h permessi + {ore_malattia_busta:.0f}h malattia"
            )

        c1, c2 = st.columns(2)

        with c1:
            st.subheader("🏖️ Ferie")
            f1, f2 = st.columns(2)
            f1.metric("Residue AP", f"{safe_float(ferie.get('residue_ap', 0)):.2f}")
            f2.metric("Maturate", f"{safe_float(ferie.get('maturate', 0)):.2f}")
            f3, f4 = st.columns(2)
            f3.metric("Godute", f"{safe_float(ferie.get('godute', 0)):.2f}")
            f4.metric("Saldo", f"{safe_float(ferie.get('saldo', 0)):.2f}")

        with c2:
            st.subheader("⏱️ Permessi (PAR)")
            p1, p2 = st.columns(2)
            p1.metric("Residui AP", f"{safe_float(par.get('residue_ap', 0)):.2f}")
            p2.metric("Spettanti", f"{safe_float(par.get('spettanti', 0)):.2f}")
            p3, p4 = st.columns(2)
            p3.metric("Fruite", f"{safe_float(par.get('fruite', 0)):.2f}")
            p4.metric("Saldo", f"{safe_float(par.get('saldo', 0)):.2f}")
