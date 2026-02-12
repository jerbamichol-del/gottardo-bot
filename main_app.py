# ==============================================================================
# GOTTARDO PAYROLL ANALYZER - VERSIONE COMPLETA
# ==============================================================================
# Features:
# - Download Busta Paga + Cartellino
# - Lettura Agenda via API (ferie, omesse, malattie)
# - Parsing AI dettagliato con fallback DeepSeek
# - Controllo incrociato triplo (Busta + Cartellino + Agenda)
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
os.system("playwright install chromium")

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

try:
    locale.setlocale(locale.LC_TIME, "it_IT.UTF-8")
except Exception:
    pass

# Costanti
MESI_IT = [
    "Gennaio",
    "Febbraio",
    "Marzo",
    "Aprile",
    "Maggio",
    "Giugno",
    "Luglio",
    "Agosto",
    "Settembre",
    "Ottobre",
    "Novembre",
    "Dicembre",
]

# Ore standard per giornata (conversione ore→giorni)
ORE_GIORNALIERE = 8.0

# Codici eventi calendario Gottardo (dallo screenshot del portale)
CALENDAR_CODES = {
    "FEP": "FERIE PIANIFICATE",  # 🟡 Giallo
    "OMT": "OMESSA TIMBRATURA",  # 🔴 Rosa/Rosso
    "RCS": "RIPOSO COMPENSATIVO SUCC",  # 🟢 Verde
    "RIC": "RIPOSO COMPENSATIVO FORZ",  # 🟢 Verde
    "MAL": "MALATTIA",  # 🔵 Azzurro
}

# Keywords per riconoscere eventi nell'agenda (DOM parsing)
AGENDA_KEYWORDS = [
    "OMESSA TIMBRATURA",
    "OMESSA",
    "OMT",
    "MALATTIA",
    "MAL",
    "RIPOSO COMPENSATIVO",
    "RCS",
    "RIC",
    "FERIE PIANIFICATE",
    "FERIE",
    "FEP",
    "PERMESSO",
    "PAR",
    "ANOMALIA",
    "ASSENZA",
]


# ==============================================================================
# AI SETUP
# ==============================================================================
def get_api_keys():
    google_key = st.secrets.get("GOOGLE_API_KEY")
    deepseek_key = st.secrets.get("DEEPSEEK_API_KEY")
    return google_key, deepseek_key


@st.cache_resource
def init_gemini_models():
    """Inizializza tutti i modelli Gemini disponibili."""
    google_key, _ = get_api_keys()
    if not google_key:
        return []

    genai.configure(api_key=google_key)

    try:
        all_models = genai.list_models()
        valid = [
            m for m in all_models if "generateContent" in m.supported_generation_methods
        ]

        gemini_models = []
        for m in valid:
            name = m.name.replace("models/", "")
            if "gemini" in name.lower() and "embedding" not in name.lower():
                try:
                    gemini_models.append((name, genai.GenerativeModel(name)))
                except:
                    continue

        # Priorità: flash > lite > pro
        def priority(n):
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


def clean_json_response(text):
    """Pulisce e parsa JSON dalla risposta AI."""
    try:
        if not text:
            return None
        text = re.sub(r"```json|```", "", text).strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        payload = text[start:end] if start != -1 else text
        return json.loads(payload)
    except:
        return None


def extract_text_from_pdf(file_path):
    """Estrae testo da PDF usando PyMuPDF o pypdf."""
    if not file_path or not os.path.exists(file_path):
        return None

    # Prova PyMuPDF
    if fitz:
        try:
            doc = fitz.open(file_path)
            text = "\n".join([p.get_text() for p in doc])
            if text.strip():
                return text.strip()
        except:
            pass

    # Prova pypdf
    if PdfReader:
        try:
            reader = PdfReader(file_path)
            text = "\n".join([p.extract_text() or "" for p in reader.pages])
            if text.strip():
                return text.strip()
        except:
            pass

    return None




from collections import defaultdict

RIPOSO_CODES = {"RDD", "RCS", "RIC", "RPS", "REC", "RDO", "RCO"}


def extract_lines_from_pdf_words(pdf_path: str) -> list:
    """Estrae righe dal PDF usando PyMuPDF page.get_text('words'), più robusto per tabelle."""
    if not pdf_path or not os.path.exists(pdf_path) or not fitz:
        return []

    lines_out = []
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return []

    for page in doc:
        try:
            words = page.get_text("words") or []
        except Exception:
            words = []

        grouped = defaultdict(list)
        for x0, y0, x1, y1, w, b, l, wn in words:
            if not w:
                continue
            grouped[(b, l)].append((x0, str(w)))

        for (b, l), items in grouped.items():
            items.sort(key=lambda t: t[0])
            line = " ".join(w for _, w in items).strip()
            if line:
                lines_out.append(line)

    return lines_out


def riposi_from_cartellino_pdf(pdf_path: str):
    """Conta riposi come giorni unici (token tipo D05) leggendo le righe del cartellino."""
    days = set()
    for line in extract_lines_from_pdf_words(pdf_path):
        ln = (line or "").replace(".", " ").strip().upper()
        m = re.match(r"^(RDD|RCS|RIC|RPS|REC|RDO|RCO)\s+([A-Z]\d{2})\b", ln)
        if m and m.group(1) in RIPOSO_CODES:
            days.add(m.group(2))
    return len(days)

def parse_cartellino_footer_metrics(pdf_path: str) -> dict:
    # Estrae metriche dal footer del cartellino con regex su righe PyMuPDF words.
    out = {
        "gg_presenza": 0.0,
        "ore_lavorate": 0.0,
        "ore_ordinarie": 0.0,
        "ore_malattia": 0.0,
        "ore_ferie": 0.0,
        "ore_permessi": 0.0,
        "ore_festivita": 0.0,
    }

    def _to_float_it(x: str) -> float:
        try:
            x = (x or "").strip()
            # Gestisce formati come 47.25 o 47,25
            x = x.replace(".", "").replace(",", ".")
            return float(x)
        except Exception:
            return 0.0

    lines = extract_lines_from_pdf_words(pdf_path) or []
    
    # Debug: Cerca la riga dei totali (es. 110,75 10,25 10,25 14,00 47,25)
    # Questa riga di solito precede i codici 0251, 0253.
    for i, raw in enumerate(lines):
        ln = (raw or "").upper().strip()
        
        # Cerca pattern di molti numeri in fila (almeno 3)
        vals = re.findall(r"(\d+[\.,]\d+)", ln)
        if len(vals) >= 4 and any("025" in l for l in lines[i:i+5]):
            # Probabile riga dei totali colonne. 
            # In Gottardo: ORD ... RIC FER
            # ORD è il primo, RIC è il penultimo, FER è l'ultimo (se presenti)
            out["ore_ordinarie"] = max(out["ore_ordinarie"], _to_float_it(vals[0]))
            if len(vals) >= 2:
                # FER è quasi sempre l'ultimo numero a destra
                out["ore_ferie"] = max(out["ore_ferie"], _to_float_it(vals[-1]))
            if len(vals) >= 3:
                # RIC è di solito il penultimo
                out["ore_festivita"] = max(out["ore_festivita"], _to_float_it(vals[-2]))

        # Cerca i codici specifici
        if "0265" in ln:
            m = re.search(r"\b0265\b.*?(\d+[\.,]\d+)", ln)
            if m: out["gg_presenza"] = max(out["gg_presenza"], _to_float_it(m.group(1)))

        if "0253" in ln:
            m = re.search(r"\b0253\b.*?(\d+[\.,]\d+)", ln)
            if m: out["ore_lavorate"] = max(out["ore_lavorate"], _to_float_it(m.group(1)))

        if "2502" in ln or ("MAL" in ln and "CARENZA" in ln):
            m = re.search(r"(\d+[\.,]\d+)", ln.split("100%")[-1] if "100%" in ln else ln)
            if m: out["ore_malattia"] = max(out["ore_malattia"], _to_float_it(m.group(1)))

    # Fallback globale se spezzato
    full = " | ".join((x or "").upper() for x in lines)
    if not out["gg_presenza"]:
        m = re.search(r"\b0265\b.*?(\d+[\.,]\d+)", full)
        if m: out["gg_presenza"] = _to_float_it(m.group(1))
    
    return out
def analyze_with_fallback(file_path, prompt, tipo="documento"):
    """Analizza PDF con Gemini, fallback su DeepSeek."""
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

    # Prova tutti i modelli Gemini
    for idx, (name, model) in enumerate(models, 1):
        try:
            progress.info(f"🔄 {tipo}: modello {idx}/{len(models)} ({name})...")
            resp = model.generate_content(
                [prompt, {"mime_type": "application/pdf", "data": pdf_bytes}]
            )
            result = clean_json_response(getattr(resp, "text", ""))
            if result and isinstance(result, dict):
                progress.success(f"✅ {tipo} analizzato!")
                time.sleep(0.3)
                progress.empty()
                return result
        except Exception as e:
            last_error = e
            continue

    # Fallback DeepSeek
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
                time.sleep(0.3)
                progress.empty()
                return result
        except Exception as e:
            last_error = e

    progress.error(f"❌ Analisi {tipo} fallita")
    if last_error:
        with st.expander("🔎 Errore"):
            st.code(str(last_error)[:500])
    return None


# ==============================================================================
# PARSERS AI DETTAGLIATI
# ==============================================================================
def parse_busta_dettagliata(path):
    """Parser completo cedolino con tutti i dettagli."""
    prompt = """
Questo è un CEDOLINO PAGA GOTTARDO S.p.A. italiano. Estrai ESATTAMENTE:

**1. DATI GENERALI:**
- NETTO: riga "PROGRESSIVI" colonna finale (es. 788,61)
- GIORNI PAGATI: riga "GG. INPS" (es. 25)
- ORE ORDINARIE: "ORE INAIL" o (GIORNI PAGATI × 8)

**2. COMPETENZE:**
- base: Cerca "RETRIBUZIONE ORDINARIA" o "PAGA BASE" (voce 1000) -> valore nella colonna Competenze
- straordinari: somma STRAORDINARIO/SUPPLEMENTARI/NOTTURNI
- festivita: MAGG. FESTIVE/FESTIVITA GODUTA
- anzianita: SCATTI/EDR/ANZ.
- lordo_totale: Cerca "TOTALE COMPETENZE" in fondo alla colonna competenze

**3. TRATTENUTE:**
- inps: sezione I.N.P.S.
- irpef_netta: sezione FISCALI
- addizionali: add.reg + add.com

**4. FERIE/PAR (tabella in alto a destra):**
- Formato: RES.PREC / SPETTANTI / FRUITE / SALDO

**5. ASSENZE DEL MESE (IMPORTANTE!):**
Cerca nella colonna centrale le voci relative a ferie/permessi/malattia fruiti nel mese corrente:
- ore_ferie_mese: Cerca "FERIE GODUTE" (voce 4521) -> prendi valore colonna "ORE/GG/MESI".
- ore_permessi_mese: Cerca "PERMESSI GODUTI" o "ROL" (voce 4529) -> prendi valore colonna "ORE/GG/MESI".
- gg_malattia_mese: Cerca righe con "MAL: CARENZA" o "MALATTIA" -> prendi valore colonna "ORE/GG/MESI".
- festivita: Cerca "FESTIVITA' GODUTA" (voce 4006) -> prendi valore colonna "ORE/GG/MESI" (solitamente è 1.00).

IMPORTANTE: Estrai i valori numerici con TUTTI i decimali. Per la malattia, se trovi più righe (es. voce 2502 e 2650), riportano lo stesso evento: prendi il valore una sola volta (massimo).

Output SOLO JSON:
{
  "e_tredicesima": false,
  "dati_generali": {"netto": 0.00, "giorni_pagati": 0, "ore_ordinarie": 0.00},
  "competenze": {"base": 0.00, "anzianita": 0.00, "straordinari": 0.00, "festivita": 0.00, "lordo_totale": 0.00},
  "trattenute": {"inps": 0.00, "irpef_netta": 0.00, "addizionali": 0.00},
  "ferie": {"residue_ap": 0.00, "maturate": 0.00, "godute": 0.00, "saldo": 0.00},
  "par": {"residue_ap": 0.00, "spettanti": 0.00, "fruite": 0.00, "saldo": 0.00},
  "assenze_mese": {"ore_ferie": 0.00, "ore_permessi": 0.00, "gg_malattia": 0.00, "gg_festivita": 0.00}
}
""".strip()

    result = analyze_with_fallback(path, prompt, "Busta Paga")
    if not result:
        return {
            "e_tredicesima": False,
            "dati_generali": {"netto": 0, "giorni_pagati": 0, "ore_ordinarie": 0},
            "competenze": {
                "base": 0,
                "anzianita": 0,
                "straordinari": 0,
                "festivita": 0,
                "lordo_totale": 0,
            },
            "trattenute": {"inps": 0, "irpef_netta": 0, "addizionali": 0},
            "ferie": {"residue_ap": 0, "maturate": 0, "godute": 0, "saldo": 0},
            "par": {"residue_ap": 0, "spettanti": 0, "fruite": 0, "saldo": 0},
            "assenze_mese": {"ore_ferie": 0, "ore_permessi": 0, "ore_malattia": 0},
        }
    return result




def parse_cartellino_dettagliato(path):
    """Parser completo cartellino presenze."""
    prompt = """
    Analizza questo CARTELLINO PRESENZE GOTTARDO S.p.A.
    
    **1. DATI DAL FOOTER (UFFICIALI):**
    - "GG PRESENZA" o codice 0265: estrai il numero esatto (es. 21,00). Assegnato a "giorni_footer".
    - "ORE LAVORATE" o codice 0253: estrai il valore (es. 153,00).
    
    **2. CONTEGGIO RIGHE (VERIFICA):**
    - Conta manualmente tutte le righe che indicano PRESENZA/LAVORO:
      - Codici che iniziano con 'V' (V70, V50, V29, V01, ecc.)
      - Righe con orari di timbratura (es. 08:30 13:00)
      - Righe "ORD" o "STR"
      - NON contare righe che hanno SOLO codici di assenza come F70 (Festività), FER (Ferie), MAL (Malattia), RCO/RDD (Riposo) SENZA timbrature.
    - Assegna questo conteggio manuale a "giorni_righe".
    
    **3. ALTRI CODICI:**
    - **FESTIVITÀ**: Solo Codice F70 (Festività). Conta i giorni (es. 1 maggio = 1).
    - **FERIE**: Solo Righe con FER, FE sulle righe.
    - **MALATTIA**: Solo Righe con MAL.
    - **IMPORTANTE**: Ignora il codice 0265 (GG PRESENZA) per quanto riguarda le assenze e le festività. Non confondere i 15 o 21 giorni di presenza con le festività.

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
    
    # Normalizzazione finale
    if result.get("giorni_footer", 0) > 0:
        result["giorni_lavorati"] = result["giorni_footer"]
    elif result.get("giorni_righe", 0) > 0:
        result["giorni_lavorati"] = result["giorni_righe"]
        

    # RIPOSI: da PDF (PyMuPDF words) per coerenza con il cartellino
    try:
        rip_det = riposi_from_cartellino_pdf(path)
        if rip_det > 0:
            result["riposi"] = int(rip_det)
    except Exception:
        pass


    # Footer metrics (deterministico): GG PRESENZA / ORE / MAL / FER / RIC
    try:
        fm = parse_cartellino_footer_metrics(path)
        if fm.get("gg_presenza", 0) > 0:
            result["giorni_footer"] = fm["gg_presenza"]
            result["giorni_lavorati"] = fm["gg_presenza"]
        if fm.get("ore_lavorate", 0) > 0:
            result["ore_lavorate"] = fm["ore_lavorate"]
        if fm.get("ore_ordinarie", 0) > 0:
            result["ore_ordinarie_footer"] = fm["ore_ordinarie"]
        if fm.get("ore_malattia", 0) > 0:
            result["ore_malattia_footer"] = fm["ore_malattia"]
        if fm.get("ore_ferie", 0) > 0:
            result["ore_ferie_footer"] = fm["ore_ferie"]
        if fm.get("ore_festivita", 0) > 0:
            result["ore_festivita_footer"] = fm["ore_festivita"]
    except Exception:
        pass
    return result


# ==============================================================================
# AGENDA - METODO MIGLIORATO CON INTERCETTAZIONE RETE
# ==============================================================================
def read_agenda_with_navigation(page, context, mese_num, anno):
    """
    Legge l'agenda navigando effettivamente al calendario e intercettando le richieste.
    Questo è più affidabile delle chiamate API dirette.
    """
    result = {"events_by_type": {}, "total_events": 0, "items": [], "debug": []}

    captured_events = []

    # Handler per catturare risposte di rete
    def capture_calendar_response(response):
        try:
            url = response.url
            if (
                "events" in url.lower()
                or "calendar" in url.lower()
                or "time" in url.lower()
                or "anomalies" in url.lower()
            ):
                if response.status == 200:
                    try:
                        data = response.json()
                        if data:
                            result["debug"].append(
                                f"📡 Catturato ({'JSON' if isinstance(data, (list, dict)) else 'TEXT'}): {url[:70]}..."
                            )
                            if isinstance(data, list):
                                captured_events.extend(data)
                            elif isinstance(data, dict) and "items" in data:
                                captured_events.extend(data["items"])
                            elif isinstance(data, dict):
                                captured_events.append(data)
                    except:
                        pass
        except:
            pass

    # Registra listener
    page.on("response", capture_calendar_response)

    try:
        # Naviga al calendario (Time -> Calendario)
        result["debug"].append("🗓️ Navigazione al calendario...")

        # 1) Clicca su Time nel menu
        try:
            page.evaluate(
                "document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()"
            )
            result["debug"].append("  Menu Time cliccato (JS)")
        except:
            try:
                page.locator("text=Time").first.click(force=True)
                result["debug"].append("  Menu Time cliccato (locator)")
            except:
                result["debug"].append("  ⚠️ Menu Time non trovato")
        time.sleep(3)

        # 2) Cerca il pannello/tab del calendario - vari tentativi
        # Guardando lo screenshot: "Mese" è un tab che mostra la vista calendario
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
            except:
                continue

        if not tab_clicked:
            # Prova con ID specifici
            for tab_id in ["lnktab_0_label", "lnktab_1_label", "lnktab_2_label"]:
                try:
                    if page.evaluate(f"!!document.getElementById('{tab_id}')"):
                        page.evaluate(f"document.getElementById('{tab_id}')?.click()")
                        result["debug"].append(f"  ✅ Tab {tab_id} cliccato")
                        break
                except:
                    pass

        time.sleep(4)

        # === CATTURA EVENTI DAL DOM (DENTRO IFRAME) ===
        result["debug"].append("🔍 Ricerca eventi nell'IFRAME del calendario...")

        # Cerca il frame del calendario
        calendar_frame = None
        for frame in page.frames:
            if "CalUI" in frame.name or "calendar" in frame.url:
                calendar_frame = frame
                result["debug"].append(f"  ✅ Frame calendario trovato: {frame.name}")
                break

        # Se non trova il frame specifico, usa il main frame ma cerca anche negli altri
        target_frames = [calendar_frame] if calendar_frame else page.frames

        # === NAVIGAZIONE AL MESE CORRETTO (LOGICA SIDEBAR) ===
        target_month_name = MESI_IT[mese_num - 1].upper()  # es: OTTOBRE
        result["debug"].append(
            f"🗓️ Navigazione al mese target: {target_month_name} {anno}"
        )

        cal_nav_success = False
        if calendar_frame:
            try:
                # 0. FORZA VISTA MENSILE (CRITICO!)
                # Cerca e clicca il bottone "Mese" nella toolbar principale
                result["debug"].append("  🖱️ Imposto vista MENSILE (click 'Mese')...")

                # Selettori per il bottone Mese
                # Cerchiamo bottoni che contengono il testo "Mese"
                month_view_btns = calendar_frame.locator(
                    ".dijitButtonText, .dijitButton"
                ).filter(has_text="Mese")

                clicked_view = False
                if month_view_btns.count() > 0:
                    # Clicca il primo visibile
                    for i in range(month_view_btns.count()):
                        btn = month_view_btns.nth(i)
                        if btn.is_visible():
                            btn.click()
                            clicked_view = True
                            result["debug"].append("  ✅ Vista 'Mese' cliccata")
                            break

                if not clicked_view:
                    # Fallback su span testo esatto
                    try:
                        calendar_frame.locator("span", has_text="Mese").first.click()
                        result["debug"].append(
                            "  ✅ Vista 'Mese' cliccata (fallback span)"
                        )
                    except:
                        result["debug"].append("  ⚠️ Bottone 'Mese' non trovato")

                time.sleep(2)  # Attesa cambio vista

                # === NUOVA NAVIGAZIONE: USA FRECCE PRINCIPALI TOOLBAR (NO SIDEBAR) ===
                # 1. Assicurati Vista MENSILE
                result["debug"].append("  🖱️ Imposto vista MENSILE...")
                month_btns = calendar_frame.locator(
                    ".dijitButtonText, .dijitButtonContents"
                ).filter(has_text="Mese")
                if month_btns.count() > 0:
                    for i in range(month_btns.count()):
                        if month_btns.nth(i).is_visible():
                            try:
                                month_btns.nth(i).click()
                                time.sleep(2)
                                break
                            except:
                                pass

                # Selettori per il titolo (es. "Gennaio 2026")
                # Tentativo 1: Selettori specifici Dojo/ZK
                title_selectors = [
                    ".dijitCalendarTitle",
                    ".dojoxCalendarTitle",
                    "#calendarTitle",
                    ".calendarTitle",
                    "span[id*='Title']",
                    "div[id*='Title']",
                    ".title",
                    ".header-title",
                ]

                found_title = False
                title_el = None

                for sel in title_selectors:
                    els = calendar_frame.locator(sel)
                    if els.count() > 0:
                        for i in range(els.count()):
                            if els.nth(i).is_visible():
                                t = els.nth(i).inner_text().strip()
                                if re.search(r"\b20\d{2}\b", t):  # Cerca anno (20xx)
                                    title_el = els.nth(i)
                                    found_title = True
                                    result["debug"].append(
                                        f"  ✅ Titolo trovato con sel '{sel}': {t}"
                                    )
                                    break
                    if found_title:
                        break

                # Tentativo 2: Ricerca testuale generica per testo che sembra una data (Mese Anno)
                if not found_title:
                    result["debug"].append(
                        "  ⚠️ Titolo non trovato con selettori, provo ricerca testo generica..."
                    )
                    # Cerca elementi che contengono l'anno corrente o target
                    # Es: "Gennaio 2026"
                    text_candidates = calendar_frame.locator(
                        "text=202"
                    ).all()  # Prende tutto ciò che ha "202..."
                    for el in text_candidates:
                        try:
                            if el.is_visible():
                                txt = (
                                    el.inner_text().strip()
                                )  # es "Gennaio 2026" o "01/01/2026"
                                # Deve essere breve (< 30 caratteri) per essere un titolo
                                if len(txt) < 30 and re.search(
                                    r"[A-Za-z]+\s+20\d{2}", txt
                                ):
                                    title_el = el
                                    current_title_text = txt
                                    found_title = True
                                    result["debug"].append(
                                        f"  ✅ Titolo trovato per euristica testo: '{txt}'"
                                    )
                                    break
                        except:
                            pass

                # DIAGNOSTICA HTML SE FALLISCE ANCORA
                if not found_title:
                    result["debug"].append(
                        "  ❌ TITOLO ASSENTE. Eseguo DUMP struttura HTML..."
                    )
                    # Salva un riassunto dei div/span visibili per capire cosa c'è
                    try:
                        visible_els = calendar_frame.locator("div, span, button").all()
                        count_vis = 0
                        for el in visible_els:
                            if count_vis > 30:
                                break
                            if el.is_visible():
                                t = el.inner_text().strip() or "[no text]"
                                if len(t) > 50:
                                    t = t[:50] + "..."
                                i_d = el.get_attribute("id") or ""
                                cls = el.get_attribute("class") or ""
                                if t != "[no text]" or i_d:  # Logga solo roba utile
                                    result["debug"].append(
                                        f"    - Tag: {t} | ID: {i_d} | Class: {cls}"
                                    )
                                    count_vis += 1
                    except Exception as dump_e:
                        result["debug"].append(f"    Errore dump: {dump_e}")

                # 3. Naviga Indietro/Avanti (STRATEGIA POPUP: ICONA -> MINI CAL -> FRECCE)
                # Il "Mini Calendar" si apre cliccando un DropDownButton

                # Cerca l'icona/bottone dropdown
                # Strategia: Trova TUTTI i candidati e clicca il primo VISIBILE
                dropdown_candidates = calendar_frame.locator(
                    ".popup-trigger, .calendar16, [widgetid^='revit_form_Button'], .dijitCalendarIcon"
                ).all()

                opened_popup = False
                result["debug"].append(
                    f"  🔍 Trovati {len(dropdown_candidates)} candidati per il Dropdown. Cerco quello visibile..."
                )

                for btn in dropdown_candidates:
                    try:
                        if btn.is_visible():
                            result["debug"].append(
                                f"  🖱️ Clicco candidato visibile: {btn.get_attribute('class')}..."
                            )
                            btn.click()
                            time.sleep(2.0)

                            # Verifica se si è aperto
                            if calendar_frame.locator(
                                ".dijitCalendar, .dijitCalendarPopup"
                            ).last.is_visible():
                                opened_popup = True
                                break
                    except:
                        pass

                if not opened_popup:
                    # Fallback: Clicca il TITOLO STESSO (spesso apre il picker)
                    result["debug"].append(
                        "  ⚠️ Nessun Dropdown visibile funzionante. Provo click su Titolo..."
                    )
                    try:
                        calendar_frame.locator(
                            f"text={current_title_text}"
                        ).first.click()
                        time.sleep(2.0)
                        if calendar_frame.locator(
                            ".dijitCalendar, .dijitCalendarPopup"
                        ).last.is_visible():
                            opened_popup = True
                    except:
                        pass

                # Ora cerchiamo il POPUP del calendario (spesso è un dijitPopup o dijitCalendarMenu)
                # Potrebbe essere dentro il frame o nel root. Proviamo nel frame.
                mini_cal = calendar_frame.locator(
                    ".dijitCalendar, .dijitCalendarPopup"
                ).last

                if mini_cal.is_visible():
                    result["debug"].append("  ✅ Mini-Calendario APERTO!")

                    moves = 0
                    max_moves = 36

                    # Calcolo Delta Iniziale (Dead Reckoning)
                    # Se la lettura del popup fallisce, usiamo la data letta dalla pagina principale (current_title_text)
                    months_delta = 0
                    start_y = -1
                    start_m = -1

                    # Parsing data principale (che sappiamo funzionare: '01 feb - 28 feb 2026')
                    try:
                        # Assicuriamoci che sia UPPER
                        current_title_upper = current_title_text.upper()

                        y_match = re.search(r"20\d{2}", current_title_upper)
                        if y_match:
                            start_y = int(y_match.group(0))

                        mesi = [m.upper() for m in MESI_IT]
                        for i, m in enumerate(mesi):
                            if m in current_title_upper or (
                                len(m) > 4 and m[:-1] in current_title_upper
                            ):
                                start_m = i + 1
                                break
                        if start_m == -1:  # Try short
                            for i, m3 in enumerate([m[:3] for m in mesi]):
                                if re.search(r"\b" + m3 + r"\b", current_title_upper):
                                    start_m = i + 1
                                    break
                    except Exception as e_delta:
                        result["debug"].append(f"    ⚠️ Errore calcolo delta: {e_delta}")

                    if start_y != -1 and start_m != -1:
                        target_val = anno * 12 + mese_num
                        start_val = start_y * 12 + start_m
                        months_delta = target_val - start_val
                        result["debug"].append(
                            f"  🧮 Navigazione Stimata (Dead Reckoning): Start={start_m}/{start_y}, Target={mese_num}/{anno}, Delta={months_delta}"
                        )
                    else:
                        result["debug"].append(
                            "  ⚠️ Impossibile calcolare delta mesi iniziale (Start date ignota)"
                        )

                    moves = 0
                    clicks_needed = abs(months_delta)
                    direction_is_back = months_delta < 0

                    while moves <= clicks_needed + 2:  # +2 buffer
                        # 3a. Leggi data (Opzionale, solo per conferma)
                        curr_title = "ERROR"
                        try:
                            # Prova a leggere per fermarci prima se funziona
                            curr_month_el = mini_cal.locator(
                                ".dijitCalendarMonthLabel"
                            ).first
                            if curr_month_el.is_visible():
                                curr_title = (
                                    curr_month_el.inner_text()
                                    + " "
                                    + mini_cal.locator(
                                        ".dijitCalendarYearLabel"
                                    ).first.inner_text()
                                )
                            curr_title = curr_title.strip().upper()
                        except:
                            pass

                        if curr_title != "ERROR" and len(curr_title) > 3:
                            # Logica Intelligente (Se la lettura funziona)
                            # ... (omissis, usiamo la logica cieca prioritariamente se abbiamo delta)
                            # Check if arrived
                            # ...
                            pass

                        # LOGICA CIECA PRIORITARIA o FALLBACK
                        if months_delta != 0:
                            # Se abbiamo un piano di navigazione, seguiamolo
                            if moves < clicks_needed:
                                arrow_sel = (
                                    ".dijitCalendarDecrease"
                                    if direction_is_back
                                    else ".dijitCalendarIncrease"
                                )
                                desc = "Indietro" if direction_is_back else "Avanti"

                                btn = mini_cal.locator(arrow_sel).first
                                if btn.is_visible():
                                    btn.click()
                                    result["debug"].append(
                                        f"    Blind Click {moves + 1}/{clicks_needed}: {desc}"
                                    )
                                else:
                                    result["debug"].append(
                                        f"    ⚠️ Bottone Blind {arrow_sel} NON VISIBILE"
                                    )
                                time.sleep(0.4)  # Click rapidi
                                moves += 1
                                continue
                            else:
                                # Finito i click previsti!
                                result["debug"].append(
                                    "    🏁 Finiti click stimati. Clicco giorno per confermare..."
                                )

                                # Clicca GIORNO
                                days = mini_cal.locator(
                                    ".dijitCalendarDateTemplate:not(.dijitCalendarPreviousMonth):not(.dijitCalendarNextMonth), .dijitCalendarCurrentMonth"
                                ).all()
                                if len(days) > 0:
                                    idx = min(15, len(days) - 1)
                                    try:
                                        days[idx].click()
                                        result["debug"].append(
                                            f"    🖱️ Click giorno {idx + 1}"
                                        )
                                        time.sleep(4)
                                        cal_nav_success = True
                                    except:
                                        pass
                                else:
                                    result["debug"].append(
                                        "    ⚠️ Nessun giorno cliccabile trovato"
                                    )
                                break
                        else:
                            # Se delta è 0 (o ignoto), prova logica standard (con lettura fallimentare -> exit)
                            break

                        moves += 1
                else:
                    result["debug"].append(
                        "  ⚠️ Popup Mini-Calendario NON APERTO dopo il click"
                    )
            except Exception as nav_err:
                result["debug"].append(f"  ❌ Errore generale navigazione: {nav_err}")

        # === CATTURA EVENTI DAL DOM (FALLBACK TOTALE) ===
        # Se la griglia non si trova, cerca OVUNQUE nel frame
        result["debug"].append(
            "🔍 Avvio scraping eventi (Ricerca Globale nel Frame)..."
        )

        dom_events = []
        found_any = False  # Inizializza flag PRIMA del loop

        if calendar_frame:
            try:
                # Url check: siamo ancora sull'agenda?
                # Aspetta body visible
                calendar_frame.locator("body").wait_for(timeout=2000)
                time.sleep(2)  # Rendering finale

                # 1. Prova prima griglia specifica (più accurata)
                grid = calendar_frame.locator(
                    "#calendarContainer, #calendarUI_ExtendedCalendar_0"
                ).first

                search_area = (
                    grid if grid.is_visible() else calendar_frame.locator("body")
                )
                src_name = "Griglia" if grid.is_visible() else "BODY (Fallback)"
                result["debug"].append(f"  Target scraping: {src_name}")

                # 2. Cerca Keyword
                keywords = [
                    "OMESSA",
                    "OMT",
                    "FERIE",
                    "FEP",
                    "MALATTIA",
                    "MAL",
                    "RIPOSO",
                    "RCS",
                    "RIC",
                    "RPS",
                ]

                # Dizionario per evitare duplicati (stesso evento letto più volte)
                # Chiave = testo + posizione approx? No, conteggio semplice per ora.

                # STRATEGIA GEOMETRICA WHITELIST
                # Invece di cercare le celle "bad", cerchiamo le celle "GOOD" (mese corrente)
                # e accettiamo SOLO gli eventi che cadono sopra di esse.
                allowed_boxes = []
                try:
                    # Prova diversi selettori per le celle del mese corrente
                    cell_selectors = [
                        ".dijitCalendarCurrentMonth",
                        "td:not(.dijitCalendarPreviousMonth):not(.dijitCalendarNextMonth)",
                        "td[style*='background']:not([style*='gray'])",
                    ]
                    
                    for sel in cell_selectors:
                        try:
                            cells = search_area.locator(sel).all()
                            for c in cells:
                                if c.is_visible():
                                    b = c.bounding_box()
                                    if b:
                                        allowed_boxes.append(b)
                            if len(allowed_boxes) >= 28:  # Minimo 28 giorni in un mese
                                break
                        except:
                            continue

                    result["debug"].append(
                        f"  ✅ Mappate {len(allowed_boxes)} celle giorni mese corrente"
                    )
                except:
                    pass

                # Nomi dei mesi per il filtro testuale (escludere eventi che menzionano altri mesi)
                mese_nome_corrente = MESI_IT[mese_num - 1]  # es: "Ottobre" per mese_num=10
                altri_mesi = [m.lower()[:3] for m in MESI_IT if m.lower()[:3] != mese_nome_corrente.lower()[:3]]
                mese_corrente_short = mese_nome_corrente.lower()[:3]  # es: "ott" per Ottobre

                # Loop completo keywords (esteso con MANCATA/ANOMALIA)
                all_kws = [
                    "OMESSA",
                    "OMT",
                    "MANCATA",
                    "ANOMALIA",
                    "FERIE",
                    "FEP",
                    "MALATTIA",
                    "MAL",
                    "RIPOSO",
                    "RCS",
                    "RIC",
                    "RPS",
                    "REC",
                ]
                for kw in all_kws:
                    # text=KW è case-insensitive
                    matches = search_area.locator(f"text={kw}")
                    count = matches.count()

                    real_matches = 0
                    for i in range(count):
                        try:
                            el = matches.nth(i)
                            if not el.is_visible():
                                continue

                            # 1. FILTRI TESTUALI (ANTI-SIDEBAR)
                            txt_upper = el.inner_text().upper()
                            txt_lower = el.inner_text().lower()
                            if "SALDO" in txt_upper or "RESIDUO" in txt_upper:
                                continue
                            if "TOTALE" in txt_upper or "PERMESSI DEL" in txt_upper:
                                continue
                            
                            # 1b. FILTRO DATE ALTRI MESI
                            # Escludi eventi che contengono date di altri mesi (es. "29 set", "1 nov")
                            skip_wrong_month = False
                            for altro_mese in altri_mesi:
                                if altro_mese in txt_lower:
                                    skip_wrong_month = True
                                    break
                            if skip_wrong_month:
                                result["debug"].append(f"    Scartato evento fuori mese: {txt_lower[:40]}...")
                                continue


                            # 2. FILTRI GEOMETRICI
                            box = el.bounding_box()
                            if not box:
                                continue

                            # a) Sidebar a sinistra
                            if box["x"] < 300:
                                continue

                            # b) WHITELIST CHECK: Deve sovrapporsi a una cella del mese corrente
                            # Se allowed_boxes è vuoto (es. scraping body fallback senza griglia), disabilitiamo il filtro per sicurezza
                            # Ma se ne abbiamo trovate (es. 31), allora il filtro è ATTIVO.
                            if allowed_boxes:
                                is_good = False
                                cx = box["x"] + box["width"] / 2
                                cy = box["y"] + box["height"] / 2
                                for gbox in allowed_boxes:
                                    if (
                                        gbox["x"] <= cx <= gbox["x"] + gbox["width"]
                                    ) and (
                                        gbox["y"] <= cy <= gbox["y"] + gbox["height"]
                                    ):
                                        is_good = True
                                        break
                                if not is_good:
                                    # result["debug"].append(f"    Scartato '{kw}' fuori dai giorni del mese")
                                    continue

                            real_matches += 1
                            if "OMESSA" in kw or "OMT" in kw:
                                dom_events.append("OMESSA TIMBRATURA")
                            elif "FERIE" in kw or "FEP" in kw:
                                dom_events.append("FERIE")
                            elif "MALATTIA" in kw or "MAL" in kw:
                                dom_events.append("MALATTIA")
                            elif (
                                "RIPOSO" in kw
                                or "RCS" in kw
                                or "RIC" in kw
                                or "RPS" in kw
                            ):
                                dom_events.append("RIPOSO")

                        except:
                            pass

                    if real_matches > 0:
                        result["debug"].append(
                            f"  📝 Trovati {real_matches} x '{kw}' validi"
                        )
                        found_any = True

                if not found_any:
                    # Se il filtro geometrico ha fallito, NON fare fallback sul testo grezzo
                    # perché potrebbe includere eventi di mesi precedenti/successivi
                    result["debug"].append(
                        "  ⚠️ Nessun evento valido trovato (il filtro geometrico potrebbe aver escluso giorni fuori mese)"
                    )

            except Exception as e:
                result["debug"].append(f"  ❌ Errore scraping globale: {e}")

        result["debug"].append(f"📋 Totale eventi validi estratti: {len(dom_events)}")

    except Exception as e:
        result["debug"].append(f"❌ Errore navigazione: {type(e).__name__}")
    finally:
        # Rimuovi listener
        try:
            page.remove_listener("response", capture_calendar_response)
        except:
            pass

    # Processa eventi catturati
    all_events = captured_events + [{"summary": e} for e in dom_events]

    for ev in all_events:
        summary = str(
            ev.get("summary", "") or ev.get("title", "") or ev.get("description", "")
        ).upper()

        # FILTRO ANTI-SIDEBAR/FOOTER (anche per API events)
        if "SALDO" in summary or "RESIDUO" in summary or "TOTALE" in summary:
            continue
        if "PERMESSI DEL" in summary:
            continue

        # Filtra per mese (se c'è data)
        start = ev.get("startTime", "") or ev.get("start", "") or ev.get("date", "")
        if start and len(str(start)) >= 7:
            try:
                ev_month = int(str(start)[5:7])
                if ev_month != mese_num:
                    continue
            except:
                pass

        # Categorizza (supporto per logica Anomaly Zucchetti)
        summary_norm = summary.upper()
        is_omessa = (
            any(k in summary_norm for k in ["OMESSA", "OMT", "MANCATA", "ANOMALIA"])
            or ev.get("isAnomaly") == True
            or ev.get("warning")
            or ev.get("type") == "Anomaly"
        )

        if is_omessa:
            result["events_by_type"]["OMESSA TIMBRATURA"] = (
                result["events_by_type"].get("OMESSA TIMBRATURA", 0) + 1
            )
            result["items"].append(f"⚠️ OMESSA: {summary[:50]}")
        elif "FERIE" in summary_norm or "FEP" in summary_norm:
            result["events_by_type"]["FERIE"] = (
                result["events_by_type"].get("FERIE", 0) + 1
            )
            result["items"].append(f"🏖️ FERIE: {summary[:50]}")
        elif "MALATTIA" in summary or "MAL" in summary:
            result["events_by_type"]["MALATTIA"] = (
                result["events_by_type"].get("MALATTIA", 0) + 1
            )
            result["items"].append(f"🤒 MALATTIA: {summary[:50]}")
        elif (
            "RIPOSO" in summary
            or "RCS" in summary
            or "RIC" in summary
            or "RPS" in summary
            or "REC" in summary
        ):
            result["events_by_type"]["RIPOSO"] = (
                result["events_by_type"].get("RIPOSO", 0) + 1
            )
            result["items"].append(f"💤 RIPOSO: {summary[:50]}")

    result["total_events"] = sum(result["events_by_type"].values())
    result["debug"].append(f"📊 Totale categorizzati: {result['total_events']}")
    result["success"] = True  # Flag Esplicito di Successo

    return result


def read_agenda_api(context, mese_num, anno):
    """Fallback: Legge l'agenda tramite chiamate API dirette."""
    result = {
        "events_by_type": {},
        "total_events": 0,
        "items": [],
        "debug": ["📡 Tentativo API dirette..."],
        "success": False,
    }

    base_url = "https://selfservice.gottardospa.it/js_rev/JSipert2"
    
    # Mappa codici API -> chiavi normalizzate (coerenti con il resto del codice)
    CODE_TO_NORMALIZED = {
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
                                        result["items"].append(
                                            f"{code}: {ev.get('summary', name)}"
                                        )
                                except:
                                    pass

                        if month_events:
                            # Usa chiave normalizzata
                            normalized_key = CODE_TO_NORMALIZED.get(code, name)
                            result["events_by_type"][normalized_key] = (
                                result["events_by_type"].get(normalized_key, 0) + len(month_events)
                            )
                            result["total_events"] += len(month_events)
                            result["debug"].append(
                                f"  ✅ {code}: {len(month_events)} eventi"
                            )
                except Exception as e:
                    result["debug"].append(f"  ❌ {code} parse error: {e}")
        except Exception as e:
            result["debug"].append(f"  ⚠️ {code}: {type(e).__name__}")

    if result["total_events"] > 0:
        result["success"] = True
    
    return result


# ==============================================================================
# SCRAPER CORE
# ==============================================================================
def _first_locator(page, selectors):
    """Ritorna il primo locator che esiste (count>0), altrimenti None."""
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                return loc.first
        except Exception:
            pass
    return None


def resilient_login(page, user: str, pwd: str, timeout_ms: int = 45000):
    """
    Login robusto:
    - preferisce attributi stabili (name=...),
    - fallback su selettori alternativi se il DOM cambia,
    - submit robusto (click submit se presente, altrimenti Enter),
    - verifica successo con segnali post-login più stabili di un testo.
    """
    login_url = "https://selfservice.gottardospa.it/js_rev/JSipert2?r=y"
    page.goto(login_url, wait_until="domcontentloaded")

    user_field = _first_locator(page, [
        'input[name="username"]',
        'input#username',
        'input[type="email"]',
        'input[type="text"][name*="user"]',
        'input[type="text"]',
    ])
    pwd_field = _first_locator(page, [
        'input[name="password"]',
        'input#password',
        'input[type="password"]',
    ])

    if not user_field or not pwd_field:
        raise Exception("Campi login non trovati (pagina o DOM cambiati).")

    user_field.fill(user)
    pwd_field.fill(pwd)

    submit = _first_locator(page, [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Accedi")',
        'button:has-text("Login")',
        'button:has-text("Entra")',
    ])

    if submit:
        submit.click()
    else:
        pwd_field.press("Enter")

    # Attendi esito (successo / schermata intermedia)
    import time
    t0 = time.time()

    while (time.time() - t0) * 1000 < timeout_ms:
        # Successo: menu/elementi post-login
        try:
            if page.locator("#revitnavigationNavHoverItem0label").count() > 0:
                return True
        except Exception:
            pass

        # Altri segnali tipici post-login (fallback)
        try:
            if page.locator("text=I miei dati").count() > 0:
                return True
        except Exception:
            pass

        # Schermata intermedia comune
        try:
            if page.locator("text=Cambia Password").count() > 0:
                raise Exception("Login bloccato: richiesta cambio password / schermata intermedia.")
        except Exception:
            raise

        time.sleep(0.25)

    raise Exception("Timeout login: nessun segnale di successo.")

def execute_download(mese_nome, anno, user, pwd, is_13ma):
    """Scarica busta paga, cartellino e legge agenda."""
    results = {"busta": None, "cart": None, "agenda": None}

    try:
        idx = MESI_IT.index(mese_nome) + 1
    except:
        return results

    suffix = "_13" if is_13ma else ""
    local_busta = os.path.abspath(f"busta_{idx}_{anno}{suffix}.pdf")
    local_cart = os.path.abspath(f"cartellino_{idx}_{anno}.pdf")
    target_busta = f"Tredicesima {anno}" if is_13ma else f"{mese_nome} {anno}"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-gpu"]
        )
        ctx = browser.new_context(
            accept_downloads=True, user_agent="Mozilla/5.0 Chrome/120.0.0.0"
        )
        ctx.set_default_timeout(45000)
        page = ctx.new_page()
        page.set_viewport_size({"width": 1920, "height": 1080})

        try:
            # === LOGIN ===
            st.toast("🔐 Login...", icon="🔐")
            try:
                resilient_login(page, user, pwd, timeout_ms=45000)
            except Exception as e:
                st.error(f"Login fallito: {e}")
                browser.close()
                return results

# === AGENDA CON NAVIGAZIONE ===
            st.toast("🗓️ Lettura Agenda...", icon="🗓️")
            try:
                # Prima prova con navigazione al calendario
                results["agenda"] = read_agenda_with_navigation(page, ctx, idx, anno)
                if results["agenda"]["total_events"] == 0:
                    # Fallback: API dirette
                    results["agenda"] = read_agenda_api(ctx, idx, anno)

                if results["agenda"]["total_events"] > 0:
                    st.toast(
                        f"✅ Agenda: {results['agenda']['total_events']} eventi",
                        icon="📅",
                    )
            except Exception as e:
                results["agenda"] = {
                    "events_by_type": {},
                    "total_events": 0,
                    "debug": [str(e)],
                }

            # === BUSTA PAGA ===
            st.toast("💰 Scarico Busta...", icon="💰")
            try:
                # 1) Clicca "I miei dati"
                try:
                    page.keyboard.press("Escape")
                    time.sleep(0.3)
                except:
                    pass

                try:
                    page.evaluate(
                        "document.getElementById('revit_navigation_NavHoverItem_0_label')?.click()"
                    )
                except:
                    page.locator("text=I miei dati").first.click(force=True)
                time.sleep(2)

                # 2) Tab "Documenti"
                try:
                    page.wait_for_selector("span[id^='lnktab_']", timeout=10000)
                except:
                    pass

                for js_id in ["lnktab_2_label", "lnktab_2"]:
                    try:
                        page.evaluate(f"document.getElementById('{js_id}')?.click()")
                        break
                    except:
                        continue

                try:
                    page.locator(
                        "span", has_text=re.compile(r"\bDocumenti\b", re.I)
                    ).first.click(force=True)
                except:
                    pass
                time.sleep(2)

                # 3) Espandi "Cedolino"
                try:
                    page.wait_for_selector("text=Cedolino", timeout=10000)
                except:
                    pass

                try:
                    page.locator("tr", has=page.locator("text=Cedolino")).locator(
                        ".z-image"
                    ).click(timeout=5000)
                except:
                    page.locator("text=Cedolino").first.click(force=True)
                time.sleep(4)

                # 4) Cerca e clicca link
                with page.expect_download(timeout=25000) as dl_info:
                    if is_13ma:
                        page.get_by_text(
                            re.compile(f"Tredicesima.*{anno}", re.I)
                        ).first.click()
                    else:
                        links = page.locator("a")
                        total = links.count()
                        found = False
                        patterns = [
                            f"{mese_nome} {anno}",
                            f"{idx:02d}/{anno}",
                            f"{idx:02d}-{anno}",
                        ]

                        for i in range(total):
                            try:
                                txt = (links.nth(i).inner_text() or "").strip()
                                if not txt or len(txt) < 4:
                                    continue
                                if "Tredicesima" in txt or "13" in txt:
                                    continue

                                for pat in patterns:
                                    if pat.lower() in txt.lower():
                                        links.nth(i).click()
                                        found = True
                                        break
                                if found:
                                    break
                            except:
                                continue

                        if not found:
                            for i in range(total):
                                try:
                                    txt = links.nth(i).inner_text() or ""
                                    if (
                                        mese_nome.lower() in txt.lower()
                                        and str(anno) in txt
                                    ):
                                        if "Tredicesima" not in txt:
                                            links.nth(i).click()
                                            found = True
                                            break
                                except:
                                    continue

                        if not found:
                            raise Exception("Link busta non trovato")

                dl_info.value.save_as(local_busta)
                if os.path.exists(local_busta) and os.path.getsize(local_busta) > 1000:
                    results["busta"] = local_busta
                    st.toast(
                        f"✅ Busta: {os.path.getsize(local_busta):,} bytes", icon="📄"
                    )

            except Exception as e:
                st.warning(f"⚠️ Busta: {e}")

            # === CARTELLINO ===
            if not is_13ma:
                st.toast("📅 Scarico Cartellino...", icon="📅")
                try:
                    # Torna home
                    try:
                        page.keyboard.press("Escape")
                        time.sleep(0.3)
                    except:
                        pass

                    try:
                        logo = page.locator("img[src*='logo'], .logo").first
                        if logo.is_visible(timeout=2000):
                            logo.click()
                            time.sleep(2)
                    except:
                        page.goto(
                            "https://selfservice.gottardospa.it/js_rev/JSipert2",
                            wait_until="domcontentloaded",
                        )
                        time.sleep(3)

                    # Time menu
                    try:
                        page.evaluate(
                            "document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()"
                        )
                    except:
                        page.locator("text=Time").first.click(force=True)
                    time.sleep(3)

                    # Tab Cartellino presenze
                    try:
                        page.evaluate(
                            "document.getElementById('lnktab_5_label')?.click()"
                        )
                    except:
                        page.locator("text=Cartellino").first.click(force=True)
                    time.sleep(5)

                    # Date
                    last_day = calendar.monthrange(anno, idx)[1]
                    d1, d2 = f"01/{idx:02d}/{anno}", f"{last_day}/{idx:02d}/{anno}"

                    dal = page.locator(
                        "input[id*='CLRICHIE'][class*='dijitInputInner']"
                    ).first
                    al = page.locator(
                        "input[id*='CLRICHI2'][class*='dijitInputInner']"
                    ).first

                    if dal.count() > 0 and al.count() > 0:
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

                    # Ricerca
                    try:
                        page.locator(
                            "//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']"
                        ).last.click(force=True)
                    except:
                        page.get_by_role(
                            "button", name=re.compile("ricerca|esegui", re.I)
                        ).last.click()
                    time.sleep(8)

                    # Icona PDF
                    pattern_cart = f"{idx:02d}/{anno}"
                    riga = page.locator(f"tr:has-text('{pattern_cart}')").first

                    if (
                        riga.count() > 0
                        and riga.locator("img[src*='search']").count() > 0
                    ):
                        icona = riga.locator("img[src*='search']").first
                    else:
                        icona = page.locator("img[src*='search']").first

                    if icona.count() > 0:
                        with ctx.expect_page(timeout=20000) as popup_info:
                            icona.click()
                        popup = popup_info.value

                        # Attendi URL PDF
                        t0 = time.time()
                        last_url = popup.url
                        while time.time() - t0 < 15:
                            u = popup.url
                            if u and u != "about:blank":
                                last_url = u
                                if "SERVIZIO=JPSC" in u:
                                    break
                            time.sleep(0.25)

                        # Download PDF
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
                        else:
                            try:
                                popup.pdf(path=local_cart, format="A4")
                                if (
                                    os.path.exists(local_cart)
                                    and os.path.getsize(local_cart) > 5000
                                ):
                                    results["cart"] = local_cart
                            except:
                                pass

                        try:
                            popup.close()
                        except:
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
            except:
                pass
    if deleted:
        st.caption(f"🗑️ Eliminati: {', '.join(deleted)}")


# ==============================================================================
# UI
# ==============================================================================
st.title("💶 Gottardo Payroll Analyzer")

# Credenziali
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
    # Barra azioni
    col_u, col_m, col_a, col_btn, col_rst = st.columns([1, 1.5, 1, 1.5, 0.5])
    col_u.markdown(f"**👤 {u}**")
    m = col_m.selectbox("Mese", MESI_IT, index=9)  # Ottobre default
    a = col_a.selectbox("Anno", [2024, 2025, 2026], index=1)

    tipo = "Cedolino"
    if m == "Dicembre":
        tipo = col_m.radio("Tipo", ["Cedolino", "Tredicesima"], horizontal=True)

    if col_btn.button("🚀 ANALIZZA", type="primary"):
        is_13 = tipo == "Tredicesima"

        with st.status("🔄 Elaborazione...", expanded=True):
            # Download
            paths = execute_download(m, a, u, pw, is_13)

            # Analisi AI
            st.write("🧠 Analisi AI...")
            res_b = parse_busta_dettagliata(paths["busta"])
            res_c = (
                parse_cartellino_dettagliato(paths["cart"])
                if not is_13 and paths["cart"]
                else {}
            )

            # Salva risultati
            st.session_state["res"] = {
                "busta": res_b,
                "cart": res_c,
                "agenda": paths.get("agenda", {}),
                "is_13": is_13,
                "mese": m,
                "anno": a,
            }

            # Pulizia
            cleanup_files(paths.get("busta"), paths.get("cart"))

    if col_rst.button("🔄"):
        st.session_state.clear()
        st.rerun()

# ==============================================================================
# RISULTATI
# ==============================================================================
if "res" in st.session_state:
    data = st.session_state["res"]
    b = data["busta"]
    c = data["cart"]
    agenda = data.get("agenda", {})
    is_13 = data["is_13"]

    dg = b.get("dati_generali", {})
    comp = b.get("competenze", {})
    tratt = b.get("trattenute", {})
    ferie = b.get("ferie", {})
    par = b.get("par", {})

    # === CONTROLLO INCROCIATO DINAMICO (BUSTA vs CARTELLINO vs AGENDA) ===
    import calendar
    from datetime import date

    # 0. Recupero e calcolo parametri del mese (Universale)
    anno = data.get("anno", 2025)  # Usa valore salvato in sessione, fallback 2025
    mese_nome = data.get("mese", "Ottobre")
    mese_num = MESI_IT.index(mese_nome) + 1

    _, total_days_month = calendar.monthrange(anno, mese_num)
    nome_mese = calendar.month_name[mese_num].capitalize()

    # Inizializza variabili agenda (disponibili sempre)
    a_evs = agenda.get("events_by_type", {}) if isinstance(agenda, dict) else {}
    a_omesse = a_evs.get("OMESSA TIMBRATURA", 0)
    a_ferie = a_evs.get("FERIE", 0)
    a_malattia = a_evs.get("MALATTIA", 0)
    a_riposi = a_evs.get("RIPOSO", 0)

    if not is_13:
        if not c:
            c = {}

        # =====================================================================
        # DATI DAL CARTELLINO (AI parsing)
        # =====================================================================
        c_lavorati = c.get("giorni_lavorati", 0)
        c_ore_lavorate = c.get("ore_lavorate", 0)
        c_omesse = c.get("omesse_timbrature", 0)
        c_riposi = c.get("riposi", 0)
        c_festivita = c.get("festivita", 0)
        c_malattia = c.get("malattia", 0)

        c_ferie = c.get("ferie", 0)

        # =====================================================================
        # DATI DALLA BUSTA (ore ferie/permessi)
        # =====================================================================
        assenze_busta = b.get("assenze_mese", {})
        def safe_float(val):
            try:
                if isinstance(val, str):
                    val = val.replace(",", ".")
                return float(val)
            except (ValueError, TypeError):
                return 0.0

        ore_ferie_busta = safe_float(assenze_busta.get("ore_ferie", 0))
        ore_permessi_busta = safe_float(assenze_busta.get("ore_permessi", 0))
        gg_malattia_busta = safe_float(assenze_busta.get("gg_malattia", 0))
        gg_festivita_busta = safe_float(assenze_busta.get("gg_festivita", 0))
        
        # 1. Calcolo dinamico ore/giorno (0253 / 0265)
        c_ore_tot = c.get("ore_lavorate", 0) or 0.0
        c_gg_tot  = c.get("giorni_footer", 0) or c.get("giorni_lavorati", 0) or 0.0
        
        ore_giorno_eff = 8.0
        if c_ore_tot > 0 and c_gg_tot > 0:
            ore_giorno_eff = c_ore_tot / c_gg_tot
            
        # 2. Converti assenze in giorni
        gg_ferie_busta_reali = ore_ferie_busta / ore_giorno_eff
        gg_permessi_busta_reali = ore_permessi_busta / ore_giorno_eff
        
        ore_assenze_busta = ore_ferie_busta + ore_permessi_busta
        gg_assenze_busta = gg_ferie_busta_reali + gg_permessi_busta_reali
        
        # Malattia: Busta > Cartellino
        if gg_malattia_busta > 0:
             gg_malattia = gg_malattia_busta
        else:
             c_mal_val = c.get("ore_malattia_footer", 0) or c.get("malattia", 0)
             gg_malattia = c_mal_val / ore_giorno_eff if c_mal_val > 5 else c_mal_val
             
        # Festività: Busta > Cartellino
        if gg_festivita_busta > 0:
             c_festivita = gg_festivita_busta
        else:
             c_fest_val = c.get("ore_festivita_footer", 0) / ore_giorno_eff if c.get("ore_festivita_footer") else c_festivita
             if c_fest_val > 0 and (c_festivita == 0 or abs(c_fest_val - c_festivita) > 0.5):
                 # Limite di sicurezza per evitare errori macroscopici (es. 15 gg festivi)
                 c_festivita = c_fest_val if c_fest_val < 5 else c_festivita

        # Permessi
        gg_permessi = gg_permessi_busta_reali
# =====================================================================
        # DATI DALL'AGENDA (FONTE PRIMARIA PER LE FERIE!)
        # L'agenda mostra i giorni REALI di ferie (linee gialle)
        # =====================================================================
        a_ferie = a_evs.get("FERIE", 0)
        a_omesse = a_evs.get("OMESSA TIMBRATURA", 0)
        a_riposi = a_evs.get("RIPOSO", 0)
        a_malattia = a_evs.get("MALATTIA", 0)

        # PRIORITÀ FONTI SECONDO RICHIESTA UTENTE:
        # 1. FERIE: Busta Paga (Documento Ufficiale)
        # 2. OMESSE: Solo Agenda (Dato informativo)
        
        gg_ferie_effettive = 0
        use_source_ferie = "Busta" # Label for UI

        # LOGICA FERIE: Priorità Busta > Cartellino
        c_ferie_val = c.get("ore_ferie_footer", 0) / ore_giorno_eff if c.get("ore_ferie_footer") else c_ferie

        if gg_assenze_busta > 0:
            gg_ferie_effettive = gg_assenze_busta
            # Info se c'è discrepanza con Cartellino
            if abs(c_ferie_val - gg_ferie_effettive) > 0.1:
                 st.info(f"ℹ️ Ferie prese dalla Busta ({gg_ferie_effettive:.2f} gg) come da documento ufficiale (Cartellino indica {c_ferie_val:.2f}).")
        elif c_ferie_val > 0:
            gg_ferie_effettive = c_ferie_val
            use_source_ferie = "Cartellino"
        elif a_ferie > 0:
            gg_ferie_effettive = a_ferie
            use_source_ferie = "Agenda"

        # =====================================================================
        # CONSOLIDAMENTO OMESSE TIMBRATURE
        # SOLO DALL'AGENDA. Il cartellino non fa testo per le omesse.
        # =====================================================================
        final_omesse = a_omesse

        # =====================================================================
        # CALCOLO GG INPS (VERIFICA PRINCIPALE)
        # =====================================================================
        gg_pagati_busta = dg.get("giorni_pagati", 0)  # GG. INPS dalla busta
        
        c_lavorati_eff = c_lavorati + final_omesse

        # Totale calcolato (somma componenti)
        # Se gg_malattia è già in giorni (valore basso), lo usiamo direttamente.
        tot_calcolato = c_lavorati_eff + gg_ferie_effettive + gg_malattia + c_festivita
        
        # Differenza
        diff_gg = tot_calcolato - gg_pagati_busta

        # =====================================================================
        # VISUALIZZAZIONE RIEPILOGO
        # =====================================================================
        st.markdown("---")
        st.subheader(f"📊 Verifica {nome_mese} {anno}")
        
        # Metriche principali
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("📅 GG INPS (Busta)", gg_pagati_busta)
        col2.metric("📋 GG Calcolati", f"{tot_calcolato:.2f}", delta=f"{diff_gg:+.2f}" if diff_gg != 0 else None, help=f"Lavorati ({c_lavorati}) + Omesse ({final_omesse}) + Ferie/Perm + Mal + Fest")
        col3.metric("👔 Lavorati + Omesse", f"{c_lavorati_eff}", help=f"{c_lavorati} (Cartellino) + {final_omesse} (Omesse)")
        col4.metric("⚠️ Omesse (Agenda)", final_omesse, help="Giorni lavorati ma non timbrati")

        # Dettaglio assenze (Restyling 5 colonne)
        c5, c6, c7, c8, c9 = st.columns(5)
        
        lbl_ferie = "Assenze Totali"
        val_ferie = gg_ferie_effettive
        help_ferie = ""
        
        if use_source_ferie == "Agenda":
            lbl_ferie = "🏖️ Ferie (Agenda)"
            help_ferie = "Dati rilevati dal calendario"
            c5.metric(lbl_ferie, f"{val_ferie:.2f}", help=help_ferie)
            c6.metric("📋 Permessi", "0.00") # Agenda non distingue bene ferie/permessi spesso

        elif use_source_ferie == "Cartellino":
            lbl_ferie = "🏖️ Ferie (Cartellino)"
            help_ferie = "Giorni 'FER' contati dal cartellino"
            c5.metric(lbl_ferie, f"{val_ferie:.2f}", help=help_ferie)
            c6.metric("📋 Permessi", "0.00")

        else:
            # BUSTA (Source of Truth)
            lbl_ferie = "🏖️ Assenze (Busta)"
            help_ferie = f"Ferie + Permessi ({ore_giorno_eff:.2f} h/gg calcolate su {c_gg_tot}gg lavorati)"
            
            # Colonna 5: Totale Assenze
            c5.metric(lbl_ferie, f"{val_ferie:.2f}", help=help_ferie)
            
            # Colonna 6: Ferie (Breakdown)
            c6.metric("🏖️ Ferie", f"{gg_ferie_busta_reali:.2f}", help=f"{ore_ferie_busta} ore")
            
            # Colonna 7: Permessi (Breakdown)
            c7.metric("📋 Permessi", f"{gg_permessi:.2f}", help=f"{ore_permessi_busta} ore")

        # Riempimento altre colonne se non usate sopra (Agenda/Cartellino usano c5 e c6, Busta usa c5,c6,c7)
        if use_source_ferie != "Busta":
             # Se non siamo in Busta mode, shiftiamo per riempire
             c7.metric("⠀", "⠀") # Spacer

        c8.metric("🤒 Malattia", f"{gg_malattia:.2f}")
        
        # Colonna 9: Festività (solide) + Riposi (info)
        c9.metric("🎉 Festività", f"{c_festivita}", help=f"Inclusi nel totale. Riposi ({c_riposi}) esclusi.")

        # Mostra dettaglio ore dalla busta se disponibile
        if ore_ferie_busta > 0 or ore_permessi_busta > 0:
            st.caption(
                f"📋 Dettaglio Busta: {ore_ferie_busta:.2f}h ferie + {ore_permessi_busta:.2f}h permessi = "
                f"{ore_assenze_busta:.2f}h ({gg_assenze_busta:.2f} gg)"
            )

        st.markdown("---")

        # =====================================================================
        # VERIFICA COERENZA GG INPS
        # =====================================================================
        if gg_pagati_busta > 0:
            if abs(diff_gg) == 0:
                msg_parts = [f"Lavorati Cartellino ({c_lavorati})"]
                if final_omesse > 0: msg_parts.append(f"Omesse ({final_omesse})")
                if gg_ferie_effettive > 0: msg_parts.append(f"Ferie ({gg_ferie_effettive})")
                if gg_malattia > 0: msg_parts.append(f"Malattia ({gg_malattia})")
                if c_festivita > 0: msg_parts.append(f"Festività ({c_festivita})")
                
                st.success(
                    f"✅ **DATI COERENTI** — GG INPS ({gg_pagati_busta}) = {(' + '.join(msg_parts))}"
                )
            elif diff_gg > 0:
                # Caso diff_gg > 0 (Es: Busta 24, Calcolato 28, Diff +4)
                # Possibile SOVRAPPOSIZIONE: I giorni del cartellino (21) includono le "Omesse" (4) (i giorni Vxx),
                # ma noi abbiamo aggiunto anche le Ferie Busta (6). Se le Vxx SONO Ferie, le abbiamo contate 2 volte.
                sovrapposizione = abs(diff_gg)
                if abs(sovrapposizione - final_omesse) <= 1:
                     st.success(
                        f"✅ **DATI COERENTI CON SOVRAPPOSIZIONE**: Il totale calcolato ({tot_calcolato}) supera la Busta di {sovrapposizione} giorni. "
                        f"Questo accade perché i **{final_omesse} giorni di 'Omesse'** (Vxx nel cartellino) sono inclusi sia nei 'Lavorati' che nelle 'Ferie Busta'. "
                        f"Eliminando il doppio conteggio, i conti tornano ({tot_calcolato} - {sovrapposizione} = {gg_pagati_busta})."
                    )
                else:
                    st.warning(
                        f"⚠️ **DISCREPANZA (ECCESSO)**: Il cartellino indica {diff_gg} giorni IN PIÙ rispetto "
                        f"ai {gg_pagati_busta} GG INPS della busta. "
                        f"Verifica se 'Lavorati' ({c_lavorati}) e 'Ferie' ({gg_ferie_effettive}) si sovrappongono."
                    )
            elif abs(diff_gg) == 1:
                st.success(
                    f"✅ **DATI COERENTI** — Scostamento di 1 giorno (possibile arrotondamento): "
                    f"Busta {gg_pagati_busta} vs Calcolato {tot_calcolato}"
                )
            else: # This 'else' now covers diff_gg < 0
                st.error(
                    f"❌ **DISCREPANZA (DIFETTO)**: {diff_gg:+.0f} giorni! "
                    f"Busta: {gg_pagati_busta} GG INPS vs Calcolato: {tot_calcolato} "
                    f"(Lavorati {c_lavorati} + Ferie {gg_ferie_effettive} + Malattia {gg_malattia} + Fest {c_festivita})"
                )

                # Suggerimento Omesse (Difetto)
                mancanti = abs(diff_gg)
                if final_omesse >= mancanti:
                     st.info(
                        f"☝️ **Nota**: La differenza di {mancanti} giorni corrisponde alle **{final_omesse} Omesse Timbrature** rilevate in Agenda. "
                        "Poiché le omesse sono giorni lavorati, i conti tornano."
                    )
        else:
            st.info(f"ℹ️ GG INPS non disponibile dalla busta. Calcolato: {tot_calcolato} giorni.")

        # Avviso solo informativo per le omesse (senza warning errori)
        if final_omesse > 0:
            st.info(
                f"ℹ️ **Nota**: Ci sono {final_omesse} giorni lavorati con 'Omessa Timbratura' (presi dall'Agenda). "
                "Questi sono stati inclusi nel calcolo dei giorni lavorati totali."
            )

        # =====================================================================
        # INFO RIPOSI (non contano come GG INPS)
        # =====================================================================
        if c_riposi > 0 or a_riposi > 0:
            riposi_totali = max(c_riposi, a_riposi)
            st.caption(
                f"💤 {riposi_totali} riposi (domeniche + compensativi) — non contano come GG INPS"
            )

    elif is_13:
        if b.get("e_tredicesima"):
            st.success("🎄 **TREDICESIMA ANALIZZATA**")
        else:
            st.info("📄 Cedolino analizzato")

    st.divider()

    # === TABS ===
    tab1, tab2, tab4 = st.tabs(["💰 Stipendio", "📅 Cartellino", "🏖️ Ferie/PAR"])

    # Helper per formattazione sicura
    def safe_float_val(val):
        try:
            if isinstance(val, str):
                val = val.replace(",", ".").replace("€", "").strip()
            return float(val)
        except (ValueError, TypeError):
            return 0.0

    # Sanitizza dati finanziari
    netto = safe_float_val(dg.get("netto", 0))
    lordo = safe_float_val(comp.get("lordo_totale", 0))
    base = safe_float_val(comp.get("base", 0))
    anzianita = safe_float_val(comp.get("anzianita", 0))
    straordinari = safe_float_val(comp.get("straordinari", 0))
    festivita_val = safe_float_val(comp.get("festivita", 0))
    inps = safe_float_val(tratt.get("inps", 0))
    irpef = safe_float_val(tratt.get("irpef_netta", 0))
    addizionali = safe_float_val(tratt.get("addizionali", 0))

    with tab1:
        # Paga, Giorni e Ore in una riga
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("💵 NETTO", f"€ {netto:,.2f}")
        k2.metric("📊 Lordo", f"€ {lordo:,.2f}")
        k3.metric("📆 Giorni Pagati", dg.get("giorni_pagati", 0))
        k4.metric("⏱️ Ore ordinarie (Busta)", dg.get("ore_ordinarie", 0))


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

    with tab2:
        if c:
            # Usa direttamente i dati CONSOLIDATI (come nel riepilogo in alto)
            # c_lavorati, gg_ferie_effettive, gg_malattia, final_omesse, ecc.
            
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("👔 Lavorati", c_lavorati, help=f"Ore Totali: {c.get('ore_lavorate', 0)}")
            
            # Label dinamico (basato sulla fonte ferie)
            if use_source_ferie == "Agenda":
                label_ferie_tab = "🏖️ Ferie (Agenda)"
            elif use_source_ferie == "Cartellino":
                label_ferie_tab = "🏖️ Ferie (Cartellino)"
            else:
                label_ferie_tab = "🏖️ Ferie (Busta)"
            
            k2.metric(label_ferie_tab, gg_ferie_effettive)
            
            k3.metric("🤒 Malattia", gg_malattia)
            k4.metric("⚠️ Omesse", final_omesse)

            st.markdown("---")

            k5, k6, k7 = st.columns(3)
            # Mostra permessi (se non inglobati in Agenda) o 0
            val_permessi = gg_permessi if not (agenda.get("success") and a_ferie > 0) else 0
            k5.metric("📋 Permessi", val_permessi, help="Inclusi nelle Ferie se da Agenda")
            
            k6.metric("💤 Riposi", c_riposi)
            k7.metric("🎉 Festività", c_festivita)

            if c.get("note"):
                st.info(f"📝 {c['note']}")
        else:
            st.info(
                "Cartellino non disponibile"
                if not is_13
                else "Non applicabile per Tredicesima"
            )

    # Tab 3 (Agenda) rimosso su richiesta utente (confluito in Cartellino)

    with tab4:
        c1, c2 = st.columns(2)

        with c1:
            st.subheader("🏖️ Ferie")
            f1, f2 = st.columns(2)
            f1.metric("Residue AP", f"{safe_float_val(ferie.get('residue_ap', 0)):.2f}")
            f2.metric("Maturate", f"{safe_float_val(ferie.get('maturate', 0)):.2f}")
            f3, f4 = st.columns(2)
            f3.metric("Godute", f"{safe_float_val(ferie.get('godute', 0)):.2f}")
            f4.metric("Saldo", f"{safe_float_val(ferie.get('saldo', 0)):.2f}")

        with c2:
            st.subheader("⏱️ Permessi (PAR)")
            p1, p2 = st.columns(2)
            p1.metric("Residui AP", f"{safe_float_val(par.get('residue_ap', 0)):.2f}")
            p2.metric("Spettanti", f"{safe_float_val(par.get('spettanti', 0)):.2f}")
            p3, p4 = st.columns(2)
            p3.metric("Fruite", f"{safe_float_val(par.get('fruite', 0)):.2f}")
            p4.metric("Saldo", f"{safe_float_val(par.get('saldo', 0)):.2f}")
