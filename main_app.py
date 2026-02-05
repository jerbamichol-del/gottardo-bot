# CARTELLINO - VERSIONE PULITA E VELOCE (popup + GET PDF raw con EMBED=y)
if tipo_documento != "tredicesima":
    st_status.info("📅 Download cartellino...")
    try:
        # Torna alla home
        page.evaluate("window.scrollTo(0, 0)")
        time.sleep(1)
        try:
            page.keyboard.press("Escape")
            time.sleep(0.5)
        except:
            pass

        try:
            logo = page.locator("img[src*='logo'], .logo").first
            if logo.is_visible(timeout=2000):
                logo.click()
                time.sleep(2)
        except:
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
        except:
            pass

        # Esegui ricerca
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(0.5)
        try:
            page.locator("//span[contains(text(),'Esegui ricerca')]/ancestor::span[@role='button']").last.click(force=True)
        except:
            page.keyboard.press("Enter")

        # Attendi risultati
        try:
            page.wait_for_selector("text=Risultati della ricerca", timeout=20000)
        except:
            pass

        # Trova riga e lente (micro-refactor: clicca sempre l’icona, niente branch su str(locator))
        target_cart_row = f"{mese_num:02d}/{anno}"  # es. 12/2025
        riga_row = page.locator(f"tr:has-text('{target_cart_row}')").first
        if riga_row.count() > 0 and riga_row.locator("img[src*='search']").count() > 0:
            icona = riga_row.locator("img[src*='search']").first
        else:
            icona = page.locator("img[src*='search']").first

        if icona.count() == 0:
            # Fallback estremo: salva la pagina corrente
            page.pdf(path=path_cart)
        else:
            # Click -> popup
            with context.expect_page(timeout=20000) as popup_info:
                icona.click()
            popup = popup_info.value

            # Prendi URL del popup (normalizza eventuale doppio slash)
            popup_url = (popup.url or "").replace("/js_rev//", "/js_rev/")

            # FORZA EMBED=y (nel tuo log, senza EMBED risponde HTML)
            if "EMBED=y" not in popup_url:
                popup_url = popup_url + ("&" if "?" in popup_url else "?") + "EMBED=y"

            # Scarica bytes PDF via API request del context (stessa sessione/cookie)
            resp = context.request.get(popup_url, timeout=60000)
            body = resp.body()

            if body[:4] == b"%PDF":
                Path(path_cart).write_bytes(body)
            else:
                # Fallback: stampa del popup (meno fedele ma produce file)
                try:
                    popup.pdf(path=path_cart, format="A4")
                except:
                    page.pdf(path=path_cart)

            try:
                popup.close()
            except:
                pass

        # Verifica file
        if os.path.exists(path_cart) and os.path.getsize(path_cart) > 5000:
            cart_ok = True
            st_status.success("✅ Cartellino OK")
        else:
            st.warning("⚠️ Cartellino scaricato ma sembra piccolo/vuoto")

    except Exception as e:
        st.error(f"❌ Errore cartellino: {e}")
        try:
            page.pdf(path=path_cart)
        except:
            pass
