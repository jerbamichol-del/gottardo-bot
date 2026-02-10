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

# --- OPTIONAL: DeepSeek ---
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

@st.cache_resource
def ensure_playwright():
    try:
        subprocess.run(["playwright", "install", "chromium"], check=False)
    except:
        pass
    return True

ensure_playwright()

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

try:
    locale.setlocale(locale.LC_TIME, "it_IT.UTF-8")
except:
    pass

# ==============================================================================
# HEADER & COSTANTI
# ==============================================================================
LOGOPATH = Path(__file__).resolve().parent / "assets" / "logo.jpg"
c_logo, c_title = st.columns([0.75, 9.25], gap="small", vertical_alignment="center")
with c_logo:
    if LOGOPATH.exists():
        st.image(str(LOGOPATH), width=100)
with c_title:
    st.markdown('<h1 style="margin:0;padding:0">Gottardo Payroll Analyzer</h1>', unsafe_allow_html=True)

st.title("💶 Analisi Stipendio & Presenze")

MESI_IT = ["Gennaio", "Febbraio", "Marzo", "Aprile", "Maggio", "Giugno", 
           "Luglio", "Agosto", "Settembre", "Ottobre", "Novembre", "Dicembre"]

# CORREZIONE CRUCIALE: 80 ore assenza = 10 giorni (26 pagati - 16 lavorati).
# Quindi il divisore deve essere 8.0, non 7.0.
ORE_PER_GIORNO = 8.0 

CALENDAR_CODES = {
    "FEP": "FERIE PIANIFICATE", 
    "OMT": "OMESSA TIMBRATURA",
    "RCS": "RIPOSO COMPENSATIVO SUCC", 
    "RIC": "RIPOSO COMPENSATIVO FORZ", 
    "MAL": "MALATTIA"
}

# ==============================================================================
# HELPERS
# ==============================================================================
def safe_float(val):
    try:
        return float(str(val).replace("€","").replace(".","").replace(",",".").strip())
    except:
        return 0.0

def safe_int(val):
    try:
        return int(float(str(val).replace(",",".").strip()))
    except:
        return 0

def clean_json(text):
    try:
        if not text: return None
        text = re.sub(r"```json|```", "", text).strip()
        s, e = text.find("{"), text.rfind("}") + 1
        return json.loads(text[s:e]) if s != -1 else json.loads(text)
    except:
        return None

def extract_text(path):
    if not path or not os.path.exists(path): return None
    try:
        if fitz:
            with fitz.open(path) as doc: return "\n".join([p.get_text() for p in doc]).strip()
    except: pass
    try:
        if PdfReader:
            reader = PdfReader(path)
            return "\n".join([p.extract_text() or "" for p in reader.pages]).strip()
    except: pass
    return None

def get_keys():
    return st.secrets.get("GOOGLE_API_KEY"), st.secrets.get("DEEPSEEK_API_KEY")

@st.cache_resource
def init_models():
    k, _ = get_keys()
    if not k: return []
    genai.configure(api_key=k)
    try:
        ms = []
        for m in genai.list_models():
            if "generateContent" in m.supported_generation_methods:
                n = m.name.replace("models/", "")
                if "gemini" in n.lower() and "embedding" not in n.lower():
                    ms.append((n, genai.GenerativeModel(n)))
        ms.sort(key=lambda x: 0 if "flash" in x[0] else (1 if "lite" in x[0] else 2))
        return ms
    except:
        return []

def analyze_ai(path, prompt, tipo="doc"):
    if not path: return None
    models = init_models()
    _, dsk = get_keys()
    
    with open(path, "rb") as f: 
        b = f.read()
    if b[:4] != b"%PDF": return None

    prog = st.empty()
    
    # Gemini
    for i, (name, model) in enumerate(models):
        try:
            prog.info(f"🔄 {tipo}: {name}...")
            res = model.generate_content([prompt, {"mime_type": "application/pdf", "data": b}])
            j = clean_json(res.text)
            if j:
                prog.success(f"✅ {tipo} analizzato!")
                time.sleep(0.3); prog.empty()
                return j
        except: continue
    
    # DeepSeek Fallback
    if dsk and OpenAI:
        try:
            prog.warning(f"⚠️ Fallback DeepSeek per {tipo}...")
            txt = extract_text(path)
            if txt and len(txt) > 50:
                client = OpenAI(api_key=dsk, base_url="https://api.deepseek.com")
                r = client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[{"role":"system","content":"JSON only"},{"role":"user","content":prompt+"\n\nTEXT:\n"+txt[:25000]}],
                    temperature=0.1
                )
                j = clean_json(r.choices[0].message.content)
                if j:
                    prog.success("✅ DeepSeek OK"); time.sleep(0.3); prog.empty()
                    return j
        except: pass

    prog.error(f"❌ Errore analisi {tipo}")
    return None

# ==============================================================================
# PARSERS (Corretti per i dati reali)
# ==============================================================================
def parse_busta(path):
    # Prompt mirato per Busta Gottardo
    p = """ESTRAI DATI CEDOLINO GOTTARDO SPA.
    1. DATI GENERALI:
       - NETTO: Valore box "NETTO" in basso a destra.
       - GIORNI PAGATI: Valore "GG. INPS" (es. 26).
       - ORE ORDINARIE: Valore "ORE INAIL".
    2. ASSENZE MESE (dal corpo centrale):
       - Somma ore "FERIE GODUTE" (v.4521) -> ore_ferie
       - Somma ore "PERMESSI GODUTI" (v.4529) -> ore_permessi
       - Somma ore "MALATTIA" -> ore_malattia
    3. TABELLA FERIE (alto dx): RES.PREC, SPETTANTI, FRUITE, SALDO.
    4. COMPETENZE: Base, Straordinari, Festivita, Lordo Totale.
    5. TRATTENUTE: INPS, IRPEF, Addizionali.
    
    Output JSON esatto."""
    
    def_val = {"e_tredicesima":False, "dati_generali":{}, "competenze":{}, "trattenute":{}, "ferie":{}, "par":{}, "assenze_mese":{}}
    return analyze_ai(path, p, "Busta") or def_val

def parse_cart(path):
    # Prompt per Cartellino con gestione Riposi
    p = """ANALIZZA CARTELLINO GOTTARDO SPA.
    1. FOOTER:
       - GG PRESENZA (cod 0265) -> giorni_footer
       - ORE LAVORATE (cod 0253)
    2. RIGHE PRESENZA (giorni_righe): Conta righe con timbrature reali.
    3. ASSENZE/RIPOSI:
       - FERIE: conta giorni con FER/FEP.
       - RIPOSI: conta giorni con RCS/RIC/RDD.
       - FESTIVITA: conta giorni con F70/FST.
    Output JSON. IMPORTANTE: Distingui bene Riposi da Ferie."""
    
    res = analyze_ai(path, p, "Cartellino")
    if not res: return {}
    
    gf = safe_int(res.get("giorni_footer", 0))
    gr = safe_int(res.get("giorni_righe", 0))
    res["giorni_lavorati"] = gf if gf > 0 else gr
    return res

# ==============================================================================
# AGENDA
# ==============================================================================
def read_agenda(ctx, m_num, anno):
    base = "https://selfservice.gottardospa.it/js_rev/JSipert2"
    res = {"events_by_type":{}, "total_events":0, "success":False}
    norm = {"FEP":"FERIE","OMT":"OMESSA TIMBRATURA","RCS":"RIPOSO","RIC":"RIPOSO","MAL":"MALATTIA"}
    
    for c, name in CALENDAR_CODES.items():
        try:
            u = f"{base}/api/time/v2/events?$filter_api=calendarCode={c},startTime={anno}-01-01T00:00:00,endTime={anno}-12-31T00:00:00"
            r = ctx.request.get(u, timeout=10000)
            if r.ok:
                evs = r.json()
                if not isinstance(evs, list): evs = [evs]
                cnt = 0
                for e in evs:
                    s = e.get("startTime","") or e.get("start","")
                    if len(s)>=7 and int(s[5:7]) == m_num: cnt += 1
                if cnt:
                    k = norm.get(c, c)
                    res["events_by_type"][k] = res["events_by_type"].get(k, 0) + cnt
                    res["total_events"] += cnt
        except: pass
    
    if res["total_events"] > 0: res["success"] = True
    return res

# ==============================================================================
# EXECUTE DOWNLOAD
# ==============================================================================
def execute(mnome, anno, user, pwd, is13):
    res = {"busta":None,"cart":None,"agenda":None,"login_ok":None}
    mnum = MESI_IT.index(mnome)+1
    anno = int(anno)
    suff = "_13" if is13 else ""
    lbusta = os.path.abspath(f"busta_{mnum}_{anno}{suff}.pdf")
    lcart = os.path.abspath(f"cartellino_{mnum}_{anno}.pdf")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-gpu"])
        ctx = browser.new_context(accept_downloads=True, user_agent="Mozilla/5.0 Chrome/120.0.0.0")
        page = ctx.new_page()
        page.set_viewport_size({"width":1920,"height":1080})
        
        try:
            # LOGIN
            st.toast("🔐 Login...", icon="🔐")
            page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2?r=y", wait_until="domcontentloaded")
            page.wait_for_selector("#ParametriLogin input[name='username']", timeout=20000)
            page.locator("#ParametriLogin input[name='username']").first.fill(user)
            page.locator("#ParametriLogin input[name='password']").first.fill(pwd)
            
            try:
                btn = page.get_by_role("button", name="Accedi")
                if btn.count() > 0 and btn.first.is_visible(): btn.first.click()
                else: page.locator("#ParametriLogin input[name='password']").first.press("Enter")
            except: pass
            
            try: page.wait_for_load_state("networkidle", timeout=25000)
            except: time.sleep(3)
            
            lok = False
            for s in ["text=I miei dati", "text=Time", "#revit_navigation_NavHoverItem_0_label"]:
                if page.locator(s).count() > 0: lok=True; break
            if not lok:
                res["login_ok"] = False
                res["login_error"] = "Login fallito"
                return res
            res["login_ok"] = True
            
            # AGENDA
            st.toast("🗓️ Agenda...", icon="🗓️")
            res["agenda"] = read_agenda(ctx, mnum, anno)
            
            # BUSTA
            st.toast("💰 Busta...", icon="💰")
            try:
                try: page.keyboard.press("Escape"); time.sleep(0.3)
                except: pass
                
                try: page.evaluate("document.getElementById('revit_navigation_NavHoverItem_0_label')?.click()")
                except: page.locator("text=I miei dati").first.click(force=True)
                time.sleep(2)
                
                try: page.evaluate("document.getElementById('lnktab_2_label')?.click()")
                except: pass
                time.sleep(2)
                
                try: page.locator("tr", has=page.locator("text=Cedolino")).locator(".z-image").click(timeout=5000)
                except: page.locator("text=Cedolino").first.click(force=True)
                time.sleep(4)
                
                with page.expect_download(timeout=25000) as dl:
                    if is13:
                        page.get_by_text(re.compile(f"Tredicesima.*{anno}", re.I)).first.click()
                    else:
                        found = False
                        links = page.locator("a").all()
                        pats = [f"{mnome} {anno}", f"{mnum:02d}/{anno}", f"{mnum:02d}-{anno}"]
                        for l in links:
                            txt = l.inner_text().strip()
                            if len(txt) < 4 or "13" in txt: continue
                            if any(x.lower() in txt.lower() for x in pats):
                                l.click(); found = True; break
                        if not found:
                             for l in links:
                                 txt = l.inner_text().strip()
                                 if mnome.lower() in txt.lower() and str(anno) in txt and "13" not in txt:
                                     l.click(); found = True; break
                        if not found: raise Exception("Link non trovato")
                
                dl.value.save_as(lbusta)
                if os.path.exists(lbusta): res["busta"] = lbusta; st.toast("✅ Busta OK")
            except Exception as e:
                st.warning(f"Busta: {e}")

            # CARTELLINO
            if not is13:
                st.toast("📅 Cartellino...", icon="📅")
                try:
                    try: page.keyboard.press("Escape"); time.sleep(0.3)
                    except: pass
                    
                    try:
                        logo = page.locator("img[src*='logo'], .logo").first
                        if logo.is_visible(timeout=2000): logo.click(); time.sleep(2)
                        else: raise Exception("no logo")
                    except:
                        page.goto("https://selfservice.gottardospa.it/js_rev/JSipert2", wait_until="domcontentloaded")
                        time.sleep(3)
                    
                    try: page.evaluate("document.getElementById('revit_navigation_NavHoverItem_2_label')?.click()")
                    except: page.locator("text=Time").first.click(force=True)
                    time.sleep(3)
                    
                    try: page.evaluate("document.getElementById('lnktab_5_label')?.click()")
                    except: page.locator("text=Cartellino").first.click(force=True)
                    time.sleep(5)
                    
                    ld = calendar.monthrange(anno, mnum)[1]
                    d1, d2 = f"01/{mnum:02d}/{anno}", f"{ld}/{mnum:02d}/{anno}"
                    dal = page.locator("input[id*='CLRICHIE']").first
                    al = page.locator("input[id*='CLRICHI2']").first
                    
                    if dal.count()>0:
                        dal.click(force=True); page.keyboard.press("Control+A"); dal.type(d1, delay=50); dal.press("Tab"); time.sleep(0.5)
                        al.click(force=True); page.keyboard.press("Control+A"); al.type(d2, delay=50); al.press("Tab"); time.sleep(0.5)
                    
                    try: page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last.click(force=True)
                    except: page.get_by_role("button", name=re.compile("ricerca|esegui", re.I)).last.click()
                    time.sleep(8)
                    
                    pat = f"{mnum:02d}/{anno}"
                    row = page.locator(f"tr:has-text('{pat}')").first
                    icon = row.locator("img[src*='search']").first if row.count()>0 else page.locator("img[src*='search']").first
                    
                    if icon.count()==0: raise Exception("Icona PDF non trovata")
                    
                    with ctx.expect_page(timeout=20000) as pinfo: icon.click()
                    popup = pinfo.value
                    
                    t0 = time.time(); u_pdf = ""
                    while time.time()-t0 < 15:
                        if "SERVIZIO=JPSC" in popup.url: 
                            u_pdf = popup.url.replace("/js_rev//", "/js_rev/") + ("&EMBED=y" if "EMBED" not in popup.url else "")
                            break
                        time.sleep(0.2)
                    
                    if u_pdf:
                        body = ctx.request.get(u_pdf, timeout=60000).body()
                        with open(lcart, "wb") as f: f.write(body)
                        res["cart"] = lcart; st.toast("✅ Cartellino OK")
                    else:
                        popup.pdf(path=lcart, format="A4")
                        if os.path.exists(lcart): res["cart"] = lcart

                    try: popup.close()
                    except: pass
                    
                except Exception as e:
                    st.warning(f"Cartellino: {e}")
                    try:
                        page.screenshot(path="debug_cartellino.png")
                        with st.expander("Debug"): st.image("debug_cartellino.png")
                    except: pass

        finally: browser.close()
    return res

def cleanup(*files):
    for f in files:
        if f and os.path.exists(f): 
            try: os.remove(f)
            except: pass

# ==============================================================================
# UI
# ==============================================================================
u = st.session_state.get("u", st.secrets.get("ZK_USER", ""))
p = st.session_state.get("p", st.secrets.get("ZK_PASS", ""))

if not u or not p:
    c1,c2,c3 = st.columns([2,2,1])
    u_in = c1.text_input("User"); p_in = c2.text_input("Pass", type="password")
    if c3.button("Login"): st.session_state.u=u_in; st.session_state.p=p_in; st.rerun()
else:
    c1,c2,c3,c4,c5 = st.columns([1, 1.5, 1, 1.5, 0.5])
    c1.markdown(f"**{u}**")
    m = c2.selectbox("Mese", MESI_IT, index=9)
    a = c3.selectbox("Anno", [2024,2025,2026], index=1)
    
    tipo = "Cedolino"
    if m == "Dicembre": tipo = c2.radio("Tipo", ["Cedolino", "Tredicesima"], horizontal=True)
    is13 = (tipo == "Tredicesima")

    if c4.button("🚀 ANALIZZA", type="primary"):
        with st.status("Elaborazione...", expanded=True):
            r = execute(m, a, u, p, is13)
            if r.get("login_ok") is False: st.error("Login KO"); st.stop()
            
            res_b = parse_busta(r["busta"])
            res_c = parse_cart(r["cart"]) if r["cart"] else {}
            st.session_state["data"] = {
                "busta": res_b, "cart": res_c, "agenda": r["agenda"] or {},
                "meta": {"m": m, "a": a, "is13": is13, "cart_ok": bool(r["cart"])}
            }
            cleanup(r["busta"], r["cart"])
            st.rerun()

    if c5.button("🔄"): st.session_state.clear(); st.rerun()

# --- DISPLAY OUTPUT ---
if "data" in st.session_state:
    d = st.session_state["data"]
    b, c, ag = d["busta"], d["cart"], d["agenda"]
    m, a = d["meta"]["m"], d["meta"]["a"]
    
    dg = b.get("dati_generali", {})
    comp = b.get("competenze", {})
    tratt = b.get("trattenute", {})
    ass = b.get("assenze_mese", {})

    # CALCOLI CORRETTI
    gg_inps = safe_int(dg.get("giorni_pagati", 0))
    c_lav = safe_float(c.get("giorni_lavorati", 0))
    c_mal = safe_int(c.get("malattia", 0))
    c_fest = safe_int(c.get("festivita", 0))
    c_riposi = safe_int(c.get("riposi", 0))
    
    # Ferie da Busta (Documento Ufficiale)
    h_fer = safe_float(ass.get("ore_ferie", 0)) + safe_float(ass.get("ore_permessi", 0))
    
    # Conversione Ore -> Giorni (con coefficiente 8.0)
    gg_fer = round(h_fer / ORE_PER_GIORNO) if h_fer > 0 else safe_int(c.get("ferie",0))
    src_fer = "Busta" if h_fer > 0 else ("Cartellino" if c else "Agenda")
    
    # Totale: Lavorati + Ferie + Malattia + Fest (ESCLUSI RIPOSI)
    tot = c_lav + gg_fer + c_mal + c_fest if d["meta"]["cart_ok"] else gg_fer + c_mal
    diff = tot - gg_inps if d["meta"]["cart_ok"] else None

    st.divider()
    st.subheader(f"📊 {m} {a}")
    
    k1,k2,k3,k4 = st.columns(4)
    k1.metric("GG INPS", gg_inps)
    k2.metric("GG Calcolati", f"{tot:.0f}", delta=f"{diff:+.0f}" if diff is not None and diff!=0 else None)
    k3.metric("Lavorati", c_lav)
    k4.metric("Omesse (A)", ag.get("events_by_type",{}).get("OMESSA TIMBRATURA",0))
    
    st.caption(f"Ferie: {gg_fer} ({src_fer}) | Malattia: {c_mal} | Fest: {c_fest} | Riposi: {c_riposi} (non sommati)")
    if h_fer > 0: st.caption(f"Dettaglio Ore Busta: {h_fer:.2f} ore / {ORE_PER_GIORNO} = {gg_fer} gg")

    t1, t2, t3 = st.tabs(["Stipendio", "Cartellino", "Ferie"])
    
    with t1:
        c1,c2,c3,c4 = st.columns(4)
        c1.metric("Netto", f"€ {safe_float(dg.get('netto',0)):,.2f}")
        c2.metric("Lordo", f"€ {safe_float(comp.get('lordo_totale',0)):,.2f}")
        c3.metric("GG Pagati", gg_inps)
        c4.metric("Ore Lav", safe_float(dg.get("ore_ordinarie",0)))
        st.markdown("---")
        c1,c2 = st.columns(2)
        c1.write("**Competenze**"); c1.write(f"Base: €{safe_float(comp.get('base',0)):,.2f}")
        c2.write("**Trattenute**"); c2.write(f"INPS: €{safe_float(tratt.get('inps',0)):,.2f}")
    
    with t2:
        if c: st.json(c)
        else: st.info("Nessun dato cartellino")
    
    with t3:
        c1,c2 = st.columns(2)
        c1.write("Ferie"); c1.write(b.get("ferie",{}))
        c2.write("PAR"); c2.write(b.get("par",{}))
