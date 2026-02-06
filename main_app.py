# ==============================================================================
# GOTTARDO PAYROLL ANALYZER v2.1
# ==============================================================================
# - Login e avvio in orizzontale (mobile-friendly)
# - Riepilogo con date eventi (assenze, malattie, omesse timbrature)
# - Niente log tecnico, niente glossario
# - Fix colori e cartellino
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

# --- OPTIONAL ---
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

try:
    import fitz
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
TIMEOUT_API = 30000
TIMEOUT_DEFAULT = 45000

CALENDAR_CODES = {
    "FEP": "Ferie",
    "OMT": "Omessa Timbratura",
    "RCS": "Riposo Compensativo",
    "RIC": "Riposo Compensativo",
    "MAL": "Malattia"
}

MESI_IT = [
    "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"
]


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
# AGENDA - API Method con dettaglio date
# ==============================================================================
def agenda_read_via_api(context, mese_num, anno, debug_log):
    """Legge l'agenda tramite API, restituisce eventi con date."""
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
                except Exception:
                    pass
        except Exception:
            pass
    
    # Saldo ferie/permessi
    try:
        url = f"{base_url}/api/time/v2/timeoffbalances?$filter_api=year={anno}"
        resp = context.request.get(url, timeout=TIMEOUT_API)
        if resp.ok:
            api_responses["balances"] = resp.json()
    except Exception:
        pass
    
    return _process_api_responses(api_responses, mese_num, anno, debug_log)


def _format_date_it(date_str):
    """Converte 2026-01-15 in 15 gen"""
    try:
        if not date_str or len(date_str) < 10:
            return date_str
        dt = datetime.strptime(date_str[:10], "%Y-%m-%d")
        mesi_short = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]
        return f"{dt.day} {mesi_short[dt.month - 1]}"
    except Exception:
        return date_str[:10] if date_str else "N/D"


def _process_api_responses(api_responses, mese_num, anno, debug_log):
    """Elabora le risposte API con dettaglio date."""
    result = {
        "events_this_month": [],
        "events_by_type": {},
        "total_events": 0,
        "absences_count": 0,
        "balances": api_responses.get("balances"),
        # Dettaglio per tipo con date
        "ferie": [],
        "malattie": [],
        "omesse_timbrature": [],
        "riposi": []
    }
    
    for code, events in api_responses.get("events", {}).items():
        if not isinstance(events, list):
            events = [events] if events else []
        
        code_name = CALENDAR_CODES.get(code, code)
        
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
                
                date_formatted = _format_date_it(start_time)
                summary = event.get("summary", "") or event.get("description", "") or code_name
                
                event_data = {
                    "type": code,
                    "type_name": code_name,
                    "summary": summary,
                    "date": date_formatted,
                    "date_raw": start_time[:10] if start_time else ""
                }
                
                result["events_this_month"].append(event_data)
                
                # Categorizza per tipo
                if code == "FEP":
                    result["ferie"].append(date_formatted)
                elif code == "MAL":
                    result["malattie"].append(date_formatted)
                elif code == "OMT":
                    result["omesse_timbrature"].append(date_formatted)
                elif code in ["RCS", "RIC"]:
                    result["riposi"].append(date_formatted)
                    
            except Exception:
                continue
        
        # Conta per tipo
        count_this_type = len([e for e in result["events_this_month"] if e["type"] == code])
        if count_this_type > 0:
            result["events_by_type"][code_name] = count_this_type
    
    result["total_events"] = len(result["events_this_month"])
    
    # Assenze = ferie + malattie + riposi (no OMT)
    result["absences_count"] = len(result["ferie"]) + len(result["malattie"]) + len(result["riposi"])
    
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

    for idx, (nome, model) in enumerate(MODELLI_DISPONIBILI, 1):
        try:
            resp = model.generate_content([prompt, {"mime_type": "application/pdf", "data": bytes_data}])
            out = clean_json_response(getattr(resp, "text", ""))
            if out and isinstance(out, dict):
                return out
        except Exception:
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
    result = {
        "status": "ok",
        "issues": [],
        "warnings": [],
        "summary": "",
        "details": {}
    }
    
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
    
    # Giorni teorici (lun-ven)
    giorni_teorici = 0
    try:
        _, ultimo_giorno = calendar.monthrange(anno, mese_num)
        for giorno in range(1, ultimo_giorno + 1):
            data = datetime(anno, mese_num, giorno)
            if data.weekday() < 5:
                giorni_teorici += 1
    except Exception:
        giorni_teorici = 22
    
    result["details"]["teorici"] = giorni_teorici
    
    # === ANALISI ===
    
    # 1. Confronto Giorni Pagati vs Lavorati
    if dati_busta and dati_cartellino and giorni_lavorati > 0:
        diff = giorni_lavorati - giorni_pagati
        
        if abs(diff) > 1:
            if diff > 0:
                result["warnings"].append(
                    f"Hai lavorato {diff} giorni in più di quelli pagati ({giorni_lavorati} vs {giorni_pagati})"
                )
            else:
                result["issues"].append(
                    f"Pagati {abs(diff)} giorni in più di quelli lavorati ({giorni_pagati} vs {giorni_lavorati})"
                )
    
    # 2. Omessa timbratura
    if dati_agenda:
        omt_count = len(dati_agenda.get("omesse_timbrature", []))
        if omt_count > 0:
            date_omt = ", ".join(dati_agenda.get("omesse_timbrature", []))
            result["warnings"].append(
                f"{omt_count} omessa/e timbratura: {date_omt}"
            )
    
    # 3. Anomalie badge
    if dati_cartellino:
        anomalie = dati_cartellino.get("giorni_senza_badge", 0)
        if anomalie > 0:
            result["warnings"].append(
                f"{anomalie} giorno/i senza timbratura badge"
            )
    
    # Status finale
    if result["issues"]:
        result["status"] = "error"
    elif result["warnings"]:
        result["status"] = "warning"
    else:
        result["status"] = "ok"
    
    # Summary
    if result["status"] == "ok":
        result["summary"] = "Tutto in ordine! I dati sono coerenti."
    elif result["status"] == "warning":
        result["summary"] = f"{len(result['warnings'])} aspetto/i da verificare"
    else:
        result["summary"] = f"{len(result['issues'])} incongruenze rilevate"
    
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
# Core Bot
# ==============================================================================
def esegui_analisi(mese_nome, anno, username, password, progress_callback=None):
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
                return {"error": "Login fallito. Verifica le credenziali."}

            # AGENDA
            log("🗓️ Lettura agenda...")
            time.sleep(2)
            try:
                result["agenda"] = agenda_read_via_api(context, mese_num, anno, debug_log)
            except Exception:
                pass

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
                                break
                        except Exception:
                            continue
            except Exception:
                pass

            # CARTELLINO
            log("📅 Scaricamento cartellino...")
            cart_ok = False
            try:
                page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2", wait_until="domcontentloaded")
                time.sleep(2)
                
                page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
                time.sleep(3)
                page.evaluate("document.getElementById('lnktab_5_label')?.click()")
                time.sleep(4)
                
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
                
                page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last.click(force=True)
                time.sleep(6)
                
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
                    
                    try:
                        popup.close()
                    except Exception:
                        pass
                        
            except Exception:
                pass

            browser.close()

    except Exception as e:
        return {"error": f"Errore: {e}"}

    # ANALISI AI
    log("🧠 Analisi documenti...")
    
    if busta_ok:
        result["busta"] = estrai_dati_busta_dettagliata(path_busta)
        try:
            os.remove(path_busta)
        except Exception:
            pass
    
    if cart_ok:
        result["cartellino"] = estrai_dati_cartellino(path_cart)
        try:
            os.remove(path_cart)
        except Exception:
            pass
    
    # Se cartellino non disponibile, stima da busta - assenze
    if not result["cartellino"] and result["busta"] and result["agenda"]:
        giorni_pagati = result["busta"].get("dati_generali", {}).get("giorni_pagati", 0)
        assenze = result["agenda"].get("absences_count", 0)
        
        # Calcola giorni teorici
        _, ultimo_giorno = calendar.monthrange(anno, mese_num)
        giorni_teorici = sum(1 for g in range(1, ultimo_giorno + 1) if datetime(anno, mese_num, g).weekday() < 5)
        
        giorni_stimati = giorni_teorici - assenze
        
        result["cartellino"] = {
            "giorni_reali": giorni_stimati,
            "giorni_senza_badge": 0,
            "note": "Stimato da agenda (cartellino non disponibile)"
        }

    # COERENZA
    log("🔍 Verifica coerenza...")
    result["coerenza"] = analizza_coerenza(
        result["busta"],
        result["cartellino"],
        result["agenda"],
        mese_num,
        anno
    )

    return result


# ==============================================================================
# UI - Streamlit (Senza sidebar, tutto in pagina)
# ==============================================================================
st.set_page_config(
    page_title="Gottardo Payroll",
    page_icon="💶",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# CSS
st.markdown("""
<style>
    /* Nascondi sidebar */
    [data-testid="stSidebar"] { display: none; }
    
    /* Status boxes */
    .status-ok { 
        background: linear-gradient(135deg, #d4edda 0%, #c3e6cb 100%);
        border-left: 4px solid #28a745;
        padding: 1rem;
        border-radius: 0.5rem;
        margin: 1rem 0;
        color: #155724;
    }
    .status-warning {
        background: linear-gradient(135deg, #fff3cd 0%, #ffeeba 100%);
        border-left: 4px solid #ffc107;
        padding: 1rem;
        border-radius: 0.5rem;
        margin: 1rem 0;
        color: #856404;
    }
    .status-error {
        background: linear-gradient(135deg, #f8d7da 0%, #f5c6cb 100%);
        border-left: 4px solid #dc3545;
        padding: 1rem;
        border-radius: 0.5rem;
        margin: 1rem 0;
        color: #721c24;
    }
    .status-ok h3, .status-warning h3, .status-error h3 { margin: 0 0 0.5rem 0; }
    .status-ok p, .status-warning p, .status-error p { margin: 0; }
    
    /* Input compatti */
    .stTextInput > div > div > input { padding: 0.5rem; }
    .stSelectbox > div > div { padding: 0; }
</style>
""", unsafe_allow_html=True)

st.title("💶 Analisi Stipendio Gottardo")

# === SEZIONE LOGIN/AVVIO IN ORIZZONTALE ===
username, password = get_credentials()

if not st.session_state.get("credentials_set"):
    st.markdown("### 🔐 Accedi")
    col1, col2, col3 = st.columns([2, 2, 1])
    
    with col1:
        input_user = st.text_input("Username", value=username or "", label_visibility="collapsed", placeholder="Username")
    with col2:
        input_pass = st.text_input("Password", type="password", label_visibility="collapsed", placeholder="Password")
    with col3:
        if st.button("� Accedi", use_container_width=True):
            if input_user and input_pass:
                st.session_state["username"] = input_user
                st.session_state["password"] = input_pass
                st.session_state["credentials_set"] = True
                st.rerun()
            else:
                st.error("Inserisci username e password")
else:
    # Utente loggato - mostra selezione periodo
    st.markdown(f"**👤 {st.session_state['username']}**")
    
    col1, col2, col3, col4 = st.columns([2, 2, 2, 1])
    
    with col1:
        sel_mese = st.selectbox("Mese", MESI_IT, index=0, label_visibility="collapsed")
    with col2:
        sel_anno = st.selectbox("Anno", [2024, 2025, 2026], index=1, label_visibility="collapsed")
    with col3:
        if st.button("🚀 ANALIZZA", type="primary", use_container_width=True):
            st.session_state.pop("result", None)
            st.session_state["running"] = True
            st.session_state["sel_mese"] = sel_mese
            st.session_state["sel_anno"] = sel_anno
    with col4:
        if st.button("🔄", help="Cambia utente"):
            st.session_state["credentials_set"] = False
            st.session_state.pop("result", None)
            st.rerun()

st.divider()

# === ESECUZIONE ANALISI ===
if st.session_state.get("running"):
    progress_placeholder = st.empty()
    
    def update_progress(msg):
        progress_placeholder.info(msg)
    
    with st.spinner("🔄 Analisi in corso..."):
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

# === RISULTATI ===
if "result" in st.session_state:
    result = st.session_state["result"]
    
    if "error" in result:
        st.error(f"❌ {result['error']}")
    else:
        coerenza = result.get("coerenza", {})
        busta = result.get("busta", {})
        cartellino = result.get("cartellino", {})
        agenda = result.get("agenda", {})
        
        # === STATUS ===
        status = coerenza.get("status", "ok")
        summary = coerenza.get("summary", "")
        
        if status == "ok":
            st.markdown(f'<div class="status-ok"><h3>✅ TUTTO OK</h3><p>{summary}</p></div>', unsafe_allow_html=True)
        elif status == "warning":
            st.markdown(f'<div class="status-warning"><h3>⚠️ ATTENZIONE</h3><p>{summary}</p></div>', unsafe_allow_html=True)
        else:
            st.markdown(f'<div class="status-error"><h3>❌ PROBLEMI</h3><p>{summary}</p></div>', unsafe_allow_html=True)
        
        # === PROBLEMI DETTAGLIATI ===
        issues = coerenza.get("issues", [])
        warnings = coerenza.get("warnings", [])
        
        if issues or warnings:
            for issue in issues:
                st.error(f"❌ {issue}")
            for warning in warnings:
                st.warning(f"⚠️ {warning}")
        
        # === RIEPILOGO ===
        st.subheader("📊 Riepilogo")
        
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            netto = busta.get("dati_generali", {}).get("netto", 0) if busta else 0
            st.metric("💵 Netto", f"€ {netto:,.2f}")
        
        with col2:
            giorni_pagati = busta.get("dati_generali", {}).get("giorni_pagati", 0) if busta else 0
            st.metric("📋 Giorni Pagati", int(giorni_pagati))
        
        with col3:
            giorni_lavorati = cartellino.get("giorni_reali", "N/D") if cartellino else "N/D"
            note = cartellino.get("note", "") if cartellino else ""
            help_text = note if "Stimato" in note else None
            st.metric("🏢 Giorni Lavorati", giorni_lavorati, help=help_text)
        
        with col4:
            assenze = agenda.get("absences_count", 0) if agenda else 0
            st.metric("🗓️ Assenze", assenze)
        
        # === EVENTI AGENDA DETTAGLIATI ===
        if agenda:
            st.subheader("📅 Dettaglio Agenda")
            
            cols = st.columns(4)
            
            # Ferie
            ferie = agenda.get("ferie", [])
            if ferie:
                with cols[0]:
                    st.markdown(f"**🏖️ Ferie ({len(ferie)})**")
                    st.write(", ".join(ferie))
            
            # Malattie
            malattie = agenda.get("malattie", [])
            if malattie:
                with cols[1]:
                    st.markdown(f"**🤒 Malattia ({len(malattie)})**")
                    st.write(", ".join(malattie))
            
            # Riposi
            riposi = agenda.get("riposi", [])
            if riposi:
                with cols[2]:
                    st.markdown(f"**😴 Riposi ({len(riposi)})**")
                    st.write(", ".join(riposi))
            
            # Omesse timbrature
            omt = agenda.get("omesse_timbrature", [])
            if omt:
                with cols[3]:
                    st.markdown(f"**⚠️ Omesse Timbrature ({len(omt)})**")
                    st.write(", ".join(omt))
        
        # === DETTAGLIO BUSTA ===
        if busta:
            with st.expander("💰 Dettaglio Busta Paga"):
                comp = busta.get("competenze", {})
                tratt = busta.get("trattenute", {})
                
                c1, c2 = st.columns(2)
                
                with c1:
                    st.markdown("**➕ Competenze**")
                    st.write(f"• Retribuzione base: € {comp.get('base', 0):,.2f}")
                    if comp.get("anzianita", 0) > 0:
                        st.write(f"• Anzianità: € {comp.get('anzianita', 0):,.2f}")
                    if comp.get("straordinari", 0) > 0:
                        st.write(f"• Straordinari: € {comp.get('straordinari', 0):,.2f}")
                    if comp.get("festivita", 0) > 0:
                        st.write(f"• Festività: € {comp.get('festivita', 0):,.2f}")
                    st.write(f"**Totale: € {comp.get('lordo_totale', 0):,.2f}**")
                
                with c2:
                    st.markdown("**➖ Trattenute**")
                    st.write(f"• INPS: € {tratt.get('inps', 0):,.2f}")
                    st.write(f"• IRPEF: € {tratt.get('irpef_netta', 0):,.2f}")
                    if tratt.get("addizionali_totali", 0) > 0:
                        st.write(f"• Addizionali: € {tratt.get('addizionali_totali', 0):,.2f}")
                
                st.divider()
                
                ferie_b = busta.get("ferie", {})
                par = busta.get("par", {})
                
                f1, f2 = st.columns(2)
                with f1:
                    st.markdown("**🏖️ Ferie**")
                    st.write(f"Residue AP: {ferie_b.get('residue_ap', 0)} | Maturate: {ferie_b.get('maturate', 0)} | Godute: {ferie_b.get('godute', 0)} | **Saldo: {ferie_b.get('saldo', 0)}**")
                
                with f2:
                    st.markdown("**⏱️ Permessi**")
                    st.write(f"Residui AP: {par.get('residue_ap', 0)} | Spettanti: {par.get('spettanti', 0)} | Fruiti: {par.get('fruite', 0)} | **Saldo: {par.get('saldo', 0)}**")
