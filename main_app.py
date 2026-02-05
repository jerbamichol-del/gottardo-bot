import sys
import asyncio
import re
import os
import time
import json
import calendar
import locale
import base64
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
try:
    GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
    genai.configure(api_key=GOOGLE_API_KEY)
    HAS_GEMINI = True
except:
    HAS_GEMINI = False

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

def get_pdf_download_link(file_path, filename):
    if not os.path.exists(file_path):
        return None
    with open(file_path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
        return f'<a href="data:application/pdf;base64,{data}" download="{filename}">📥 Scarica {filename}</a>'

# --- PARSER CARTELLINO (Deterministico) ---
def cartellino_parse_deterministico(file_path: str):
    text = extract_text_from_pdf(file_path)
    if not text or len(text.strip()) < 20:
        return {
            "giorni_reali": 0.0, "gg_presenza": 0.0, "ore_ordinarie_riepilogo": 0.0,
            "ore_ordinarie_0251": 0.0, "ore_lavorate_0253": 0.0, "giorni_senza_badge": 0.0,
            "note": "PDF vuoto o illeggibile.", "debug_prime_righe": text[:500] if text else "Nessun testo"
        }

    upper = text.upper()
    debug_text = "\n".join(text.splitlines()[:40])
    
    has_days = re.search(r"\b[LMGVSD]\d{2}\b", upper) is not None
    has_timbr = "TIMBRATURE" in upper or "TIMBRATURA" in upper

    if ("NESSUN DATO" in upper or "NESSUNA" in upper) and (not has_days) and (not has_timbr):
         return {
            "giorni_reali": 0.0, "gg_presenza": 0.0, "ore_ordinarie_riepilogo": 0.0,
            "ore_ordinarie_0251": 0.0, "ore_lavorate_0253": 0.0, "giorni_senza_badge": 0.0,
            "note": "Cartellino vuoto (Nessun dato).", "debug_prime_righe": debug_text
        }

    day_tokens = sorted(set(re.findall(r"\b[LMGVSD]\d{2}\b", upper)))
    giorni_reali = float(len(day_tokens))

    m = re.search(r"0265\s+GG\s+PRESENZA.*?(\d{1,3}[.,]\d{2})", upper)
    gg_presenza = parse_it_number(m.group(1)) if m else 0.0

    m1 = re.search(r"0251\s+ORE\s+ORDINARIE.*?(\d{1,3}[.,]\d{2})", upper)
    ore_ord_0251 = parse_it_number(m1.group(1)) if m1 else 0.0

    m2 = re.search(r"0253\s+ORE\s+LAVORATE.*?(\d{1,3}[.,]\d{2})", upper)
    ore_lav_0253 = parse_it_number(m2.group(1)) if m2 else 0.0

    ore_riep = 0.0
    for line in text.splitlines():
        ln = line.strip()
        if not ln or re.search(r"\b02\d{2}\b", ln): continue
        if re.match(r"^\d{1,3}[.,]\d{2}(\s+\d{1,3}[.,]\d{2}){2,}$", ln):
            first_num = re.findall(r"\d{1,3}[.,]\d{2}", ln)
            if first_num:
                ore_riep = parse_it_number(first_num[0])
                break

    note_parts = []
    if gg_presenza > 0: note_parts.append(f"0265 GG PRESENZA={gg_presenza:.2f}.")
    if ore_ord_0251 > 0: note_parts.append(f"0251 ORE ORDINARIE={ore_ord_0251:.2f}.")
    if ore_lav_0253 > 0: note_parts.append(f"0253 ORE LAVORATE={ore_lav_0253:.2f}.")
    if ore_riep > 0: note_parts.append(f"Riepilogo ore={ore_riep:.2f}.")
    if giorni_reali > 0: note_parts.append(f"Token giorni={int(giorni_reali)}.")

    return {
        "giorni_reali": giorni_reali, "gg_presenza": gg_presenza,
        "ore_ordinarie_riepilogo": ore_riep, "ore_ordinarie_0251": ore_ord_0251,
        "ore_lavorate_0253": ore_lav_0253, "giorni_senza_badge": 0.0,
        "note": " ".join(note_parts) if note_parts else "Nessun dato numerico trovato.",
        "debug_prime_righe": debug_text
    }

# --- PARSER AGENDA (AI Vision / Text) ---
def analizza_agenda(file_path, prompt_text="Analizza questa schermata dell'agenda presenze."):
    """Usa Gemini Vision per estrarre eventi dall'immagine agenda"""
    if not HAS_GEMINI or not os.path.exists(file_path):
        return None
    
    try:
        model = genai.GenerativeModel('gemini-pro-vision') # O flash
        img = {'mime_type': 'image/jpeg', 'data': Path(file_path).read_bytes()}
        
        prompt = """
        Analizza lo screenshot dell'agenda mensile.
        Estrai una lista di eventi anomali o rilevanti visibili nelle celle dei giorni.
        Cerca testi come: "OMESSA TIMBRATURA", "MALATTIA", "FERIE", "RIPOSO", "MISSING", "ASSENZA".
        
        Restituisci JSON:
        {
          "eventi": [
             {"giorno": "dd", "tipo": "OMESSA TIMBRATURA", "colore": "blu/rosso..."},
             ...
          ],
          "conteggi": {
             "omesse_timbrature": int,
             "malattia": int,
             "ferie": int
          },
          "note_visive": "stringa riassuntiva"
        }
        """
        resp = model.generate_content([prompt, img])
        return clean_json_response(resp.text)
    except Exception as e:
        return None

# --- AI WRAPPERS ---
@st.cache_resource
def get_gemini_models():
    if not HAS_GEMINI: return []
    try:
        models = genai.list_models()
        valid = [m for m in models if 'generateContent' in m.supported_generation_methods]
        gemini_list = []
        for m in valid:
            name = m.name.replace('models/', '')
            if 'gemini' in name.lower() and 'embedding' not in name.lower():
                gemini_list.append((name, genai.GenerativeModel(name)))
        return sorted(gemini_list, key=lambda x: 0 if 'flash' in x[0] else 1)
    except: return []

def estrai_con_fallback(file_path, prompt, tipo, validate_fn=None):
    if not file_path or not os.path.exists(file_path): return None
    
    # 1) GEMINI
    models = get_gemini_models()
    if models:
        try:
            with open(file_path, "rb") as f: pdf_bytes = f.read()
            if pdf_bytes[:4] == b"%PDF":
                for name, model in models:
                    try:
                        resp = model.generate_content([prompt, {"mime_type": "application/pdf", "data": pdf_bytes}])
                        res = clean_json_response(resp.text)
                        if res and isinstance(res, dict):
                            if validate_fn and not validate_fn(res): continue
                            return res
                    except: continue
        except: pass

    # 2) DEEPSEEK
    if HAS_DEEPSEEK and OpenAI:
        text = extract_text_from_pdf(file_path)
        if text and len(text) > 50:
            try:
                client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
                resp = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[{"role": "user", "content": f"{prompt}\n\nTESTO:\n{text[:50000]}"}],
                    temperature=0.1
                )
                res = clean_json_response(resp.choices[0].message.content)
                if res and isinstance(res, dict): 
                     if validate_fn and not validate_fn(res): return None
                     return res
            except: pass
    return None

# --- PROMPT AI ---
def get_busta_prompt():
    return """
    Analizza CEDOLINO PAGA. JSON strict.
    Campi: e_tredicesima (bool), dati_generali (netto, giorni_pagati/GG.INPS),
    competenze (base, straordinari, festivita, anzianita, lordo_totale),
    trattenute (inps, irpef_netta, addizionali_totali),
    ferie (residue_ap, maturate, godute, saldo), par (idem).
    Se manca valore usa 0.0.
    """

def get_cartellino_prompt_ai_only():
    return """Analizza CARTELLINO. JSON: giorni_reali, gg_presenza (0265), ore_ordinarie_0251, ore_lavorate_0253, giorni_senza_badge, note."""

def validate_cartellino_ai_fallback(res):
    try:
        if any(float(res.get(k, 0) or 0) > 0 for k in ["gg_presenza", "ore_ordinarie_0251", "ore_lavorate_0253"]): return True
    except: pass
    txt = (str(res.get("debug_prime_righe","")) + str(res.get("note",""))).upper()
    return "TIMBRATURE" in txt or "PRESENZA" in txt

# --- NAVIGAZIONE AGENDA ---
def naviga_e_scarica_agenda(page, mese_num, anno, path_agenda_img):
    """Naviga sull'agenda, imposta data e salva screenshot"""
    try:
        # 1. Vai su Agenda
        page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2", wait_until="domcontentloaded")
        time.sleep(3)
        
        # 2. Clicca icona calendario (revit_form_Button_6)
        # Selettore specifico fornito dall'utente
        cal_btn = page.locator("#revit_form_Button_6")
        if cal_btn.count() > 0:
            cal_btn.click()
            time.sleep(1)
            
            # 3. Gestione Widget Dojo Calendario
            # Clicca sul Mese (label centrale) per aprire selezione mese
            month_label = page.locator(".dijitCalendarMonthLabel").first
            if month_label.is_visible():
                month_label.click()
                time.sleep(0.5)
                
                # Seleziona Mese (es. "Gennaio")
                nomi_mesi = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
                             "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
                mese_target = nomi_mesi[mese_num - 1]
                page.locator(f".dijitCalendarMonthMenu .dijitCalendarMonthLabel:has-text('{mese_target}')").click()
                time.sleep(0.5)

                # Seleziona Anno (se diverso)
                current_year_elem = page.locator(".dijitCalendarYearLabel").first
                if current_year_elem.is_visible():
                    curr_y = current_year_elem.inner_text().strip()
                    if curr_y != str(anno):
                        current_year_elem.click()
                        # Qui servirebbe logica complessa per anno +/- ma assumiamo range vicino o input diretto se possibile. 
                        # Fallback semplice: scriviamo l'anno se è un input, o usiamo frecce. 
                        # Per ora proviamo click anno specifico se appare in menu.
                        page.locator(f".dijitCalendarYearMenu .dijitCalendarYearLabel:has-text('{anno}')").click()
            
            # Clicca giorno 1 per confermare
            page.locator(".dijitCalendarDateTemplate:not(.dijitCalendarOtherMonth) >> text=1").first.click()
            time.sleep(2)
        
        # 4. Assicura vista MESE
        # Pulsante "Mese" fuori calendario
        btn_mese = page.locator("#dijit_form_Button_10_label", has_text="Mese")
        if btn_mese.count() > 0:
            btn_mese.click()
            time.sleep(3)
        
        # 5. Screenshot
        page.screenshot(path=path_agenda_img, full_page=True)
        return True, "Agenda scaricata"
        
    except Exception as e:
        return False, f"Errore agenda: {e}"

# --- BOT PRINCIPALE ---
def scarica_documenti_automatici(mese_nome, anno, username, password, tipo_documento="cedolino"):
    nomi_mesi_it = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
                    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
    try: mese_num = nomi_mesi_it.index(mese_nome) + 1
    except: return None, None, None, "Mese invalido"

    wd = Path.cwd()
    suffix = "_13" if tipo_documento == "tredicesima" else ""
    path_busta = str(wd / f"busta_{mese_num}_{anno}{suffix}.pdf")
    path_cart = str(wd / f"cartellino_{mese_num}_{anno}.pdf")
    path_agenda = str(wd / f"agenda_{mese_num}_{anno}.jpg")
    
    target_busta = f"Tredicesima {anno}" if tipo_documento == "tredicesima" else f"{mese_nome} {anno}"

    st_status = st.empty()
    st_status.info(f"🤖 Bot avviato: {mese_nome} {anno}")
    
    b_ok, c_ok, a_ok = False, False, False
    log_info = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-gpu'])
            context = browser.new_context(accept_downloads=True)
            page = context.new_page()
            page.set_viewport_size({"width": 1920, "height": 1080})

            # LOGIN
            st_status.info("🔐 Login...")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y")
            page.fill('input[type="text"]', username)
            page.fill('input[type="password"]', password)
            page.press('input[type="password"]', 'Enter')
            time.sleep(3)
            
            try:
                page.wait_for_selector("text=I miei dati", timeout=15000)
                st_status.info("✅ Login OK")
            except:
                return None, None, None, "LOGIN_FALLITO"

            # 1. AGENDA (Prima del cartellino per non perdere sessione navigando)
            if tipo_documento != "tredicesima":
                st_status.info("📅 Scarico Agenda...")
                a_ok, msg_a = naviga_e_scarica_agenda(page, mese_num, anno, path_agenda)
                log_info.append(msg_a)

            # 2. CARTELLINO (Navigazione diretta Time -> Cartellino)
            if tipo_documento != "tredicesima":
                st_status.info("📅 Scarico Cartellino...")
                try:
                    page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()") # Time
                    time.sleep(2)
                    page.evaluate("document.getElementById('lnktab_5_label')?.click()") # Cartellino
                    time.sleep(4)
                    
                    # Date fill
                    last_day = calendar.monthrange(anno, mese_num)[1]
                    d1, d2 = f"01/{mese_num:02d}/{anno}", f"{last_day}/{mese_num:02d}/{anno}"
                    
                    try:
                        dal = page.locator("input[id*='CLRICHIE'][class*='dijitInputInner']").first
                        al = page.locator("input[id*='CLRICHI2'][class*='dijitInputInner']").first
                        dal.click(); page.keyboard.press("Control+A"); dal.type(d1); dal.press("Tab")
                        al.click(); page.keyboard.press("Control+A"); al.type(d2); al.press("Tab")
                    except: pass
                    
                    page.keyboard.press("Enter")
                    time.sleep(4)
                    
                    # Cerca riga + icona
                    row = page.locator(f"tr:has-text('{mese_num:02d}/{anno}')").first
                    icon = row.locator("img[src*='search']").first if row.count() else page.locator("img[src*='search']").first
                    
                    if icon.count() > 0:
                        with context.expect_page() as popup_ev: icon.click()
                        popup = popup_ev.value
                        url = popup.url.replace("/js_rev//", "/js_rev/")
                        if "EMBED=y" not in url: url += "&EMBED=y"
                        
                        resp = context.request.get(url)
                        if resp.body()[:4] == b"%PDF":
                            Path(path_cart).write_bytes(resp.body())
                            c_ok = True
                        popup.close()
                except Exception as e: log_info.append(f"Err Cart: {e}")

            # 3. BUSTA
            st_status.info("💰 Scarico Busta...")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2")
            page.click("text=I miei dati", force=True)
            page.click("text=Documenti", force=True)
            time.sleep(3)
            try: page.locator(".z-image").first.click() 
            except: page.click("text=Cedolino", force=True)
            time.sleep(3)
            
            links = page.locator("a")
            for i in range(links.count()):
                t = links.nth(i).inner_text().strip()
                if target_busta.lower() in t.lower():
                    is_13 = "13" in t or "Tred" in t
                    if (tipo_documento=="tredicesima") != is_13: continue
                    with page.expect_download() as dl: links.nth(i).click()
                    dl.value.save_as(path_busta)
                    b_ok = True
                    break
            
            browser.close()

    except Exception as e: return None, None, None, str(e)
    
    return (path_busta if b_ok else None), (path_cart if c_ok else None), (path_agenda if a_ok else None), None

# --- UI APP ---
st.set_page_config(page_title="Gottardo Payroll", page_icon="💶", layout="wide")
st.title("💶 Analisi Stipendio & Presenze")

with st.sidebar:
    st.header("🔐 Credenziali")
    username, password = get_credentials()
    if not st.session_state.get('credentials_set'):
        u = st.text_input("User", value=username if username else "")
        p = st.text_input("Pass", type="password")
        if st.button("Salva"):
            st.session_state.update({'username': u, 'password': p, 'credentials_set': True})
            st.rerun()
    else:
        st.success(f"Loggato: {st.session_state.get('username')}")
        if st.button("Logout"):
            st.session_state.update({'credentials_set': False}); st.rerun()
    
    st.divider()
    
    if st.session_state.get('credentials_set'):
        sel_anno = st.selectbox("Anno", [2024, 2025, 2026], index=1)
        sel_mese = st.selectbox("Mese", ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"], index=0)
        tipo = st.radio("Tipo", ["📄 Cedolino", "🎄 Tredicesima"])
        
        if st.button("🚀 AVVIA", type="primary"):
            for k in ["done", "busta", "cart", "agenda", "db", "dc", "da"]: st.session_state.pop(k, None)
            doc_type = "tredicesima" if "Tred" in tipo else "cedolino"
            pb, pc, pa, err = scarica_documenti_automatici(sel_mese, sel_anno, st.session_state['username'], st.session_state['password'], doc_type)
            
            if err == "LOGIN_FALLITO": st.error("Login Fallito")
            elif err: st.error(err)
            else: st.session_state.update({'busta': pb, 'cart': pc, 'agenda': pa, 'tipo': doc_type, 'done': False})

# RISULTATI
if st.session_state.get('done') is False:
    with st.spinner("Analisi in corso..."):
        db = estrai_con_fallback(st.session_state.get('busta'), get_busta_prompt(), "Busta") if st.session_state.get('busta') else None
        
        dc = cartellino_parse_deterministico(st.session_state.get('cart')) if st.session_state.get('cart') else None
        # Fallback AI cartellino
        if dc and dc.get('giorni_reali', 0) == 0:
             dc_ai = estrai_con_fallback(st.session_state.get('cart'), get_cartellino_prompt_ai_only(), "Cartellino AI", validate_cartellino_ai_fallback)
             if dc_ai: dc = dc_ai
        
        da = analizza_agenda(st.session_state.get('agenda')) if st.session_state.get('agenda') else None
        
        st.session_state.update({'db': db, 'dc': dc, 'da': da, 'done': True})

if st.session_state.get('done'):
    db, dc, da = st.session_state.get('db'), st.session_state.get('dc'), st.session_state.get('da')
    
    tab1, tab2, tab3 = st.tabs(["💰 Stipendio", "📅 Cartellino & Agenda", "📊 Analisi Incrociata"])
    
    with tab1:
        if db:
            dg = db.get('dati_generali', {})
            comp = db.get('competenze', {})
            k1, k2, k3 = st.columns(3)
            k1.metric("Netto", f"€ {float(dg.get('netto',0)):.2f}")
            k2.metric("Lordo", f"€ {float(comp.get('lordo_totale',0)):.2f}")
            k3.metric("GG INPS", int(float(dg.get('giorni_pagati',0))))
        else: st.warning("No dati Busta")

    with tab2:
        c1, c2 = st.columns(2)
        with c1:
            if dc:
                st.subheader("Cartellino")
                st.write(f"**GG Presenza:** {dc.get('gg_presenza', 0)}")
                st.write(f"**Ore Ordinarie:** {dc.get('ore_ordinarie_0251', 0)}")
            else: st.error("Cartellino mancante")
        with c2:
            if da:
                st.subheader("Agenda (Eventi)")
                st.json(da.get('conteggi', {}))
                st.write(da.get('note_visive', ''))
            else: st.info("Agenda non analizzata")

    with tab3:
        if db and dc:
            st.subheader("Confronto Totale")
            gg_inps = float(db.get('dati_generali', {}).get('giorni_pagati', 0))
            gg_cart = float(dc.get('gg_presenza', 0))
            
            col1, col2, col3 = st.columns(3)
            col1.metric("Busta (INPS)", gg_inps)
            col2.metric("Cartellino", gg_cart, delta=f"{gg_cart - gg_inps:.1f}")
            
            anomalie_agenda = 0
            if da:
                conteggi = da.get('conteggi', {})
                anomalie_agenda = sum(v for k,v in conteggi.items() if k in ['omesse_timbrature', 'malattia', 'assenza'])
            
            col3.metric("Anomalie Agenda", anomalie_agenda, delta="-Controllo" if anomalie_agenda > 0 else "OK")
            
            if anomalie_agenda > 0:
                st.warning(f"⚠️ Attenzione: Ci sono {anomalie_agenda} eventi in agenda (Omesse timbrature/Malattia) che potrebbero spiegare discrepanze.")
