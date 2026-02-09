# ==============================================================================
# GOTTARDO PAYROLL ANALYZER - COMPLETO (LOGO + CARTELLINO ROBUSTO)
# ==============================================================================
# - Header con logo (assets/logo.jpg)
# - Download Busta + Cartellino (Playwright)
# - Agenda: navigazione+intercetto rete + fallback API
# - Parsing AI: Gemini PDF + fallback DeepSeek (testo PDF)
# - Verifica GG: se cartellino manca => "parziale" (niente delta fuorviante)
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

@st.cache_resource
def ensure_playwright_installed():
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
# HEADER (LOGO + TITOLO)
# ==============================================================================
LOGOPATH = Path(__file__).resolve().parent / "assets" / "logo.jpg"

c_logo, c_title = st.columns([0.75, 9.25], gap="small", vertical_alignment="center")
with c_logo:
    if LOGOPATH.exists():
        st.image(str(LOGOPATH), width=100)
with c_title:
    st.markdown('<h1 style="margin:0;padding:0">Gottardo Payroll Analyzer</h1>', unsafe_allow_html=True)

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

def page_or_frame_with_selector(page, selector: str):
    """Ritorna un oggetto che ha .locator(): page oppure un frame che contiene il selector."""
    try:
        if page.locator(selector).count() > 0:
            return page
    except Exception:
        pass
    for fr in page.frames:
        try:
            if fr.locator(selector).count() > 0:
                return fr
        except Exception:
            pass
    return page

def try_click_js_id(page, element_id: str) -> bool:
    try:
        exists = page.evaluate(f"!!document.getElementById('{element_id}')")
        if exists:
            page.evaluate(f"document.getElementById('{element_id}')?.click()")
            return True
    except Exception:
        pass
    return False

def click_esegui_ricerca_cartellino(scope) -> None:
    time.sleep(0.8)
    candidates = [
        # quello “buono” della versione che scaricava
        "//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']",
        # varianti
        "//span[contains(.,'Esegui ricerca')]/ancestor::*[@role='button'][1]",
        "[role='button']:has-text('Esegui ricerca')",
        "span[role='button']:has-text('Esegui ricerca')",
        "[role='button']:has-text('Esegui')",
    ]
    last_err = None
    for sel in candidates:
        try:
            loc = scope.locator(sel).last
            loc.wait_for(state="visible", timeout=25000)
            loc.click(force=True, timeout=25000)
            return
        except Exception as e:
            last_err = e

    try:
        scope.get_by_role("button", name=re.compile(r"ricerca|esegui", re.I)).last.click(timeout=25000)
        return
    except Exception as e:
        raise Exception(f"Bottone 'Esegui ricerca' non trovato/cliccabile: {last_err or e}")


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
        models = []
        for m in valid:
            name = m.name.replace("models/", "")
            if "gemini" in name.lower() and "embedding" not in name.lower():
                try:
                    models.append((name, genai.GenerativeModel(name)))
                except Exception:
                    continue

        def prio(n: str) -> int:
            n = n.lower()
            if "flash" in n and "lite" not in n:
                return 0
            if "lite" in n:
                return 1
            if "pro" in n:
                return 2
            return 3

        models.sort(key=lambda x: prio(x[0]))
        return models
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
# PARSERS
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
            "giorni_lavorati": 0, "giorni_footer": 0, "giorni_righe": 0, "ore_lavorate": 0,
            "ferie": 0, "malattia": 0, "permessi": 0, "riposi": 0, "omesse_timbrature": 0,
            "festivita": 0, "note": ""
        }

    if safe_int(result.get("giorni_footer", 0)) > 0:
        result["giorni_lavorati"] = safe_int(result.get("giorni_footer", 0))
    elif safe_int(result.get("giorni_righe", 0)) > 0:
        result["giorni_lavorati"] = safe_int(result.get("giorni_righe", 0))
    return result


# ==============================================================================
# AGENDA (semplice: nav+intercetto, fallback API)
# ==============================================================================
def read_agenda_api(context, mese_num, anno):
    result = {"events_by_type": {}, "total_events": 0, "success": False}
    base_url = "https://selfservice.gottardospa.it/js_rev/JSipert2"

    code_to_norm = {"FEP": "FERIE", "OMT": "OMESSA TIMBRATURA", "RCS": "RIPOSO", "RIC": "RIPOSO", "MAL": "MALATTIA"}

    for code in CALENDAR_CODES.keys():
        try:
            url = f"{base_url}/api/time/v2/events?$filter_api=calendarCode={code},startTime={anno}-01-01T00:00:00,endTime={anno}-12-31T00:00:00"
            resp = context.request.get(url, timeout=10000)
            if not resp.ok:
                continue
            data = resp.json()
            events = data if isinstance(data, list) else [data]
            month_events = 0
            for ev in events:
                start = ev.get("startTime", "") or ev.get("start", "")
                if start and len(start) >= 7:
                    try:
                        if int(start[5:7]) == mese_num:
                            month_events += 1
                    except Exception:
                        pass
            if month_events:
                k = code_to_norm.get(code, code)
                result["events_by_type"][k] = result["events_by_type"].get(k, 0) + month_events
                result["total_events"] += month_events
        except Exception:
            pass

    if result["total_events"] > 0:
        result["success"] = True
    return result


# ==============================================================================
# DOWNLOAD
# ==============================================================================
def execute_download(mese_nome, anno, user, pwd, is_13ma):
    results = {"busta": None, "cart": None, "agenda": None, "login_ok": None, "login_error": None}

    try:
        mese_num = MESI_IT.index(mese_nome) + 1
    except Exception:
        return results

    anno = safe_int(anno)
    suffix = "_13" if is_13ma else ""
    local_busta = os.path.abspath(f"busta_{mese_num}_{anno}{suffix}.pdf")
    local_cart = os.path.abspath(f"cartellino_{mese_num}_{anno}.pdf")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context(accept_downloads=True, user_agent="Mozilla/5.0 Chrome/120.0.0.0")
        ctx.set_default_timeout(45000)
        page = ctx.new_page()
        page.set_viewport_size({"width": 1920, "height": 1080})

        try:
            # LOGIN
            st.toast("🔐 Login...", icon="🔐")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y", wait_until="domcontentloaded")
            page.wait_for_selector("#ParametriLogin input[name='username']", timeout=20000)

            page.locator("#ParametriLogin input[name='username']").first.fill(user)
            pin = page.locator("#ParametriLogin input[name='password']").first
            pin.fill(pwd)

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
                    pin.press("Enter")
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
                return results
            results["login_ok"] = True

            # AGENDA (fallback API diretto: semplice e stabile)
            st.toast("🗓️ Agenda (API)...", icon="🗓️")
            results["agenda"] = read_agenda_api(ctx, mese_num, anno)

            # BUSTA
            st.toast("💰 Scarico Busta...", icon="💰")
            try:
                try:
                    page.keyboard.press("Escape")
                    time.sleep(0.2)
                except Exception:
                    pass

                try_click_js_id(page, "revit_navigation_NavHoverItem_0_label") or page.locator("text=I miei dati").first.click(force=True)
                time.sleep(2)

                try_click_js_id(page, "lnktab_2_label") or try_click_js_id(page, "lnktab_2")
                time.sleep(2)

                try:
                    page.wait_for_selector("text=Cedolino", timeout=10000)
                except Exception:
                    pass

                try:
                    page.locator("tr", has=page.locator("text=Cedolino")).locator(".z-image").click(timeout=7000)
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
                        patterns = [f"{mese_nome} {anno}", f"{mese_num:02d}/{anno}", f"{mese_num}/{anno}", f"{mese_num:02d}-{anno}", f"{mese_num}-{anno}"]
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

            # CARTELLINO
            if not is_13ma:
                st.toast("📅 Scarico Cartellino...", icon="📅")
                try:
                    # torna home come nel codice stabile: prova click logo, altrimenti goto
                    try:
                        page.keyboard.press("Escape")
                        time.sleep(0.2)
                    except Exception:
                        pass

                    try:
                        logo = page.locator("img[src*='logo'], .logo").first
                        if logo.is_visible(timeout=2000):
                            logo.click()
                            time.sleep(2)
                        else:
                            raise Exception("logo non visibile")
                    except Exception:
                        page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2", wait_until="domcontentloaded")
                        time.sleep(3)

                    # menu Time
                    try_click_js_id(page, "revit_navigation_NavHoverItem_2_label") or page.locator("text=Time").first.click(force=True)
                    time.sleep(3)

                    # tab Cartellino presenze
                    ok_tab = try_click_js_id(page, "lnktab_5_label") or try_click_js_id(page, "lnktab_5")
                    if not ok_tab:
                        try:
                            page.locator("text=Cartellino").first.click(force=True)
                        except Exception:
                            pass
                    time.sleep(5)

                    # SCOPE: page o frame dove compaiono CLRICHIE/CLRICHI2
                    scope = page_or_frame_with_selector(page, "input#CLRICHIE, input[id='CLRICHIE'], input[id*='CLRICHIE']")

                    # selettori permissivi (prima id esatti, poi contains)
                    dal = scope.locator("input#CLRICHIE, input[id='CLRICHIE'], input[id*='CLRICHIE']").first
                    al = scope.locator("input#CLRICHI2, input[id='CLRICHI2'], input[id*='CLRICHI2']").first

                    dal.wait_for(state="visible", timeout=30000)
                    al.wait_for(state="visible", timeout=30000)

                    last_day = calendar.monthrange(anno, mese_num)[1]
                    d1 = f"01/{mese_num:02d}/{anno}"
                    d2 = f"{last_day}/{mese_num:02d}/{anno}"

                    dal.click(force=True)
                    try:
                        scope.keyboard.press("Control+A")
                    except Exception:
                        page.keyboard.press("Control+A")
                    dal.fill("")
                    dal.type(d1, delay=80)
                    dal.press("Tab")
                    time.sleep(0.6)

                    al.click(force=True)
                    try:
                        scope.keyboard.press("Control+A")
                    except Exception:
                        page.keyboard.press("Control+A")
                    al.fill("")
                    al.type(d2, delay=80)
                    al.press("Tab")
                    time.sleep(0.6)

                    click_esegui_ricerca_cartellino(scope)
                    time.sleep(8)

                    # icona PDF
                    pattern_cart = f"{mese_num:02d}/{anno}"
                    riga = page.locator(f"tr:has-text('{pattern_cart}')").first
                    if riga.count() > 0 and riga.locator("img[src*='search']").count() > 0:
                        icona = riga.locator("img[src*='search']").first
                    else:
                        icona = page.locator("img[src*='search']").first
                    if icona.count() == 0:
                        # prova nel scope/frame
                        icona = scope.locator("img[src*='search']").first
                    if icona.count() == 0:
                        raise Exception("Icona PDF cartellino non trovata")

                    popup = None
                    try:
                        with page.expect_popup(timeout=20000) as pop_info:
                            icona.click()
                        popup = pop_info.value
                    except Exception:
                        with ctx.expect_page(timeout=20000) as pop_info:
                            icona.click()
                        popup = pop_info.value

                    # aspetta URL JPSC
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
                        # fallback popup.pdf come nella versione che scaricava
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
                    # screenshot utile in cloud
                    try:
                        page.screenshot(path="debug_cartellino.png", full_page=True)
                        with st.expander("🧩 Debug Cartellino (screenshot)"):
                            st.image("debug_cartellino.png")
                    except Exception:
                        pass
                    st.warning(f"⚠️ Cartellino: {e}")

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
# UI LOGIN + RUN
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
        is_13 = (tipo == "Tredicesima")
        with st.status("🔄 Elaborazione...", expanded=True):
            paths = execute_download(m, a, u, pw, is_13)

            if paths.get("login_ok") is False:
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
# OUTPUT
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

    mese_nome = data.get("mese", "Ottobre")
    mese_num = MESI_IT.index(mese_nome) + 1
    anno = safe_int(data.get("anno", 2025))

    a_evs = agenda.get("events_by_type", {}) if isinstance(agenda, dict) else {}
    a_omesse = safe_int(a_evs.get("OMESSA TIMBRATURA", 0))
    a_ferie = safe_int(a_evs.get("FERIE", 0))

    tab1, tab2, tab3 = st.tabs(["💰 Stipendio", "📅 Cartellino", "🏖️ Ferie/PAR"])

    # ---- calcoli (parziale se cartellino mancante)
    gg_pagati_busta = safe_int(dg.get("giorni_pagati", 0))
    ore_assenze_busta = safe_float((b.get("assenze_mese", {}) or {}).get("ore_ferie", 0)) + safe_float((b.get("assenze_mese", {}) or {}).get("ore_permessi", 0))
    gg_assenze_busta = round(ore_assenze_busta / ORE_PER_GIORNO) if ore_assenze_busta > 0 else 0

    c_lavorati = safe_float(c.get("giorni_lavorati", 0))
    cart_ok = bool(c) and c_lavorati > 0

    # ferie: busta > cartellino > agenda
    gg_ferie_eff = gg_assenze_busta if gg_assenze_busta > 0 else (safe_int(c.get("ferie", 0)) if safe_int(c.get("ferie", 0)) > 0 else a_ferie)

    if cart_ok:
        tot_calcolato = c_lavorati + gg_ferie_eff + safe_int(c.get("festivita", 0)) + safe_int(c.get("malattia", 0))
        diff_gg = tot_calcolato - gg_pagati_busta
    else:
        tot_calcolato = gg_ferie_eff
        diff_gg = None

    st.markdown("---")
    st.subheader(f"📊 Verifica {mese_nome} {anno}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("📅 GG INPS (Busta)", gg_pagati_busta)
    c2.metric("📋 GG Calcolati" + ("" if cart_ok else " (parziale)"), f"{tot_calcolato:.0f}",
              delta=(f"{diff_gg:+.0f}" if cart_ok and diff_gg != 0 else None))
    c3.metric("👔 Lavorati (Cartellino)", c_lavorati)
    c4.metric("⚠️ Omesse (Agenda)", a_omesse)

    if not cart_ok and not is_13:
        st.warning("📌 Cartellino non disponibile: la verifica GG è parziale (mancano i lavorati).")

    with tab1:
        netto = safe_float(dg.get("netto", 0))
        lordo = safe_float(comp.get("lordo_totale", 0))
        base = safe_float(comp.get("base", 0))
        stra = safe_float(comp.get("straordinari", 0))
        fest = safe_float(comp.get("festivita", 0))
        inps = safe_float(tratt.get("inps", 0))
        irpef = safe_float(tratt.get("irpef_netta", 0))
        add = safe_float(tratt.get("addizionali", 0))

        k1, k2, k3, k4 = st.columns(4)
        k1.metric("💵 NETTO", f"€ {netto:,.2f}")
        k2.metric("📊 Lordo", f"€ {lordo:,.2f}")
        k3.metric("📆 Giorni Pagati", gg_pagati_busta)
        k4.metric("⏱️ Ore Lavorate", safe_float(dg.get("ore_ordinarie", 0)))

        st.markdown("---")
        a1, a2 = st.columns(2)
        with a1:
            st.subheader("➕ Competenze")
            st.write(f"Paga Base: € {base:,.2f}")
            if stra > 0:
                st.write(f"Straordinari: € {stra:,.2f}")
            if fest > 0:
                st.write(f"Festività: € {fest:,.2f}")
        with a2:
            st.subheader("➖ Trattenute")
            st.write(f"INPS: € {inps:,.2f}")
            st.write(f"IRPEF: € {irpef:,.2f}")
            if add > 0:
                st.write(f"Addizionali: € {add:,.2f}")

    with tab2:
        if c:
            st.json(c)
        else:
            st.info("Cartellino non disponibile" if not is_13 else "Non applicabile per Tredicesima")

    with tab3:
        f1, f2 = st.columns(2)
        with f1:
            st.subheader("🏖️ Ferie")
            st.write(ferie)
        with f2:
            st.subheader("⏱️ PAR")
            st.write(par)


