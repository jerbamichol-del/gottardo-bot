# ==============================================================================
# GOTTARDO PAYROLL ANALYZER - VERSIONE COMPLETA (DOWNLOAD ROBUSTO)
# ==============================================================================
# - Download Busta + Cartellino (Playwright)
# - Lettura Agenda via Navigazione (intercetta rete) + fallback API
# - Parsing AI (Gemini PDF) + fallback DeepSeek (testo PDF)
# - Verifica coerente: se Cartellino manca => verifica GG "parziale" (no falsi allarmi)
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

@st.cache_resource
def ensure_playwright_installed():
    # In cloud a volte serve; lo facciamo una sola volta per sessione.
    try:
        subprocess.run(["playwright", "install", "chromium"], check=False)
    except Exception:
        pass
    return True

ensure_playwright_installed()

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

# Conversione ore -> giorni (coerente con il tuo 80h ≈ 11gg)
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
            val = val.strip().replace(",", ".")
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
- NETTO: riga "PROGRESSIVI" colonna finale
- GIORNI PAGATI: riga "GG. INPS"
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

5) ASSENZE DEL MESE:
- ore_ferie: "FERIE GODUTE" (spesso 4521) -> colonna ORE
- ore_permessi: "PERMESSI GODUTI"/"ROL GODUTI" (spesso 4529) -> colonna ORE
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
- Codici che iniziano con 'V'
- Righe con orari timbratura
- Righe "ORD" o "STR"
NON contare righe che hanno SOLO assenze senza timbrature.
-> giorni_righe

3) ALTRI CODICI:
- festivita: F70/FST/FES
- ferie: FER/FE/FEP
- permessi: PAR/PER/ROL
- malattia: MAL
- omesse_timbrature: SOLO se scritto OMESSA/ANOMALIA/MANCATA TIMBRATURA

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
# AGENDA - NAVIGAZIONE + INTERCETTAZIONE RETE (con fallback API)
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
        # Time menu
        try:
            page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
        except Exception:
            try:
                page.locator("text=Time").first.click(force=True)
            except Exception:
                pass
        time.sleep(2.5)

        # Tab calendario
        tab_clicked = False
        for tab_name in ["Mese", "Calendario", "Agenda", "Calendar", "Month"]:
            try:
                tab = page.locator(f"text={tab_name}").first
                if tab.is_visible(timeout=2000):
                    tab.click(force=True)
                    tab_clicked = True
                    break
            except Exception:
                continue

        if not tab_clicked:
            for tab_id in ["lnktab_0_label", "lnktab_1_label", "lnktab_2_label"]:
                try:
                    if page.evaluate(f"!!document.getElementById('{tab_id}')"):
                        page.evaluate(f"document.getElementById('{tab_id}')?.click()")
                        break
                except Exception:
                    pass

        time.sleep(3.5)

        # Frame calendario
        calendar_frame = None
        for frame in page.frames:
            if "CalUI" in frame.name or "calendar" in frame.url:
                calendar_frame = frame
                break

        dom_events = []
        if calendar_frame:
            try:
                calendar_frame.locator("body").wait_for(timeout=4000)
                time.sleep(1.5)

                grid = calendar_frame.locator("#calendarContainer, #calendarUI_ExtendedCalendar_0").first
                search_area = grid if grid.is_visible() else calendar_frame.locator("body")

                allowed_boxes = []
                for sel in [
                    ".dijitCalendarCurrentMonth",
                    "td:not(.dijitCalendarPreviousMonth):not(.dijitCalendarNextMonth)",
                    "td[style*='background']:not([style*='gray'])",
                ]:
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

                mese_nome_corrente = MESI_IT[mese_num - 1]
                altri_mesi = [m.lower()[:3] for m in MESI_IT if m.lower()[:3] != mese_nome_corrente.lower()[:3]]

                all_kws = ["OMESSA", "OMT", "MANCATA", "ANOMALIA", "FERIE", "FEP", "MALATTIA", "MAL", "RIPOSO", "RCS", "RIC", "RPS", "REC"]
                for kw in all_kws:
                    matches = search_area.locator(f"text={kw}")
                    for i in range(matches.count()):
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
                            if any(am in txt_lower for am in altri_mesi):
                                continue

                            box = el.bounding_box()
                            if not box:
                                continue
                            if box["x"] < 300:
                                continue

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
            except Exception:
                pass

    except Exception:
        pass
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
    result["success"] = True
    return result


def read_agenda_api(context, mese_num, anno):
    result = {"events_by_type": {}, "total_events": 0, "items": [], "debug": [], "success": False}
    base_url = "https://selfservice.gottardospa.it/js_rev/JSipert2"

    code_to_norm = {
        "FEP": "FERIE",
        "OMT": "OMESSA TIMBRATURA",
        "RCS": "RIPOSO",
        "RIC": "RIPOSO",
        "MAL": "MALATTIA",
    }

    for code in CALENDAR_CODES.keys():
        try:
            url = f"{base_url}/api/time/v2/events?$filter_api=calendarCode={code},startTime={anno}-01-01T00:00:00,endTime={anno}-12-31T00:00:00"
            resp = context.request.get(url, timeout=10000)
            if resp.ok:
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
                        key = code_to_norm.get(code, code)
                        result["events_by_type"][key] = result["events_by_type"].get(key, 0) + len(month_events)
                        result["total_events"] += len(month_events)
        except Exception:
            pass

    if result["total_events"] > 0:
        result["success"] = True
    return result


# ==============================================================================
# CARTELLINO CLICK ROBUSTO (best-of: selettore "buono" + timeouts lunghi)
# ==============================================================================
def click_esegui_ricerca_cartellino(page) -> None:
    # Aspetta che eventuale validazione date abiliti il bottone
    time.sleep(0.8)

    candidates = [
        # IDENTICO al selettore della versione che scaricava
        "//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']",
        # Varianti utili
        "//span[contains(.,'Esegui ricerca')]/ancestor::*[@role='button'][1]",
        "//span[contains(text(),'Ricerca')]/ancestor::span[@role='button']",
        "[role='button']:has-text('Esegui ricerca')",
        "span[role='button']:has-text('Esegui ricerca')",
        "[role='button']:has-text('Esegui')",
        "span[role='button']:has-text('Esegui')",
    ]

    last_err = None
    for sel in candidates:
        try:
            loc = page.locator(sel).last
            loc.wait_for(state="visible", timeout=20000)
            loc.click(force=True, timeout=20000)
            return
        except Exception as e:
            last_err = e

    # fallback finale (a volte matcha)
    try:
        page.get_by_role("button", name=re.compile(r"ricerca|esegui", re.I)).last.click(timeout=20000)
        return
    except Exception as e:
        raise Exception(f"Bottone 'Esegui ricerca' non trovato/cliccabile: {last_err or e}")


# ==============================================================================
# SCRAPER CORE
# ==============================================================================
def execute_download(mese_nome, anno, user, pwd, is_13ma):
    results = {"busta": None, "cart": None, "agenda": None, "login_ok": None, "login_error": None}

    try:
        idx = MESI_IT.index(mese_nome) + 1
    except Exception:
        return results

    anno = safe_int(anno)
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
            for sel in [
                "text=I miei dati",
                "text=Time",
                "text=Documenti",
                "#revit_navigation_NavHoverItem_0_label",
                "#revit_navigation_NavHoverItem_2_label",
            ]:
                try:
                    if page.locator(sel).first.is_visible(timeout=5000):
                        login_ok = True
                        break
                except Exception:
                    continue

            if not login_ok:
                results["login_ok"] = False
                results["login_error"] = "Login fallito: elementi post-login non trovati."
                return results

            results["login_ok"] = True

            # === AGENDA ===
            st.toast("🗓️ Lettura Agenda...", icon="🗓️")
            try:
                results["agenda"] = read_agenda_with_navigation(page, ctx, idx, anno)
                if results["agenda"].get("total_events", 0) == 0:
                    results["agenda"] = read_agenda_api(ctx, idx, anno)
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
                time.sleep(3.5)

                with page.expect_download(timeout=25000) as dl_info:
                    if is_13ma:
                        page.get_by_text(re.compile(f"Tredicesima.*{anno}", re.I)).first.click()
                    else:
                        links = page.locator("a")
                        total = links.count()
                        found = False
                        patterns = [
                            f"{mese_nome} {anno}",
                            f"{idx:02d}/{anno}",
                            f"{idx}/{anno}",
                            f"{idx:02d}-{anno}",
                            f"{idx}-{anno}",
                        ]

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

                    # Torna in home (più stabile che restare in Documenti)
                    try:
                        page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2", wait_until="domcontentloaded")
                        time.sleep(2.5)
                    except Exception:
                        pass

                    # Menu Time
                    try:
                        page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
                    except Exception:
                        page.locator("text=Time").first.click(force=True)
                    time.sleep(2.5)

                    # Tab Cartellino
                    try:
                        page.evaluate("document.getElementById('lnktab_5_label')?.click()")
                    except Exception:
                        page.locator("text=Cartellino").first.click(force=True)
                    time.sleep(5)

                    last_day = calendar.monthrange(anno, idx)[1]
                    d1 = f"01/{idx:02d}/{anno}"
                    d2 = f"{last_day}/{idx:02d}/{anno}"

                    dal = page.locator("input[id*='CLRICHIE'][class*='dijitInputInner']").first
                    al = page.locator("input[id*='CLRICHI2'][class*='dijitInputInner']").first

                    # aspetta input visibili
                    dal.wait_for(state="visible", timeout=20000)
                    al.wait_for(state="visible", timeout=20000)

                    # fill con typing lento (ZK)
                    dal.click(force=True)
                    page.keyboard.press("Control+A")
                    dal.fill("")
                    dal.type(d1, delay=80)
                    dal.press("Tab")
                    time.sleep(0.6)

                    al.click(force=True)
                    page.keyboard.press("Control+A")
                    al.fill("")
                    al.type(d2, delay=80)
                    al.press("Tab")
                    time.sleep(0.6)

                    # click ricerca
                    click_esegui_ricerca_cartellino(page)
                    time.sleep(8)

                    # aspetta che compaiano risultati / icone
                    try:
                        page.locator("img[src*='search']").first.wait_for(state="visible", timeout=20000)
                    except Exception:
                        pass

                    # trova riga mese (tollerante)
                    patterns = [f"{idx:02d}/{anno}", f"{idx}/{anno}", f"{idx:02d}-{anno}", f"{idx}-{anno}"]
                    riga = None
                    for pat in patterns:
                        rr = page.locator(f"tr:has-text('{pat}')").first
                        if rr.count() > 0:
                            riga = rr
                            break

                    if riga is not None and riga.locator("img[src*='search']").count() > 0:
                        icona = riga.locator("img[src*='search']").first
                    else:
                        icona = page.locator("img[src*='search']").first

                    if icona.count() == 0:
                        raise Exception("Icona PDF cartellino non trovata")

                    # popup: prova expect_popup, poi expect_page
                    popup = None
                    try:
                        with page.expect_popup(timeout=20000) as pop_info:
                            icona.click()
                        popup = pop_info.value
                    except Exception:
                        with ctx.expect_page(timeout=20000) as pop_info:
                            icona.click()
                        popup = pop_info.value

                    # aspetta URL "SERVIZIO=JPSC"
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

                    # prova fetch diretto
                    body = b""
                    try:
                        resp = ctx.request.get(popup_url, timeout=60000)
                        body = resp.body()
                    except Exception:
                        body = b""

                    if body[:4] == b"%PDF":
                        with open(local_cart, "wb") as f:
                            f.write(body)
                        if os.path.exists(local_cart) and os.path.getsize(local_cart) > 1000:
                            results["cart"] = local_cart
                    else:
                        # fallback "stampa a PDF" (come nella versione che scaricava)
                        try:
                            popup.pdf(path=local_cart, format="A4")
                            if os.path.exists(local_cart) and os.path.getsize(local_cart) > 5000:
                                results["cart"] = local_cart
                        except Exception:
                            pass

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

st.title("💶 Analisi Stipendio & Presenze")

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
        is_13 = (tipo == "Tredicesima")

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
                "cart_download_ok": bool(paths.get("cart")),
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

    a_evs = agenda.get("events_by_type", {}) if isinstance(agenda, dict) else {}
    a_omesse = safe_int(a_evs.get("OMESSA TIMBRATURA", 0))
    a_ferie = safe_int(a_evs.get("FERIE", 0))
    a_malattia = safe_int(a_evs.get("MALATTIA", 0))
    a_riposi = safe_int(a_evs.get("RIPOSO", 0))

    tab1, tab2, tab3 = st.tabs(["💰 Stipendio", "📅 Cartellino", "🏖️ Ferie/PAR"])

    # --------------------
    # Calcoli coerenti (no locals hack)
    # --------------------
    calc = {
        "cart_ok": False,
        "c_lavorati": 0.0,
        "c_festivita": 0,
        "c_riposi": 0,
        "gg_ferie_eff": 0,
        "gg_mal": 0,
        "gg_perm": 0,
        "use_source_ferie": "Busta",
        "tot_calcolato": 0.0,
        "diff_gg": None,
        "gg_pagati_busta": safe_int(dg.get("giorni_pagati", 0)),
        "final_omesse": a_omesse,
        "ore_ferie_busta": 0.0,
        "ore_permessi_busta": 0.0,
        "ore_malattia_busta": 0.0,
        "gg_assenze_busta": 0,
    }

    if not is_13:
        calc["c_lavorati"] = safe_float(c.get("giorni_lavorati", 0))
        c_ore_lavorate = safe_float(c.get("ore_lavorate", 0))
        calc["c_riposi"] = safe_int(c.get("riposi", 0))
        calc["c_festivita"] = safe_int(c.get("festivita", 0))
        c_malattia = safe_int(c.get("malattia", 0))
        c_ferie = safe_int(c.get("ferie", 0))

        assenze_busta = b.get("assenze_mese", {}) or {}
        calc["ore_ferie_busta"] = safe_float(assenze_busta.get("ore_ferie", 0))
        calc["ore_permessi_busta"] = safe_float(assenze_busta.get("ore_permessi", 0))
        calc["ore_malattia_busta"] = safe_float(assenze_busta.get("ore_malattia", 0))

        ore_assenze_busta = calc["ore_ferie_busta"] + calc["ore_permessi_busta"]
        calc["gg_assenze_busta"] = round(ore_assenze_busta / ORE_PER_GIORNO) if ore_assenze_busta > 0 else 0
        calc["gg_mal"] = round(calc["ore_malattia_busta"] / ORE_PER_GIORNO) if calc["ore_malattia_busta"] > 0 else c_malattia
        calc["gg_perm"] = round(calc["ore_permessi_busta"] / ORE_PER_GIORNO) if calc["ore_permessi_busta"] > 0 else 0

        # Priorità fonte ferie: Busta > Cartellino > Agenda
        if calc["gg_assenze_busta"] > 0:
            calc["gg_ferie_eff"] = calc["gg_assenze_busta"]
            calc["use_source_ferie"] = "Busta"
        elif c_ferie > 0:
            calc["gg_ferie_eff"] = c_ferie
            calc["use_source_ferie"] = "Cartellino"
        elif a_ferie > 0:
            calc["gg_ferie_eff"] = a_ferie
            calc["use_source_ferie"] = "Agenda"

        calc["cart_ok"] = bool(c) and (calc["c_lavorati"] > 0 or c_ore_lavorate > 0)

        if calc["cart_ok"]:
            calc["tot_calcolato"] = calc["c_lavorati"] + calc["gg_ferie_eff"] + calc["gg_mal"] + calc["c_festivita"]
            calc["diff_gg"] = calc["tot_calcolato"] - calc["gg_pagati_busta"]
        else:
            calc["tot_calcolato"] = calc["gg_ferie_eff"] + calc["gg_mal"]
            calc["diff_gg"] = None

        # VERIFICA (top)
        st.markdown("---")
        st.subheader(f"📊 Verifica {mese_nome} {anno}")

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("📅 GG INPS (Busta)", calc["gg_pagati_busta"])
        col2.metric(
            "📋 GG Calcolati" + ("" if calc["cart_ok"] else " (parziale)"),
            f"{calc['tot_calcolato']:.0f}",
            delta=(f"{calc['diff_gg']:+.0f}" if (calc["cart_ok"] and calc["diff_gg"] != 0) else None),
            help=("Lavorati + Ferie + Malattia + Festività" if calc["cart_ok"] else "Solo Ferie/Permessi/Malattia (Cartellino non disponibile)"),
        )
        col3.metric("👔 Lavorati (Cartellino)", calc["c_lavorati"])
        col4.metric("⚠️ Omesse (Agenda)", calc["final_omesse"], help="Solo informativo")

        col5, col6, col7, col8 = st.columns(4)
        if calc["use_source_ferie"] == "Agenda":
            lbl_ferie = "🏖️ Ferie (Agenda)"
        elif calc["use_source_ferie"] == "Cartellino":
            lbl_ferie = "🏖️ Ferie (Cartellino)"
        else:
            lbl_ferie = "🏖️ Ferie (Busta)"

        col5.metric(lbl_ferie, calc["gg_ferie_eff"])
        col6.metric("🤒 Malattia", calc["gg_mal"])
        col7.metric("💤 Riposi", calc["c_riposi"])
        col8.metric("🎉 Festività", calc["c_festivita"])

        if calc["ore_ferie_busta"] > 0 or calc["ore_permessi_busta"] > 0 or calc["ore_malattia_busta"] > 0:
            ore_assenze_busta = calc["ore_ferie_busta"] + calc["ore_permessi_busta"]
            st.caption(
                f"📋 Dettaglio Busta: {calc['ore_ferie_busta']:.0f}h ferie + {calc['ore_permessi_busta']:.0f}h permessi"
                f"{(' + ' + str(int(calc['ore_malattia_busta'])) + 'h malattia') if calc['ore_malattia_busta'] > 0 else ''}"
                f" = {(ore_assenze_busta + calc['ore_malattia_busta']):.0f}h (assenze ferie/permessi: {calc['gg_assenze_busta']} gg)"
            )

        if not calc["cart_ok"]:
            st.warning("📌 Cartellino non disponibile: la verifica GG è parziale (mancano i lavorati).")
        else:
            if calc["diff_gg"] == 0:
                st.success("✅ DATI COERENTI")
            elif calc["diff_gg"] > 0:
                st.warning(f"⚠️ DISCREPANZA (ECCESSO): +{calc['diff_gg']:.0f} gg. Possibile sovrapposizione lavorati/assenze.")
            else:
                st.error(
                    f"❌ DISCREPANZA (DIFETTO): {calc['diff_gg']:.0f} gg. "
                    f"Busta {calc['gg_pagati_busta']} vs Calcolato {calc['tot_calcolato']:.0f}"
                )

        st.divider()

    # TAB 1 - Stipendio
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
            st.write(f"Paga Base: € {base:,.2f}")
            if anzianita > 0:
                st.write(f"Anzianità: € {anzianita:,.2f}")
            if straordinari > 0:
                st.write(f"Straordinari: € {straordinari:,.2f}")
            if festivita_val > 0:
                st.write(f"Festività: € {festivita_val:,.2f}")

        with c2:
            st.subheader("➖ Trattenute")
            st.write(f"INPS: € {inps:,.2f}")
            st.write(f"IRPEF: € {irpef:,.2f}")
            if addizionali > 0:
                st.write(f"Addizionali: € {addizionali:,.2f}")

    # TAB 2 - Cartellino
    with tab2:
        if c:
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("👔 Lavorati", safe_float(c.get("giorni_lavorati", 0)), help=f"Ore Totali: {safe_float(c.get('ore_lavorate', 0))}")
            k2.metric("🏖️ Ferie", safe_int(c.get("ferie", 0)))
            k3.metric("🤒 Malattia", safe_int(c.get("malattia", 0)))
            k4.metric("🎉 Festività", safe_int(c.get("festivita", 0)))

            if c.get("note"):
                st.info(c["note"])
        else:
            st.info("Cartellino non disponibile" if not is_13 else "Non applicabile per Tredicesima")

    # TAB 3 - Ferie / PAR
    with tab3:
        if calc["ore_ferie_busta"] > 0 or calc["ore_permessi_busta"] > 0 or calc["ore_malattia_busta"] > 0:
            st.caption(
                f"📋 Dettaglio busta (ore): {calc['ore_ferie_busta']:.2f} ferie + {calc['ore_permessi_busta']:.2f} permessi + {calc['ore_malattia_busta']:.2f} malattia"
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
