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
# Google API Key (obbligatoria)
try:
    GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
except Exception:
    st.error("❌ Google API Key mancante in secrets")
    st.stop()

genai.configure(api_key=GOOGLE_API_KEY)

# DeepSeek API Key (opzionale)
try:
    DEEPSEEK_API_KEY = st.secrets["DEEPSEEK_API_KEY"]
except Exception:
    DEEPSEEK_API_KEY = None


# ==============================================================================
# 1) GEMINI: autodiscovery + fallback multi-modello
# ==============================================================================
@st.cache_resource
def inizializza_modelli_gemini():
    """Auto-discovery: Scopre automaticamente tutti i modelli Gemini disponibili"""
    try:
        tutti_modelli = genai.list_models()
        modelli_validi = [
            m for m in tutti_modelli
            if 'generateContent' in m.supported_generation_methods
        ]

        modelli_gemini = []
        for m in modelli_validi:
            nome_pulito = m.name.replace('models/', '')
            if 'gemini' in nome_pulito.lower() and 'embedding' not in nome_pulito.lower():
                try:
                    modello = genai.GenerativeModel(nome_pulito)
                    modelli_gemini.append((nome_pulito, modello))
                except Exception:
                    continue

        if len(modelli_gemini) == 0:
            st.error("❌ Nessun modello Gemini disponibile")
            st.stop()

        def priorita(nome):
            n = nome.lower()
            if 'flash' in n and 'lite' not in n:
                return 0
            elif 'lite' in n:
                return 1
            elif 'pro' in n:
                return 2
            else:
                return 3

        modelli_gemini.sort(key=lambda x: priorita(x[0]))
        return modelli_gemini

    except Exception as e:
        st.error(f"❌ Errore caricamento modelli: {e}")
        st.stop()


MODELLI_DISPONIBILI = inizializza_modelli_gemini()

if 'modelli_mostrati' not in st.session_state:
    st.sidebar.success(f"✅ {len(MODELLI_DISPONIBILI)} modelli AI pronti")
    st.session_state['modelli_mostrati'] = True


# ==============================================================================
# 2) Helpers: JSON cleaning + PDF text extraction (per DeepSeek)
# ==============================================================================
def clean_json_response(text):
    """Estrae JSON pulito da risposta AI"""
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


def extract_text_from_pdf_any(file_path: str) -> str | None:
    """Estrae testo dal PDF (serve per DeepSeek). Ritorna None se non disponibile."""
    if not file_path or not os.path.exists(file_path):
        return None

    # 1) PyMuPDF
    if fitz is not None:
        try:
            doc = fitz.open(file_path)
            chunks = []
            for p in doc:
                chunks.append(p.get_text())
            txt = "\n".join(chunks).strip()
            return txt if len(txt) > 0 else None
        except Exception:
            pass

    # 2) pypdf
    if PdfReader is not None:
        try:
            reader = PdfReader(file_path)
            chunks = []
            for page in reader.pages:
                chunks.append((page.extract_text() or ""))
            txt = "\n".join(chunks).strip()
            return txt if len(txt) > 0 else None
        except Exception:
            pass

    return None


# ==============================================================================
# 3) AI: Gemini -> DeepSeek fallback
# ==============================================================================
def estrai_con_fallback(file_path, prompt, tipo="documento"):
    """
    Prova multipli modelli Gemini con fallback automatico.
    Se Gemini fallisce/quota: fallback su DeepSeek (solo se riesco a estrarre testo dal PDF).
    """
    if not file_path or not os.path.exists(file_path):
        return None

    with open(file_path, "rb") as f:
        bytes_data = f.read()

    if not bytes_data[:4] == b'%PDF':
        st.error(f"❌ Il file {tipo} non è un PDF valido")
        return None

    progress_placeholder = st.empty()
    last_err = None

    # 1) Gemini (PDF nativo)
    for idx, (nome_modello, modello) in enumerate(MODELLI_DISPONIBILI, 1):
        try:
            progress_placeholder.info(f"🔄 Analisi {tipo}: modello {idx}/{len(MODELLI_DISPONIBILI)} ({nome_modello})...")

            response = modello.generate_content([
                prompt,
                {"mime_type": "application/pdf", "data": bytes_data}
            ])

            result = clean_json_response(getattr(response, "text", None))

            if result and isinstance(result, dict):
                progress_placeholder.success(f"✅ {tipo.capitalize()} analizzato!")
                time.sleep(0.8)
                progress_placeholder.empty()
                return result

        except Exception as e:
            last_err = e
            error_msg = str(e).lower()
            # quota/429: passa al prossimo modello
            if "429" in error_msg or "quota" in error_msg or "resource_exhausted" in error_msg:
                continue
            # altri errori: comunque tenta gli altri modelli
            continue

    # 2) DeepSeek fallback (solo testo estratto)
    if DEEPSEEK_API_KEY and OpenAI is not None:
        try:
            progress_placeholder.warning(f"⚠️ Gemini non disponibile/quote. Fallback DeepSeek per {tipo}...")
            text = extract_text_from_pdf_any(file_path)

            if not text or len(text.strip()) < 50:
                progress_placeholder.error("❌ DeepSeek: testo PDF non estraibile (probabile PDF a immagini).")
                time.sleep(1)
                progress_placeholder.empty()
                return None

            client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

            full_prompt = (
                prompt
                + "\n\n--- CONTENUTO PDF (testo estratto) ---\n"
                + text[:25000]
            )

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
                progress_placeholder.success(f"✅ {tipo.capitalize()} analizzato (DeepSeek)!")
                time.sleep(0.8)
                progress_placeholder.empty()
                return result

        except Exception:
            # non bloccare la UI: semplicemente fallisce
            progress_placeholder.error("❌ Analisi fallita anche con DeepSeek.")
            time.sleep(1)
            progress_placeholder.empty()
            return None

    progress_placeholder.error(f"❌ Analisi {tipo} fallita (quote esaurite o errori AI)")
    if last_err:
        with st.expander("🔎 Dettaglio ultimo errore AI"):
            st.code(str(last_err)[:2000])
    return None


# ==============================================================================
# 4) Estrattori specifici (busta/cartellino)
# ==============================================================================
def estrai_dati_busta_dettagliata(file_path):
    """Estrae dati dalla busta paga"""

    prompt = """
Questo è un CEDOLINO PAGA GOTTARDO S.p.A. italiano. Segui ESATTAMENTE queste istruzioni:

**1. DATI GENERALI (PRIMA PAGINA, RIGA PROGRESSIVI):**
- **NETTO:** Cerca la riga "PROGRESSIVI" in fondo. Il NETTO è nella colonna finale prima di "ESTREMI ELABORAZIONE"
- **GIORNI PAGATI:** Cerca in alto la riga con "GG. INPS" (numero a sinistra della colonna, es. "26")
- **ORE ORDINARIE:** Cerca "ORE INAIL" oppure calcola: giorni_pagati × 8

**2. COMPETENZE (TABELLA CENTRALE):**
- **RETRIBUZIONE ORDINARIA (voce 1000):** Colonna "COMPETENZE"
- **STRAORDINARI:** Somma tutte le voci tipo "STRAORDINARIO", "SUPPLEMENTARI", "NOTTURNI"
- **FESTIVITA:** Somma voci "MAGG. FESTIVE", "FESTIVITA GODUTA"
- **ANZIANITA:** Se vedi voci "SCATTI", "EDR", "ANZ." usale, altrimenti 0
- **LORDO TOTALE:** Cerca riga "TOTALE COMPETENZE" o "PROGRESSIVI" → colonna "TOTALE COMPETENZE"

**3. TRATTENUTE (SEZIONE I.N.P.S. + IRPEF):**
- **INPS:** Sezione "IMPONIBILE / TRATTENUTE" → riga sotto "I.N.P.S."
- **IRPEF NETTA:** Sezione "FISCALI" → riga "TRATTENUTE" sotto "IRPEF CONG."
- **ADDIZIONALI:** Cerca voci "ADD.REG." e "ADD.COM." (sono rateizzate)

**4. FERIE (TABELLA IN ALTO A DESTRA):**
- DUE colonne: FERIE e P.A.R.
- Residue AP = "RES. PREC."
- Maturate = "SPETTANTI"
- Godute = "FRUITE"
- Saldo = "SALDO"

**5. TREDICESIMA:**
- Se nel titolo o nella colonna "Mensilità" c'è "TREDICESIMA" o "13MA" → e_tredicesima=true

**IMPORTANTE:**
- Se un valore non esiste scrivi 0
- Usa il punto come separatore decimale
- Restituisci SOLO JSON (niente testo):

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
    """Estrae dati dal cartellino - con debug integrato"""

    prompt = r"""
Analizza questo cartellino presenze GOTTARDO S.p.A.

OBIETTIVO: capire se il PDF contiene timbrature dettagliate oppure è un PDF vuoto (solo pagina ricerca).

1) Se vedi tabella timbrature con righe tipo "L01", "M02", "G03" e colonne con E/U:
- conta i giorni che hanno almeno una timbratura
- giorni_senza_badge: conta righe/anomalie con badge mancante se rilevabile, altrimenti 0

2) Se vedi un totale ore ordinarie in fondo (es. "160,00"):
- puoi usarlo come supporto, ma il campo "giorni_reali" deve restare il conteggio giorni con timbrature

3) Se il PDF mostra solo "Parametri di ricerca" / "Risultati della ricerca" e NON timbrature:
- giorni_reali = 0 e nota che è vuoto

OUTPUT (solo JSON):
{
  "giorni_reali": 0,
  "giorni_senza_badge": 0,
  "note": "",
  "debug_prime_righe": ""
}

debug_prime_righe: metti una trascrizione delle prime 20 righe che stai usando (anche approssimata).
""".strip()

    result = estrai_con_fallback(file_path, prompt, tipo="cartellino")

    if not result or not isinstance(result, dict):
        return {
            "giorni_reali": 0,
            "giorni_senza_badge": 0,
            "note": "Nessun dato da visualizzare.",
            "debug_prime_righe": ""
        }

    # Debug extra: prova a estrarre testo (se disponibile) per capire se il PDF è vuoto/a immagini
    extracted = extract_text_from_pdf_any(file_path)
    if extracted:
        # solo un assaggio, non enorme
        with st.expander("🔍 DEBUG: Testo estratto dal PDF (local extractor)"):
            st.text(extracted[:2500])

    if 'debug_prime_righe' in result:
        with st.expander("🔍 DEBUG: Prime righe estratte dall'AI"):
            st.text(result.get('debug_prime_righe', ''))
            timbrature = re.findall(r'[LMGVSD]\d{2}', result.get('debug_prime_righe', ''))
            st.info(f"📊 Timbrature trovate (pattern): **{len(timbrature)}**")
            if len(timbrature) > 0:
                st.success("✅ Cartellino CON timbrature dettagliate!")
                st.write(f"**Prime 5 timbrature:** {', '.join(timbrature[:5])}")

    return result


# ==============================================================================
# 5) Pulizia file
# ==============================================================================
def pulisci_file(path_busta, path_cart):
    """Elimina i file PDF scaricati dopo l'analisi"""
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
        st.info(f"🗑️ File eliminati: {', '.join(file_eliminati)}")


# ==============================================================================
# 6) Core bot (tuo: busta + cartellino robusto popup/GET)
# ==============================================================================
def scarica_documenti_automatici(mese_nome, anno, username, password, tipo_documento="cedolino"):
    """Bot completo con gestione popup/iframe per cartellino (tuo codice, con micro-fix)"""
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
    st_status.info(f"🤖 Bot: {nome_tipo} {mese_nome} {anno}")

    busta_ok = False
    cart_ok = False

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
            st_status.info("🔐 Login...")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y", wait_until="domcontentloaded")
            page.wait_for_selector('input[type="text"]', timeout=10000)
            page.fill('input[type="text"]', username)
            page.fill('input[type="password"]', password)
            page.press('input[type="password"]', 'Enter')
            time.sleep(3)

            try:
                page.wait_for_selector("text=I miei dati", timeout=15000)
                st_status.info("✅ Login OK")
            except Exception:
                st_status.error("❌ Login fallito")
                browser.close()
                return None, None, "LOGIN_FALLITO"

            # BUSTA PAGA
            st_status.info(f"💰 Download {nome_tipo}...")
            try:
                page.click("text=I miei dati")
                page.wait_for_selector("text=Documenti", timeout=10000).click()
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
                            st_status.success("✅ Tredicesima scaricata")
                else:
                    all_links = page.locator("a")
                    total_links = all_links.count()
                    link_matches = []

                    for i in range(total_links):
                        try:
                            txt = (all_links.nth(i).inner_text() or "").strip()
                            if not txt or len(txt) < 3:
                                continue
                            if any(mese in txt for mese in nomi_mesi_it) and str(anno) in txt:
                                ha_target = target_busta.lower() in txt.lower()
                                e_tredicesima = any(kw in txt for kw in ["Tredicesima", "13", "XIII"])
                                if ha_target and not e_tredicesima:
                                    link_matches.append((i, txt))
                        except Exception:
                            continue

                    if len(link_matches) > 0:
                        link_index, _ = link_matches[-1]
                        with page.expect_download(timeout=20000) as download_info:
                            all_links.nth(link_index).click()
                        download_info.value.save_as(path_busta)
                        if os.path.exists(path_busta):
                            busta_ok = True
                            st_status.success("✅ Cedolino scaricato")

            except Exception as e:
                st.error(f"❌ Errore busta: {e}")

            # CARTELLINO - SOLUZIONE ROBUSTA (popup + GET bytes PDF)
            if tipo_documento != "tredicesima":
                st_status.info("📅 Download cartellino...")
                debug_log = []

                def _normalize_url(u: str) -> str:
                    return (u or "").replace("/js_rev//", "/js_rev/")

                def _save_pdf_via_request(url: str) -> bool:
                    try:
                        url = _normalize_url(url)
                        resp = context.request.get(url, timeout=60000)
                        ct = (resp.headers.get("content-type") or "").lower()
                        body = resp.body()
                        debug_log.append(f"🌐 HTTP GET -> status={resp.status}, content-type={ct}, bytes={len(body)}")
                        debug_log.append(f"🔎 First bytes: {body[:8]!r}")

                        if body[:4] == b"%PDF":
                            Path(path_cart).write_bytes(body)
                            debug_log.append("✅ Salvato PDF raw da HTTP (firma %PDF ok)")
                            return True

                        debug_log.append("⚠️ Response non sembra un PDF (%PDF mancante)")
                        return False
                    except Exception as e:
                        debug_log.append(f"❌ Errore GET PDF: {str(e)[:220]}")
                        return False

                try:
                    page.evaluate("window.scrollTo(0, 0)")
                    time.sleep(2)
                    try:
                        page.keyboard.press("Escape")
                        time.sleep(1)
                    except Exception:
                        pass

                    # Torna home
                    debug_log.append("🏠 Tornando alla home...")
                    try:
                        logo = page.locator("img[src*='logo'], .logo").first
                        if logo.is_visible(timeout=2000):
                            logo.click()
                            time.sleep(2)
                    except Exception:
                        page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2", wait_until="domcontentloaded")
                        time.sleep(3)

                    # Vai su Time
                    debug_log.append("⏰ Navigazione a Time...")
                    page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
                    time.sleep(3)
                    debug_log.append("✅ Time aperto")

                    # Vai su Cartellino presenze
                    debug_log.append("📋 Apertura Cartellino presenze...")
                    page.evaluate("document.getElementById('lnktab_5_label')?.click()")
                    time.sleep(5)
                    debug_log.append("✅ Cartellino presenze aperto")

                    # Date
                    debug_log.append(f"📅 Impostazione date: {d_from_vis} - {d_to_vis}")
                    try:
                        dal = page.locator("input[id*='CLRICHIE'][class*='dijitInputInner']").first
                        al = page.locator("input[id*='CLRICHI2'][class*='dijitInputInner']").first
                        if dal.count() > 0 and al.count() > 0:
                            dal.click(force=True)
                            page.keyboard.press("Control+A")
                            dal.fill("")
                            dal.type(d_from_vis, delay=100)
                            dal.press("Tab")
                            time.sleep(1)

                            al.click(force=True)
                            page.keyboard.press("Control+A")
                            al.fill("")
                            al.type(d_to_vis, delay=100)
                            al.press("Tab")
                            time.sleep(1)
                            debug_log.append("✅ Date impostate")
                    except Exception as e:
                        debug_log.append(f"⚠️ Errore date: {e}")

                    # Ricerca
                    debug_log.append("🔍 Esecuzione ricerca...")
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(1)
                    try:
                        page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last.click(force=True)
                        debug_log.append("✅ Click 'Esegui ricerca' OK")
                    except Exception:
                        page.keyboard.press("Enter")
                        debug_log.append("✅ Enter per ricerca OK")

                    time.sleep(8)

                    try:
                        page.wait_for_selector("text=Risultati della ricerca", timeout=20000)
                        debug_log.append("✅ Risultati caricati")
                    except Exception:
                        debug_log.append("⚠️ Timeout risultati")

                    # Screenshot PRE click
                    try:
                        screenshot_pre = page.screenshot(timeout=15000)
                        with st.expander("📸 Screenshot PRIMA del click"):
                            st.image(screenshot_pre, use_container_width=True)
                    except Exception:
                        pass

                    # Riga + lente
                    debug_log.append("🔍 Ricerca riga cartellino...")
                    pattern_da_provare = [
                        f"{mese_num:02d}/{anno}",
                        f"{mese_num}/{anno}",
                        f"{mese_num}/{str(anno)[-2:]}",
                    ]

                    riga_target = None
                    for pattern in pattern_da_provare:
                        debug_log.append(f"🔍 Cerco pattern: '{pattern}'")
                        riga_test = page.locator(f"tr:has-text('{pattern}')").first
                        if riga_test.count() > 0 and riga_test.locator("img[src*='search']").count() > 0:
                            riga_target = riga_test
                            debug_log.append(f"✅ Riga trovata con pattern: '{pattern}'")
                            break

                    if not riga_target:
                        debug_log.append("⚠️ Riga non trovata, fallback: prima icona search")
                        icona = page.locator("img[src*='search']").first
                    else:
                        icona = riga_target.locator("img[src*='search']").first

                    if icona.count() == 0:
                        debug_log.append("❌ Icona lente non trovata")
                        page.pdf(path=path_cart)
                        debug_log.append("⚠️ Usato fallback page.pdf()")
                    else:
                        debug_log.append("📥 Click lente: attendo popup...")
                        with context.expect_page(timeout=20000) as popup_info:
                            icona.click()

                        popup = popup_info.value

                        # Poll URL
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
                        debug_log.append(f"✅ Popup catturato: {popup_url}")

                        # Screenshot popup
                        try:
                            screenshot_popup = popup.screenshot(timeout=15000)
                            with st.expander("📸 Screenshot POPUP"):
                                st.image(screenshot_popup, use_container_width=True)
                        except Exception as e_shot:
                            debug_log.append(f"⚠️ Screenshot popup fallito (ignoro): {str(e_shot)[:220]}")

                        ok = _save_pdf_via_request(popup_url)

                        if (not ok) and ("EMBED=y" not in popup_url):
                            debug_log.append("↩️ Retry con &EMBED=y")
                            ok = _save_pdf_via_request(popup_url + "&EMBED=y")

                        if not ok:
                            debug_log.append("↩️ Fallback finale: popup.pdf()")
                            try:
                                popup.pdf(path=path_cart, format="A4")
                            except Exception as e_pdf:
                                debug_log.append(f"❌ popup.pdf fallito: {str(e_pdf)[:220]}")
                                page.pdf(path=path_cart)
                                debug_log.append("⚠️ Usato fallback page.pdf()")

                        try:
                            popup.close()
                        except Exception:
                            pass

                    # Verifica file
                    if os.path.exists(path_cart):
                        size = os.path.getsize(path_cart)
                        debug_log.append(f"📊 File trovato: {size:,} bytes")
                        if size > 5000:
                            cart_ok = True
                            st_status.success(f"✅ Cartellino OK ({size:,} bytes)")
                            debug_log.append("✅ CARTELLINO VALIDO")
                        else:
                            st.warning(f"⚠️ PDF piccolo ({size:,} bytes) - potrebbe essere vuoto")
                            debug_log.append(f"⚠️ FILE PICCOLO: {size} bytes")
                    else:
                        st.error("❌ File non trovato")
                        debug_log.append("❌ FILE NON TROVATO")

                except Exception as e:
                    debug_log.append(f"❌ ERRORE GENERALE CARTELLINO: {str(e)[:240]}")
                    st.error(f"❌ Errore: {e}")
                    import traceback
                    tb = traceback.format_exc()
                    debug_log.append(f"Traceback:\n{tb}")
                    st.code(tb)

                with st.expander("🔍 LOG DEBUG COMPLETO"):
                    for log_entry in debug_log:
                        st.text(log_entry)

                    log_path = work_dir / f"debug_cartellino_{mese_num}_{anno}.txt"
                    try:
                        with open(log_path, "w", encoding="utf-8") as f:
                            f.write("\n".join(debug_log))
                        st.info(f"📝 Log salvato: {log_path}")
                    except Exception:
                        pass

            browser.close()

    except Exception as e:
        st.error(f"Errore generale: {e}")

    final_busta = path_busta if busta_ok else None
    final_cart = path_cart if cart_ok else None
    return final_busta, final_cart, None


# ==============================================================================
# 7) UI (identica alla tua)
# ==============================================================================
st.set_page_config(page_title="Gottardo Payroll", page_icon="💶", layout="wide")
st.title("💶 Analisi Stipendio & Presenze")

with st.sidebar:
    st.header("🔐 Credenziali")
    username, password = get_credentials()

    if not st.session_state.get('credentials_set'):
        st.info("Inserisci le tue credenziali Gottardo SelfService")
        input_user = st.text_input("Username", value=username if username else "", key="input_user")
        input_pass = st.text_input("Password", type="password", value="", key="input_pass")

        if st.button("💾 Salva Credenziali"):
            if input_user and input_pass:
                st.session_state['username'] = input_user
                st.session_state['password'] = input_pass
                st.session_state['credentials_set'] = True
                st.success("✅ Credenziali salvate!")
                st.rerun()
            else:
                st.error("⚠️ Inserisci username e password")
    else:
        st.success(f"✅ Loggato: **{st.session_state['username']}**")
        if st.button("🔄 Cambia Credenziali"):
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

        tipo_doc = st.radio(
            "Tipo documento",
            ["📄 Cedolino Mensile", "🎄 Tredicesima"],
            index=0
        )

        if st.button("🚀 AVVIA ANALISI", type="primary", use_container_width=True):
            for key in ['busta', 'cart', 'db', 'dc', 'done']:
                st.session_state.pop(key, None)

            tipo = "tredicesima" if "Tredicesima" in tipo_doc else "cedolino"
            username = st.session_state.get('username')
            password = st.session_state.get('password')

            busta, cart, errore = scarica_documenti_automatici(
                sel_mese, sel_anno, username, password, tipo_documento=tipo
            )

            if errore == "LOGIN_FALLITO":
                st.error("❌ LOGIN FALLITO")
                st.stop()

            st.session_state['busta'] = busta
            st.session_state['cart'] = cart
            st.session_state['tipo'] = tipo
            st.session_state['done'] = False
    else:
        st.warning("⚠️ Inserisci le credenziali")

# ANALISI
if st.session_state.get('busta') or st.session_state.get('cart'):
    if not st.session_state.get('done'):
        with st.spinner("🧠 Analisi AI in corso..."):
            db = estrai_dati_busta_dettagliata(st.session_state.get('busta'))
            dc = estrai_dati_cartellino(st.session_state.get('cart')) if st.session_state.get('cart') else None

            st.session_state['db'] = db
            st.session_state['dc'] = dc
            st.session_state['done'] = True

            # cleanup
            pulisci_file(st.session_state.get('busta'), st.session_state.get('cart'))
            st.session_state.pop('busta', None)
            st.session_state.pop('cart', None)

    db = st.session_state.get('db')
    dc = st.session_state.get('dc')
    tipo = st.session_state.get('tipo', 'cedolino')

    if db and db.get('e_tredicesima'):
        st.success("🎄 **Cedolino TREDICESIMA**")

    st.divider()
    tab1, tab2, tab3 = st.tabs(["💰 Dettaglio Stipendio", "📅 Cartellino & Presenze", "📊 Analisi & Confronto"])

    with tab1:
        if db:
            dg = db.get('dati_generali', {})
            comp = db.get('competenze', {})
            tratt = db.get('trattenute', {})
            ferie = db.get('ferie', {})
            par = db.get('par', {})

            k1, k2, k3 = st.columns(3)
            k1.metric("💵 NETTO IN BUSTA", f"€ {dg.get('netto', 0):.2f}", delta="Pagamento")
            k2.metric("📊 Lordo Totale", f"€ {comp.get('lordo_totale', 0):.2f}")
            k3.metric("📆 Giorni Pagati", int(dg.get('giorni_pagati', 0)))

            st.markdown("---")
            c_entr, c_usc = st.columns(2)

            with c_entr:
                st.subheader("➕ Competenze")
                st.write(f"**Paga Base:** € {comp.get('base', 0):.2f}")
                if comp.get('anzianita', 0) > 0:
                    st.write(f"**Anzianità:** € {comp.get('anzianita', 0):.2f}")
                if comp.get('straordinari', 0) > 0:
                    st.write(f"**Straordinari/Suppl.:** € {comp.get('straordinari', 0):.2f}")
                if comp.get('festivita', 0) > 0:
                    st.write(f"**Festività/Maggiorazioni:** € {comp.get('festivita', 0):.2f}")

            with c_usc:
                st.subheader("➖ Trattenute")
                st.write(f"**Contributi INPS:** € {tratt.get('inps', 0):.2f}")
                st.write(f"**IRPEF Netta:** € {tratt.get('irpef_netta', 0):.2f}")
                if tratt.get('addizionali_totali', 0) > 0:
                    st.write(f"**Addizionali:** € {tratt.get('addizionali_totali', 0):.2f}")

            with st.expander("🏖️ Situazione Ferie"):
                f1, f2, f3, f4 = st.columns(4)
                f1.metric("Residue AP", f"{ferie.get('residue_ap', 0):.2f}")
                f2.metric("Maturate", f"{ferie.get('maturate', 0):.2f}")
                f3.metric("Godute", f"{ferie.get('godute', 0):.2f}")
                saldo_f = ferie.get('saldo', 0)
                f4.metric("✅ SALDO", f"{saldo_f:.2f}", delta="OK" if saldo_f >= 0 else "Negativo")

            with st.expander("⏱️ Situazione Permessi"):
                p1, p2, p3, p4 = st.columns(4)
                p1.metric("Residue AP", f"{par.get('residue_ap', 0):.2f}")
                p2.metric("Spettanti", f"{par.get('spettanti', 0):.2f}")
                p3.metric("Fruite", f"{par.get('fruite', 0):.2f}")
                saldo_p = par.get('saldo', 0)
                p4.metric("✅ SALDO", f"{saldo_p:.2f}", delta="OK" if saldo_p >= 0 else "Negativo")
        else:
            st.warning("⚠️ Dati busta non disponibili")

    with tab2:
        if dc:
            c1, c2 = st.columns([1, 2])
            with c1:
                giorni_reali = dc.get('giorni_reali', 0)
                if giorni_reali and giorni_reali > 0:
                    st.metric("📅 Giorni Lavorati", giorni_reali)
                else:
                    st.metric("📅 Giorni Lavorati", "N/D", help="Timbrature non disponibili o PDF vuoto")

                anomalie = dc.get('giorni_senza_badge', 0)
                if anomalie and anomalie > 0:
                    st.metric("⚠️ Anomalie Badge", anomalie, delta="Controlla")
                else:
                    st.metric("✅ Anomalie Badge", 0, delta="OK")

            with c2:
                note = dc.get('note', '')
                st.info(f"**📝 Note:** {note}")
        else:
            if tipo == "tredicesima":
                st.warning("⚠️ Cartellino non disponibile (Tredicesima)")
            else:
                st.error("❌ Errore cartellino")

    with tab3:
        if db and dc:
            pagati = float(db.get('dati_generali', {}).get('giorni_pagati', 0))
            reali = float(dc.get('giorni_reali', 0))

            st.subheader("🔍 Analisi Discrepanze")

            if reali == 0:
                st.info("ℹ️ **Cartellino senza timbrature dettagliate**")
                st.write(f"📋 Giorni pagati in busta: **{int(pagati)}**")
                st.success("✅ Usa i giorni pagati dalla busta come riferimento")
            else:
                diff = reali - pagati

                col_a, col_b = st.columns(2)
                col_a.metric("Giorni Pagati (Busta)", pagati)
                col_b.metric("Giorni Lavorati (Cartellino)", reali, delta=f"{diff:.1f}")

                st.markdown("---")

                if abs(diff) < 0.5:
                    st.success("✅ **Perfetto!** Giorni lavorati = giorni pagati")
                elif diff > 0:
                    st.info(
                        f"ℹ️ Hai lavorato **{diff:.1f} giorni in più**\n\n"
                        f"Controlla Straordinari: € {db.get('competenze', {}).get('straordinari', 0):.2f}"
                    )
                else:
                    st.warning(
                        f"⚠️ **{abs(diff):.1f} giorni pagati in più**\n\n"
                        f"Possibili cause:\n"
                        f"- Ferie godute: {db.get('ferie', {}).get('godute', 0):.2f} giorni\n"
                        f"- Permessi: {db.get('par', {}).get('fruite', 0):.2f} ore"
                    )
        elif tipo == "tredicesima":
            st.info("ℹ️ Analisi non disponibile per Tredicesima")
        else:
            st.warning("⚠️ Servono entrambi i documenti")
