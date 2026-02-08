# ==============================================================================
# GOTTARDO PAYROLL ANALYZER - VERSIONE COMPLETA (FIXED)
# ==============================================================================
# Obiettivi:
# - Scarica Busta Paga + Cartellino (Playwright)
# - Legge Agenda (API + fallback navigazione)
# - Parsing AI (Gemini con fallback DeepSeek) -> JSON strutturato
# - Controllo incrociato triplo (Busta + Cartellino + Agenda)
#
# REGOLE BUSINESS IMPORTANTI (come richiesto):
# 1) "OMESSA TIMBRATURA" = GIORNO LAVORATO (anomalia), NON assenza.
# 2) Le Omesse si prendono SOLO dall'AGENDA.
# 3) I permessi possono essere trasformati dall'azienda in FERIE sulla BUSTA:
#    - non pretendere che Cartellino mostri quei permessi/ferie allo stesso modo.
#    - per i controlli usare sempre: Busta = ore (ufficiale), Cartellino = presenza/ore lavorate.
# ==============================================================================

import sys
import asyncio
import re
import os
import json
import time
import calendar
import locale
from pathlib import Path
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

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

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

try:
    locale.setlocale(locale.LC_TIME, "it_IT.UTF-8")
except Exception:
    pass

MESI_IT = [
    "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre",
]

# Codici eventi calendario (approssimazione: dipende dal portale)
CALENDAR_CODES = {
    "FEP": "FERIE PIANIFICATE",
    "OMT": "OMESSA TIMBRATURA",
    "RCS": "RIPOSO COMPENSATIVO SUCC",
    "RIC": "RIPOSO COMPENSATIVO FORZ",
    "MAL": "MALATTIA",
}

CODE_TO_NORMALIZED = {
    "FEP": "FERIE",
    "OMT": "OMESSA TIMBRATURA",
    "RCS": "RIPOSO",
    "RIC": "RIPOSO",
    "MAL": "MALATTIA",
}

AGENDA_KEYWORDS = [
    "OMESSA TIMBRATURA", "OMESSA", "OMT", "MANCATA", "ANOMALIA",
    "MALATTIA", "MAL",
    "RIPOSO COMPENSATIVO", "RCS", "RIC", "RPS", "REC",
    "FERIE PIANIFICATE", "FERIE", "FEP",
    "PERMESSO", "PAR", "ASSENZA",
]


# ==============================================================================
# UTIL
# ==============================================================================
def to_float(v, default=0.0):
    try:
        if isinstance(v, str):
            v = v.replace(".", "").replace(",", ".").strip()
        return float(v)
    except Exception:
        return float(default)


def to_int(v, default=0):
    try:
        if isinstance(v, str):
            v = v.strip()
        return int(float(v))
    except Exception:
        return int(default)


def clamp(n, lo, hi):
    return max(lo, min(hi, n))


def safe_get(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return cur if cur is not None else default


def normalize_month_name(mese_nome: str) -> str:
    if not mese_nome:
        return ""
    m = mese_nome.strip().lower()
    for it in MESI_IT:
        if it.lower() == m:
            return it
    # accetta abbreviazioni: "ago", "sett", ecc.
    for it in MESI_IT:
        if it.lower().startswith(m[:3]):
            return it
    return mese_nome.strip().capitalize()


def month_to_num(mese_nome: str) -> int:
    mese_nome = normalize_month_name(mese_nome)
    try:
        return MESI_IT.index(mese_nome) + 1
    except Exception:
        return 0


def build_file_path(prefix: str, mese_num: int, anno: int, suffix: str = "", ext: str = "pdf"):
    sfx = f"_{suffix}" if suffix else ""
    return os.path.abspath(f"{prefix}_{mese_num:02d}_{anno}{sfx}.{ext}")


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


def analyze_with_fallback(file_path: str, prompt: str, tipo="documento"):
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

    # Fallback DeepSeek (testo estratto)
    if deepseek_key and OpenAI:
        try:
            progress.warning(f"⚠️ Gemini esaurito. Fallback DeepSeek per {tipo}...")
            text = extract_text_from_pdf(file_path)
            if not text or len(text) < 50:
                progress.error("❌ PDF non leggibile per DeepSeek")
                return None

            client = OpenAI(api_key=deepseek_key, base_url="https://api.deepseek.com")
            full_prompt = prompt + "\n\n--- TESTO PDF (TRONCATO) ---\n" + text[:25000]

            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "Rispondi solo JSON valido."},
                    {"role": "user", "content": full_prompt},
                ],
                temperature=0.1,
            )

            result = clean_json_response(resp.choices[0].message.content)
            if result and isinstance(result, dict):
                progress.success(f"✅ {tipo} analizzato (DeepSeek)!")
                time.sleep(0.25)
                progress.empty()
                return result

        except Exception as e:
            last_error = e

    progress.error(f"❌ Analisi {tipo} fallita")
    if last_error:
        with st.expander("🔎 Errore"):
            st.code(str(last_error)[:1200])
    return None


# ==============================================================================
# PARSERS AI
# ==============================================================================
def parse_busta_dettagliata(path: str):
    prompt = r"""
Questo è un CEDOLINO PAGA italiano (GOTTARDO S.p.A.). Estrai ESATTAMENTE:

1) DATI GENERALI:
- netto: valore del NETTO (campo "NETTO" o progressivi colonna netto).
- giorni_pagati: riga "GG. INPS" (intero).
- ore_ordinarie: "ORE INAIL" se presente (numero), altrimenti 0.

2) COMPETENZE:
- base: "RETRIBUZIONE ORDINARIA" (voce 1000) -> competenze.
- straordinari: somma straordinario/supplementari/notturni se presenti.
- festivita: "MAGG.% FESTIVE" + "FESTIVITA GODUTA" (se presenti).
- anzianita: scatti/EDR/anzianità se presenti.
- lordo_totale: "TOTALE COMPETENZE" (colonna competenze).

3) TRATTENUTE:
- inps: sezione I.N.P.S. (totale trattenute INPS).
- irpef_netta: IRPEF (netta) o riga equivalente.
- addizionali: add. reg + add. com (somma).

4) FERIE/PAR (tabella riepilogo):
- ferie: residue_ap, maturate, godute, saldo
- par: residue_ap, spettanti, fruite, saldo

5) ASSENZE DEL MESE (ORE):
- assenze_mese.ore_ferie: ore della voce "FERIE GODUTE" (es. 4521) se presente.
- assenze_mese.ore_permessi: ore "PERMESSI GODUTI"/"ROL GODUTI" se presente.
- assenze_mese.ore_malattia: ore "MALATTIA" se presenti.

6) 13MA:
- e_tredicesima = true se è un cedolino 13ma.

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


def parse_cartellino_dettagliato(path: str):
    prompt = r"""
Analizza questo CARTELLINO PRESENZE.

1) FOOTER UFFICIALE:
- giorni_footer: valore "GG PRESENZA" o codice 0265 (numero, può avere decimali).
- ore_lavorate: valore "ORE LAVORATE" o codice 0253 (numero).

2) CONTEGGIO RIGHE (VERIFICA):
- giorni_righe: conta righe che indicano PRESENZA/LAVORO:
  - codici che iniziano con V (V70, V50, V29, V01, ecc.) e/o righe con orari.
  - NON contare righe di sola assenza (FER/MAL/RCO/RDD/F70) senza timbrature.

3) ALTRI CODICI (in giorni):
- ferie: conta giorni con FER/FE/FEP.
- permessi: PAR/PER/ROL (giorni).
- malattia: MAL (giorni).
- riposi: RCO/RDD/RIPOSO (giorni).
- festivita: F70/FST/FES (giorni).

IMPORTANTE: "OMESSE TIMBRATURE" nel cartellino NON serve: le omesse vanno prese SOLO dall'agenda.

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
            "festivita": 0,
            "note": "",
        }

    # Normalizzazione: giorni_lavorati = footer se disponibile, altrimenti righe
    gf = to_float(result.get("giorni_footer", 0), 0)
    gr = to_float(result.get("giorni_righe", 0), 0)
    if gf > 0:
        result["giorni_lavorati"] = gf
    elif gr > 0:
        result["giorni_lavorati"] = gr

    # Forza tipi
    for k in ["giorni_lavorati", "giorni_footer", "giorni_righe"]:
        result[k] = to_float(result.get(k, 0), 0)
    result["ore_lavorate"] = to_float(result.get("ore_lavorate", 0), 0)
    for k in ["ferie", "malattia", "permessi", "riposi", "festivita"]:
        result[k] = to_int(result.get(k, 0), 0)
    result["note"] = str(result.get("note", "") or "")

    return result


# ==============================================================================
# AGENDA READERS
# ==============================================================================
def read_agenda_api(context, mese_num: int, anno: int):
    """
    API fallback. Se l'endpoint cambia spesso, questo può fallire: in quel caso si usa la navigazione.
    """
    result = {"events_by_type": {}, "total_events": 0, "items": [], "debug": [], "success": False}

    base_url = "https://selfservice.gottardospa.it/jsrev/JSipert2"
    # Endpoint storicamente visto (può variare)
    # Nota: lasciamo l'anno intero e filtriamo per mese su startTime se presente.
    for code, label in CALENDAR_CODES.items():
        try:
            url = f"{base_url}/api/timev2/events?filter=apiCalendarCode:{code},startTime:{anno}-01-01T00:00:00,endTime:{anno}-12-31T23:59:59"
            resp = context.request.get(url, timeout=10000)
            result["debug"].append(f"API {code} status {resp.status}")
            if not resp.ok:
                continue

            data = resp.json()
            events = data if isinstance(data, list) else data.get("items", []) if isinstance(data, dict) else []
            month_events = []
            for ev in events:
                stt = ev.get("startTime") or ev.get("start") or ev.get("date") or ""
                if isinstance(stt, str) and len(stt) >= 7:
                    try:
                        ev_m = int(stt[5:7])
                        if ev_m != mese_num:
                            continue
                    except Exception:
                        # se non parseable, lo teniamo (ma può inquinare)
                        pass
                month_events.append(ev)

            if month_events:
                norm_key = CODE_TO_NORMALIZED.get(code, label)
                result["events_by_type"][norm_key] = result["events_by_type"].get(norm_key, 0) + len(month_events)
                for ev in month_events[:30]:
                    result["items"].append(f"{code} {ev.get('summary') or ev.get('title') or label}")
        except Exception as e:
            result["debug"].append(f"API {code} error {type(e).__name__}")

    result["total_events"] = sum(result["events_by_type"].values())
    result["success"] = result["total_events"] > 0
    return result


def _normalize_event_summary(summary: str) -> str:
    s = (summary or "").upper()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def read_agenda_with_navigation(page, context, mese_num: int, anno: int):
    """
    Navigazione al calendario + intercettazione risposte JSON + fallback scraping testo DOM.
    Scopo: estrarre conteggi per mese target con filtri anti-sidebar.
    """
    result = {"events_by_type": {}, "total_events": 0, "items": [], "debug": [], "success": False}
    captured_events = []

    def capture_calendar_response(response):
        try:
            url = response.url.lower()
            if any(k in url for k in ["events", "calendar", "time", "anomal"]):
                if response.status == 200:
                    try:
                        data = response.json()
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
        # Click "Time" se presente
        try:
            page.locator("text=Time").first.click(force=True, timeout=3000)
        except Exception:
            pass
        time.sleep(2)

        # Tab "Mese"
        try:
            page.locator("text=Mese").first.click(force=True, timeout=3000)
        except Exception:
            try:
                page.locator("text=Month").first.click(force=True, timeout=3000)
            except Exception:
                pass
        time.sleep(2)

        # Prova ad individuare un frame calendario (se c'è)
        calendar_frame = None
        for fr in page.frames:
            if "calendar" in (fr.url or "").lower() or "cal" in (fr.name or "").lower():
                calendar_frame = fr
                break

        frames_to_scan = [calendar_frame] if calendar_frame else page.frames

        # --- Scraping DOM semplice: cerca keyword e filtra elementi sidebar (x < 300)
        dom_hits = []
        for fr in frames_to_scan:
            try:
                body = fr.locator("body")
                if not body.count():
                    continue

                for kw in ["OMESSA", "OMT", "MANCATA", "ANOMALIA", "FERIE", "FEP", "MAL", "MALATTIA", "RIPOSO", "RCS", "RIC"]:
                    matches = fr.locator(f"text={kw}")
                    for i in range(min(matches.count(), 200)):
                        el = matches.nth(i)
                        try:
                            if not el.is_visible():
                                continue
                            box = el.bounding_box()
                            if box and box.get("x", 9999) < 300:
                                continue  # sidebar
                            txt = _normalize_event_summary(el.inner_text())
                            if any(w in txt for w in ["SALDO", "RESIDUO", "TOTALE", "PERMESSI DEL"]):
                                continue
                            dom_hits.append(txt)
                        except Exception:
                            continue
            except Exception:
                continue

        # --- Unisci: eventi catturati + hits DOM
        # Categorizza per mese (se startTime presente)
        def add_norm(k):
            result["events_by_type"][k] = result["events_by_type"].get(k, 0) + 1

        for ev in captured_events:
            try:
                summary = _normalize_event_summary(ev.get("summary") or ev.get("title") or ev.get("description") or "")
                if not summary:
                    continue

                stt = ev.get("startTime") or ev.get("start") or ev.get("date") or ""
                if isinstance(stt, str) and len(stt) >= 7:
                    try:
                        ev_m = int(stt[5:7])
                        if ev_m != mese_num:
                            continue
                    except Exception:
                        pass

                if any(x in summary for x in ["OMESSA", "OMT", "MANCATA", "ANOMALIA"]) or ev.get("isAnomaly") is True:
                    add_norm("OMESSA TIMBRATURA")
                    result["items"].append(f"OMESSA {summary[:80]}")
                elif "FERIE" in summary or "FEP" in summary:
                    add_norm("FERIE")
                    result["items"].append(f"FERIE {summary[:80]}")
                elif "MALATTIA" in summary or re.search(r"\bMAL\b", summary):
                    add_norm("MALATTIA")
                    result["items"].append(f"MAL {summary[:80]}")
                elif any(x in summary for x in ["RIPOSO", "RCS", "RIC", "RPS", "REC"]):
                    add_norm("RIPOSO")
                    result["items"].append(f"RIPOSO {summary[:80]}")
            except Exception:
                continue

        for txt in dom_hits:
            # DOM hits: non hanno data, quindi assumiamo siano del mese visualizzato
            if any(x in txt for x in ["OMESSA", "OMT", "MANCATA", "ANOMALIA"]):
                add_norm("OMESSA TIMBRATURA")
            elif "FERIE" in txt or "FEP" in txt:
                add_norm("FERIE")
            elif "MALATTIA" in txt or re.search(r"\bMAL\b", txt):
                add_norm("MALATTIA")
            elif any(x in txt for x in ["RIPOSO", "RCS", "RIC", "RPS", "REC"]):
                add_norm("RIPOSO")

        result["total_events"] = sum(result["events_by_type"].values())
        result["success"] = result["total_events"] > 0
        return result

    finally:
        try:
            page.remove_listener("response", capture_calendar_response)
        except Exception:
            pass


# ==============================================================================
# DOWNLOAD
# ==============================================================================
def _fix_download_url(url: str, extra_params: dict):
    try:
        pr = urlparse(url)
        q = dict(parse_qsl(pr.query, keep_blank_values=True))
        q.update(extra_params or {})
        new_q = urlencode(q, doseq=True)
        return urlunparse((pr.scheme, pr.netloc, pr.path, pr.params, new_q, pr.fragment))
    except Exception:
        return url


def execute_download_mese(mese_nome: str, anno: int, user: str, pwd: str, is_13ma: bool):
    """
    Scarica busta paga, cartellino e legge agenda.
    """
    results = {"busta_path": None, "cart_path": None, "agenda": None, "debug": []}

    mese_nome = normalize_month_name(mese_nome)
    mese_num = month_to_num(mese_nome)
    if mese_num <= 0:
        results["debug"].append("Mese non valido")
        return results

    suffix = "13" if is_13ma else ""
    local_busta = build_file_path("busta", mese_num, anno, suffix=suffix)
    local_cart = build_file_path("cartellino", mese_num, anno, suffix=suffix)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context(accept_downloads=True, user_agent="Mozilla/5.0 Chrome/120.0.0.0")
        ctx.set_default_timeout(45000)
        page = ctx.new_page()
        page.set_viewport_size({"width": 1920, "height": 1080})

        try:
            st.toast("Login...", icon="🔐")
            page.goto("https://selfservice.gottardospa.it/jsrev/JSipert2?ry", wait_until="domcontentloaded")
            page.wait_for_selector("input[type=text]", timeout=20000)
            page.fill("input[type=text]", user)
            page.fill("input[type=password]", pwd)
            page.press("input[type=password]", "Enter")
            time.sleep(3)

            # Verifica login (euristica)
            try:
                page.wait_for_selector("text=I miei dati", timeout=15000)
            except Exception:
                # seconda euristica: header/menù
                pass

            # --- AGENDA (prima, così almeno abbiamo qualcosa anche se i download falliscono)
            st.toast("Lettura Agenda...", icon="🗓️")
            agenda = read_agenda_api(ctx, mese_num, anno)
            if not agenda.get("success"):
                agenda = read_agenda_with_navigation(page, ctx, mese_num, anno)
            results["agenda"] = agenda

            # --- DOWNLOAD BUSTA
            st.toast("Download Busta...", icon="📄")
            # Qui devi adattare il selettore/link al tuo portale (dipende dalla UI).
            # Strategia: cerca un link con testo "Cedolino" o "Busta" e scarica.
            # Se non trova, lascia vuoto e l'utente potrà caricare manualmente.
            try:
                with page.expect_download(timeout=20000) as dl_info:
                    # click euristico
                    candidates = [
                        "text=Cedolino",
                        "text=Busta",
                        "text=Payslip",
                        "text=Stampa",
                        "text=PDF",
                    ]
                    clicked = False
                    for sel in candidates:
                        try:
                            loc = page.locator(sel).first
                            if loc.is_visible(timeout=1500):
                                loc.click(force=True)
                                clicked = True
                                break
                        except Exception:
                            continue
                    if not clicked:
                        raise RuntimeError("Link download busta non trovato (euristica).")
                dl = dl_info.value
                dl.save_as(local_busta)
                results["busta_path"] = local_busta
            except Exception as e:
                results["debug"].append(f"Busta download: {type(e).__name__}")

            # --- DOWNLOAD CARTELLINO
            if not is_13ma:
                st.toast("Download Cartellino...", icon="🧾")
                try:
                    with page.expect_download(timeout=20000) as dl_info:
                        candidates = [
                            "text=Cartellino",
                            "text=Presenze",
                            "text=Timesheet",
                            "text=PDF",
                        ]
                        clicked = False
                        for sel in candidates:
                            try:
                                loc = page.locator(sel).first
                                if loc.is_visible(timeout=1500):
                                    loc.click(force=True)
                                    clicked = True
                                    break
                            except Exception:
                                continue
                        if not clicked:
                            raise RuntimeError("Link download cartellino non trovato (euristica).")
                    dl = dl_info.value
                    dl.save_as(local_cart)
                    results["cart_path"] = local_cart
                except Exception as e:
                    results["debug"].append(f"Cartellino download: {type(e).__name__}")

        finally:
            try:
                browser.close()
            except Exception:
                pass

    return results


# ==============================================================================
# CROSS-CHECK ENGINE
# ==============================================================================
def compute_hours_per_day(cart: dict, default_ore_giorno: float):
    giorni = to_float(cart.get("giorni_lavorati", 0), 0)
    ore = to_float(cart.get("ore_lavorate", 0), 0)
    if giorni > 0 and ore > 0:
        # media mese: utile per convertire ore assenze (busta) in "equivalente giorni"
        return clamp(ore / giorni, 4.0, 9.0)
    return float(default_ore_giorno)


def compute_busta_absence_days(busta: dict, ore_giorno: float):
    ass = safe_get(busta, "assenze_mese", default={}) or {}
    ore_ferie = to_float(ass.get("ore_ferie", 0), 0)
    ore_permessi = to_float(ass.get("ore_permessi", 0), 0)
    ore_malattia = to_float(ass.get("ore_malattia", 0), 0)

    ore_assenze_retribuite = ore_ferie + ore_permessi
    gg_assenze_eq = (ore_assenze_retribuite / ore_giorno) if ore_giorno > 0 else 0.0
    gg_mal_eq = (ore_malattia / ore_giorno) if ore_giorno > 0 else 0.0

    return {
        "ore_giorno": ore_giorno,
        "ore_ferie": ore_ferie,
        "ore_permessi": ore_permessi,
        "ore_assenze_retribuite": ore_assenze_retribuite,
        "ore_malattia": ore_malattia,
        "gg_assenze_equivalenti": gg_assenze_eq,     # float
        "gg_malattia_equivalenti": gg_mal_eq,        # float
        "gg_assenze_round": int(round(gg_assenze_eq)),  # int per check macro
        "gg_malattia_round": int(round(gg_mal_eq)),     # int per check macro
    }


def cross_check_all(busta: dict, cart: dict, agenda: dict, default_ore_giorno: float):
    """
    Ritorna un report con:
    - check periodi
    - check cartellino interno (footer vs righe)
    - check GG INPS vs totalizzazioni stimate
    - check omesse (solo agenda, come presenza)
    - check coerenza eventi agenda vs ore busta (soft)
    """
    report = {
        "status": "INFO",
        "messages": [],
        "metrics": {},
        "warnings": [],
        "errors": [],
    }

    dg = safe_get(busta, "dati_generali", default={}) or {}
    gg_inps = to_int(dg.get("giorni_pagati", 0), 0)

    # Cartellino
    c_lav = to_float(cart.get("giorni_lavorati", 0), 0)
    c_footer = to_float(cart.get("giorni_footer", 0), 0)
    c_righe = to_float(cart.get("giorni_righe", 0), 0)
    c_ore = to_float(cart.get("ore_lavorate", 0), 0)
    c_fest = to_int(cart.get("festivita", 0), 0)

    # Agenda (conteggi per tipo)
    aev = (agenda or {}).get("events_by_type", {}) if isinstance(agenda, dict) else {}
    a_omesse = to_int(aev.get("OMESSA TIMBRATURA", 0), 0)  # SOLO AGENDA
    a_ferie = to_int(aev.get("FERIE", 0), 0)
    a_mal = to_int(aev.get("MALATTIA", 0), 0)
    a_rip = to_int(aev.get("RIPOSO", 0), 0)

    # Ore/giorno per conversione
    ore_giorno = compute_hours_per_day(cart, default_ore_giorno)
    b_abs = compute_busta_absence_days(busta, ore_giorno)

    # GG stimati (macro): presenza cartellino + festività + assenze (equivalenti) + malattia (equivalente)
    # NOTA: le omesse NON aggiungono giorni: sono "dentro" ai giorni lavorati (presenza).
    gg_calcolati = c_lav + c_fest + b_abs["gg_assenze_round"] + b_abs["gg_malattia_round"]
    diff = gg_calcolati - gg_inps if gg_inps > 0 else None

    # Metriche
    report["metrics"] = {
        "gg_inps": gg_inps,
        "gg_calcolati_stima": gg_calcolati,
        "diff_gg": diff,
        "c_giorni_lavorati": c_lav,
        "c_ore_lavorate": c_ore,
        "c_festivita": c_fest,
        "ore_giorno_usate": ore_giorno,
        "b_ore_ferie": b_abs["ore_ferie"],
        "b_ore_permessi": b_abs["ore_permessi"],
        "b_ore_malattia": b_abs["ore_malattia"],
        "b_gg_assenze_eq": b_abs["gg_assenze_equivalenti"],
        "b_gg_mal_eq": b_abs["gg_malattia_equivalenti"],
        "agenda_omesse": a_omesse,
        "agenda_ferie": a_ferie,
        "agenda_malattia": a_mal,
        "agenda_riposi": a_rip,
    }

    # 1) Check cartellino interno
    if c_footer > 0 and c_righe > 0 and abs(c_footer - c_righe) >= 1.0:
        report["warnings"].append(
            f"Cartellino: footer GG presenza={c_footer} ma conteggio righe={c_righe}. Possibile parsing AI impreciso."
        )

    if c_lav <= 0 and c_ore > 0:
        report["warnings"].append("Cartellino: ore lavorate > 0 ma giorni lavorati = 0 (parsing incompleto).")

    # 2) Omesse: solo agenda, trattate come presenza (subset)
    if a_omesse > 0:
        # Coerenza minima: non può superare i lavorati del cartellino in modo macroscopico
        if c_lav > 0 and a_omesse > (c_lav + 2):
            report["warnings"].append(
                f"Omesse agenda={a_omesse} >> giorni lavorati cartellino={c_lav}: verifica mese selezionato o parsing."
            )
        report["messages"].append(
            f"Omesse timbrature (solo agenda): {a_omesse} (considerate giorni lavorati/anomalia, non assenza)."
        )

    # 3) Check GG INPS (macro)
    if gg_inps <= 0:
        report["status"] = "INFO"
        report["messages"].append(
            "Busta: GG INPS non disponibili o non letti; il controllo giorni è solo indicativo."
        )
    else:
        if diff == 0:
            report["status"] = "OK"
            report["messages"].append(
                f"GG INPS coerenti: busta={gg_inps} vs calcolato(stima)={int(gg_calcolati)}."
            )
        elif abs(diff) == 1:
            report["status"] = "WARN"
            report["messages"].append(
                f"Quasi coerente (±1): busta GG INPS={gg_inps} vs calcolato(stima)={int(gg_calcolati)} (diff={diff:+.0f})."
            )
        else:
            report["status"] = "ERROR"
            report["errors"].append(
                f"Discrepanza giorni: busta GG INPS={gg_inps} vs calcolato(stima)={int(gg_calcolati)} (diff={diff:+.0f})."
            )
            report["messages"].append(
                "Nota: le assenze da busta sono convertite da ORE a giorni equivalenti usando la media ore/giorno del cartellino; "
                "se ci sono permessi convertiti in ferie o giornate miste, la stima può differire."
            )

    # 4) Coerenza soft agenda vs busta (non bloccante)
    # Malattia: se agenda segnala molte malattie ma busta ore_malattia=0 -> warning soft
    if a_mal > 0 and b_abs["ore_malattia"] == 0:
        report["warnings"].append(
            f"Agenda indica MALATTIA={a_mal} ma busta ore malattia=0: verifica mese o codifica evento."
        )

    # Ferie: agenda ferie vs busta ore ferie/permessi
    if a_ferie > 0 and (b_abs["ore_ferie"] + b_abs["ore_permessi"]) == 0:
        report["warnings"].append(
            f"Agenda indica FERIE={a_ferie} ma busta non riporta ore ferie/permessi nel mese (o parsing non le ha lette)."
        )

    # Riposi: tipicamente non contano come GG INPS, lasciamo solo info
    if a_rip > 0:
        report["messages"].append(f"Riposi agenda: {a_rip} (non conteggiati come GG INPS).")

    return report


# ==============================================================================
# UI
# ==============================================================================
st.title("Gottardo Payroll Analyzer (fixed)")

with st.sidebar:
    st.header("Credenziali / Periodo")
    user = st.text_input("Username", value="", type="default")
    pwd = st.text_input("Password", value="", type="password")
    mese_nome = st.selectbox("Mese", MESI_IT, index=7)  # Agosto default
    anno = st.number_input("Anno", min_value=2020, max_value=2035, value=2025, step=1)
    is_13ma = st.checkbox("Cedolino 13ma", value=False)

    st.divider()
    st.header("Conversioni / Regole")
    default_ore_giorno = st.number_input("Ore standard per giorno (fallback)", min_value=4.0, max_value=9.0, value=7.0, step=0.25)
    st.caption("La conversione ore→giorni usa la media ore/giorno del cartellino se disponibile; altrimenti questo fallback.")
    st.divider()

    run_btn = st.button("Scarica + Analizza", type="primary")

st.caption("Se i download non funzionano (selettori portale), puoi caricare manualmente i PDF sotto e usare comunque analisi/cross-check.")

# Upload manuale (sempre disponibile)
col_up1, col_up2 = st.columns(2)
with col_up1:
    up_busta = st.file_uploader("Carica Busta (PDF)", type=["pdf"], accept_multiple_files=False)
with col_up2:
    up_cart = st.file_uploader("Carica Cartellino (PDF)", type=["pdf"], accept_multiple_files=False)

if "session" not in st.session_state:
    st.session_state.session = {}

def _save_uploaded(uploaded, path):
    if not uploaded:
        return None
    data = uploaded.read()
    with open(path, "wb") as f:
        f.write(data)
    return path

# Esecuzione
if run_btn:
    if not user or not pwd:
        st.error("Inserisci username e password.")
    else:
        with st.spinner("Esecuzione download + agenda..."):
            dl = execute_download_mese(mese_nome, int(anno), user, pwd, bool(is_13ma))
            st.session_state.session["download"] = dl
            st.session_state.session["mese"] = mese_nome
            st.session_state.session["anno"] = int(anno)
            st.session_state.session["is_13ma"] = bool(is_13ma)

# Se upload manuale, sovrascrive i path
mese_num = month_to_num(mese_nome)
suffix = "13" if is_13ma else ""
manual_busta_path = build_file_path("busta_manual", mese_num, int(anno), suffix=suffix)
manual_cart_path = build_file_path("cartellino_manual", mese_num, int(anno), suffix=suffix)

if up_busta:
    st.session_state.session["manual_busta_path"] = _save_uploaded(up_busta, manual_busta_path)
if up_cart:
    st.session_state.session["manual_cart_path"] = _save_uploaded(up_cart, manual_cart_path)

# Determina path finali
dl = st.session_state.session.get("download", {}) if isinstance(st.session_state.session.get("download"), dict) else {}
busta_path = st.session_state.session.get("manual_busta_path") or dl.get("busta_path")
cart_path = st.session_state.session.get("manual_cart_path") or dl.get("cart_path")
agenda_data = dl.get("agenda") or {"events_by_type": {}, "total_events": 0, "items": [], "debug": [], "success": False}

tab1, tab2, tab3 = st.tabs(["Risultato", "Dettagli documenti", "Debug"])

if busta_path:
    with st.spinner("Parsing Busta..."):
        busta = parse_busta_dettagliata(busta_path)
else:
    busta = {
        "e_tredicesima": False,
        "dati_generali": {"netto": 0, "giorni_pagati": 0, "ore_ordinarie": 0},
        "competenze": {"base": 0, "anzianita": 0, "straordinari": 0, "festivita": 0, "lordo_totale": 0},
        "trattenute": {"inps": 0, "irpef_netta": 0, "addizionali": 0},
        "ferie": {"residue_ap": 0, "maturate": 0, "godute": 0, "saldo": 0},
        "par": {"residue_ap": 0, "spettanti": 0, "fruite": 0, "saldo": 0},
        "assenze_mese": {"ore_ferie": 0, "ore_permessi": 0, "ore_malattia": 0},
    }

if cart_path and not is_13ma:
    with st.spinner("Parsing Cartellino..."):
        cart = parse_cartellino_dettagliato(cart_path)
else:
    cart = {
        "giorni_lavorati": 0,
        "giorni_footer": 0,
        "giorni_righe": 0,
        "ore_lavorate": 0,
        "ferie": 0,
        "malattia": 0,
        "permessi": 0,
        "riposi": 0,
        "festivita": 0,
        "note": "Cartellino non disponibile (13ma o mancante).",
    }

report = cross_check_all(busta, cart, agenda_data, float(default_ore_giorno))

with tab1:
    status = report["status"]
    if status == "OK":
        st.success("✅ Controlli principali: OK")
    elif status == "WARN":
        st.warning("⚠️ Controlli principali: Attenzione (stima/±1 o mismatch soft)")
    elif status == "ERROR":
        st.error("❌ Controlli principali: Discrepanza")
    else:
        st.info("ℹ️ Controlli principali: Informazioni")

    m = report["metrics"] or {}
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("GG INPS (Busta)", m.get("gg_inps", 0))
    c2.metric("GG calcolati (stima)", f"{m.get('gg_calcolati_stima', 0):.0f}", delta=(f"{m.get('diff_gg'):+.0f}" if m.get("diff_gg") is not None else None))
    c3.metric("Lavorati (Cartellino)", f"{m.get('c_giorni_lavorati', 0):.0f}", help=f"Ore lavorate: {m.get('c_ore_lavorate', 0):.2f}")
    c4.metric("Omesse (Agenda)", m.get("agenda_omesse", 0), help="Solo agenda. Considerate giorni lavorati/anomalia, non assenza.")

    st.divider()

    for msg in report["messages"]:
        st.write(f"- {msg}")

    if report["warnings"]:
        st.subheader("Warning")
        for w in report["warnings"]:
            st.warning(w)

    if report["errors"]:
        st.subheader("Errori")
        for e in report["errors"]:
            st.error(e)

with tab2:
    st.subheader("Busta (estratto)")
    dg = safe_get(busta, "dati_generali", default={}) or {}
    comp = safe_get(busta, "competenze", default={}) or {}
    tratt = safe_get(busta, "trattenute", default={}) or {}
    ass = safe_get(busta, "assenze_mese", default={}) or {}

    cA, cB, cC, cD = st.columns(4)
    cA.metric("Netto", f"{to_float(dg.get('netto', 0), 0):.2f}")
    cB.metric("GG INPS", to_int(dg.get("giorni_pagati", 0), 0))
    cC.metric("Lordo (tot)", f"{to_float(comp.get('lordo_totale', 0), 0):.2f}")
    cD.metric("INPS", f"{to_float(tratt.get('inps', 0), 0):.2f}")

    st.caption(
        f"Assenze mese (ore): ferie={to_float(ass.get('ore_ferie', 0), 0):.2f}h, "
        f"permessi={to_float(ass.get('ore_permessi', 0), 0):.2f}h, "
        f"malattia={to_float(ass.get('ore_malattia', 0), 0):.2f}h"
    )

    st.divider()
    st.subheader("Cartellino (estratto)")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("GG Presenza (usati)", f"{to_float(cart.get('giorni_lavorati', 0), 0):.0f}")
    c2.metric("Ore lavorate", f"{to_float(cart.get('ore_lavorate', 0), 0):.2f}")
    c3.metric("Festività", to_int(cart.get("festivita", 0), 0))
    c4.metric("Riposi", to_int(cart.get("riposi", 0), 0))

    if cart.get("note"):
        st.info(cart.get("note"))

    st.divider()
    st.subheader("Agenda (conteggi)")
    aev = agenda_data.get("events_by_type", {}) if isinstance(agenda_data, dict) else {}
    st.json(aev)

with tab3:
    st.subheader("Debug download/agenda")
    if isinstance(dl, dict) and dl.get("debug"):
        st.code("\n".join(dl.get("debug")), language="text")
    if isinstance(agenda_data, dict) and agenda_data.get("debug"):
        with st.expander("Debug agenda"):
            st.code("\n".join(agenda_data.get("debug")), language="text")
    if isinstance(agenda_data, dict) and agenda_data.get("items"):
        with st.expander("Esempi eventi agenda (troncati)"):
            st.write("\n".join(agenda_data.get("items")[:100]))
