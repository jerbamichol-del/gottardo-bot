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
MESI_IT = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", 
           "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]

# Codici eventi calendario Gottardo (dallo screenshot del portale)
CALENDAR_CODES = {
    "FEP": "FERIE PIANIFICATE",           # 🟡 Giallo
    "OMT": "OMESSA TIMBRATURA",            # 🔴 Rosa/Rosso
    "RCS": "RIPOSO COMPENSATIVO SUCC",     # 🟢 Verde
    "RIC": "RIPOSO COMPENSATIVO FORZ",     # 🟢 Verde
    "MAL": "MALATTIA"                      # 🔵 Azzurro
}

# Keywords per riconoscere eventi nell'agenda (DOM parsing)
AGENDA_KEYWORDS = [
    "OMESSA TIMBRATURA", "OMESSA", "OMT",
    "MALATTIA", "MAL",
    "RIPOSO COMPENSATIVO", "RCS", "RIC",
    "FERIE PIANIFICATE", "FERIE", "FEP",
    "PERMESSO", "PAR",
    "ANOMALIA", "ASSENZA"
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
        valid = [m for m in all_models if "generateContent" in m.supported_generation_methods]
        
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
            if "flash" in n and "lite" not in n: return 0
            if "lite" in n: return 1
            if "pro" in n: return 2
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
            resp = model.generate_content([prompt, {"mime_type": "application/pdf", "data": pdf_bytes}])
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
                    {"role": "user", "content": full_prompt}
                ],
                temperature=0.1
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
- ORE ORDINARIE: "ORE INAIL" o giorni×8

**2. COMPETENZE:**
- base: "RETRIBUZIONE ORDINARIA" (voce 1000)
- straordinari: somma STRAORDINARIO/SUPPLEMENTARI/NOTTURNI
- festivita: MAGG. FESTIVE/FESTIVITA GODUTA
- anzianita: SCATTI/EDR/ANZ.
- lordo_totale: TOTALE COMPETENZE

**3. TRATTENUTE:**
- inps: sezione I.N.P.S.
- irpef_netta: sezione FISCALI
- addizionali: add.reg + add.com

**4. FERIE/PAR (tabella in alto a destra):**
- Formato: RES.PREC / SPETTANTI / FRUITE / SALDO

**5. TREDICESIMA:**
- e_tredicesima=true se trovi "TREDICESIMA"/"13MA"

Output SOLO JSON:
{
  "e_tredicesima": false,
  "dati_generali": {"netto": 0.0, "giorni_pagati": 0, "ore_ordinarie": 0.0},
  "competenze": {"base": 0.0, "anzianita": 0.0, "straordinari": 0.0, "festivita": 0.0, "lordo_totale": 0.0},
  "trattenute": {"inps": 0.0, "irpef_netta": 0.0, "addizionali": 0.0},
  "ferie": {"residue_ap": 0.0, "maturate": 0.0, "godute": 0.0, "saldo": 0.0},
  "par": {"residue_ap": 0.0, "spettanti": 0.0, "fruite": 0.0, "saldo": 0.0}
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
            "par": {"residue_ap": 0, "spettanti": 0, "fruite": 0, "saldo": 0}
        }
    return result


def parse_cartellino_dettagliato(path):
    """Parser completo cartellino presenze."""
    prompt = """
Analizza questo CARTELLINO PRESENZE GOTTARDO S.p.A.

Conta:
1. giorni_lavorati: giorni con almeno una timbratura (es 08:00-17:00)
2. ferie: FE, FERIE
3. malattia: MAL, MALATTIA
4. permessi: PERMESSO, PAR, ROL
5. riposi: RIPOSO, RIP, RECUPERO, riposo compensativo (NON sono giorni retribuiti INPS)
6. omesse_timbrature: ANOMALIA, OMESSA, OMT (NOTA: sono comunque giorni LAVORATI, solo senza timbratura registrata)
7. festivita: FESTIVO, FES

IMPORTANTE: 
- Le "omesse timbrature" sono giorni in cui si è lavorato ma manca la timbratura (badge dimenticato)
- I "riposi compensativi" NON contano come giorni INPS pagati

Output JSON:
{
  "giorni_lavorati": 0,
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
            "giorni_lavorati": 0, "ferie": 0, "malattia": 0,
            "permessi": 0, "riposi": 0, "omesse_timbrature": 0,
            "festivita": 0, "note": ""
        }
    return result


# ==============================================================================
# AGENDA - METODO MIGLIORATO CON INTERCETTAZIONE RETE
# ==============================================================================
def read_agenda_with_navigation(page, context, mese_num, anno):
    """
    Legge l'agenda navigando effettivamente al calendario e intercettando le richieste.
    Questo è più affidabile delle chiamate API dirette.
    """
    result = {
        "events_by_type": {},
        "total_events": 0,
        "items": [],
        "debug": []
    }
    
    captured_events = []
    
    # Handler per catturare risposte di rete
    def capture_calendar_response(response):
        try:
            url = response.url
            if "events" in url.lower() or "calendar" in url.lower() or "time" in url.lower():
                if response.status == 200:
                    try:
                        data = response.json()
                        if data:
                            result["debug"].append(f"📡 Catturato: {url[:80]}...")
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
            page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
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
        target_month_name = MESI_IT[mese_num - 1].upper() # es: OTTOBRE
        result["debug"].append(f"🗓️ Navigazione al mese target: {target_month_name} {anno}")
        
        cal_nav_success = False
        if calendar_frame:
            try:
                # 0. FORZA VISTA MENSILE (CRITICO!)
                # Cerca e clicca il bottone "Mese" nella toolbar principale
                result["debug"].append("  🖱️ Imposto vista MENSILE (click 'Mese')...")
                
                # Selettori per il bottone Mese
                # Cerchiamo bottoni che contengono il testo "Mese"
                month_view_btns = calendar_frame.locator(".dijitButtonText, .dijitButton").filter(has_text="Mese")
                
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
                         result["debug"].append("  ✅ Vista 'Mese' cliccata (fallback span)")
                     except:
                         result["debug"].append("  ⚠️ Bottone 'Mese' non trovato")

                time.sleep(2) # Attesa cambio vista

                # === NUOVA NAVIGAZIONE: USA FRECCE PRINCIPALI TOOLBAR (NO SIDEBAR) ===
                # 1. Assicurati Vista MENSILE
                result["debug"].append("  🖱️ Imposto vista MENSILE...")
                month_btns = calendar_frame.locator(".dijitButtonText, .dijitButtonContents").filter(has_text="Mese")
                if month_btns.count() > 0:
                    for i in range(month_btns.count()):
                        if month_btns.nth(i).is_visible():
                            try:
                                month_btns.nth(i).click()
                                time.sleep(2)
                                break
                            except: pass
                
                # Selettori per il titolo (es. "Gennaio 2026")
                # Tentativo 1: Selettori specifici Dojo/ZK
                title_selectors = [
                    ".dijitCalendarTitle", ".dojoxCalendarTitle", 
                    "#calendarTitle", ".calendarTitle",
                    "span[id*='Title']", "div[id*='Title']",
                    ".title", ".header-title"
                ]
                
                found_title = False
                title_el = None
                
                for sel in title_selectors:
                    els = calendar_frame.locator(sel)
                    if els.count() > 0:
                        for i in range(els.count()):
                            if els.nth(i).is_visible():
                                t = els.nth(i).inner_text().strip()
                                if re.search(r'\b20\d{2}\b', t): # Cerca anno (20xx)
                                    title_el = els.nth(i)
                                    found_title = True
                                    result["debug"].append(f"  ✅ Titolo trovato con sel '{sel}': {t}")
                                    break
                    if found_title: break
                
                # Tentativo 2: Ricerca testuale generica per testo che sembra una data (Mese Anno)
                if not found_title:
                     result["debug"].append("  ⚠️ Titolo non trovato con selettori, provo ricerca testo generica...")
                     # Cerca elementi che contengono l'anno corrente o target
                     # Es: "Gennaio 2026"
                     text_candidates = calendar_frame.locator("text=202").all() # Prende tutto ciò che ha "202..."
                     for el in text_candidates:
                         try:
                             if el.is_visible():
                                 txt = el.inner_text().strip() # es "Gennaio 2026" o "01/01/2026"
                                 # Deve essere breve (< 30 caratteri) per essere un titolo
                                 if len(txt) < 30 and re.search(r'[A-Za-z]+\s+20\d{2}', txt):
                                     title_el = el
                                     found_title = True
                                     result["debug"].append(f"  ✅ Titolo trovato per euristica testo: '{txt}'")
                                     break
                         except: pass

                # DIAGNOSTICA HTML SE FALLISCE ANCORA
                if not found_title:
                    result["debug"].append("  ❌ TITOLO ASSENTE. Eseguo DUMP struttura HTML...")
                    # Salva un riassunto dei div/span visibili per capire cosa c'è
                    try:
                        visible_els = calendar_frame.locator("div, span, button").all()
                        count_vis = 0
                        for el in visible_els:
                            if count_vis > 30: break
                            if el.is_visible():
                                t = el.inner_text().strip() or "[no text]"
                                if len(t) > 50: t = t[:50] + "..."
                                i_d = el.get_attribute("id") or ""
                                cls = el.get_attribute("class") or ""
                                if t != "[no text]" or i_d: # Logga solo roba utile
                                    result["debug"].append(f"    - Tag: {t} | ID: {i_d} | Class: {cls}")
                                    count_vis += 1
                    except Exception as dump_e:
                        result["debug"].append(f"    Errore dump: {dump_e}")

                # 3. Naviga Indietro/Avanti (STRATEGIA POPUP: ICONA -> MINI CAL -> FRECCE)
                # Il "Mini Calendar" si apre cliccando un DropDownButton
                
                # Cerca l'icona/bottone dropdown
                # Strategia: Trova TUTTI i candidati e clicca il primo VISIBILE
                dropdown_candidates = calendar_frame.locator(".popup-trigger, .calendar16, [widgetid^='revit_form_Button'], .dijitCalendarIcon").all()
                
                opened_popup = False
                result["debug"].append(f"  🔍 Trovati {len(dropdown_candidates)} candidati per il Dropdown. Cerco quello visibile...")
                
                for btn in dropdown_candidates:
                    try:
                        if btn.is_visible():
                            result["debug"].append(f"  🖱️ Clicco candidato visibile: {btn.get_attribute('class')}...")
                            btn.click()
                            time.sleep(2.0)
                            
                            # Verifica se si è aperto
                            if calendar_frame.locator(".dijitCalendar, .dijitCalendarPopup").last.is_visible():
                                opened_popup = True
                                break
                    except: pass
                
                if not opened_popup:
                    # Fallback: Clicca il TITOLO STESSO (spesso apre il picker)
                    result["debug"].append("  ⚠️ Nessun Dropdown visibile funzionante. Provo click su Titolo...")
                    try:
                        calendar_frame.locator(f"text={current_title_text}").first.click()
                        time.sleep(2.0)
                        if calendar_frame.locator(".dijitCalendar, .dijitCalendarPopup").last.is_visible():
                            opened_popup = True
                    except: pass
                
                # Ora cerchiamo il POPUP del calendario (spesso è un dijitPopup o dijitCalendarMenu)
                # Potrebbe essere dentro il frame o nel root. Proviamo nel frame.
                mini_cal = calendar_frame.locator(".dijitCalendar, .dijitCalendarPopup").last
                
                if mini_cal.is_visible():
                    result["debug"].append("  ✅ Mini-Calendario APERTO!")
                    
                    moves = 0
                    max_moves = 36
                    
                    # Calcolo Delta Iniziale (Dead Reckoning)
                    # Se la lettura del popup fallisce, usiamo la data letta dalla pagina principale (current_title_text)
                    months_delta = 0
                    start_y = -1; start_m = -1
                    
                    # Parsing data principale (che sappiamo funzionare: '01 feb - 28 feb 2026')
                    try:
                        # Assicuriamoci che sia UPPER
                        current_title_upper = current_title_text.upper()
                        
                        y_match = re.search(r'20\d{2}', current_title_upper)
                        if y_match: start_y = int(y_match.group(0))
                        
                        mesi = [m.upper() for m in MESI_IT]
                        for i, m in enumerate(mesi):
                            if m in current_title_upper or (len(m)>4 and m[:-1] in current_title_upper):
                                start_m = i + 1; break
                        if start_m == -1: # Try short
                             for i, m3 in enumerate([m[:3] for m in mesi]):
                                 if re.search(r'\b' + m3 + r'\b', current_title_upper):
                                     start_m = i + 1; break
                    except Exception as e_delta: 
                        result["debug"].append(f"    ⚠️ Errore calcolo delta: {e_delta}")
                    
                    if start_y != -1 and start_m != -1:
                        target_val = anno * 12 + mese_num
                        start_val = start_y * 12 + start_m
                        months_delta = target_val - start_val
                        result["debug"].append(f"  🧮 Navigazione Stimata (Dead Reckoning): Start={start_m}/{start_y}, Target={mese_num}/{anno}, Delta={months_delta}")
                    else:
                        result["debug"].append("  ⚠️ Impossibile calcolare delta mesi iniziale (Start date ignota)")

                    moves = 0
                    clicks_needed = abs(months_delta)
                    direction_is_back = months_delta < 0
                    
                    while moves <= clicks_needed + 2: # +2 buffer
                        # 3a. Leggi data (Opzionale, solo per conferma)
                        curr_title = "ERROR"
                        try:
                            # Prova a leggere per fermarci prima se funziona
                            curr_month_el = mini_cal.locator(".dijitCalendarMonthLabel").first
                            if curr_month_el.is_visible(): 
                                curr_title = curr_month_el.inner_text() + " " + mini_cal.locator(".dijitCalendarYearLabel").first.inner_text()
                            curr_title = curr_title.strip().upper()
                        except: pass
                        
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
                                arrow_sel = ".dijitCalendarDecrease" if direction_is_back else ".dijitCalendarIncrease"
                                desc = "Indietro" if direction_is_back else "Avanti"
                                
                                btn = mini_cal.locator(arrow_sel).first
                                if btn.is_visible():
                                    btn.click()
                                    result["debug"].append(f"    Blind Click {moves+1}/{clicks_needed}: {desc}")
                                else:
                                    result["debug"].append(f"    ⚠️ Bottone Blind {arrow_sel} NON VISIBILE")
                                time.sleep(0.4) # Click rapidi
                                moves += 1
                                continue
                            else:
                                 # Finito i click previsti!
                                 result["debug"].append("    🏁 Finiti click stimati. Clicco giorno per confermare...")
                                 
                                 # Clicca GIORNO
                                 days = mini_cal.locator(".dijitCalendarDateTemplate:not(.dijitCalendarPreviousMonth):not(.dijitCalendarNextMonth), .dijitCalendarCurrentMonth").all()
                                 if len(days) > 0:
                                     idx = min(15, len(days)-1)
                                     try:
                                         days[idx].click()
                                         result["debug"].append(f"    🖱️ Click giorno {idx+1}")
                                         time.sleep(4)
                                         cal_nav_success = True
                                     except: pass
                                 else:
                                     result["debug"].append("    ⚠️ Nessun giorno cliccabile trovato")
                                 break
                        else:
                            # Se delta è 0 (o ignoto), prova logica standard (con lettura fallimentare -> exit)
                            break
                        
                        moves += 1
                else:
                    result["debug"].append("  ⚠️ Popup Mini-Calendario NON APERTO dopo il click")
            except Exception as nav_err:
                result["debug"].append(f"  ❌ Errore generale navigazione: {nav_err}")
                
        # === CATTURA EVENTI DAL DOM (FALLBACK TOTALE) ===
        # Se la griglia non si trova, cerca OVUNQUE nel frame
        result["debug"].append("🔍 Avvio scraping eventi (Ricerca Globale nel Frame)...")
        
        dom_events = []
        
        if calendar_frame:
            try:
                # Url check: siamo ancora sull'agenda?
                # Aspetta body visible
                calendar_frame.locator("body").wait_for(timeout=2000)
                time.sleep(2) # Rendering finale
                
                # 1. Prova prima griglia specifica (più accurata)
                grid = calendar_frame.locator("#calendarContainer, #calendarUI_ExtendedCalendar_0").first
                
                search_area = grid if grid.is_visible() else calendar_frame.locator("body")
                src_name = "Griglia" if grid.is_visible() else "BODY (Fallback)"
                result["debug"].append(f"  Target scraping: {src_name}")

                # 2. Cerca Keyword
                keywords = ["OMESSA", "OMT", "FERIE", "FEP", "MALATTIA", "MAL", "RIPOSO", "RCS", "RIC", "RPS"]
                
                # Dizionario per evitare duplicati (stesso evento letto più volte)
                # Chiave = testo + posizione approx? No, conteggio semplice per ora.
                
                found_any = False
                for kw in ["OMESSA", "FERIE", "MALATTIA", "RIPOSO"]:
                    # Cerca elementi visibili contenenti il testo
                    # text=KW è case-insensitive e smart
                    matches = search_area.locator(f"text={kw}")
                    count = matches.count()
                    
                    real_matches = 0
                    for i in range(count):
                        el = matches.nth(i)
                        if el.is_visible():
                            # MEGA FIX: scarta se elemento è "LeftColumn" (Sidebar)
                            # Cerca antenati con classe LeftColumn
                            # Playwright non ha "has_parent" facile nei locators a cascata inversa senza xpath
                            # Usiamo bounding box? 
                            # Se x < 300 (sidebar solitamente a sx), ignoralo.
                            box = el.bounding_box()
                            if box and box['x'] < 300:
                                # result["debug"].append(f"    Scartato '{kw}' in sidebar (x={box['x']})")
                                continue
                            
                            real_matches += 1
                            if kw == "OMESSA": dom_events.append("OMESSA TIMBRATURA")
                            elif kw == "FERIE": dom_events.append("FERIE")
                            elif kw == "MALATTIA": dom_events.append("MALATTIA")
                            elif kw == "RIPOSO": dom_events.append("RIPOSO")
                    
                    if real_matches > 0:
                        result["debug"].append(f"  📝 Trovati {real_matches} x '{kw}'")
                        found_any = True
                
                if not found_any:
                     # Check testo grezzo se locator fallisce
                     full = search_area.inner_text().upper()
                     if "OMESSA" in full:
                         cnt = full.count("OMESSA")
                         result["debug"].append(f"  📝 Trovati {cnt} 'OMESSA' nel testo grezzo")
                         for _ in range(cnt): dom_events.append("OMESSA TIMBRATURA")

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
        summary = str(ev.get("summary", "") or ev.get("title", "") or ev.get("description", "")).upper()
        
        # Filtra per mese (se c'è data)
        start = ev.get("startTime", "") or ev.get("start", "") or ev.get("date", "")
        if start and len(str(start)) >= 7:
            try:
                ev_month = int(str(start)[5:7])
                if ev_month != mese_num:
                    continue
            except:
                pass
        
        # Categorizza
        if "OMESSA" in summary or "OMT" in summary:
            result["events_by_type"]["OMESSA TIMBRATURA"] = result["events_by_type"].get("OMESSA TIMBRATURA", 0) + 1
            result["items"].append(f"⚠️ OMESSA: {summary[:50]}")
        elif "FERIE" in summary or "FEP" in summary:
            result["events_by_type"]["FERIE"] = result["events_by_type"].get("FERIE", 0) + 1
            result["items"].append(f"🏖️ FERIE: {summary[:50]}")
        elif "MALATTIA" in summary or "MAL" in summary:
            result["events_by_type"]["MALATTIA"] = result["events_by_type"].get("MALATTIA", 0) + 1
            result["items"].append(f"🤒 MALATTIA: {summary[:50]}")
        elif "RIPOSO" in summary or "RCS" in summary or "RIC" in summary:
            result["events_by_type"]["RIPOSO"] = result["events_by_type"].get("RIPOSO", 0) + 1
            result["items"].append(f"💤 RIPOSO: {summary[:50]}")
    
    result["total_events"] = sum(result["events_by_type"].values())
    result["debug"].append(f"📊 Totale categorizzati: {result['total_events']}")
    
    return result


def read_agenda_api(context, mese_num, anno):
    """Fallback: Legge l'agenda tramite chiamate API dirette."""
    result = {
        "events_by_type": {},
        "total_events": 0,
        "items": [],
        "debug": ["📡 Tentativo API dirette..."]
    }
    
    base_url = "https://selfservice.gottardospa.it/js_rev/JSipert2"
    
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
                                        result["items"].append(f"{code}: {ev.get('summary', name)}")
                                except:
                                    pass
                        
                        if month_events:
                            result["events_by_type"][name] = len(month_events)
                            result["total_events"] += len(month_events)
                            result["debug"].append(f"  ✅ {code}: {len(month_events)} eventi")
                except Exception as e:
                    result["debug"].append(f"  ❌ {code} parse error: {e}")
        except Exception as e:
            result["debug"].append(f"  ⚠️ {code}: {type(e).__name__}")
    
    return result


# ==============================================================================
# SCRAPER CORE
# ==============================================================================
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
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context(accept_downloads=True, user_agent="Mozilla/5.0 Chrome/120.0.0.0")
        ctx.set_default_timeout(45000)
        page = ctx.new_page()
        page.set_viewport_size({"width": 1920, "height": 1080})
        
        try:
            # === LOGIN ===
            st.toast("🔐 Login...", icon="🔐")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y", wait_until="domcontentloaded")
            page.wait_for_selector('input[type="text"]', timeout=10000)
            page.fill('input[type="text"]', user)
            page.fill('input[type="password"]', pwd)
            page.press('input[type="password"]', "Enter")
            time.sleep(3)
            
            try:
                page.wait_for_selector("text=I miei dati", timeout=15000)
            except:
                st.error("❌ Login fallito")
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
                    st.toast(f"✅ Agenda: {results['agenda']['total_events']} eventi", icon="📅")
            except Exception as e:
                results["agenda"] = {"events_by_type": {}, "total_events": 0, "debug": [str(e)]}
            
            # === BUSTA PAGA ===
            st.toast("💰 Scarico Busta...", icon="💰")
            try:
                # 1) Clicca "I miei dati"
                try:
                    page.keyboard.press("Escape")
                    time.sleep(0.3)
                except: pass
                
                try:
                    page.evaluate("document.getElementById('revit_navigation_NavHoverItem_0_label')?.click()")
                except:
                    page.locator("text=I miei dati").first.click(force=True)
                time.sleep(2)
                
                # 2) Tab "Documenti"
                try:
                    page.wait_for_selector("span[id^='lnktab_']", timeout=10000)
                except: pass
                
                for js_id in ["lnktab_2_label", "lnktab_2"]:
                    try:
                        page.evaluate(f"document.getElementById('{js_id}')?.click()")
                        break
                    except: continue
                
                try:
                    page.locator("span", has_text=re.compile(r"\bDocumenti\b", re.I)).first.click(force=True)
                except: pass
                time.sleep(2)
                
                # 3) Espandi "Cedolino"
                try:
                    page.wait_for_selector("text=Cedolino", timeout=10000)
                except: pass
                
                try:
                    page.locator("tr", has=page.locator("text=Cedolino")).locator(".z-image").click(timeout=5000)
                except:
                    page.locator("text=Cedolino").first.click(force=True)
                time.sleep(4)
                
                # 4) Cerca e clicca link
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
                                if not txt or len(txt) < 4: continue
                                if "Tredicesima" in txt or "13" in txt: continue
                                
                                for pat in patterns:
                                    if pat.lower() in txt.lower():
                                        links.nth(i).click()
                                        found = True
                                        break
                                if found: break
                            except: continue
                        
                        if not found:
                            for i in range(total):
                                try:
                                    txt = links.nth(i).inner_text() or ""
                                    if mese_nome.lower() in txt.lower() and str(anno) in txt:
                                        if "Tredicesima" not in txt:
                                            links.nth(i).click()
                                            found = True
                                            break
                                except: continue
                        
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
                    # Torna home
                    try:
                        page.keyboard.press("Escape")
                        time.sleep(0.3)
                    except: pass
                    
                    try:
                        logo = page.locator("img[src*='logo'], .logo").first
                        if logo.is_visible(timeout=2000):
                            logo.click()
                            time.sleep(2)
                    except:
                        page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2", wait_until="domcontentloaded")
                        time.sleep(3)
                    
                    # Time menu
                    try:
                        page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
                    except:
                        page.locator("text=Time").first.click(force=True)
                    time.sleep(3)
                    
                    # Tab Cartellino presenze
                    try:
                        page.evaluate("document.getElementById('lnktab_5_label')?.click()")
                    except:
                        page.locator("text=Cartellino").first.click(force=True)
                    time.sleep(5)
                    
                    # Date
                    last_day = calendar.monthrange(anno, idx)[1]
                    d1, d2 = f"01/{idx:02d}/{anno}", f"{last_day}/{idx:02d}/{anno}"
                    
                    dal = page.locator("input[id*='CLRICHIE'][class*='dijitInputInner']").first
                    al = page.locator("input[id*='CLRICHI2'][class*='dijitInputInner']").first
                    
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
                        page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last.click(force=True)
                    except:
                        page.get_by_role("button", name=re.compile("ricerca|esegui", re.I)).last.click()
                    time.sleep(8)
                    
                    # Icona PDF
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
                                if os.path.exists(local_cart) and os.path.getsize(local_cart) > 5000:
                                    results["cart"] = local_cart
                            except: pass
                        
                        try:
                            popup.close()
                        except: pass
                        
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
            except: pass
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
        is_13 = (tipo == "Tredicesima")
        
        with st.status("🔄 Elaborazione...", expanded=True):
            # Download
            paths = execute_download(m, a, u, pw, is_13)
            
            # Analisi AI
            st.write("🧠 Analisi AI...")
            res_b = parse_busta_dettagliata(paths["busta"])
            res_c = parse_cartellino_dettagliato(paths["cart"]) if not is_13 and paths["cart"] else {}
            
            # Salva risultati
            st.session_state["res"] = {
                "busta": res_b,
                "cart": res_c,
                "agenda": paths.get("agenda", {}),
                "is_13": is_13,
                "mese": m,
                "anno": a
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
    
    # === CONTROLLO INCROCIATO ===
    if not is_13 and c:
        gg_lavorati = c.get("giorni_lavorati", 0)
        gg_ferie = c.get("ferie", 0)
        gg_malattia = c.get("malattia", 0)
        gg_permessi = c.get("permessi", 0)
        gg_riposi = c.get("riposi", 0)  # NON contano come giorni INPS pagati!
        gg_omesse = c.get("omesse_timbrature", 0)  # Sono giorni LAVORATI!
        
        # Aggiungi eventi agenda
        agenda_events = agenda.get("events_by_type", {})
        agenda_omesse = agenda_events.get("OMESSA TIMBRATURA", 0)
        agenda_ferie = agenda_events.get("FERIE", 0) or agenda_events.get("FERIE PIANIFICATE", 0)
        agenda_malattia = agenda_events.get("MALATTIA", 0)
        agenda_riposi = agenda_events.get("RIPOSO", 0)
        
        # CALCOLO CORRETTO:
        # Le OMESSE TIMBRATURE sono giorni LAVORATI (hai lavorato ma dimenticato il badge)
        # I RIPOSI COMPENSATIVI NON sono giorni pagati INPS
        tot_lavorati_effettivi = gg_lavorati + gg_omesse  # Omesse = lavorato senza timbratura
        tot_retribuiti = tot_lavorati_effettivi + gg_ferie + gg_malattia + gg_permessi
        gg_pagati = dg.get("giorni_pagati", 0)
        diff = tot_retribuiti - gg_pagati
        
        # Mostra info dettagliata
        st.info(f"""
        📊 **Riepilogo Giorni**:
        - Lavorati con badge: **{gg_lavorati}** | Omesse timbrature: **{gg_omesse}** (= lavorati senza badge)
        - **Totale giorni lavorati**: {tot_lavorati_effettivi}
        - Ferie: **{gg_ferie}** | Malattia: **{gg_malattia}** | Permessi: **{gg_permessi}**
        - Riposi compensativi: **{gg_riposi}** (non contano GG.INPS)
        - **Totale retribuiti**: {tot_retribuiti} vs **Giorni INPS pagati**: {gg_pagati}
        """)
        
        if abs(diff) <= 1:
            st.success(f"✅ **DATI COERENTI** — Retribuiti: {tot_retribuiti} vs Pagati INPS: {gg_pagati}")
        else:
            if diff > 0:
                st.warning(f"⚠️ **DIFFERENZA**: {diff} giorni in più nel cartellino (verifica i dati)")
            else:
                st.error(f"❌ **ATTENZIONE**: Mancano {abs(diff)} giorni! Retribuiti: {tot_retribuiti} vs Pagati: {gg_pagati}")
        
        # Info sulle omesse timbrature (reminder, non errore)
        if gg_omesse > 0 or agenda_omesse > 0:
            tot_omesse = max(gg_omesse, agenda_omesse)
            st.caption(f"ℹ️ **Reminder**: {tot_omesse} omesse timbrature da regolarizzare (hai lavorato ma manca il badge)")
        
        if agenda_riposi > 0 or gg_riposi > 0:
            tot_riposi = max(agenda_riposi, gg_riposi)
            st.caption(f"💤 {tot_riposi} riposi compensativi (non contano come GG.INPS pagati)")
    elif is_13:
        if b.get("e_tredicesima"):
            st.success("🎄 **TREDICESIMA ANALIZZATA**")
        else:
            st.info("📄 Cedolino analizzato")
    
    st.divider()
    
    # === TABS ===
    tab1, tab2, tab3, tab4 = st.tabs(["💰 Stipendio", "📅 Cartellino", "🗓️ Agenda", "🏖️ Ferie/PAR"])
    
    with tab1:
        k1, k2, k3 = st.columns(3)
        k1.metric("💵 NETTO", f"€ {dg.get('netto', 0):,.2f}")
        k2.metric("📊 Lordo", f"€ {comp.get('lordo_totale', 0):,.2f}")
        k3.metric("📆 Giorni Pagati", dg.get("giorni_pagati", 0))
        
        st.markdown("---")
        
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("➕ Competenze")
            st.write(f"**Paga Base:** € {comp.get('base', 0):,.2f}")
            if comp.get("anzianita", 0) > 0:
                st.write(f"**Anzianità:** € {comp.get('anzianita', 0):,.2f}")
            if comp.get("straordinari", 0) > 0:
                st.write(f"**Straordinari:** € {comp.get('straordinari', 0):,.2f}")
            if comp.get("festivita", 0) > 0:
                st.write(f"**Festività:** € {comp.get('festivita', 0):,.2f}")
        
        with c2:
            st.subheader("➖ Trattenute")
            st.write(f"**INPS:** € {tratt.get('inps', 0):,.2f}")
            st.write(f"**IRPEF:** € {tratt.get('irpef_netta', 0):,.2f}")
            if tratt.get("addizionali", 0) > 0:
                st.write(f"**Addizionali:** € {tratt.get('addizionali', 0):,.2f}")
    
    with tab2:
        if c:
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("👔 Lavorati", c.get("giorni_lavorati", 0))
            k2.metric("🏖️ Ferie", c.get("ferie", 0))
            k3.metric("🤒 Malattia", c.get("malattia", 0))
            k4.metric("⚠️ Omesse", c.get("omesse_timbrature", 0))
            
            st.markdown("---")
            
            k5, k6, k7 = st.columns(3)
            k5.metric("📋 Permessi", c.get("permessi", 0))
            k6.metric("💤 Riposi", c.get("riposi", 0))
            k7.metric("🎉 Festività", c.get("festivita", 0))
            
            if c.get("note"):
                st.info(f"📝 {c['note']}")
        else:
            st.info("Cartellino non disponibile" if not is_13 else "Non applicabile per Tredicesima")
    
    with tab3:
        if agenda and agenda.get("total_events", 0) > 0:
            st.subheader(f"🗓️ Eventi Agenda: {agenda['total_events']} nel mese")
            
            for tipo_ev, count in agenda.get("events_by_type", {}).items():
                if "OMESSA" in tipo_ev:
                    st.error(f"⚠️ **{tipo_ev}**: {count}")
                elif "MALATTIA" in tipo_ev:
                    st.warning(f"🤒 **{tipo_ev}**: {count}")
                elif "FERIE" in tipo_ev:
                    st.info(f"🏖️ **{tipo_ev}**: {count}")
                else:
                    st.write(f"📌 **{tipo_ev}**: {count}")
            
            with st.expander("🔍 Debug Agenda"):
                for line in agenda.get("debug", []):
                    st.text(line)
                for item in agenda.get("items", [])[:20]:
                    st.text(f"• {item}")
        else:
            st.info("ℹ️ Nessun evento agenda per questo mese")
    
    with tab4:
        c1, c2 = st.columns(2)
        
        with c1:
            st.subheader("🏖️ Ferie")
            f1, f2 = st.columns(2)
            f1.metric("Residue AP", f"{ferie.get('residue_ap', 0):.1f}")
            f2.metric("Maturate", f"{ferie.get('maturate', 0):.1f}")
            f3, f4 = st.columns(2)
            f3.metric("Godute", f"{ferie.get('godute', 0):.1f}")
            f4.metric("Saldo", f"{ferie.get('saldo', 0):.1f}")
        
        with c2:
            st.subheader("⏱️ Permessi (PAR)")
            p1, p2 = st.columns(2)
            p1.metric("Residue AP", f"{par.get('residue_ap', 0):.1f}")
            p2.metric("Spettanti", f"{par.get('spettanti', 0):.1f}")
            p3, p4 = st.columns(2)
            p3.metric("Fruite", f"{par.get('fruite', 0):.1f}")
            p4.metric("Saldo", f"{par.get('saldo', 0):.1f}")
