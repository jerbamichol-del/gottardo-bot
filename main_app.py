# ==============================================================================
# GOTTARDO PAYROLL ANALYZER v2.0
# ==============================================================================
# Versione ottimizzata con:
# - Report di coerenza con sistema a semafori
# - Confronto incrociato automatico (busta + cartellino + agenda)
# - Glossario voci busta paga
# - UI pulita senza log di debug visibili
# ==============================================================================

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
from datetime import datetime

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


# ==============================================================================
# Costanti
# ==============================================================================
TIMEOUT_API = 30000  # Coerente con context.set_default_timeout(45000)
TIMEOUT_DEFAULT = 45000

CALENDAR_CODES = {
    "FEP": "Ferie Pianificate",
    "OMT": "Omessa Timbratura",
    "RCS": "Riposo Compensativo",
    "RIC": "Riposo Compensativo Forzato",
    "MAL": "Malattia"
}

MESI_IT = [
    "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"
]

# Glossario voci busta paga
GLOSSARIO_BUSTA = {
    "Retribuzione Base": "Il tuo stipendio mensile lordo fisso, stabilito dal contratto.",
    "Contingenza": "Indennità storica per adeguamento al costo della vita (congelata).",
    "EDR": "Elemento Distinto della Retribuzione - importo fisso aggiuntivo.",
    "Scatti Anzianità": "Aumenti periodici automatici basati sugli anni di servizio.",
    "Straordinari": "Compenso per ore lavorate oltre l'orario contrattuale.",
    "Maggiorazione Festiva": "Compenso extra per lavoro in giorni festivi.",
    "Contributi INPS": "Trattenute previdenziali (circa 9.19% del lordo).",
    "IRPEF": "Imposta sul Reddito delle Persone Fisiche - tassazione progressiva.",
    "Addizionale Regionale": "Imposta aggiuntiva IRPEF dovuta alla Regione.",
    "Addizionale Comunale": "Imposta aggiuntiva IRPEF dovuta al Comune.",
    "TFR": "Trattamento di Fine Rapporto - accantonamento per liquidazione.",
    "Ferie Residue AP": "Giorni di ferie dell'anno precedente non ancora goduti.",
    "Ferie Maturate": "Giorni di ferie maturati nell'anno corrente.",
    "Ferie Godute": "Giorni di ferie effettivamente utilizzati.",
    "PAR": "Permessi Annui Retribuiti - ore/giorni di permesso spettanti.",
}


def get_credentials():
    if "credentials_set" in st.session_state and st.session_state.get("credentials_set"):
        return st.session_state.get("username"), st.session_state.get("password")
    try:
        return st.secrets["ZK_USER"], st.secrets["ZK_PASS"]
    except Exception:
        return None, None


# --- API Keys ---
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
# Gemini Model Setup
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


# ==============================================================================
# AGENDA - API Method
# ==============================================================================
def agenda_read_via_api(context, mese_num, anno, debug_log):
    """Legge l'agenda tramite chiamate API dirette."""
    debug_log.append("📡 Lettura agenda via API...")
    
    api_responses = {"events": {}, "balances": None}
    base_url = "https://selfservice.gottardospa.it/js_rev/JSipert2"
    
    for code, name in CALENDAR_CODES.items():
        try:
            url = f"{base_url}/api/time/v2/events?$filter_api=calendarCode={code},startTime={anno}-01-01T00:00:00,endTime={anno}-12-31T00:00:00"
            resp = context.request.get(url, timeout=TIMEOUT_API)
            
            if resp.ok:
                try:
                    data = resp.json()
                    if data:
                        api_responses["events"][code] = data
                        debug_log.append(f"  ✅ {code}: {len(data) if isinstance(data, list) else 1}")
                except Exception:
                    pass
        except Exception as e:
            debug_log.append(f"  ⚠️ {code}: {type(e).__name__}")
    
    # Saldo ferie/permessi
    try:
        url = f"{base_url}/api/time/v2/timeoffbalances?$filter_api=year={anno}"
        resp = context.request.get(url, timeout=TIMEOUT_API)
        if resp.ok:
            api_responses["balances"] = resp.json()
    except Exception:
        pass
    
    return _process_api_responses(api_responses, mese_num, anno, debug_log)


def _process_api_responses(api_responses, mese_num, anno, debug_log):
    """Elabora le risposte API."""
    result = {
        "events_this_month": [],
        "events_by_type": {},
        "total_events": 0,
        "absences_count": 0,
        "balances": api_responses.get("balances")
    }
    
    for code, events in api_responses.get("events", {}).items():
        if not isinstance(events, list):
            events = [events] if events else []
        
        code_name = CALENDAR_CODES.get(code, code)
        events_this_month = []
        
        for event in events:
            try:
                start_time = event.get("startTime", "") or event.get("start", "") or ""
                if start_time and len(start_time) >= 7:
                    try:
                        event_month = int(start_time[5:7])
                        if event_month != mese_num:
                            continue
                    except ValueError:
                        pass
                
                summary = event.get("summary", "") or event.get("description", "") or code_name
                events_this_month.append({
                    "type": code,
                    "type_name": code_name,
                    "summary": summary,
                    "date": start_time[:10] if start_time else "N/D"
                })
            except Exception:
                continue
        
        if events_this_month:
            result["events_by_type"][code_name] = len(events_this_month)
            result["events_this_month"].extend(events_this_month)
    
    result["total_events"] = len(result["events_this_month"])
    
    # Conta assenze (escludendo anomalie come OMT che non sono assenze vere)
    assenze_codes = ["FEP", "RCS", "RIC", "MAL"]
    result["absences_count"] = sum(
        result["events_by_type"].get(CALENDAR_CODES.get(c, c), 0) 
        for c in assenze_codes
    )
    
    debug_log.append(f"  📊 Eventi mese: {result['total_events']}, Assenze: {result['absences_count']}")
    return result


# ==============================================================================
# AI Parsing
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
            chunks = [p.get_text() for p in doc]
            txt = "\n".join(chunks).strip()
            return txt if txt else None
        except Exception:
            pass

    if PdfReader is not None:
        try:
            reader = PdfReader(file_path)
            chunks = [(page.extract_text() or "") for page in reader.pages]
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
        return None

    last_err = None

    for idx, (nome, model) in enumerate(MODELLI_DISPONIBILI, 1):
        try:
            resp = model.generate_content([prompt, {"mime_type": "application/pdf", "data": bytes_data}])
            out = clean_json_response(getattr(resp, "text", ""))
            if out and isinstance(out, dict):
                return out
        except Exception as e:
            last_err = e
            continue

    # Fallback DeepSeek
    if DEEPSEEK_API_KEY and OpenAI is not None:
        try:
            txt = extract_text_from_pdf_any(file_path)
            if txt and len(txt.strip()) >= 50:
                client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
                full_prompt = prompt + "\n\n--- TESTO ESTRATTO DAL PDF ---\n" + txt[:25000]
                r = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[
                        {"role": "system", "content": "Rispondi solo con JSON valido."},
                        {"role": "user", "content": full_prompt},
                    ],
                    temperature=0.1,
                )
                out = clean_json_response(r.choices[0].message.content)
                if out and isinstance(out, dict):
                    return out
        except Exception:
            pass

    return None


def estrai_dati_busta_dettagliata(file_path):
    prompt = """
Questo è un CEDOLINO PAGA GOTTARDO S.p.A. italiano. Estrai:

**1. DATI GENERALI:**
- NETTO: riga "PROGRESSIVI" colonna finale
- GIORNI PAGATI: "GG. INPS"
- ORE ORDINARIE: "ORE INAIL" oppure giorni_pagati × 8

**2. COMPETENZE:**
- base: "RETRIBUZIONE ORDINARIA" (voce 1000)
- straordinari: somma "STRAORDINARIO"/"SUPPLEMENTARI"/"NOTTURNI"
- festivita: "MAGG. FESTIVE"/"FESTIVITA GODUTA"
- anzianita: "SCATTI"/"EDR"/"ANZ."
- lordo_totale: "TOTALE COMPETENZE"

**3. TRATTENUTE:**
- inps: sezione I.N.P.S.
- irpef_netta: sezione FISCALI
- addizionali_totali: add.reg + add.com

**4. FERIE/PAR:**
- tabella ferie (RES.PREC / SPETTANTI / FRUITE / SALDO)

**5.** e_tredicesima: true se "TREDICESIMA"/"13MA"

Valori mancanti = 0. Decimale = punto. Solo JSON:

{
  "e_tredicesima": false,
  "dati_generali": {"netto": 0.0, "giorni_pagati": 0, "ore_ordinarie": 0.0},
  "competenze": {"base": 0.0, "anzianita": 0.0, "straordinari": 0.0, "festivita": 0.0, "lordo_totale": 0.0},
  "trattenute": {"inps": 0.0, "irpef_netta": 0.0, "addizionali_totali": 0.0},
  "ferie": {"residue_ap": 0.0, "maturate": 0.0, "godute": 0.0, "saldo": 0.0},
  "par": {"residue_ap": 0.0, "spettanti": 0.0, "fruite": 0.0, "saldo": 0.0}
}
""".strip()
    return estrai_con_fallback(file_path, prompt, tipo="busta paga")


def estrai_dati_cartellino(file_path):
    prompt = r"""
Analizza il cartellino presenze GOTTARDO S.p.A.
Conta i giorni con almeno una timbratura (E/U).
Se vuoto: giorni_reali=0.

Solo JSON:
{
  "giorni_reali": 0,
  "giorni_senza_badge": 0,
  "note": ""
}
""".strip()
    return estrai_con_fallback(file_path, prompt, tipo="cartellino")


# ==============================================================================
# Analisi Coerenza
# ==============================================================================
def analizza_coerenza(dati_busta, dati_cartellino, dati_agenda, mese_num, anno):
    """
    Analizza la coerenza tra i tre documenti e genera un report.
    
    Returns:
        dict con:
        - status: "ok" | "warning" | "error"
        - issues: lista di problemi trovati
        - summary: riepilogo testuale
        - details: dettagli per ogni area
    """
    result = {
        "status": "ok",
        "issues": [],
        "warnings": [],
        "summary": "",
        "details": {}
    }
    
    # Estrai dati
    giorni_pagati = 0
    giorni_lavorati = 0
    giorni_assenza_agenda = 0
    
    if dati_busta:
        giorni_pagati = int(dati_busta.get("dati_generali", {}).get("giorni_pagati", 0))
        result["details"]["busta"] = {
            "netto": dati_busta.get("dati_generali", {}).get("netto", 0),
            "giorni_pagati": giorni_pagati,
            "lordo": dati_busta.get("competenze", {}).get("lordo_totale", 0)
        }
    
    if dati_cartellino:
        giorni_lavorati = int(dati_cartellino.get("giorni_reali", 0))
        result["details"]["cartellino"] = {
            "giorni_lavorati": giorni_lavorati,
            "anomalie_badge": dati_cartellino.get("giorni_senza_badge", 0)
        }
    
    if dati_agenda:
        giorni_assenza_agenda = dati_agenda.get("absences_count", 0)
        result["details"]["agenda"] = {
            "eventi_totali": dati_agenda.get("total_events", 0),
            "assenze": giorni_assenza_agenda,
            "per_tipo": dati_agenda.get("events_by_type", {})
        }
    
    # Calcola giorni teorici del mese (escludendo weekend)
    giorni_teorici = 0
    try:
        _, ultimo_giorno = calendar.monthrange(anno, mese_num)
        for giorno in range(1, ultimo_giorno + 1):
            data = datetime(anno, mese_num, giorno)
            if data.weekday() < 5:  # Lun-Ven
                giorni_teorici += 1
    except Exception:
        giorni_teorici = 22  # fallback
    
    result["details"]["teorici"] = giorni_teorici
    
    # === ANALISI COERENZA ===
    
    # 1. Confronto Giorni Pagati vs Lavorati
    if dati_busta and dati_cartellino and giorni_lavorati > 0:
        diff = giorni_lavorati - giorni_pagati
        
        if abs(diff) <= 1:
            # OK - tolleranza di 1 giorno
            pass
        elif diff > 0:
            result["warnings"].append(
                f"Hai lavorato {diff} giorni in più di quelli pagati ({giorni_lavorati} vs {giorni_pagati})"
            )
        else:
            result["issues"].append(
                f"Pagati {abs(diff)} giorni in più di quelli lavorati ({giorni_pagati} vs {giorni_lavorati})"
            )
    
    # 2. Confronto con Agenda
    if dati_agenda and giorni_assenza_agenda > 0:
        giorni_attesi = giorni_teorici - giorni_assenza_agenda
        
        if dati_cartellino and giorni_lavorati > 0:
            if abs(giorni_lavorati - giorni_attesi) > 2:
                result["warnings"].append(
                    f"Giorni lavorati ({giorni_lavorati}) diversi da attesi ({giorni_attesi} = {giorni_teorici} teorici - {giorni_assenza_agenda} assenze)"
                )
    
    # 3. Anomalie badge
    if dati_cartellino:
        anomalie = dati_cartellino.get("giorni_senza_badge", 0)
        if anomalie > 0:
            result["warnings"].append(
                f"{anomalie} giorno/i senza timbratura badge"
            )
    
    # 4. Omessa timbratura in agenda
    if dati_agenda:
        omt_count = dati_agenda.get("events_by_type", {}).get("Omessa Timbratura", 0)
        if omt_count > 0:
            result["warnings"].append(
                f"{omt_count} evento/i di 'Omessa Timbratura' segnalati"
            )
    
    # Determina status finale
    if result["issues"]:
        result["status"] = "error"
    elif result["warnings"]:
        result["status"] = "warning"
    else:
        result["status"] = "ok"
    
    # Genera summary
    if result["status"] == "ok":
        result["summary"] = "✅ Tutto in ordine! I dati di busta paga, cartellino e agenda sono coerenti."
    elif result["status"] == "warning":
        result["summary"] = f"⚠️ Attenzione: {len(result['warnings'])} aspetto/i da verificare."
    else:
        result["summary"] = f"❌ Rilevate {len(result['issues'])} incongruenze significative."
    
    return result


# ==============================================================================
# Navigation Helpers
# ==============================================================================
def open_documenti(page, debug_log):
    try:
        page.keyboard.press("Escape")
        time.sleep(0.2)
    except Exception:
        pass

    try:
        page.evaluate("document.getElementById('revit_navigation_NavHoverItem_0_label')?.click()")
        time.sleep(1.0)
    except Exception:
        pass

    try:
        page.wait_for_selector("span[id^='lnktab_']", timeout=15000)
    except Exception:
        pass

    for js_id in ["lnktab_2_label", "lnktab_2"]:
        try:
            page.evaluate(f"document.getElementById('{js_id}')?.click()")
            time.sleep(1.0)
            break
        except Exception:
            continue

    try:
        page.locator("span", has_text=re.compile(r"\bDocumenti\b", re.I)).first.click(force=True, timeout=8000)
        time.sleep(1.0)
    except Exception:
        pass

    try:
        page.wait_for_selector("text=Cedolino", timeout=15000)
        return True
    except Exception:
        return False


def _ensure_query(url: str, key: str, value: str) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q[key] = value
    new_q = urlencode(q, doseq=True)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, new_q, p.fragment))


# ==============================================================================
# Core Bot (Semplificato)
# ==============================================================================
def esegui_analisi(mese_nome, anno, username, password, progress_callback=None):
    """
    Esegue l'analisi completa: scarica documenti, estrae dati, analizza coerenza.
    
    Returns:
        dict con: busta, cartellino, agenda, coerenza, debug_log
    """
    try:
        mese_num = MESI_IT.index(mese_nome) + 1
    except Exception:
        return {"error": "Mese non valido"}

    last_day = calendar.monthrange(anno, mese_num)[1]
    d_from = f"01/{mese_num:02d}/{anno}"
    d_to = f"{last_day}/{mese_num:02d}/{anno}"

    work_dir = Path.cwd()
    path_busta = str(work_dir / f"busta_{mese_num}_{anno}.pdf")
    path_cart = str(work_dir / f"cartellino_{mese_num}_{anno}.pdf")

    result = {
        "busta": None,
        "cartellino": None,
        "agenda": None,
        "coerenza": None,
        "debug_log": []
    }
    debug_log = result["debug_log"]

    def log(msg):
        debug_log.append(msg)
        if progress_callback:
            progress_callback(msg)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                slow_mo=300,
                args=["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                accept_downloads=True,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36",
            )
            context.set_default_timeout(TIMEOUT_DEFAULT)
            page = context.new_page()
            page.set_viewport_size({"width": 1920, "height": 1080})

            # LOGIN
            log("🔐 Login in corso...")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y", wait_until="domcontentloaded")
            page.wait_for_selector('input[type="text"]', timeout=10000)
            page.fill('input[type="text"]', username)
            page.fill('input[type="password"]', password)
            page.press('input[type="password"]', "Enter")
            time.sleep(3)

            try:
                page.wait_for_selector("text=I miei dati", timeout=15000)
                log("✅ Login riuscito")
            except Exception:
                browser.close()
                return {"error": "LOGIN_FALLITO"}

            # AGENDA
            log("🗓️ Lettura agenda...")
            time.sleep(2)
            try:
                result["agenda"] = agenda_read_via_api(context, mese_num, anno, debug_log)
                log(f"✅ Agenda: {result['agenda'].get('total_events', 0)} eventi")
            except Exception as e:
                log(f"⚠️ Agenda non disponibile: {type(e).__name__}")

            # BUSTA PAGA
            log("💰 Scaricamento cedolino...")
            busta_ok = False
            try:
                if open_documenti(page, debug_log):
                    time.sleep(1.5)
                    
                    try:
                        page.locator("tr", has=page.locator("text=Cedolino")).locator(".z-image").click(timeout=8000)
                    except Exception:
                        page.locator("text=Cedolino").first.click(force=True, timeout=8000)
                    
                    time.sleep(4)
                    
                    target = f"{mese_nome} {anno}"
                    all_links = page.locator("a")
                    
                    for i in range(all_links.count()):
                        try:
                            txt = (all_links.nth(i).inner_text() or "").strip()
                            if target.lower() in txt.lower() and "tredicesima" not in txt.lower():
                                with page.expect_download(timeout=20000) as dl:
                                    all_links.nth(i).click()
                                dl.value.save_as(path_busta)
                                if os.path.exists(path_busta):
                                    busta_ok = True
                                    log("✅ Cedolino scaricato")
                                break
                        except Exception:
                            continue
            except Exception as e:
                log(f"⚠️ Errore cedolino: {type(e).__name__}")

            # CARTELLINO
            log("📅 Scaricamento cartellino...")
            cart_ok = False
            try:
                # Torna alla home
                page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2", wait_until="domcontentloaded")
                time.sleep(2)
                
                # Vai a Time > Cartellino
                page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
                time.sleep(3)
                page.evaluate("document.getElementById('lnktab_5_label')?.click()")
                time.sleep(4)
                
                # Imposta date
                dal = page.locator("input[id*='CLRICHIE'][class*='dijitInputInner']").first
                al = page.locator("input[id*='CLRICHI2'][class*='dijitInputInner']").first
                
                if dal.count() > 0 and al.count() > 0:
                    dal.click(force=True)
                    page.keyboard.press("Control+A")
                    dal.fill("")
                    dal.type(d_from, delay=80)
                    dal.press("Tab")
                    time.sleep(0.5)
                    
                    al.click(force=True)
                    page.keyboard.press("Control+A")
                    al.fill("")
                    al.type(d_to, delay=80)
                    al.press("Tab")
                    time.sleep(0.5)
                
                # Esegui ricerca
                page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last.click(force=True)
                time.sleep(6)
                
                # Click su icona visualizza
                icona = page.locator("img[src*='search']").first
                if icona.count() > 0:
                    with context.expect_page(timeout=20000) as popup_info:
                        icona.click()
                    popup = popup_info.value
                    
                    time.sleep(3)
                    popup_url = popup.url.replace("/js_rev//", "/js_rev/")
                    popup_url = _ensure_query(popup_url, "EMBED", "y")
                    
                    resp = context.request.get(popup_url, timeout=TIMEOUT_API)
                    body = resp.body()
                    
                    if body[:4] == b"%PDF":
                        Path(path_cart).write_bytes(body)
                        cart_ok = True
                        log("✅ Cartellino scaricato")
                    
                    try:
                        popup.close()
                    except Exception:
                        pass
                        
            except Exception as e:
                log(f"⚠️ Errore cartellino: {type(e).__name__}")

            browser.close()

    except Exception as e:
        return {"error": f"Errore generale: {e}"}

    # ANALISI AI
    log("🧠 Analisi documenti con AI...")
    
    if busta_ok:
        result["busta"] = estrai_dati_busta_dettagliata(path_busta)
        if result["busta"]:
            log("✅ Busta paga analizzata")
        try:
            os.remove(path_busta)
        except Exception:
            pass
    
    if cart_ok:
        result["cartellino"] = estrai_dati_cartellino(path_cart)
        if result["cartellino"]:
            log("✅ Cartellino analizzato")
        try:
            os.remove(path_cart)
        except Exception:
            pass

    # ANALISI COERENZA
    log("🔍 Verifica coerenza...")
    result["coerenza"] = analizza_coerenza(
        result["busta"],
        result["cartellino"],
        result["agenda"],
        mese_num,
        anno
    )
    log("✅ Analisi completata!")

    return result


# ==============================================================================
# UI - Streamlit
# ==============================================================================
st.set_page_config(
    page_title="Gottardo Payroll Analyzer",
    page_icon="💶",
    layout="wide"
)

# CSS Custom per UI pulita
st.markdown("""
<style>
    .status-ok { 
        background: linear-gradient(135deg, #d4edda 0%, #c3e6cb 100%);
        border-left: 4px solid #28a745;
        padding: 1rem;
        border-radius: 0.5rem;
        margin: 1rem 0;
    }
    .status-warning {
        background: linear-gradient(135deg, #fff3cd 0%, #ffeeba 100%);
        border-left: 4px solid #ffc107;
        padding: 1rem;
        border-radius: 0.5rem;
        margin: 1rem 0;
    }
    .status-error {
        background: linear-gradient(135deg, #f8d7da 0%, #f5c6cb 100%);
        border-left: 4px solid #dc3545;
        padding: 1rem;
        border-radius: 0.5rem;
        margin: 1rem 0;
    }
    .metric-card {
        background: white;
        padding: 1rem;
        border-radius: 0.5rem;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        text-align: center;
    }
    .big-number {
        font-size: 2rem;
        font-weight: bold;
        color: #333;
    }
</style>
""", unsafe_allow_html=True)

st.title("💶 Analisi Stipendio Gottardo")
st.caption("Confronto automatico tra busta paga, cartellino e agenda")

# Sidebar - Credenziali e Parametri
with st.sidebar:
    st.header("🔐 Accesso")
    
    username, password = get_credentials()
    
    if not st.session_state.get("credentials_set"):
        input_user = st.text_input("Username", value=username or "")
        input_pass = st.text_input("Password", type="password")
        
        if st.button("💾 Salva", use_container_width=True):
            if input_user and input_pass:
                st.session_state["username"] = input_user
                st.session_state["password"] = input_pass
                st.session_state["credentials_set"] = True
                st.rerun()
    else:
        st.success(f"✅ {st.session_state['username']}")
        if st.button("🔄 Cambia utente"):
            st.session_state["credentials_set"] = False
            st.rerun()
    
    st.divider()
    
    if st.session_state.get("credentials_set"):
        st.header("📅 Periodo")
        sel_anno = st.selectbox("Anno", [2024, 2025, 2026], index=1)
        sel_mese = st.selectbox("Mese", MESI_IT, index=11)
        
        if st.button("🚀 ANALIZZA", type="primary", use_container_width=True):
            st.session_state.pop("result", None)
            st.session_state["running"] = True
            st.session_state["sel_mese"] = sel_mese
            st.session_state["sel_anno"] = sel_anno

# Main Content
if st.session_state.get("running"):
    with st.spinner("🔄 Analisi in corso... Attendi qualche minuto."):
        progress_placeholder = st.empty()
        
        def update_progress(msg):
            progress_placeholder.info(msg)
        
        result = esegui_analisi(
            st.session_state["sel_mese"],
            st.session_state["sel_anno"],
            st.session_state.get("username"),
            st.session_state.get("password"),
            progress_callback=update_progress
        )
        
        progress_placeholder.empty()
        st.session_state["result"] = result
        st.session_state["running"] = False
        st.rerun()

if "result" in st.session_state:
    result = st.session_state["result"]
    
    if "error" in result:
        st.error(f"❌ {result['error']}")
    else:
        coerenza = result.get("coerenza", {})
        busta = result.get("busta", {})
        cartellino = result.get("cartellino", {})
        agenda = result.get("agenda", {})
        
        # === HEADER STATUS ===
        status = coerenza.get("status", "ok")
        summary = coerenza.get("summary", "")
        
        if status == "ok":
            st.markdown(f'<div class="status-ok"><h3>✅ COERENZA VERIFICATA</h3><p>{summary}</p></div>', unsafe_allow_html=True)
        elif status == "warning":
            st.markdown(f'<div class="status-warning"><h3>⚠️ ATTENZIONE</h3><p>{summary}</p></div>', unsafe_allow_html=True)
        else:
            st.markdown(f'<div class="status-error"><h3>❌ INCONGRUENZE RILEVATE</h3><p>{summary}</p></div>', unsafe_allow_html=True)
        
        # === PROBLEMI/WARNING ===
        issues = coerenza.get("issues", [])
        warnings = coerenza.get("warnings", [])
        
        if issues or warnings:
            with st.expander("🔍 Dettaglio problemi rilevati", expanded=True):
                for issue in issues:
                    st.error(f"❌ {issue}")
                for warning in warnings:
                    st.warning(f"⚠️ {warning}")
        
        # === RIEPILOGO VELOCE ===
        st.subheader("📊 Riepilogo")
        
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            netto = busta.get("dati_generali", {}).get("netto", 0) if busta else 0
            st.metric("💵 Netto in Busta", f"€ {netto:,.2f}")
        
        with col2:
            giorni_pagati = busta.get("dati_generali", {}).get("giorni_pagati", 0) if busta else 0
            st.metric("📋 Giorni Pagati", int(giorni_pagati))
        
        with col3:
            giorni_lavorati = cartellino.get("giorni_reali", 0) if cartellino else "N/D"
            st.metric("🏢 Giorni Lavorati", giorni_lavorati)
        
        with col4:
            assenze = agenda.get("absences_count", 0) if agenda else 0
            st.metric("🗓️ Assenze Agenda", assenze)
        
        # === DETTAGLIO BUSTA PAGA ===
        if busta:
            with st.expander("💰 Dettaglio Busta Paga"):
                comp = busta.get("competenze", {})
                tratt = busta.get("trattenute", {})
                
                c1, c2 = st.columns(2)
                
                with c1:
                    st.markdown("**➕ Competenze**")
                    st.write(f"• Retribuzione base: € {comp.get('base', 0):,.2f}")
                    if comp.get("anzianita", 0) > 0:
                        st.write(f"• Anzianità/Scatti: € {comp.get('anzianita', 0):,.2f}")
                    if comp.get("straordinari", 0) > 0:
                        st.write(f"• Straordinari: € {comp.get('straordinari', 0):,.2f}")
                    if comp.get("festivita", 0) > 0:
                        st.write(f"• Festività: € {comp.get('festivita', 0):,.2f}")
                    st.write(f"**Totale Lordo: € {comp.get('lordo_totale', 0):,.2f}**")
                
                with c2:
                    st.markdown("**➖ Trattenute**")
                    st.write(f"• INPS: € {tratt.get('inps', 0):,.2f}")
                    st.write(f"• IRPEF: € {tratt.get('irpef_netta', 0):,.2f}")
                    if tratt.get("addizionali_totali", 0) > 0:
                        st.write(f"• Addizionali: € {tratt.get('addizionali_totali', 0):,.2f}")
                    totale_tratt = tratt.get("inps", 0) + tratt.get("irpef_netta", 0) + tratt.get("addizionali_totali", 0)
                    st.write(f"**Totale Trattenute: € {totale_tratt:,.2f}**")
                
                st.divider()
                
                ferie = busta.get("ferie", {})
                par = busta.get("par", {})
                
                f1, f2 = st.columns(2)
                with f1:
                    st.markdown("**🏖️ Ferie**")
                    st.write(f"Residue anno precedente: {ferie.get('residue_ap', 0)}")
                    st.write(f"Maturate: {ferie.get('maturate', 0)}")
                    st.write(f"Godute: {ferie.get('godute', 0)}")
                    st.write(f"**Saldo: {ferie.get('saldo', 0)}**")
                
                with f2:
                    st.markdown("**⏱️ Permessi (PAR)**")
                    st.write(f"Residui anno precedente: {par.get('residue_ap', 0)}")
                    st.write(f"Spettanti: {par.get('spettanti', 0)}")
                    st.write(f"Fruiti: {par.get('fruite', 0)}")
                    st.write(f"**Saldo: {par.get('saldo', 0)}**")
        
        # === EVENTI AGENDA ===
        if agenda and agenda.get("total_events", 0) > 0:
            with st.expander("🗓️ Eventi Agenda del Mese"):
                for tipo, count in agenda.get("events_by_type", {}).items():
                    st.write(f"• {tipo}: **{count}** evento/i")
        
        # === GLOSSARIO ===
        with st.expander("📖 Glossario Voci Busta Paga"):
            for voce, spiegazione in GLOSSARIO_BUSTA.items():
                st.markdown(f"**{voce}**: {spiegazione}")
        
        # === DEBUG (nascosto) ===
        with st.expander("🔧 Log Tecnico", expanded=False):
            for line in result.get("debug_log", []):
                st.text(line)
