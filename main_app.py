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
# 1. Gemini
try:
    GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
    genai.configure(api_key=GOOGLE_API_KEY)
    HAS_GEMINI = True
except:
    HAS_GEMINI = False

# 2. DeepSeek (Fallback)
try:
    DEEPSEEK_API_KEY = st.secrets["DEEPSEEK_API_KEY"]
    HAS_DEEPSEEK = True
except:
    DEEPSEEK_API_KEY = None
    HAS_DEEPSEEK = False


# ==============================================================================
# 1. HELPERS & PARSING
# ==============================================================================

def parse_it_number(s: str) -> float:
    if s is None: return 0.0
    s = str(s).strip()
    if not s: return 0.0
    s = s.replace(".", "").replace(",", ".")
    try: return float(s)
    except: return 0.0

def clean_json_response(text):
    try:
        text = re.sub(r"```json|```", "", text).strip()
        start = text.find('{')
        end = text.rfind('}') + 1
        return json.loads(text[start:end]) if start != -1 else json.loads(text)
    except:
        return None

def extract_text_from_pdf(file_path):
    if not fitz: return None
    try:
        doc = fitz.open(file_path)
        return "\n".join([page.get_text() for page in doc])
    except: return None

def get_pdf_download_link(file_path, filename):
    if not os.path.exists(file_path): return None
    with open(file_path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    return f'<a href="data:application/pdf;base64,{data}" download="{filename}">📥 Scarica {filename}</a>'

# --- PROMPT ---

def get_busta_prompt():
    return """Analizza CEDOLINO PAGA. JSON valido:
{
    "dati_generali": {"netto": float, "giorni_pagati": float},
    "competenze": {"lordo_totale": float, "base": float, "straordinari": float, "festivita": float},
    "trattenute": {"inps": float, "irpef_netta": float},
    "ferie": {"residue_ap": float, "maturate": float, "godute": float, "saldo": float},
    "par": {"residue_ap": float, "spettanti": float, "fruite": float, "saldo": float}
}"""

def get_cartellino_prompt():
    return """Analizza CARTELLINO PRESENZE. JSON valido:
{
    "gg_presenza": float, "giorni_reali": float,
    "ore_ordinarie_0251": float, "ore_lavorate_0253": float, 
    "ore_ordinarie_riepilogo": float, "giorni_senza_badge": float,
    "note": "string"
}"""

# --- LOGICA AI IBRIDA (GEMINI -> DEEPSEEK) ---

def estrai_con_ai(file_path, prompt, tipo):
    if not file_path or not os.path.exists(file_path): return None
    
    status = st.empty()
    res = None
    
    # 1. TENTATIVO GEMINI (Preferito: Multimodale Nativo)
    if HAS_GEMINI:
        try:
            status.info(f"🤖 Analisi {tipo} (Gemini)...")
            models = [m for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
            if models:
                with open(file_path, "rb") as f: pdf_bytes = f.read()
                # Prova il primo modello disponibile (solitamente Flash o Pro)
                model = genai.GenerativeModel(models[0].name)
                resp = model.generate_content([prompt, {"mime_type": "application/pdf", "data": pdf_bytes}])
                res = clean_json_response(resp.text)
                if res:
                    status.empty()
                    return res
        except Exception as e:
            status.warning(f"⚠️ Gemini fallito ({e}). Passo al fallback...")
    
    # 2. TENTATIVO DEEPSEEK (Fallback: Testo puro via OpenAI client)
    if not res and HAS_DEEPSEEK and OpenAI:
        try:
            status.info(f"🧠 Analisi {tipo} (DeepSeek)...")
            
            # DeepSeek vuole testo, non bytes PDF
            text_content = extract_text_from_pdf(file_path)
            if not text_content:
                status.error("❌ Impossibile estrarre testo per DeepSeek")
                return None

            client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
            
            # Prompt arricchito col testo
            full_prompt = f"{prompt}\n\n--- CONTENUTO DOCUMENTO ---\n{text_content[:25000]}" # Limite token sicurezza
            
            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "Sei un parser JSON rigoroso. Rispondi solo con JSON."},
                    {"role": "user", "content": full_prompt}
                ],
                temperature=0.1
            )
            
            raw_text = response.choices[0].message.content
            res = clean_json_response(raw_text)
            if res:
                status.success("✅ Analisi DeepSeek completata")
                time.sleep(1)
                status.empty()
                return res
                
        except Exception as e:
            status.error(f"❌ Errore DeepSeek: {e}")
            
    status.empty()
    return None

def cartellino_parse_deterministico(file_path):
    # Regex rapida per evitare costo AI se il PDF è semplice
    text = extract_text_from_pdf(file_path)
    if not text: return None
    upper = text.upper()
    
    m_gg = re.search(r"0265\s+GG\s+PRESENZA.*?(\d{1,3}[.,]\d{2})", upper)
    gg = parse_it_number(m_gg.group(1)) if m_gg else 0.0
    
    m_ore = re.search(r"0253\s+ORE\s+LAVORATE.*?(\d{1,3}[.,]\d{2})", upper)
    ore = parse_it_number(m_ore.group(1)) if m_ore else 0.0
    
    days = len(set(re.findall(r"\b[LMGVSD]\d{2}\b", upper)))
    
    return {
        "gg_presenza": gg,
        "giorni_reali": float(days),
        "ore_lavorate_0253": ore,
        "note": "Analisi Regex rapida",
        "debug_prime_righe": text[:300]
    }


# ==============================================================================
# 2. AGENDA ENGINE (PATCHATA + RICORSIVA)
# ==============================================================================

AGENDA_KEYWORDS = ["OMESSA TIMBRATURA", "MALATTIA", "RIPOSO", "FERIE", "PERMESS", "CHIUSURA", "INFORTUN"]

def find_element_recursive(ctx, selector):
    """Cerca elemento in tutti i frame ricorsivamente."""
    try:
        if ctx.locator(selector).count() > 0:
            return ctx, ctx.locator(selector).first
    except: pass
    
    frames = getattr(ctx, 'frames', []) or getattr(ctx, 'child_frames', [])
    for f in frames:
        if f == ctx: continue
        try:
            c, e = find_element_recursive(f, selector)
            if e: return c, e
        except: continue
    return None, None

def agenda_set_month_enter(page, mese_num, anno, debug_info):
    nomi_mesi = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
                 "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
    target_mese = nomi_mesi[mese_num - 1]

    debug_info.append("Agenda: Ricerca Toolbar...")
    try: page.wait_for_selector(".dijitToolbar", timeout=15000)
    except: pass

    # Cerchiamo #revit_form_Button_6 (id preciso) o .calendar16 (classe icona)
    frame, btn = find_element_recursive(page, "#revit_form_Button_6")
    if not btn:
        frame, btn = find_element_recursive(page, ".calendar16")
    
    if not btn:
        debug_info.append("Agenda: Bottone calendario NON trovato")
        return

    try:
        # Se abbiamo trovato l'icona, clicchiamo il genitore bottone se esiste
        if "calendar16" in (btn.get_attribute("class") or ""):
            parent = btn.locator("xpath=..")
            if parent.count()>0: btn = parent
        
        btn.click(force=True, timeout=8000)
        time.sleep(1.5)
        debug_info.append("Agenda: Click Calendario OK")
    except Exception as e:
        debug_info.append(f"Agenda: Errore click ({e})")
        return

    # Popup Data
    ctx = page if page.locator(".dijitCalendarMonthLabel").count() >= 2 else frame
    if ctx and ctx.locator(".dijitCalendarMonthLabel").count() >= 2:
        labels = ctx.locator(".dijitCalendarMonthLabel")
        try:
            if labels.nth(0).inner_text().strip().lower() != target_mese.lower():
                labels.nth(0).click()
                time.sleep(0.5)
                ctx.locator("body").get_by_text(target_mese, exact=True).last.click()
                time.sleep(0.5)
            
            if str(anno) not in labels.nth(1).inner_text():
                labels.nth(1).click()
                time.sleep(0.5)
                ctx.locator("body").get_by_text(str(anno), exact=True).last.click()
                time.sleep(0.5)
            
            ctx.locator(".dijitCalendarDateTemplate", has_text=re.compile(r"^1$")).first.click()
            debug_info.append("Agenda: Data impostata")
            time.sleep(1)
        except Exception as e:
            debug_info.append(f"Agenda: Errore impostazione data ({e})")
            page.keyboard.press("Escape")
    else:
        debug_info.append("Agenda: Labels popup non trovate")
        page.keyboard.press("Escape")

    # Click Tasto "Mese"
    time.sleep(1)
    try:
        btn_view = frame.locator("[aria-label='Mese']").first
        if btn_view.count() == 0:
             btn_view = frame.locator(".dijitButtonText", has_text="Mese").first
        
        if btn_view.count() > 0:
            btn_view.click(force=True)
            debug_info.append("Agenda: Click tasto 'Mese' OK")
            time.sleep(3)
        else:
            debug_info.append("Agenda: Tasto 'Mese' NON trovato")
    except Exception as e:
        debug_info.append(f"Agenda: Errore click Mese ({e})")

def agenda_extract_events(page):
    texts = []
    def rec_extract(ctx):
        loc = []
        cands = ctx.locator("[class*='event'], [class*='appointment'], .dijitCalendarEvent")
        try:
            for i in range(cands.count()):
                t = cands.nth(i).inner_text().strip()
                if t: loc.append(t)
        except: pass
        
        frames = getattr(ctx, 'frames', []) or getattr(ctx, 'child_frames', [])
        for f in frames:
            if f != ctx: loc.extend(rec_extract(f))
        return loc

    texts = rec_extract(page)
    blob = "\n".join(texts).upper()
    counts = {k: blob.count(k) for k in AGENDA_KEYWORDS}
    lines = sorted(list(set([t for t in texts if any(k in t.upper() for k in AGENDA_KEYWORDS)])))
    return {"counts": counts, "lines": lines, "raw_len": len(blob)}


# ==============================================================================
# 3. DOWNLOADER AUTOMATICO
# ==============================================================================

def scarica_documenti_automatici(mese_nome, anno, username, password, tipo_documento):
    nomi_mesi = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
                 "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
    try: mese_num = nomi_mesi.index(mese_nome) + 1
    except: return None, None, "Mese invalido"

    wd = Path.cwd()
    suffix = "_13" if tipo_documento == "tredicesima" else ""
    path_busta = str(wd / f"busta_{mese_num}_{anno}{suffix}.pdf")
    path_cart = str(wd / f"cartellino_{mese_num}_{anno}.pdf")
    target_busta = f"Tredicesima {anno}" if tipo_documento == "tredicesima" else f"{mese_nome} {anno}"
    
    last_day = calendar.monthrange(anno, mese_num)[1]
    d_from_vis = f"01/{mese_num:02d}/{anno}"
    d_to_vis = f"{last_day}/{mese_num:02d}/{anno}"

    st_status = st.empty()
    st_status.info("🤖 Avvio Bot...")
    debug_info = []
    b_ok, c_ok = False, False

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-gpu'])
            context = browser.new_context(accept_downloads=True, user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/121.0.0.0 Safari/537.36")
            page = context.new_page()
            page.set_viewport_size({"width": 1920, "height": 1080})

            # LOGIN
            st_status.info("🔐 Login...")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y")
            page.fill('input[type="text"]', username)
            page.fill('input[type="password"]', password)
            page.press('input[type="password"]', 'Enter')
            try:
                page.wait_for_selector("text=I miei dati", timeout=20000)
                debug_info.append("Login: OK")
            except:
                browser.close(); return None, None, "LOGIN FALLITO"

            # AGENDA
            st_status.info("🗓️ Agenda...")
            try:
                agenda_set_month_enter(page, mese_num, anno, debug_info)
                agenda_data = agenda_extract_events(page)
                st.session_state["agenda_data"] = agenda_data
                debug_info.append(f"Agenda: Letta ({agenda_data['raw_len']} chars)")
            except Exception as e:
                debug_info.append(f"Agenda Error: {e}")

            # BUSTA PAGA
            st_status.info("💰 Busta Paga...")
            try:
                page.click("text=I miei dati", force=True)
                page.wait_for_selector("text=Documenti", timeout=10000).click()
                time.sleep(3)
                try: page.click("text=Cedolino", force=True)
                except: pass
                time.sleep(3)

                links = page.locator("a")
                found = None
                for i in range(links.count()):
                    txt = links.nth(i).inner_text().strip().lower()
                    if target_busta.lower() in txt:
                        found = links.nth(i); break
                
                if found:
                    with page.expect_download(timeout=30000) as dl_info:
                        found.click()
                    dl_info.value.save_as(path_busta)
                    if os.path.exists(path_busta): 
                        b_ok = True
                        debug_info.append("Busta: OK")
                else: debug_info.append("Busta: Non trovata")
            except Exception as e: debug_info.append(f"Busta Error: {e}")

            # CARTELLINO
            if tipo_documento != "tredicesima":
                st_status.info("📅 Cartellino...")
                try:
                    # Reset Home
                    page.evaluate("window.scrollTo(0, 0)")
                    time.sleep(1)
                    try: page.keyboard.press("Escape")
                    except: pass
                    
                    # Navigazione Menu
                    page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
                    time.sleep(3)
                    page.evaluate("document.getElementById('lnktab_5_label')?.click()")
                    time.sleep(5)

                    # Date
                    try:
                        dal = page.locator("input[id*='CLRICHIE'][class*='dijitInputInner']").first
                        al = page.locator("input[id*='CLRICHI2'][class*='dijitInputInner']").first
                        if dal.count()>0:
                            dal.click(force=True); page.keyboard.press("Control+A"); dal.type(d_from_vis); dal.press("Tab")
                            time.sleep(0.5)
                            al.click(force=True); page.keyboard.press("Control+A"); al.type(d_to_vis); al.press("Tab")
                    except: pass

                    # Ricerca
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    try: page.locator("//span[contains(text(),'Esegui ricerca')]").last.click(force=True)
                    except: page.keyboard.press("Enter")
                    time.sleep(4)

                    # Download Popup EMBED=y
                    row_txt = f"{mese_num:02d}/{anno}"
                    row = page.locator(f"tr:has-text('{row_txt}')").first
                    icon = row.locator("img[src*='search']").first if row.count()>0 else page.locator("img[src*='search']").first
                    
                    if icon.count()>0:
                        with context.expect_page(timeout=20000) as pi:
                            icon.click()
                        popup = pi.value
                        url = (popup.url or "").replace("/js_rev//", "/js_rev/")
                        if "EMBED=y" not in url: url += ("&" if "?" in url else "?") + "EMBED=y"
                        
                        resp = context.request.get(url, timeout=60000)
                        if resp.body()[:4] == b"%PDF":
                            Path(path_cart).write_bytes(resp.body())
                            c_ok = True
                            debug_info.append("Cartellino: OK")
                        else:
                            # Fallback PDF stampa
                            popup.pdf(path=path_cart)
                            c_ok = True
                        try: popup.close()
                        except: pass
                    else:
                        debug_info.append("Cartellino: Icona non trovata")
                        
                except Exception as e:
                    debug_info.append(f"Cartellino Error: {e}")

            browser.close()
            st_status.empty()

    except Exception as e:
        return None, None, str(e)
    
    st.session_state['debug_info'] = debug_info
    return (path_busta if b_ok else None), (path_cart if c_ok else None), None


# ==============================================================================
# 4. UI STREAMLIT
# ==============================================================================

st.set_page_config(page_title="Gottardo Payroll", page_icon="💶", layout="wide")
st.title("💶 Analisi Stipendio & Presenze")

with st.sidebar:
    st.header("🔐 Credenziali")
    u, p = get_credentials()
    if not st.session_state.get('credentials_set'):
        ui = st.text_input("User", value=u if u else "")
        pi = st.text_input("Pass", type="password")
        if st.button("Salva"):
            st.session_state.update({'username': ui, 'password': pi, 'credentials_set': True})
            st.rerun()
    else:
        st.success(f"Loggato: {st.session_state.get('username')}")
        if st.button("Esci"):
            st.session_state.update({'credentials_set': False})
            st.rerun()
    
    st.divider()
    if st.session_state.get('credentials_set'):
        sel_anno = st.selectbox("Anno", [2024, 2025, 2026], index=1)
        sel_mese = st.selectbox("Mese", ["Gennaio","Febbraio","Marzo","Aprile","Maggio","Giugno","Luglio","Agosto","Settembre","Ottobre","Novembre","Dicembre"], index=0)
        tipo = st.radio("Tipo", ["📄 Cedolino", "🎄 Tredicesima"])
        
        if st.button("🚀 AVVIA ANALISI", type="primary"):
            for k in ['agenda_data', 'debug_info', 'busta', 'cart', 'db', 'dc', 'done']:
                st.session_state.pop(k, None)
            
            t_doc = "tredicesima" if "Tredicesima" in tipo else "cedolino"
            pb, pc, err = scarica_documenti_automatici(sel_mese, sel_anno, st.session_state['username'], st.session_state['password'], t_doc)
            
            if err: st.error(err)
            else: st.session_state.update({'busta': pb, 'cart': pc, 'tipo': t_doc})

if (st.session_state.get('busta') or st.session_state.get('cart')) and not st.session_state.get('done'):
    with st.spinner("🧠 Analisi AI in corso..."):
        db = estrai_con_ai(st.session_state.get('busta'), get_busta_prompt(), "Busta")
        
        path_c = st.session_state.get('cart')
        dc = cartellino_parse_deterministico(path_c)
        if not dc or dc.get('gg_presenza', 0) == 0:
             dc_ai = estrai_con_ai(path_c, get_cartellino_prompt(), "Cartellino")
             if dc_ai: dc = dc_ai
        
        st.session_state.update({'db': db, 'dc': dc, 'done': True})

if st.session_state.get('done'):
    db = st.session_state.get('db')
    dc = st.session_state.get('dc')
    
    t1, t2, t3, t4 = st.tabs(["💰 Stipendio", "📅 Cartellino", "📊 Confronto", "🔧 Debug"])
    
    with t1:
        if db:
            dg = db.get('dati_generali', {})
            cmp = db.get('competenze', {})
            trt = db.get('trattenute', {})
            c1, c2 = st.columns(2)
            c1.metric("Netto", f"€ {dg.get('netto', 0)}")
            c2.metric("Lordo", f"€ {cmp.get('lordo_totale', 0)}")
            st.json(db)
        else: st.warning("Busta non disponibile")
        
    with t2:
        if dc:
            c1, c2 = st.columns(2)
            c1.metric("GG Presenza", dc.get('gg_presenza', 0))
            c2.metric("Ore Lavorate", dc.get('ore_lavorate_0253', 0))
            st.json(dc)
        else: st.warning("Cartellino non disponibile")
        
    with t3:
        if db and dc:
            gg_busta = db.get('dati_generali', {}).get('giorni_pagati', 0)
            gg_cart = dc.get('gg_presenza', 0)
            st.metric("Discrepanza Giorni", f"{gg_cart - gg_busta:.2f}", delta_color="inverse")
        else: st.info("Dati insufficienti")
        
    with t4:
        st.subheader("Agenda")
        if st.session_state.get('agenda_data'):
            ad = st.session_state['agenda_data']
            cols = st.columns(len(AGENDA_KEYWORDS))
            for i, k in enumerate(AGENDA_KEYWORDS):
                if ad['counts'].get(k, 0) > 0:
                    cols[i % 3].metric(k, ad['counts'][k])
            st.write(ad['lines'])
        
        st.write("LOG:")
        st.write(st.session_state.get('debug_info', []))
