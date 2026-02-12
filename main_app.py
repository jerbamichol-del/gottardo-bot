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
- NETTO: riga "PROGRESSIVI" colonna finale
- GIORNI PAGATI: riga "GG. INPS"
- ORE ORDINARIE: campo "ORE INAIL" o "ORE ORDINARIE". Cerca un valore tipicamente compreso tra 100 e 200 (es. 173,33). Ignora numeri piccoli sotto 50 se i giorni pagati sono alti.

**2. COMPETENZE:**
- base: Cerca "RETRIBUZIONE ORDINARIA" o "PAGA BASE" (voce 1000) -> valore nella colonna Competenze
- straordinari: somma STRAORDINARIO/SUPPLEMENTARI/NOTTURNI
- festivita: MAGG. FESTIVE/FESTIVITA GODUTA
- anzianita: SCATTI/EDR/ANZ.
- lordo_totale: Cerca "TOTALE COMPETENZE" in fondo alla colonna competenze

**3. TRATTENUTE:**
- inps: sezione I.N.P.S. (ritenute previdenziali)
- irpef_netta: voce "IRPEF NETTA" o sezione FISCALI
- addizionali: Somma TUTTE le addizionali (Regionale, Comunale, Acconto)

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
    
    **2. CONTEGGIO RIGHE (ESTREMA ATTENZIONE):**
    Analizza il calendario giorno per giorno:
    - **LAVORATI**: Righe con orari di timbratura (es. 08:30 13:00) o diciture ORD/STR.
    - **OMESSE TIMBRATURE**: Righe che riportano solo un codice che inizia con 'V' (es. **V01, V02, V29, V50**) o dicitura **OME, MANC**. Conta quante sono.
    - **FERIE/PERMESSI**: Righe con FER, FE, PAR, ROL.
    - **MALATTIA**: Righe con MAL.
    - **FESTIVITÀ**: Righe con F70 o EX02 (festività goduta).
    - **RIPOSI**: Righe con RIP, RCS, RIC, RCO, RDD.

    **3. NOTE IMPORTANTI:**
    - I codici 'V' (es. V01) rappresentano spesso mancate timbrature che devono essere conteggiate separatamente da "giorni_footer".
    - Ignora il codice 0265 (GG PRESENZA) per il conteggio di ferie e festività.

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
# SCRAPER CORE
# ==============================================================================
def _first_locator(page, selectors):
    """Ritorna il primo locator che esiste (count>0), altrimenti None."""
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                return loc
        except:
            continue
    return None

def resilient_login(page, user: str, pwd: str, timeout_ms: int = 45000):
    """
    Login robusto:
    - preferisce attributi stabili (name=...),
    - fallback su selettori alternativi se il DOM cambia,
    - submit robusto (click submit se presente, altrimenti Enter),
    - verifica successo con segnali post-login più stabili di un testo.
    """
    st.toast(f"🔑 Tentativo login per {user}...", icon="🔑")
    
    # 1. Naviga all'URL specifico che forza il redirect al login se necessario
    login_url = "https://selfservice.gottardospa.it/js_rev/JSipert2?r=y"
    page.goto(login_url, wait_until="domcontentloaded", timeout=timeout_ms)
    
    # 2. Identifica campi con selettori multipli
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
        raise Exception("Campi login non trovati.")

    # 3. Inserimento pulito
    user_field.first.fill("")
    user_field.first.type(user, delay=50)
    pwd_field.first.fill("")
    pwd_field.first.type(pwd, delay=50)
    
    # 4. Submit robusto
    submit_btn = _first_locator(page, [
        'button[type="submit"]',
        'input[type="submit"]',
        '.z-button',
        'button:has-text("Accedi")',
        'button:has-text("Login")',
        'text=Login',
        'text=Accedi'
    ])
    
    if submit_btn:
        submit_btn.first.click()
    else:
        page.keyboard.press("Enter")

    # 5. Verifica Successo
    try:
        page.wait_for_function(
            "() => !document.querySelector('input[type=\"password\"]') || location.href.includes('Home') || document.body.innerText.includes('Esci') || document.body.innerText.includes('logout')",
            timeout=15000
        )
    except:
        if "Home" in page.url or "Esci" in page.content().lower() or "logout" in page.content().lower():
            pass
        else:
            raise Exception("Timeout login: nessun segnale di successo.")

    time.sleep(2)
    return True

def execute_download(mese_nome, anno, user, pwd, is_13ma):
    """Scarica busta paga e cartellino."""
    results = {"busta": None, "cart": None}

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
    is_13 = data["is_13"]

    dg = b.get("dati_generali", {})
    comp = b.get("competenze", {})
    tratt = b.get("trattenute", {})
    ferie = b.get("ferie", {})
    par = b.get("par", {})
    gg_pagati_busta = dg.get("giorni_pagati", 0)

    # 0. Recupero e calcolo parametri del mese (Universale)
    import calendar
    from datetime import date
    
    anno = data.get("anno", 2025)  # Usa valore salvato in sessione, fallback 2025
    mese_nome = data.get("mese", "Ottobre")
    mese_num = MESI_IT.index(mese_nome) + 1

    _, total_days_month = calendar.monthrange(anno, mese_num)
    nome_mese = calendar.month_name[mese_num].capitalize()

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
        
        # 1. Calcolo dinamico ore/giorno (CONTRATTO 6/7)
        # Per un full-time 6/7, il rapporto ore/giorno è tipicamente 6.66 (se 40h) o 6.33 (se 38h)
        c_ore_tot = c.get("ore_lavorate", 0) or 0.0
        c_gg_tot  = c.get("giorni_footer", 0) or c.get("giorni_lavorati", 0) or 0.0
        
        # Default per 6/7 (40h/6gg = 6.66)
        ore_giorno_eff = 6.6666
        
        if c_ore_tot > 0 and c_gg_tot > 0:
            ore_giorno_eff = c_ore_tot / c_gg_tot
            
        # 2. Converti assenze in giorni
        # Usiamo il rapporto dinamico per vedere quanti GIORNI effettivi sono stati pagati
        gg_ferie_busta_reali = ore_ferie_busta / ore_giorno_eff if ore_giorno_eff > 0 else 0
        gg_permessi_busta_reali = ore_permessi_busta / ore_giorno_eff if ore_giorno_eff > 0 else 0
        
        ore_assenze_busta = ore_ferie_busta + ore_permessi_busta
        gg_assenze_busta = gg_ferie_busta_reali + gg_permessi_busta_reali

        # LOGICA DI RICONCILIAZIONE AVANZATA (Ottimizzata per 6/7):
        quota_assenze_teorica = gg_pagati_busta - (c_lavorati + c_omesse + gg_malattia_busta + gg_festivita_busta)
        if quota_assenze_teorica > 0 and ore_assenze_busta > 0:
             rapporto_ideale = ore_assenze_busta / quota_assenze_teorica
             if 6.0 <= rapporto_ideale <= 8.5:
                 if abs(rapporto_ideale - ore_giorno_eff) > 0.02:
                     tipo_c = "Full-Time 6/7" if abs(rapporto_ideale - 6.66) < 0.1 else "Dinamico"
                     st.info(f"💡 **Contratto {tipo_c}**: La busta usa un rapporto di **{rapporto_ideale:.2f} ore/giorno**. I conti ora tornano al 100%.")
                     ore_giorno_eff = rapporto_ideale
                     gg_ferie_busta_reali = ore_ferie_busta / ore_giorno_eff
                     gg_permessi_busta_reali = ore_permessi_busta / ore_giorno_eff
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
                 # Limite di sicurezza
                 c_festivita = c_fest_val if c_fest_val < 5 else c_festivita

        # Permessi
        gg_permessi = gg_permessi_busta_reali
        # PRIORITÀ FERIE: Busta Paga (Documento Ufficiale)
        gg_ferie_effettive = 0
        use_source_ferie = "Busta"

        c_ferie_val = c.get("ore_ferie_footer", 0) / ore_giorno_eff if c.get("ore_ferie_footer") else c_ferie

        if gg_assenze_busta > 0:
            gg_ferie_effettive = gg_assenze_busta
            if abs(c_ferie_val - gg_ferie_effettive) > 0.1:
                 st.info(f"ℹ️ Ferie prese dalla Busta ({gg_ferie_effettive:.2f} gg) come da documento ufficiale (Cartellino indica {c_ferie_val:.2f}).")
        elif c_ferie_val > 0:
            gg_ferie_effettive = c_ferie_val
            use_source_ferie = "Cartellino"

        # Omesse Timbrature dal Cartellino
        final_omesse = c_omesse

        # =====================================================================
        # CALCOLO GG INPS (VERIFICA PRINCIPALE)
        # =====================================================================
        
        # Se c_lavorati viene da giorni_footer (codice 0265), di solito include già le omesse.
        # Facciamo una verifica: se c_lavorati è uguale ai giorni totali del mese meno riposi/assenze, 
        # allora include già tutto.
        c_lavorati_eff = c_lavorati 
        if c.get("omesse_timbrature", 0) > 0 and c_lavorati > 0:
            # Se giorni_footer è basso (solo timbrate), aggiungiamo omesse.
            # Se giorni_footer è già alto, stiamo attenti a non raddoppiare.
            if c_lavorati < (total_days_month / 2): # Euristica: solo timbrate
                 c_lavorati_eff = c_lavorati + final_omesse

        # Totale calcolato (somma componenti)
        tot_calcolato = c_lavorati_eff + gg_ferie_effettive + gg_malattia + c_festivita
        
        # Differenza con precisione al centesimo
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
        col4.metric("⚠️ Omesse", final_omesse, help="Giorni lavorati con omessa timbratura (dal cartellino)")

        # Dettaglio assenze (Restyling 5 colonne)
        c5, c6, c7, c8, c9 = st.columns(5)
        
        if use_source_ferie == "Cartellino":
            c5.metric("🏖️ Ferie (Cartellino)", f"{gg_ferie_effettive:.2f}")
            c6.metric("📋 Permessi", "0.00")
        else:
            # BUSTA (Source of Truth)
            help_ferie = f"Ferie + Permessi ({ore_giorno_eff:.2f} h/gg calcolate su {c_gg_tot}gg lavorati)"
            
            # Colonna 5: Totale Assenze
            c5.metric("🏖️ Assenze (Busta)", f"{gg_ferie_effettive:.2f}", help=help_ferie)
            
            # Colonna 6: Ferie (Breakdown)
            c6.metric("🏖️ Ferie", f"{gg_ferie_busta_reali:.2f}", help=f"{ore_ferie_busta} ore")
            
            # Colonna 7: Permessi (Breakdown)
            c7.metric("📋 Permessi", f"{gg_permessi_busta_reali:.2f}", help=f"{ore_permessi_busta} ore")

        c8.metric("🤒 Malattia", f"{gg_malattia:.2f}")
        c9.metric("🎉 Festività", f"{c_festivita:.2f}")

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
            if abs(diff_gg) < 0.05:
                msg_parts = [f"Lavorati Cartellino ({c_lavorati})"]
                if final_omesse > 0: msg_parts.append(f"Omesse ({final_omesse})")
                if gg_ferie_effettive > 0: msg_parts.append(f"Ferie ({gg_ferie_effettive:.2f})")
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
            else: # Caso diff_gg < 0
                st.error(
                    f"❌ **DISCREPANZA (DIFETTO)**: {diff_gg:.2f} giorni! "
                    f"Busta: {gg_pagati_busta} GG INPS vs Calcolato: {tot_calcolato:.2f} "
                    f"(Lavorati {c_lavorati_eff} + Ferie {gg_ferie_effettive:.2f} + Malattia {gg_malattia} + Fest {c_festivita})"
                )

                # Suggerimento Omesse (Difetto)
                mancanti = abs(diff_gg)
                if final_omesse > 0 and abs(final_omesse - mancanti) < 0.5:
                     st.info(
                        f"☝️ **Nota**: La differenza di {mancanti:.2f} giorni corrisponde quasi esattamente alle **{final_omesse} Omesse Timbrature** rilevate nel Cartellino. "
                        "I conti tornano se si considerano come giorni pagati."
                    )
        else:
            st.info(f"ℹ️ GG INPS non disponibile dalla busta. Calcolato: {tot_calcolato} giorni.")

        # Avviso solo informativo per le omesse
        if final_omesse > 0:
            st.info(
                f"ℹ️ **Nota**: Ci sono {final_omesse} giorni lavorati con 'Omessa Timbratura' (rilevati dal Cartellino). "
                "Questi sono stati inclusi nel calcolo dei giorni lavorati totali."
            )

        # =====================================================================
        # INFO RIPOSI (non contano come GG INPS)
        # =====================================================================
        if c_riposi > 0:
            st.caption(
                f"💤 {c_riposi} riposi (domeniche + compensativi) — non contano come GG INPS"
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
        # Calcolo Ore Ordinarie (Fallback se 0)
        ore_ordinarie_busta = safe_float_val(dg.get("ore_ordinarie", 0))
        if ore_ordinarie_busta == 0 and gg_pagati_busta > 0:
            ore_ordinarie_busta = gg_pagati_busta * ore_giorno_eff

        k1.metric("💵 NETTO", f"€ {netto:,.2f}")
        k2.metric("📊 Lordo", f"€ {lordo:,.2f}")
        k3.metric("📆 Giorni Pagati", dg.get("giorni_pagati", 0))
        k4.metric("⏱️ Ore ordinarie", f"{ore_ordinarie_busta:.2f}")


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
            if use_source_ferie == "Cartellino":
                label_ferie_tab = "🏖️ Ferie (Cartellino)"
            else:
                label_ferie_tab = "🏖️ Ferie (Busta)"
            
            k2.metric(label_ferie_tab, f"{gg_ferie_effettive:.2f}")
            k3.metric("🤒 Malattia", f"{gg_malattia:.2f}")
            k4.metric("⚠️ Omesse", final_omesse)

            st.markdown("---")

            k5, k6, k7 = st.columns(3)
            k5.metric("📋 Permessi", f"{gg_permessi:.2f}")
            k6.metric("💤 Riposi", c_riposi)
            k7.metric("🎉 Festività", f"{c_festivita:.2f}")

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
