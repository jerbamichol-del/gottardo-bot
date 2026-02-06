# main_app.py
import os
import sys
import time
import json
import re
import base64
import calendar
import locale
from pathlib import Path

import streamlit as st
from playwright.sync_api import sync_playwright

# Optional deps
try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None

try:
    import google.generativeai as genai
except Exception:
    genai = None

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


# ----------------------------
# Setup
# ----------------------------
# In Streamlit Cloud di solito playwright è già installato, ma lo lasciamo per compatibilità.
os.system("playwright install chromium")

if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

try:
    locale.setlocale(locale.LC_TIME, "it_IT.UTF-8")
except Exception:
    pass


# ----------------------------
# Secrets / credentials
# ----------------------------
def get_credentials_defaults():
    try:
        return st.secrets["ZK_USER"], st.secrets["ZK_PASS"]
    except Exception:
        return "", ""


def get_credentials():
    if st.session_state.get("credentials_set"):
        return st.session_state.get("username", ""), st.session_state.get("password", "")
    return get_credentials_defaults()


# ----------------------------
# AI configuration
# ----------------------------
HAS_GEMINI = False
GOOGLE_API_KEY = None
if genai is not None:
    try:
        GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
        genai.configure(api_key=GOOGLE_API_KEY)
        HAS_GEMINI = True
    except Exception:
        HAS_GEMINI = False

HAS_DEEPSEEK = False
DEEPSEEK_API_KEY = None
try:
    DEEPSEEK_API_KEY = st.secrets["DEEPSEEK_API_KEY"]
    HAS_DEEPSEEK = True
except Exception:
    HAS_DEEPSEEK = False
    DEEPSEEK_API_KEY = None


# ----------------------------
# Helpers
# ----------------------------
def parse_it_number(s: str) -> float:
    if s is None:
        return 0.0
    s = str(s).strip()
    if not s:
        return 0.0
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except Exception:
        return 0.0


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
    if not fitz:
        return None
    try:
        doc = fitz.open(file_path)
        chunks = []
        for page in doc:
            chunks.append(page.get_text())
        return "\n".join(chunks)
    except Exception:
        return None


def get_pdf_download_link(file_path, filename):
    if not file_path or not os.path.exists(file_path):
        return None
    with open(file_path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    return f'<a href="data:application/pdf;base64,{data}" download="{filename}">Scarica {filename}</a>'


# ----------------------------
# Prompts
# ----------------------------
def get_busta_prompt():
    return """
Analizza CEDOLINO PAGA (Italia). Restituisci SOLO JSON valido.

Campi:
dati_generali:
- netto: float
- giorni_pagati: float (GG. INPS)

competenze:
- base, straordinari, festivita, anzianita, lordo_totale (float)

trattenute:
- inps, irpef_netta, addizionali_totali (float)

ferie: residue_ap, maturate, godute, saldo (float)
par: residue_ap, spettanti, fruite, saldo (float)

JSON:
{
  "dati_generali": {"netto": 0.0, "giorni_pagati": 0.0},
  "competenze": {"base": 0.0, "straordinari": 0.0, "festivita": 0.0, "anzianita": 0.0, "lordo_totale": 0.0},
  "trattenute": {"inps": 0.0, "irpef_netta": 0.0, "addizionali_totali": 0.0},
  "ferie": {"residue_ap": 0.0, "maturate": 0.0, "godute": 0.0, "saldo": 0.0},
  "par": {"residue_ap": 0.0, "spettanti": 0.0, "fruite": 0.0, "saldo": 0.0}
}
Se manca un valore -> 0.0.
""".strip()


def get_cartellino_prompt():
    return r"""
Analizza CARTELLINO PRESENZE. Restituisci SOLO JSON valido.

Campi:
- giorni_reali: conta token \b[LMGVSD]\d{2}\b
- gg_presenza: da "0265 GG PRESENZA"
- ore_ordinarie_0251: da "0251 ORE ORDINARIE"
- ore_lavorate_0253: da "0253 ORE LAVORATE"
- ore_ordinarie_riepilogo: se presente
- giorni_senza_badge: 0 se incerto
- note: string breve

JSON:
{
  "giorni_reali": 0.0,
  "gg_presenza": 0.0,
  "ore_ordinarie_riepilogo": 0.0,
  "ore_ordinarie_0251": 0.0,
  "ore_lavorate_0253": 0.0,
  "giorni_senza_badge": 0.0,
  "note": ""
}
""".strip()


# ----------------------------
# AI extraction (Gemini -> DeepSeek)
# ----------------------------
@st.cache_resource
def _get_gemini_model():
    if not HAS_GEMINI:
        return None
    try:
        models = list(genai.list_models())
        # prefer "flash" se presente
        preferred = None
        for m in models:
            name = (m.name or "").lower()
            if "generatecontent" in str(getattr(m, "supported_generation_methods", "")).lower():
                if "flash" in name and "lite" not in name:
                    preferred = m.name
                    break
        if not preferred:
            # fallback: primo generative
            for m in models:
                if "generatecontent" in str(getattr(m, "supported_generation_methods", "")).lower():
                    preferred = m.name
                    break
        if not preferred:
            return None
        return genai.GenerativeModel(preferred)
    except Exception:
        return None


def estrai_con_ai(file_path: str, prompt: str, tipo: str):
    if not file_path or not os.path.exists(file_path):
        return None

    status = st.empty()

    # 1) Gemini: PDF nativo
    if HAS_GEMINI and genai is not None:
        try:
            status.info(f"Analisi {tipo} (Gemini)...")
            model = _get_gemini_model()
            if model:
                with open(file_path, "rb") as f:
                    pdf_bytes = f.read()
                resp = model.generate_content([prompt, {"mime_type": "application/pdf", "data": pdf_bytes}])
                res = clean_json_response(getattr(resp, "text", "") or "")
                if isinstance(res, dict):
                    status.empty()
                    return res
        except Exception as e:
            # continua su fallback
            status.warning(f"Gemini fallito, fallback attivo. ({type(e).__name__})")

    # 2) DeepSeek: via OpenAI-compatible API (testo estratto)
    if HAS_DEEPSEEK and OpenAI is not None and DEEPSEEK_API_KEY:
        try:
            status.info(f"Analisi {tipo} (DeepSeek)...")
            text = extract_text_from_pdf(file_path)
            if not text or len(text.strip()) < 50:
                status.empty()
                return None

            client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")

            full_prompt = f"{prompt}\n\n--- TESTO DOCUMENTO (estratto) ---\n{text[:25000]}"
            resp = client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "Rispondi solo con JSON valido, senza testo extra."},
                    {"role": "user", "content": full_prompt},
                ],
                temperature=0.1,
            )
            raw = resp.choices[0].message.content
            res = clean_json_response(raw)
            if isinstance(res, dict):
                status.empty()
                return res
        except Exception as e:
            status.error(f"DeepSeek fallito: {type(e).__name__}")
            status.empty()
            return None

    status.empty()
    return None


def cartellino_parse_deterministico(file_path: str):
    text = extract_text_from_pdf(file_path)
    if not text:
        return None
    upper = text.upper()

    giorni_reali = float(len(set(re.findall(r"\b[LMGVSD]\d{2}\b", upper))))
    m = re.search(r"0265\s+GG\s+PRESENZA.*?(\d{1,3}[.,]\d{2})", upper)
    gg_presenza = parse_it_number(m.group(1)) if m else 0.0

    m1 = re.search(r"0251\s+ORE\s+ORDINARIE.*?(\d{1,3}[.,]\d{2})", upper)
    ore_ord_0251 = parse_it_number(m1.group(1)) if m1 else 0.0

    m2 = re.search(r"0253\s+ORE\s+LAVORATE.*?(\d{1,3}[.,]\d{2})", upper)
    ore_lav_0253 = parse_it_number(m2.group(1)) if m2 else 0.0

    return {
        "giorni_reali": giorni_reali,
        "gg_presenza": gg_presenza,
        "ore_ordinarie_riepilogo": 0.0,
        "ore_ordinarie_0251": ore_ord_0251,
        "ore_lavorate_0253": ore_lav_0253,
        "giorni_senza_badge": 0.0,
        "note": "Parser deterministico (regex).",
    }


# ----------------------------
# Agenda patch
# ----------------------------
AGENDA_KEYWORDS = [
    "OMESSA TIMBRATURA",
    "MALATTIA",
    "RIPOSO",
    "FERIE",
    "PERMESS",
    "CHIUSURA",
    "INFORTUN",
]


def _dump_frames_for_debug(page, debug_info):
    try:
        frames = list(page.frames)
        for i, fr in enumerate(frames):
            try:
                c1 = fr.locator("#revit_form_Button_6").count()
                c2 = fr.locator("span.popup-trigger:has(.calendar16)").count()
                c3 = fr.locator(".calendar16").count()
                debug_info.append(
                    f"Agenda[frames] {i} name={fr.name!r} url={fr.url[:90]!r} #6={c1} popup-trigger+icon={c2} icon={c3}"
                )
            except Exception:
                debug_info.append(f"Agenda[frames] {i} scan_error")
    except Exception:
        debug_info.append("Agenda[frames] dump_failed")


def agenda_set_month_enter(page, mese_num, anno, debug_info):
    nomi_mesi_it = [
        "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
        "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"
    ]
    mese_nome_target = nomi_mesi_it[mese_num - 1]

    try:
        page.wait_for_selector(".dijitToolbar", timeout=20000)
    except Exception:
        debug_info.append("Agenda: toolbar_not_found")

    opener = None
    opener_frame = None

    # Cerca in page + frames (page.frames include anche annidati)
    for fr in [page] + list(page.frames):
        try:
            cand = fr.locator("#revit_form_Button_6")
            if cand.count() > 0:
                opener, opener_frame = cand.first, fr
                break

            cand = fr.locator("span.popup-trigger:has(.calendar16)")
            if cand.count() > 0:
                opener, opener_frame = cand.first, fr
                break
        except Exception:
            continue

    if opener is None:
        debug_info.append("Agenda: opener_not_found")
        _dump_frames_for_debug(page, debug_info)
        return

    try:
        opener.wait_for(state="visible", timeout=20000)
        opener.scroll_into_view_if_needed()
        opener.click(timeout=20000)
        debug_info.append("Agenda: opener_click_ok")
    except Exception as e1:
        debug_info.append(f"Agenda: opener_click_fail_normal {type(e1).__name__}")
        try:
            opener.scroll_into_view_if_needed()
            opener.click(timeout=20000, force=True)
            debug_info.append("Agenda: opener_click_ok_force")
        except Exception as e2:
            debug_info.append(f"Agenda: opener_click_fail_force {type(e2).__name__}")
            _dump_frames_for_debug(page, debug_info)
            return

    # popup context: spesso è sul page, ma proviamo anche sul frame
    popup_ctx = None
    try:
        page.wait_for_selector(".dijitCalendarMonthLabel", timeout=15000)
        popup_ctx = page
    except Exception:
        try:
            opener_frame.wait_for_selector(".dijitCalendarMonthLabel", timeout=15000)
            popup_ctx = opener_frame
        except Exception:
            debug_info.append("Agenda: popup_not_detected")
            return

    labels = popup_ctx.locator(".dijitCalendarMonthLabel")
    if labels.count() < 2:
        debug_info.append("Agenda: popup_labels_missing")
        return

    # Mese
    try:
        labels.nth(0).click()
        time.sleep(0.3)
        popup_ctx.locator("body").get_by_text(mese_nome_target, exact=True).last.click(timeout=8000)
        time.sleep(0.3)
        debug_info.append("Agenda: month_set_ok")
    except Exception as e:
        debug_info.append(f"Agenda: month_set_fail {type(e).__name__}")

    # Anno
    try:
        labels.nth(1).click()
        time.sleep(0.3)
        popup_ctx.locator("body").get_by_text(str(anno), exact=True).last.click(timeout=8000)
        time.sleep(0.3)
        debug_info.append("Agenda: year_set_ok")
    except Exception as e:
        debug_info.append(f"Agenda: year_set_fail {type(e).__name__}")

    # Conferma: click giorno 1
    try:
        popup_ctx.locator(".dijitCalendarDateTemplate", has_text=re.compile(r"^1$")).first.click(timeout=8000)
        time.sleep(1.0)
        debug_info.append("Agenda: day_confirm_ok")
    except Exception as e:
        debug_info.append(f"Agenda: day_confirm_fail {type(e).__name__}")

    # Vista Mese (obbligatorio)
    try:
        btn_mese = opener_frame.locator("[aria-label='Mese']").first
        if btn_mese.count() == 0:
            btn_mese = page.locator("[aria-label='Mese']").first
        if btn_mese.count() == 0:
            btn_mese = opener_frame.locator(".dijitButtonText", has_text=re.compile(r"^\s*Mese\s*$", re.I)).first

        if btn_mese.count() > 0:
            btn_mese.scroll_into_view_if_needed()
            btn_mese.click(timeout=15000, force=True)
            time.sleep(2.0)
            debug_info.append("Agenda: view_mese_ok")
        else:
            debug_info.append("Agenda: view_mese_not_found")
    except Exception as e:
        debug_info.append(f"Agenda: view_mese_fail {type(e).__name__}")


def agenda_extract_events(page):
    texts = []

    def rec_extract(ctx):
        out = []
        cands = ctx.locator("[class*='event'], [class*='appointment'], .dijitCalendarEvent")
        try:
            n = min(cands.count(), 300)
            for i in range(n):
                t = (cands.nth(i).inner_text() or "").strip()
                if t:
                    out.append(t)
        except Exception:
            pass

        for f in getattr(ctx, "frames", []) or getattr(ctx, "child_frames", []):
            if f != ctx:
                out.extend(rec_extract(f))
        return out

    texts = rec_extract(page)
    blob = "\n".join(texts)
    up = blob.upper()
    counts = {k: up.count(k) for k in AGENDA_KEYWORDS}
    lines = []
    for s in blob.splitlines():
        ss = (s or "").strip()
        if not ss:
            continue
        if any(k in ss.upper() for k in AGENDA_KEYWORDS):
            lines.append(ss)
    # dedup
    uniq = []
    seen = set()
    for s in lines:
        if s in seen:
            continue
        seen.add(s)
        uniq.append(s)

    return {"counts": counts, "lines": uniq[:200], "raw_len": len(blob)}


# ----------------------------
# Downloader (login + busta + cartellino + agenda)
# ----------------------------
def scarica_documenti_automatici(mese_nome, anno, username, password, tipo_documento):
    nomi_mesi_it = [
        "Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
        "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"
    ]
    try:
        mese_num = nomi_mesi_it.index(mese_nome) + 1
    except Exception:
        return None, None, "MESE_INVALIDO"

    wd = Path.cwd()
    suffix = "_13" if tipo_documento == "tredicesima" else ""
    path_busta = str(wd / f"busta_{mese_num}_{anno}{suffix}.pdf")
    path_cart = str(wd / f"cartellino_{mese_num}_{anno}.pdf")

    target_busta = f"Tredicesima {anno}" if tipo_documento == "tredicesima" else f"{mese_nome} {anno}"

    last_day = calendar.monthrange(anno, mese_num)[1]
    d_from_vis = f"01/{mese_num:02d}/{anno}"
    d_to_vis = f"{last_day}/{mese_num:02d}/{anno}"

    st_status = st.empty()
    st_status.info("Login in corso...")
    debug_info = []
    busta_ok = False
    cart_ok = False

    st.session_state.pop("login_error_png", None)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                slow_mo=250,
                args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
            )
            context = browser.new_context(
                accept_downloads=True,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            context.set_default_timeout(60000)

            page = context.new_page()
            page.set_viewport_size({"width": 1920, "height": 1080})

            # --------------------
            # LOGIN robusto
            # --------------------
            try:
                page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y", wait_until="domcontentloaded", timeout=60000)
                page.wait_for_load_state("domcontentloaded")

                # se form presente, compila
                if page.locator('input[type="text"]').count() > 0:
                    page.fill('input[type="text"]', username)
                    page.fill('input[type="password"]', password)
                    # enter è spesso ok
                    page.press('input[type="password"]', "Enter")

                # attesa "qualcosa" post login
                ok = False
                for _ in range(30):  # ~60s
                    time.sleep(2)
                    if page.locator("text=I miei dati").count() > 0:
                        ok = True
                        break
                    # alcune schermate "utente già collegato" cambiano testo: gestiamo genericamente
                    if page.locator("text=collegat").count() > 0 or page.locator("text=session").count() > 0:
                        # prova a cliccare un bottone "Continua"/"Si"
                        btn = page.locator("text=Continua").first
                        if btn.count() > 0:
                            btn.click(force=True)
                        btn = page.locator("text=Si").first
                        if btn.count() > 0:
                            btn.click(force=True)

                if not ok:
                    page.wait_for_selector("text=I miei dati", timeout=10000)

                debug_info.append("Login: OK")
            except Exception:
                png = str(wd / "login_error.png")
                try:
                    page.screenshot(path=png, full_page=True)
                    st.session_state["login_error_png"] = png
                except Exception:
                    pass
                browser.close()
                st.session_state["debug_info"] = debug_info
                return None, None, "LOGIN_TIMEOUT"

            # --------------------
            # AGENDA
            # --------------------
            try:
                st_status.info("Lettura agenda...")
                agenda_set_month_enter(page, mese_num, anno, debug_info)
                agenda_data = agenda_extract_events(page)
                st.session_state["agenda_data"] = agenda_data
                debug_info.append(f"Agenda: OK raw_len={agenda_data.get('raw_len', 0)}")
            except Exception as e:
                debug_info.append(f"Agenda Error: {type(e).__name__}")

            # --------------------
            # BUSTA (Documenti -> Cedolino)
            # --------------------
            st_status.info("Download busta...")
            try:
                page.click("text=I miei dati", force=True)
                page.wait_for_selector("text=Documenti", timeout=20000).click()
                time.sleep(3)
                try:
                    page.click("text=Cedolino", force=True)
                except Exception:
                    pass
                time.sleep(3)

                links = page.locator("a")
                found_link = None
                for i in range(links.count()):
                    txt = (links.nth(i).inner_text() or "").strip()
                    if not txt:
                        continue
                    low = txt.lower()
                    if target_busta.lower() in low:
                        found_link = links.nth(i)
                        break

                if found_link is not None:
                    with page.expect_download(timeout=60000) as dl_info:
                        found_link.click()
                    dl_info.value.save_as(path_busta)
                    if os.path.exists(path_busta) and os.path.getsize(path_busta) > 5000:
                        busta_ok = True
                        debug_info.append("Busta: OK")
                else:
                    debug_info.append("Busta: link_non_trovato")

            except Exception as e:
                debug_info.append(f"Busta Error: {type(e).__name__}")

            # --------------------
            # CARTELLINO (tuo blocco EMBED=y)
            # --------------------
            if tipo_documento != "tredicesima":
                st_status.info("Download cartellino...")
                try:
                    # Torna alla home
                    page.evaluate("window.scrollTo(0, 0)")
                    time.sleep(1)
                    try:
                        page.keyboard.press("Escape")
                        time.sleep(0.5)
                    except Exception:
                        pass

                    try:
                        logo = page.locator("img[src*='logo'], .logo").first
                        if logo.is_visible(timeout=2000):
                            logo.click()
                            time.sleep(2)
                    except Exception:
                        page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2", wait_until="domcontentloaded")
                        time.sleep(3)

                    # Vai su Time
                    page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
                    time.sleep(3)

                    # Vai su Cartellino presenze
                    page.evaluate("document.getElementById('lnktab_5_label')?.click()")
                    time.sleep(5)

                    # Imposta date
                    try:
                        dal = page.locator("input[id*='CLRICHIE'][class*='dijitInputInner']").first
                        al = page.locator("input[id*='CLRICHI2'][class*='dijitInputInner']").first

                        if dal.count() > 0 and al.count() > 0:
                            dal.click(force=True)
                            page.keyboard.press("Control+A")
                            dal.fill("")
                            dal.type(d_from_vis, delay=80)
                            dal.press("Tab")
                            time.sleep(0.5)

                            al.click(force=True)
                            page.keyboard.press("Control+A")
                            al.fill("")
                            al.type(d_to_vis, delay=80)
                            al.press("Tab")
                            time.sleep(0.5)
                    except Exception:
                        pass

                    # Esegui ricerca
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    time.sleep(0.5)
                    try:
                        page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last.click(force=True)
                    except Exception:
                        page.keyboard.press("Enter")

                    # Attendi risultati
                    try:
                        page.wait_for_selector("text=Risultati della ricerca", timeout=20000)
                    except Exception:
                        pass

                    # Trova riga e lente
                    target_cart_row = f"{mese_num:02d}/{anno}"  # es. 12/2025
                    riga_row = page.locator(f"tr:has-text('{target_cart_row}')").first
                    if riga_row.count() > 0 and riga_row.locator("img[src*='search']").count() > 0:
                        icona = riga_row.locator("img[src*='search']").first
                    else:
                        icona = page.locator("img[src*='search']").first

                    if icona.count() == 0:
                        page.pdf(path=path_cart)
                    else:
                        # Click -> popup
                        with context.expect_page(timeout=20000) as popup_info:
                            icona.click()
                        popup = popup_info.value

                        # Prendi URL del popup (normalizza eventuale doppio slash)
                        popup_url = (popup.url or "").replace("/js_rev//", "/js_rev/")

                        # FORZA EMBED=y
                        if "EMBED=y" not in popup_url:
                            popup_url = popup_url + ("&" if "?" in popup_url else "?") + "EMBED=y"

                        resp = context.request.get(popup_url, timeout=60000)
                        body = resp.body()

                        if body[:4] == b"%PDF":
                            Path(path_cart).write_bytes(body)
                        else:
                            try:
                                popup.pdf(path=path_cart, format="A4")
                            except Exception:
                                page.pdf(path=path_cart)

                        try:
                            popup.close()
                        except Exception:
                            pass

                    if os.path.exists(path_cart) and os.path.getsize(path_cart) > 5000:
                        cart_ok = True
                        debug_info.append("Cartellino: OK")
                    else:
                        debug_info.append("Cartellino: piccolo_o_vuoto")

                except Exception as e:
                    debug_info.append(f"Cartellino Error: {type(e).__name__}")
                    try:
                        page.pdf(path=path_cart)
                    except Exception:
                        pass

            browser.close()
            st_status.empty()

    except Exception as e:
        st.session_state["debug_info"] = debug_info
        return None, None, f"BOT_ERROR:{type(e).__name__}"

    st.session_state["debug_info"] = debug_info
    return (path_busta if busta_ok else None), (path_cart if cart_ok else None), None


# ----------------------------
# UI
# ----------------------------
st.set_page_config(page_title="Analisi Stipendio & Presenze", layout="wide")
st.title("Analisi Stipendio & Presenze")

with st.sidebar:
    st.header("Credenziali")
    default_u, default_p = get_credentials_defaults()

    if not st.session_state.get("credentials_set"):
        u = st.text_input("User", value=default_u or "")
        p = st.text_input("Pass", type="password", value="")
        if st.button("Salva"):
            st.session_state["username"] = u
            st.session_state["password"] = p
            st.session_state["credentials_set"] = True
            st.rerun()
    else:
        st.success(f"Loggato: {st.session_state.get('username', '')}")
        if st.button("Esci"):
            st.session_state["credentials_set"] = False
            st.rerun()

    st.divider()

    if st.session_state.get("credentials_set"):
        sel_anno = st.selectbox("Anno", [2024, 2025, 2026], index=1)
        sel_mese = st.selectbox(
            "Mese",
            ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
             "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"],
            index=0,
        )
        tipo_label = st.radio("Tipo", ["Cedolino", "Tredicesima"], index=0)

        if st.button("AVVIA ANALISI", type="primary"):
            # reset session state (solo dati)
            for k in ["agenda_data", "debug_info", "busta", "cart", "db", "dc", "done", "login_error_png"]:
                st.session_state.pop(k, None)

            tipo_doc = "tredicesima" if tipo_label.lower().startswith("tred") else "cedolino"
            pb, pc, err = scarica_documenti_automatici(
                sel_mese,
                sel_anno,
                st.session_state.get("username", ""),
                st.session_state.get("password", ""),
                tipo_doc,
            )

            if err:
                st.error(err)
                if err == "LOGIN_TIMEOUT":
                    png = st.session_state.get("login_error_png")
                    if png and os.path.exists(png):
                        st.image(png, caption="Schermata login (debug)")
            else:
                st.session_state["busta"] = pb
                st.session_state["cart"] = pc
                st.session_state["tipo_doc"] = tipo_doc
                st.session_state["done"] = False

# Analisi post-download
if (st.session_state.get("busta") or st.session_state.get("cart")) and not st.session_state.get("done"):
    with st.spinner("Analisi documenti..."):
        db = estrai_con_ai(st.session_state.get("busta"), get_busta_prompt(), "Busta")
        dc = None

        if st.session_state.get("cart"):
            # 1) deterministico
            dc = cartellino_parse_deterministico(st.session_state["cart"])
            # 2) fallback AI
            if not dc or float(dc.get("gg_presenza", 0) or 0) == 0.0:
                dc_ai = estrai_con_ai(st.session_state["cart"], get_cartellino_prompt(), "Cartellino")
                if isinstance(dc_ai, dict):
                    dc = dc_ai

        st.session_state["db"] = db
        st.session_state["dc"] = dc
        st.session_state["done"] = True

# Tabs UI
tab1, tab2, tab3, tab4 = st.tabs(["Dettaglio Stipendio", "Cartellino & Presenze", "Analisi & Confronto", "Debug"])

with tab1:
    db = st.session_state.get("db")
    if not db:
        st.warning("Nessun dato busta.")
    else:
        dg = db.get("dati_generali", {}) if isinstance(db, dict) else {}
        comp = db.get("competenze", {}) if isinstance(db, dict) else {}
        tr = db.get("trattenute", {}) if isinstance(db, dict) else {}

        c1, c2, c3 = st.columns(3)
        c1.metric("Netto", f"{dg.get('netto', 0.0)}")
        c2.metric("Giorni pagati", f"{dg.get('giorni_pagati', 0.0)}")
        c3.metric("Lordo totale", f"{comp.get('lordo_totale', 0.0)}")

        st.subheader("Dettaglio (JSON)")
        st.json(db)

with tab2:
    dc = st.session_state.get("dc")
    ad = st.session_state.get("agenda_data")

    c1, c2, c3 = st.columns(3)
    if isinstance(dc, dict):
        c1.metric("GG Presenza", f"{dc.get('gg_presenza', 0.0)}")
        c2.metric("Ore lavorate (0253)", f"{dc.get('ore_lavorate_0253', 0.0)}")
        c3.metric("Giorni reali", f"{dc.get('giorni_reali', 0.0)}")
        st.subheader("Dettaglio cartellino (JSON)")
        st.json(dc)
    else:
        st.warning("Cartellino non analizzato o mancante.")

    st.divider()
    st.subheader("Agenda (conteggi)")
    if isinstance(ad, dict):
        cols = st.columns(4)
        for i, k in enumerate(AGENDA_KEYWORDS):
            cols[i % 4].metric(k, int(ad.get("counts", {}).get(k, 0)))
        with st.expander("Righe trovate (Agenda)"):
            st.write(ad.get("lines", []))
    else:
        st.info("Agenda non disponibile.")

with tab3:
    db = st.session_state.get("db")
    dc = st.session_state.get("dc")

    if isinstance(db, dict) and isinstance(dc, dict):
        gg_busta = float(db.get("dati_generali", {}).get("giorni_pagati", 0.0) or 0.0)
        gg_cart = float(dc.get("gg_presenza", 0.0) or 0.0)
        st.metric("Differenza giorni (cartellino - busta)", f"{(gg_cart - gg_busta):.2f}")
    else:
        st.info("Servono busta e cartellino analizzati per il confronto.")

with tab4:
    st.subheader("Log")
    st.write(st.session_state.get("debug_info", []))

    st.subheader("Download")
    if st.session_state.get("busta"):
        link = get_pdf_download_link(st.session_state["busta"], "busta.pdf")
        if link:
            st.markdown(link, unsafe_allow_html=True)
    if st.session_state.get("cart"):
        link = get_pdf_download_link(st.session_state["cart"], "cartellino.pdf")
        if link:
            st.markdown(link, unsafe_allow_html=True)
