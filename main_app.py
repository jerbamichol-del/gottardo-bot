import sys
import asyncio
import re
import os
import time
import json
import calendar
import locale
import streamlit as st
import google.generativeai as genai
from playwright.sync_api import sync_playwright
from pathlib import Path

# --- GESTIONE DIPENDENZE ---
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

# --- SETUP SISTEMA ---
os.system("playwright install chromium")
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
try:
    locale.setlocale(locale.LC_TIME, 'it_IT.UTF-8')
except:
    pass

# --- CREDENZIALI ---
def get_credentials():
    if 'credentials_set' in st.session_state and st.session_state.get('credentials_set'):
        return st.session_state.get('username'), st.session_state.get('password')
    try:
        return st.secrets["ZK_USER"], st.secrets["ZK_PASS"]
    except:
        return None, None

# --- CONFIGURAZIONE AI ---
# 1) GEMINI
try:
    GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
    genai.configure(api_key=GOOGLE_API_KEY)
    HAS_GEMINI = True
except:
    HAS_GEMINI = False

# 2) DEEPSEEK / OPENAI SDK
try:
    DEEPSEEK_API_KEY = st.secrets["DEEPSEEK_API_KEY"]
    HAS_DEEPSEEK = True
except:
    DEEPSEEK_API_KEY = None
    HAS_DEEPSEEK = False


# -------------------------
# HELPERS
# -------------------------
def parse_it_number(s: str) -> float:
    if s is None:
        return 0.0
    s = str(s).strip()
    if not s:
        return 0.0
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except:
        return 0.0

def clean_json_response(text):
    try:
        text = re.sub(r"```json|```", "", text).strip()
        start = text.find('{')
        end = text.rfind('}') + 1
        return json.loads(text[start:end]) if start != -1 else json.loads(text)
    except:
        return None

def extract_text_from_pdf(file_path):
    """Estrae testo da PDF via PyMuPDF (fitz)."""
    if not fitz:
        return None
    try:
        doc = fitz.open(file_path)
        chunks = []
        for page in doc:
            chunks.append(page.get_text())
        return "\n".join(chunks)
    except:
        return None

def cartellino_parse_deterministico(file_path: str):
    """
    Parser deterministic per cartellino:
    - giorni_reali: conteggio token unici L01/M02/... presenti nel testo
    - gg_presenza: valore in riga 0265
    - ore_ordinarie_0251 / ore_lavorate_0253 / ore_ordinarie_riepilogo
    """
    text = extract_text_from_pdf(file_path)
    if not text or len(text.strip()) < 20:
        return None

    upper = text.upper()

    # Vuoto reale (se compare "NESSUN DATO" e non ci sono segnali di timbrature)
    has_days = re.search(r"\b[LMGVSD]\d{2}\b", upper) is not None
    has_timbr = "TIMBRATURE" in upper
    if ("NESSUN DATO" in upper) and (not has_days) and (not has_timbr):
        return {
            "giorni_reali": 0.0,
            "gg_presenza": 0.0,
            "ore_ordinarie_riepilogo": 0.0,
            "ore_ordinarie_0251": 0.0,
            "ore_lavorate_0253": 0.0,
            "giorni_senza_badge": 0.0,
            "note": "Cartellino vuoto (testo contiene 'Nessun dato' e nessuna timbratura).",
            "debug_prime_righe": "\n".join(text.splitlines()[:35])
        }

    # Giorni timbrati (token unici)
    day_tokens = sorted(set(re.findall(r"\b[LMGVSD]\d{2}\b", upper)))
    giorni_reali = float(len(day_tokens))

    # GG PRESENZA (0265)
    m = re.search(r"0265\s+GG\s+PRESENZA.*?(\d{1,3}[.,]\d{2})", upper)
    gg_presenza = parse_it_number(m.group(1)) if m else 0.0

    # Ore 0251 / 0253
    m1 = re.search(r"0251\s+ORE\s+ORDINARIE.*?(\d{1,3}[.,]\d{2})", upper)
    ore_ord_0251 = parse_it_number(m1.group(1)) if m1 else 0.0

    m2 = re.search(r"0253\s+ORE\s+LAVORATE.*?(\d{1,3}[.,]\d{2})", upper)
    ore_lav_0253 = parse_it_number(m2.group(1)) if m2 else 0.0

    # Riga riepilogo tipo: " 160,00 7,00 13,00 15,00 7,00"
    ore_riep = 0.0
    for line in text.splitlines():
        ln = line.strip()
        if not ln:
            continue
        # evita righe "0251 ..." e simili
        if re.search(r"\b02\d{2}\b", ln):
            continue
        if re.match(r"^\d{1,3}[.,]\d{2}(\s+\d{1,3}[.,]\d{2}){2,}$", ln):
            first_num = re.findall(r"\d{1,3}[.,]\d{2}", ln)
            if first_num:
                ore_riep = parse_it_number(first_num[0])
                break

    # Giorni senza badge: non sempre deducibile senza regole aziendali; lo lasciamo 0 (ma senza inventare)
    giorni_senza_badge = 0.0

    note_parts = []
    if gg_presenza > 0:
        note_parts.append(f"Rilevato 0265 GG PRESENZA={gg_presenza:.2f}.")
    if ore_ord_0251 > 0:
        note_parts.append(f"Rilevato 0251 ORE ORDINARIE={ore_ord_0251:.2f}.")
    if ore_lav_0253 > 0:
        note_parts.append(f"Rilevato 0253 ORE LAVORATE={ore_lav_0253:.2f}.")
    if ore_riep > 0:
        note_parts.append(f"Riepilogo ore (prima colonna)={ore_riep:.2f}.")
    if giorni_reali > 0:
        note_parts.append(f"Giorni con token (L01/M02/...)={int(giorni_reali)}.")

    # Se NON ho trovato nessun segnale forte, considera parsing fallito
    strong = (gg_presenza > 0) or (ore_ord_0251 > 0) or (ore_lav_0253 > 0) or (ore_riep > 0) or (giorni_reali > 0 and has_timbr)
    if not strong:
        return None

    return {
        "giorni_reali": giorni_reali,
        "gg_presenza": gg_presenza,
        "ore_ordinarie_riepilogo": ore_riep,
        "ore_ordinarie_0251": ore_ord_0251,
        "ore_lavorate_0253": ore_lav_0253,
        "giorni_senza_badge": giorni_senza_badge,
        "note": " ".join(note_parts) if note_parts else "Cartellino parsato da testo.",
        "debug_prime_righe": "\n".join(text.splitlines()[:35])
    }

@st.cache_resource
def get_gemini_models():
    if not HAS_GEMINI:
        return []
    try:
        models = genai.list_models()
        valid = [m for m in models if 'generateContent' in m.supported_generation_methods]
        gemini_list = []
        for m in valid:
            name = m.name.replace('models/', '')
            if 'gemini' in name.lower() and 'embedding' not in name.lower():
                try:
                    gemini_list.append((name, genai.GenerativeModel(name)))
                except:
                    pass

        def priority(n):
            n = n.lower()
            if 'flash' in n and 'lite' not in n:
                return 0
            if 'lite' in n:
                return 1
            if 'pro' in n:
                return 2
            return 3

        gemini_list.sort(key=lambda x: priority(x[0]))
        return gemini_list
    except:
        return []

def estrai_con_fallback(file_path, prompt, tipo, validate_fn=None):
    if not file_path or not os.path.exists(file_path):
        return None

    status = st.empty()

    # 1) TENTATIVO GEMINI (PDF nativo)
    models = get_gemini_models()
    if models:
        try:
            with open(file_path, "rb") as f:
                pdf_bytes = f.read()
        except:
            pdf_bytes = None

        if pdf_bytes and pdf_bytes[:4] == b"%PDF":
            for name, model in models:
                try:
                    status.info(f"🤖 Analisi {tipo} (Gemini: {name})...")
                    resp = model.generate_content([prompt, {"mime_type": "application/pdf", "data": pdf_bytes}])
                    res = clean_json_response(resp.text)

                    if res and isinstance(res, dict):
                        if validate_fn and not validate_fn(res):
                            continue
                        status.success(f"✅ {tipo} analizzato (Gemini)")
                        time.sleep(0.3)
                        status.empty()
                        return res
                except Exception as e:
                    msg = str(e).lower()
                    if "429" in msg or "quota" in msg or "resource_exhausted" in msg:
                        continue
                    continue

    # 2) TENTATIVO DEEPSEEK (testo estratto)
    if HAS_DEEPSEEK and OpenAI:
        text = extract_text_from_pdf(file_path)
        if text and len(text) > 50:
            try:
                status.warning(f"⚠️ Gemini non disponibile/esausto. Analisi {tipo} con DeepSeek...")
                client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
                full_prompt = f"{prompt}\n\n--- TESTO PDF (estratto) ---\n{text[:50000]}"

                resp = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[
                        {"role": "system", "content": "Sei un estrattore JSON. Rispondi solo con JSON valido."},
                        {"role": "user", "content": full_prompt}
                    ],
                    temperature=0.1
                )

                res = clean_json_response(resp.choices[0].message.content)
                if res and isinstance(res, dict):
                    if validate_fn and not validate_fn(res):
                        status.error("❌ Dati DeepSeek non validi")
                    else:
                        status.success(f"✅ {tipo} analizzato (DeepSeek)")
                        time.sleep(0.3)
                        status.empty()
                        return res
            except Exception as e:
                status.error(f"Errore DeepSeek: {e}")

    status.error(f"❌ Analisi {tipo} fallita")
    return None


# --- PROMPT AI ---
def get_busta_prompt():
    return """
Analizza CEDOLINO PAGA (Italia). Restituisci SOLO JSON valido, numeri con punto decimale.

Campi:
- e_tredicesima: true se è 13ma (Tredicesima/13ma/XIII), altrimenti false.

dati_generali:
- netto: cerca nella sezione PROGRESSIVI/NETTO.
- giorni_pagati: valore "GG. INPS" (o "giorni retribuiti").

competenze:
- base: voce retribuzione ordinaria (es. 1000) -> competenze.
- straordinari: somma voci straordinario/supplementare/notturno.
- festivita: somma voci festive/maggiorazioni/festività goduta.
- anzianita: scatti/anzianità se presenti, altrimenti 0.
- lordo_totale: totale competenze (o progressivi totale competenze).

trattenute:
- inps: trattenute INPS totali.
- irpef_netta: trattenuta IRPEF netta (conguaglio se presente).
- addizionali_totali: somma add.reg + add.com (anche se rateizzate).

ferie:
- residue_ap, maturate, godute, saldo.

par:
- residue_ap, spettanti, fruite, saldo.

JSON:
{
  "e_tredicesima": boolean,
  "dati_generali": {"netto": float, "giorni_pagati": float},
  "competenze": {"base": float, "straordinari": float, "festivita": float, "anzianita": float, "lordo_totale": float},
  "trattenute": {"inps": float, "irpef_netta": float, "addizionali_totali": float},
  "ferie": {"residue_ap": float, "maturate": float, "godute": float, "saldo": float},
  "par": {"residue_ap": float, "spettanti": float, "fruite": float, "saldo": float}
}
Se un valore manca -> 0.0.
""".strip()

def get_cartellino_prompt_ai_only():
    # usato SOLO come fallback se fitz non c'è o parsing deterministic fallisce
    return r"""
Analizza CARTELLINO PRESENZE. Restituisci SOLO JSON valido.

Obiettivi:
- giorni_reali: conta giorni con token \b[LMGVSD]\d{2}\b (L01/M02/...).
- gg_presenza: estrai valore da "0265 GG PRESENZA" se presente.
- ore_ordinarie_0251: da "0251 ORE ORDINARIE".
- ore_lavorate_0253: da "0253 ORE LAVORATE".
- ore_ordinarie_riepilogo: da riga tipo "160,00 7,00 13,00 15,00 ..." (primo numero).
- giorni_senza_badge: se non certo -> 0.
- debug_prime_righe: copia prime ~30 righe REALI dal PDF (non inventare).
- note: spiegazione breve.

JSON:
{
  "giorni_reali": float,
  "gg_presenza": float,
  "ore_ordinarie_riepilogo": float,
  "ore_ordinarie_0251": float,
  "ore_lavorate_0253": float,
  "giorni_senza_badge": float,
  "note": "string",
  "debug_prime_righe": "string"
}
""".strip()

def validate_cartellino_ai_fallback(res):
    # valida solo se porta almeno un numero sensato o una prova nel debug
    try:
        if any(float(res.get(k, 0) or 0) > 0 for k in ["gg_presenza", "ore_ordinarie_0251", "ore_lavorate_0253", "ore_ordinarie_riepilogo", "giorni_reali"]):
            return True
    except:
        pass
    txt = (str(res.get("debug_prime_righe", "")) + " " + str(res.get("note", ""))).upper()
    return ("TIMBRATURE" in txt) or ("GG PRESENZA" in txt) or ("0265" in txt) or ("0251" in txt) or ("0253" in txt)


# --- BOT DOWNLOAD ---
def scarica_documenti_automatici(mese_nome, anno, username, password, tipo_documento="cedolino"):
    nomi_mesi_it = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
                    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
    try:
        mese_num = nomi_mesi_it.index(mese_nome) + 1
    except:
        return None, None, "Mese invalido"

    wd = Path.cwd()
    suffix = "_13" if tipo_documento == "tredicesima" else ""
    path_busta = str(wd / f"busta_{mese_num}_{anno}{suffix}.pdf")
    path_cart = str(wd / f"cartellino_{mese_num}_{anno}.pdf")
    target_busta = f"Tredicesima {anno}" if tipo_documento == "tredicesima" else f"{mese_nome} {anno}"

    status = st.empty()
    status.info(f"🤖 Bot avviato: {mese_nome} {anno}")

    b_ok, c_ok = False, False

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                slow_mo=300,
                args=['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage']
            )
            context = browser.new_context(
                accept_downloads=True,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36"
            )
            context.set_default_timeout(45000)
            page = context.new_page()

            # LOGIN
            status.info("🔐 Login...")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y", wait_until="domcontentloaded")
            page.fill('input[type="text"]', username)
            page.fill('input[type="password"]', password)
            page.press('input[type="password"]', 'Enter')

            try:
                page.wait_for_selector("text=I miei dati", timeout=15000)
            except:
                browser.close()
                return None, None, "LOGIN_FALLITO"

            # BUSTA
            status.info("📄 Scarico busta...")
            page.click("text=I miei dati", force=True)
            page.click("text=Documenti", force=True)
            time.sleep(3)

            try:
                page.locator("tr", has=page.locator("text=Cedolino")).locator(".z-image").first.click()
            except:
                page.click("text=Cedolino", force=True)

            time.sleep(3)

            # Ricerca link target
            links = page.locator("a")
            idx = -1
            for i in range(links.count()):
                try:
                    t = (links.nth(i).inner_text() or "").strip()
                except:
                    continue
                if not t:
                    continue
                low = t.lower()
                if target_busta.lower() not in low:
                    continue

                # filtra 13ma in modo un po' più robusto
                is_13 = ("tredicesima" in low) or ("13ma" in low) or ("xiii" in low)
                if tipo_documento == "tredicesima" and not is_13:
                    continue
                if tipo_documento != "tredicesima" and is_13:
                    continue

                idx = i

            if idx >= 0:
                with page.expect_download(timeout=25000) as dl:
                    links.nth(idx).click()
                dl.value.save_as(path_busta)
                b_ok = os.path.exists(path_busta) and os.path.getsize(path_busta) > 5000
            else:
                b_ok = False

            # CARTELLINO
            if tipo_documento != "tredicesima":
                status.info("📅 Scarico cartellino...")
                page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2", wait_until="domcontentloaded")
                time.sleep(2)

                page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")  # Time
                time.sleep(2)
                page.evaluate("document.getElementById('lnktab_5_label')?.click()")  # Cartellino
                time.sleep(4)

                last = calendar.monthrange(anno, mese_num)[1]
                d1 = f"01/{mese_num:02d}/{anno}"
                d2 = f"{last}/{mese_num:02d}/{anno}"

                # campi data (più preciso)
                try:
                    dal = page.locator("input[id*='CLRICHIE'][class*='dijitInputInner']").first
                    al = page.locator("input[id*='CLRICHI2'][class*='dijitInputInner']").first

                    dal.click(force=True)
                    page.keyboard.press("Control+A")
                    dal.fill("")
                    dal.type(d1, delay=60)
                    dal.press("Tab")
                    time.sleep(0.3)

                    al.click(force=True)
                    page.keyboard.press("Control+A")
                    al.fill("")
                    al.type(d2, delay=60)
                    al.press("Tab")
                    time.sleep(0.3)
                except:
                    pass

                # Esegui ricerca
                try:
                    page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last.click(force=True)
                except:
                    page.keyboard.press("Enter")

                # attesa minima
                try:
                    page.wait_for_selector("text=Risultati della ricerca", timeout=20000)
                except:
                    pass

                # riga mese + lente
                target_row = f"{mese_num:02d}/{anno}"
                row = page.locator(f"tr:has-text('{target_row}')").first
                if row.count() > 0 and row.locator("img[src*='search']").count() > 0:
                    icon = row.locator("img[src*='search']").first
                else:
                    icon = page.locator("img[src*='search']").first

                if icon.count() > 0:
                    with context.expect_page(timeout=20000) as popup_ev:
                        icon.click()
                    popup = popup_ev.value

                    url = (popup.url or "").replace("/js_rev//", "/js_rev/")
                    if "EMBED=y" not in url:
                        url += "&EMBED=y" if "?" in url else "?EMBED=y"

                    # request associata al context: usa cookie della sessione
                    resp = context.request.get(url, timeout=60000)
                    body = resp.body()

                    try:
                        popup.close()
                    except:
                        pass

                    if body[:4] == b"%PDF":
                        Path(path_cart).write_bytes(body)
                        c_ok = os.path.exists(path_cart) and os.path.getsize(path_cart) > 5000
                    else:
                        c_ok = False
                else:
                    c_ok = False

            browser.close()
            status.empty()

    except Exception as e:
        status.error(f"Errore: {e}")
        return None, None, str(e)

    return (path_busta if b_ok else None), (path_cart if c_ok else None), None


# --- UI APP ---
st.set_page_config(page_title="Gottardo Payroll", page_icon="💶", layout="wide")
st.title("💶 Analisi Stipendio & Presenze")

with st.sidebar:
    st.header("🔐 Credenziali")
    username, password = get_credentials()

    if not st.session_state.get('credentials_set'):
        u = st.text_input("Username", value=username if username else "")
        p = st.text_input("Password", type="password")
        if st.button("💾 Salva"):
            st.session_state.update({'username': u, 'password': p, 'credentials_set': True})
            st.rerun()
    else:
        st.success(st.session_state.get('username', ''))
        if st.button("🔄 Cambia"):
            st.session_state.update({'credentials_set': False})
            st.session_state.pop('username', None)
            st.session_state.pop('password', None)
            st.rerun()

    st.divider()

    if st.session_state.get('credentials_set'):
        sel_anno = st.selectbox("Anno", [2024, 2025, 2026], index=1)
        sel_mese = st.selectbox(
            "Mese",
            ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
             "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"],
            index=11
        )
        tipo_doc = st.radio("Tipo", ["📄 Cedolino Mensile", "🎄 Tredicesima"])

        if st.button("🚀 AVVIA ANALISI", type="primary"):
            # reset
            for k in ["done", "busta", "cart", "db", "dc", "tipo"]:
                st.session_state.pop(k, None)

            tipo = "tredicesima" if "Tredicesima" in tipo_doc else "cedolino"
            pb, pc, err = scarica_documenti_automatici(sel_mese, sel_anno, st.session_state['username'], st.session_state['password'], tipo)

            if err == "LOGIN_FALLITO":
                st.error("LOGIN FALLITO")
            elif err:
                st.error(err)
            else:
                st.session_state.update({'busta': pb, 'cart': pc, 'tipo': tipo, 'done': False})
    else:
        st.warning("⚠️ Inserisci le credenziali")

# RISULTATI
if st.session_state.get('busta') or st.session_state.get('cart'):
    if not st.session_state.get('done'):
        with st.spinner("🧠 Analisi..."):
            # BUSTA: AI (Gemini → DeepSeek)
            db = None
            if st.session_state.get('busta'):
                db = estrai_con_fallback(st.session_state.get('busta'), get_busta_prompt(), "Busta Paga")

            # CARTELLINO: deterministico (fitz) → AI fallback
            dc = None
            if st.session_state.get('cart'):
                dc = cartellino_parse_deterministico(st.session_state.get('cart'))
                if dc is None:
                    dc = estrai_con_fallback(
                        st.session_state.get('cart'),
                        get_cartellino_prompt_ai_only(),
                        "Cartellino",
                        validate_fn=validate_cartellino_ai_fallback
                    )

            st.session_state.update({'db': db, 'dc': dc, 'done': True})

            # elimina file
            try:
                if st.session_state.get('busta') and os.path.exists(st.session_state['busta']):
                    os.remove(st.session_state['busta'])
            except:
                pass
            try:
                if st.session_state.get('cart') and os.path.exists(st.session_state['cart']):
                    os.remove(st.session_state['cart'])
            except:
                pass

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
            k1.metric("💵 NETTO IN BUSTA", f"€ {float(dg.get('netto', 0) or 0):.2f}", delta="Pagamento")
            k2.metric("📊 Lordo Totale", f"€ {float(comp.get('lordo_totale', 0) or 0):.2f}")
            k3.metric("📆 GG INPS (Busta)", int(float(dg.get('giorni_pagati', 0) or 0)))

            st.markdown("---")

            c_entr, c_usc = st.columns(2)
            with c_entr:
                st.subheader("➕ Competenze")
                st.write(f"**Paga Base:** € {float(comp.get('base', 0) or 0):.2f}")
                if float(comp.get('anzianita', 0) or 0) > 0:
                    st.write(f"**Anzianità:** € {float(comp.get('anzianita', 0) or 0):.2f}")
                if float(comp.get('straordinari', 0) or 0) > 0:
                    st.write(f"**Straordinari/Suppl.:** € {float(comp.get('straordinari', 0) or 0):.2f}")
                if float(comp.get('festivita', 0) or 0) > 0:
                    st.write(f"**Festività/Maggiorazioni:** € {float(comp.get('festivita', 0) or 0):.2f}")

            with c_usc:
                st.subheader("➖ Trattenute")
                st.write(f"**Contributi INPS:** € {float(tratt.get('inps', 0) or 0):.2f}")
                st.write(f"**IRPEF Netta:** € {float(tratt.get('irpef_netta', 0) or 0):.2f}")
                if float(tratt.get('addizionali_totali', 0) or 0) > 0:
                    st.write(f"**Addizionali:** € {float(tratt.get('addizionali_totali', 0) or 0):.2f}")

            with st.expander("🏖️ Ferie / Permessi"):
                f1, f2 = st.columns(2)
                with f1:
                    st.write("**FERIE**")
                    st.write(f"Residue AP: {float(ferie.get('residue_ap', 0) or 0):.2f}")
                    st.write(f"Maturate: {float(ferie.get('maturate', 0) or 0):.2f}")
                    st.write(f"Godute: {float(ferie.get('godute', 0) or 0):.2f}")
                    st.write(f"Saldo: {float(ferie.get('saldo', 0) or 0):.2f}")
                with f2:
                    st.write("**PAR**")
                    st.write(f"Residue AP: {float(par.get('residue_ap', 0) or 0):.2f}")
                    st.write(f"Spettanti: {float(par.get('spettanti', 0) or 0):.2f}")
                    st.write(f"Fruite: {float(par.get('fruite', 0) or 0):.2f}")
                    st.write(f"Saldo: {float(par.get('saldo', 0) or 0):.2f}")
        else:
            st.warning("⚠️ Dati busta non disponibili")

    with tab2:
        if dc:
            c1, c2 = st.columns([1, 2])
            with c1:
                gg_presenza = float(dc.get('gg_presenza', 0) or 0)
                giorni_reali = float(dc.get('giorni_reali', 0) or 0)

                if gg_presenza > 0:
                    st.metric("📅 GG Presenza (Cartellino)", gg_presenza)
                elif giorni_reali > 0:
                    st.metric("📅 Giorni timbrati (token)", giorni_reali)
                else:
                    st.metric("📅 Presenze", "N/D")

                st.metric("✅ Anomalie Badge", float(dc.get('giorni_senza_badge', 0) or 0))

            with c2:
                st.info(f"**📝 Note:** {dc.get('note', '')}")
        else:
            if tipo == "tredicesima":
                st.warning("⚠️ Cartellino non disponibile (Tredicesima)")
            else:
                st.error("❌ Errore cartellino")

    with tab3:
        if db and dc:
            gg_inps = float(db.get('dati_generali', {}).get('giorni_pagati', 0) or 0)

            gg_presenza = float(dc.get('gg_presenza', 0) or 0)
            giorni_reali = float(dc.get('giorni_reali', 0) or 0)
            val_cart = gg_presenza if gg_presenza > 0 else giorni_reali

            st.subheader("🔍 Analisi Discrepanze")
            col_a, col_b = st.columns(2)
            col_a.metric("GG INPS (Busta)", gg_inps)
            col_b.metric("GG Cartellino", val_cart, delta=f"{(val_cart - gg_inps):.1f}")

            # Confronto ore (se disponibili)
            ore_ord = float(dc.get("ore_ordinarie_0251", 0) or 0)
            ore_lav = float(dc.get("ore_lavorate_0253", 0) or 0)
            ore_riep = float(dc.get("ore_ordinarie_riepilogo", 0) or 0)

            if ore_ord > 0 or ore_lav > 0 or ore_riep > 0:
                st.markdown("---")
                st.write("**Ore dal cartellino (se presenti):**")
                st.write(f"- 0251 ORE ORDINARIE: {ore_ord:.2f}")
                st.write(f"- 0253 ORE LAVORATE: {ore_lav:.2f}")
                st.write(f"- Riepilogo ore (prima colonna): {ore_riep:.2f}")

        elif tipo == "tredicesima":
            st.info("Analisi non disponibile per Tredicesima")
        else:
            st.warning("Servono entrambi i documenti")
