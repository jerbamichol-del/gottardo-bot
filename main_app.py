import sys
import asyncio
import re
import os
import time
import json
import calendar
import locale
import requests
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
# 1. GEMINI
try:
    GOOGLE_API_KEY = st.secrets["GOOGLE_API_KEY"]
    genai.configure(api_key=GOOGLE_API_KEY)
    HAS_GEMINI = True
except:
    HAS_GEMINI = False

# 2. DEEPSEEK / OPENAI
try:
    DEEPSEEK_API_KEY = st.secrets["DEEPSEEK_API_KEY"]
    HAS_DEEPSEEK = True
except:
    DEEPSEEK_API_KEY = None
    HAS_DEEPSEEK = False


# --- FUNZIONI AI HELPER ---

@st.cache_resource
def get_gemini_models():
    if not HAS_GEMINI: return []
    try:
        models = genai.list_models()
        valid = [m for m in models if 'generateContent' in m.supported_generation_methods]
        gemini_list = []
        for m in valid:
            name = m.name.replace('models/', '')
            if 'gemini' in name.lower():
                gemini_list.append((name, genai.GenerativeModel(name)))
        
        def priority(n):
            if 'flash' in n.lower(): return 0
            if 'pro' in n.lower(): return 2
            return 1
        return sorted(gemini_list, key=lambda x: priority(x[0]))
    except:
        return []

def extract_text_from_pdf(file_path):
    """Fallback per DeepSeek: Estrae testo da PDF"""
    if not fitz: return None
    try:
        doc = fitz.open(file_path)
        text = ""
        for page in doc: text += page.get_text() + "\n"
        return text
    except: return None

def clean_json_response(text):
    try:
        text = re.sub(r"```json|```", "", text).strip()
        start = text.find('{')
        end = text.rfind('}') + 1
        return json.loads(text[start:end]) if start != -1 else json.loads(text)
    except:
        return None

def estrai_con_fallback(file_path, prompt, tipo, validate_fn=None):
    if not os.path.exists(file_path): return None
    
    status = st.empty()
    
    # 1. TENTATIVO GEMINI (Legge PDF nativo)
    models = get_gemini_models()
    if models:
        with open(file_path, "rb") as f: pdf_bytes = f.read()

        for name, model in models:
            try:
                status.info(f"🤖 Analisi {tipo} con Gemini ({name})...")
                resp = model.generate_content([prompt, {"mime_type": "application/pdf", "data": pdf_bytes}])
                res = clean_json_response(resp.text)
                
                if res and isinstance(res, dict):
                    if validate_fn and not validate_fn(res): continue
                    status.success(f"✅ {tipo} analizzato (Gemini)")
                    time.sleep(0.5)
                    status.empty()
                    return res
            except Exception as e:
                if "429" in str(e) or "quota" in str(e).lower(): continue
                continue

    # 2. TENTATIVO DEEPSEEK (Legge testo estratto)
    if HAS_DEEPSEEK and OpenAI:
        status.warning("⚠️ Gemini esausto. Tenton con DeepSeek...")
        text = extract_text_from_pdf(file_path)
        
        if text and len(text) > 10:
            try:
                client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
                full_prompt = f"{prompt}\n\n--- TESTO PDF ---\n{text[:50000]}"
                
                resp = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[
                        {"role": "system", "content": "Sei un estrattore JSON."},
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
                        time.sleep(0.5)
                        status.empty()
                        return res
            except Exception as e:
                status.error(f"Errore DeepSeek: {e}")

    status.error(f"❌ Analisi {tipo} fallita")
    return None

# --- PROMPT AI ---
def get_busta_prompt():
    return """
    Analizza CEDOLINO PAGA. Estrai JSON:
    
    dati_generali:
    - netto (cerca PROGRESSIVI -> netto, colonna finale)
    - giorni_pagati (GG. INPS o GIORNI RETRIBUITI)
    
    competenze:
    - base (paga base)
    - straordinari (somma str/suppl/notturni)
    - festivita (somma festività/maggiorazioni)
    - anzianita (scatti)
    - lordo_totale (totale competenze)
    
    trattenute:
    - inps (totale contributi)
    - irpef_netta (trattenuta irpef)
    - addizionali_totali
    
    ferie (box FERIE): residue_ap, maturate, godute, saldo
    par (box PAR/ROL): residue_ap, spettanti, fruite, saldo
    
    e_tredicesima: boolean
    
    JSON Output strict. Usa 0.0 se manca valore.
    """

def get_cartellino_prompt():
    return """
    Analizza CARTELLINO PRESENZE.
    
    1. Conta i giorni con timbrature (es L01, M02...) -> giorni_reali.
       Se vuoto -> giorni_reali=0.
    2. Cerca "GG PRESENZA" o "0265" -> gg_presenza.
    3. Cerca totali ore "0251" o "Totale Ore" -> ore_ordinarie_0251.
    4. Cerca anomalie (giorni senza badge) -> giorni_senza_badge.
    
    JSON: {giorni_reali, gg_presenza, ore_ordinarie_0251, ore_lavorate_0253, giorni_senza_badge, note, debug_prime_righe}
    """

def validate_cartellino(res):
    # Permissiva: basta un numero o un testo chiave
    if any(float(res.get(k,0) or 0) > 0 for k in ['giorni_reali','gg_presenza','ore_ordinarie_0251']): return True
    txt = str(res.get('debug_prime_righe','')).upper() + str(res.get('note','')).upper()
    return "TIMBRATURE" in txt or "PRESENZA" in txt or "NESSUN DATO" in txt

# --- BOT DOWNLOAD ROBUSTO ---
def scarica_documenti_automatici(mese_nome, anno, username, password, tipo_documento="cedolino"):
    nomi_mesi_it = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno",
                    "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]
    try: mese_num = nomi_mesi_it.index(mese_nome) + 1
    except: return None, None, "Mese invalido"

    wd = Path.cwd()
    path_busta = str(wd / f"busta_{mese_num}_{anno}.pdf")
    path_cart = str(wd / f"cartellino_{mese_num}_{anno}.pdf")
    target_busta = f"Tredicesima {anno}" if tipo_documento == "tredicesima" else f"{mese_nome} {anno}"
    
    log = st.empty()
    log.info(f"🤖 Bot avviato: {mese_nome} {anno}")
    
    b_ok, c_ok = False, False

    try:
        with sync_playwright() as p:
            # Opzioni Browser Stealth
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox', 
                    '--disable-gpu', 
                    '--disable-blink-features=AutomationControlled', # Anti-detection
                    '--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                ]
            )
            context = browser.new_context(
                accept_downloads=True, 
                locale='it-IT'
            )
            page = context.new_page()
            page.set_default_timeout(45000)

            # LOGIN
            log.info("🔐 Login...")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y", wait_until="domcontentloaded")
            time.sleep(2) # Pausa tattica
            
            page.fill('input[type="text"]', username)
            time.sleep(0.5)
            page.fill('input[type="password"]', password)
            time.sleep(0.5)
            page.press('input[type="password"]', 'Enter')
            
            # Ciclo di controllo "intelligente" post-login
            logged_in = False
            for _ in range(15): # Prova per 15 secondi
                time.sleep(1)
                # Killa popup
                page.keyboard.press("Escape")
                try: page.evaluate("document.querySelectorAll('.dijitDialogUnderlay, .dijitDialog').forEach(e => e.style.display='none')")
                except: pass
                
                # Check successo
                if page.locator("text=I miei dati").count() > 0:
                    logged_in = True
                    break
            
            if not logged_in:
                browser.close()
                return None, None, "LOGIN_FALLITO"

            # BUSTA
            log.info(f"📄 Scarico Busta...")
            try:
                page.click("text=I miei dati", force=True)
                time.sleep(0.5)
                page.click("text=Documenti", force=True)
                time.sleep(3)
                
                try: page.locator("tr", has=page.locator("text=Cedolino")).locator(".z-image").first.click()
                except: page.click("text=Cedolino", force=True)
                time.sleep(3)
                
                links = page.locator("a")
                idx = -1
                for i in range(links.count()):
                    t = links.nth(i).inner_text()
                    if target_busta.lower() in t.lower():
                        if tipo_documento == "tredicesima" and "13" not in t: continue
                        if tipo_documento != "tredicesima" and "13" in t: continue
                        idx = i
                
                if idx >= 0:
                    with page.expect_download(timeout=20000) as dl: links.nth(idx).click()
                    dl.value.save_as(path_busta)
                    b_ok = True
                    log.success("✅ Busta OK")
                else:
                    log.warning("⚠️ Busta non trovata")
            except Exception as e:
                log.warning(f"Errore step Busta: {e}")

            # CARTELLINO
            if tipo_documento != "tredicesima":
                log.info("📅 Scarico Cartellino...")
                page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2")
                time.sleep(3)
                
                page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()") # Time
                time.sleep(2)
                page.evaluate("document.getElementById('lnktab_5_label')?.click()") # Cartellino
                time.sleep(4)
                
                last = calendar.monthrange(anno, mese_num)[1]
                d1 = f"01/{mese_num:02d}/{anno}"
                d2 = f"{last}/{mese_num:02d}/{anno}"
                
                try:
                    page.locator("input[id*='CLRICHIE']").fill(d1) 
                    page.locator("input[id*='CLRICHI2']").fill(d2)
                    page.keyboard.press("Enter")
                    time.sleep(5)
                except: pass
                
                # Check riga + scarica
                try:
                    target_row = f"{mese_num:02d}/{anno}"
                    row = page.locator(f"tr:has-text('{target_row}')").first
                    icon = row.locator("img[src*='search']").first if row.count() > 0 else page.locator("img[src*='search']").first
                    
                    if icon.count() > 0:
                        with context.expect_page() as popup_ev: icon.click()
                        popup = popup_ev.value
                        
                        url = popup.url.replace("/js_rev//", "/js_rev/")
                        if "EMBED=y" not in url: url += "&EMBED=y" if "?" in url else "?EMBED=y"
                        
                        resp = context.request.get(url)
                        if resp.body()[:4] == b"%PDF":
                            Path(path_cart).write_bytes(resp.body())
                            c_ok = True
                            log.success("✅ Cartellino OK")
                except Exception as e:
                    log.error(f"Errore Cartellino: {e}")

            browser.close()
            log.empty()

    except Exception as e:
        log.error(f"Err Generico: {e}")
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
        st.success(f"✅ {st.session_state['username']}")
        if st.button("🔄 Cambia"):
            st.session_state.update({'credentials_set': False})
            st.rerun()
            
    st.divider()
    
    if st.session_state.get('credentials_set'):
        sel_anno = st.selectbox("Anno", [2024, 2025, 2026], index=1)
        sel_mese = st.selectbox("Mese", ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"], index=11)
        tipo_doc = st.radio("Tipo", ["📄 Cedolino Mensile", "🎄 Tredicesima"])
        
        if st.button("🚀 AVVIA ANALISI", type="primary"):
            st.session_state['done'] = False
            tipo = "tredicesima" if "Tredicesima" in tipo_doc else "cedolino"
            
            pb, pc, err = scarica_documenti_automatici(sel_mese, sel_anno, st.session_state['username'], st.session_state['password'], tipo)
            
            if err == "LOGIN_FALLITO": st.error("LOGIN FALLITO - Controlla credenziali o riprova")
            else:
                st.session_state.update({'busta': pb, 'cart': pc, 'tipo': tipo})

# RISULTATI UI
if st.session_state.get('busta') or st.session_state.get('cart'):
    if not st.session_state.get('done'):
        with st.spinner("🧠 Analisi AI..."):
            db = estrai_con_fallback(st.session_state.get('busta'), get_busta_prompt(), "Busta Paga")
            dc = estrai_con_fallback(st.session_state.get('cart'), get_cartellino_prompt(), "Cartellino", validate_fn=validate_cartellino)
            st.session_state.update({'db': db, 'dc': dc, 'done': True})
            if st.session_state.get('busta'): os.remove(st.session_state['busta'])
            if st.session_state.get('cart'): os.remove(st.session_state['cart'])

    db = st.session_state.get('db')
    dc = st.session_state.get('dc')
    tipo = st.session_state.get('tipo', 'cedolino')

    if db and db.get('e_tredicesima'): st.success("🎄 **Cedolino TREDICESIMA**")
    
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
            k3.metric("📆 GG INPS (Busta)", int(dg.get('giorni_pagati', 0)))

            st.markdown("---")

            c_entr, c_usc = st.columns(2)
            with c_entr:
                st.subheader("➕ Competenze")
                st.write(f"**Paga Base:** € {comp.get('base', 0):.2f}")
                if comp.get('anzianita', 0) > 0: st.write(f"**Anzianità:** € {comp.get('anzianita', 0):.2f}")
                if comp.get('straordinari', 0) > 0: st.write(f"**Straordinari/Suppl.:** € {comp.get('straordinari', 0):.2f}")
                if comp.get('festivita', 0) > 0: st.write(f"**Festività/Maggiorazioni:** € {comp.get('festivita', 0):.2f}")

            with c_usc:
                st.subheader("➖ Trattenute")
                st.write(f"**Contributi INPS:** € {tratt.get('inps', 0):.2f}")
                st.write(f"**IRPEF Netta:** € {tratt.get('irpef_netta', 0):.2f}")
                if tratt.get('addizionali_totali', 0) > 0: st.write(f"**Addizionali:** € {tratt.get('addizionali_totali', 0):.2f}")

            with st.expander("🏖️ Situazione Ferie"):
                f1, f2, f3, f4 = st.columns(4)
                f1.metric("Residue AP", f"{ferie.get('residue_ap', 0):.2f}")
                f2.metric("Maturate", f"{ferie.get('maturate', 0):.2f}")
                f3.metric("Fruite", f"{ferie.get('godute', 0):.2f}")
                saldo_f = ferie.get('saldo', 0)
                f4.metric("Saldo", f"{saldo_f:.2f}", delta="OK" if saldo_f >= 0 else "Negativo")

            with st.expander("⏱️ Situazione Permessi"):
                p1, p2, p3, p4 = st.columns(4)
                p1.metric("Residue AP", f"{par.get('residue_ap', 0):.2f}")
                p2.metric("Spettanti", f"{par.get('spettanti', 0):.2f}")
                p3.metric("Fruite", f"{par.get('fruite', 0):.2f}")
                saldo_p = par.get('saldo', 0)
                p4.metric("Saldo", f"{saldo_p:.2f}", delta="OK" if saldo_p >= 0 else "Negativo")
        else:
            st.warning("⚠️ Dati busta non disponibili")

    with tab2:
        if dc:
            c1, c2 = st.columns([1, 2])
            with c1:
                gg_presenza = float(dc.get('gg_presenza', 0) or 0)
                giorni_reali = float(dc.get('giorni_reali', 0) or 0)
                if gg_presenza > 0: st.metric("📅 GG Presenza (Cartellino)", gg_presenza)
                elif giorni_reali > 0: st.metric("📅 Giorni timbrati (stimati)", giorni_reali)
                else: st.metric("📅 Presenze", "N/D")
                
                anomalie = dc.get('giorni_senza_badge', 0)
                if anomalie > 0: st.metric("⚠️ Anomalie Badge", anomalie, delta="Controlla")
            
            with c2:
                st.info(f"**📝 Note:** {dc.get('note', '')}")
                if dc.get('debug_prime_righe'):
                     with st.expander("Dati grezzi"): st.text(dc['debug_prime_righe'])
        else:
            if tipo == "tredicesima": st.warning("⚠️ Cartellino non disponibile (Tredicesima)")
            else: st.error("❌ Errore cartellino")

    with tab3:
        if db and dc:
            gg_inps = float(db.get('dati_generali', {}).get('giorni_pagati', 0) or 0)
            gg_presenza = float(dc.get('gg_presenza', 0) or 0)
            giorni_reali = float(dc.get('giorni_reali', 0) or 0)
            
            st.subheader("🔍 Analisi Discrepanze")
            
            val_cart = gg_presenza if gg_presenza > 0 else giorni_reali
            diff = val_cart - gg_inps
            
            col_a, col_b = st.columns(2)
            col_a.metric("GG INPS (Busta)", gg_inps)
            col_b.metric("GG Cartellino", val_cart, delta=f"{diff:.1f}")
            
            st.markdown("---")
            if abs(diff) < 0.5: st.success("✅ Tutto OK")
            else: st.warning(f"⚠️ Discrepanza di {diff:.1f} giorni")
        elif tipo == "tredicesima": st.info("Analisi non disponibile per Tredicesima")
        else: st.warning("Servono entrambi i documenti")
