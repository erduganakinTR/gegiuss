"""Touristica scraper — Playwright + stealth ile (Cloudflare korumasi nedeniyle).

Diger acenteler Selenium kullanir; Touristica'nin arama API'si Cloudflare bot
yonetimiyle korundugu icin standart headless Chrome bloklaniyor. Bu yuzden
SADECE bu scraper Playwright + stealth kullanir. Disaridan arayuzu digerleriyle
ayni: get_price(...) -> scrape_result(price/status/rooms).

ETS/Jolly/Setur/Tatilsepeti'de oldugu gibi "tarayicisiz API" arastirmasi
bu acente icin de yapildi (bkz. Browser pane ile /Ajax/Main.asmx/HotelSearchNow
istegi incelendi): SONUC OLUMSUZ. Diger 4 acentenin aksine arama istegi
('xc' alani) client-side AES ile SIFRELENMIS gonderiliyor — bu, gercek ve
kasitli bir bot-onleme katmani (yalniz Cloudflare degil). Bu sifrelemeyi
kirip taklit etmek kirilgan olur (anahtar donebilir) ve digerlerinden farkli
olarak GERCEK bir anti-bot mekanizmasini asmak anlamina gelir — bu yuzden
DENENMEDI. Bunun yerine mevcut Playwright yaklasimi ICINDE hizlandirma
yapildi: page.route() ile resim/font/analytics gibi gereksiz kaynaklar
engelleniyor (Tatilbudur'daki CDP kaynak-engelleme optimizasyonuyla ayni
mantik, Selenium yerine Playwright'in kendi route API'si kullanilarak).
"""
import re, html as _html, threading
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from .base import room_score, scrape_result, MATCH_THRESHOLD, redirected_away

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_BLOCKED_RESOURCE_TYPES = {"image", "font", "media"}
_BLOCKED_URL_SUBSTRINGS = (
    "google-analytics", "googletagmanager", "doubleclick", "facebook.net",
    "googleadservices", "hotjar", "clarity.ms",
)
# ONEMLI: cdn-cgi/challenge-platform (Cloudflare'in kendi bot-dogrulama script'i)
# KASITLI OLARAK burada YOK — bu script'i engellemek Cloudflare'e "gercek bir
# tarayici degilim" sinyali verip sayfayi tamamen bloklatabilir.


def _route_filter(route):
    req = route.request
    if req.resource_type in _BLOCKED_RESOURCE_TYPES:
        return route.abort()
    url = req.url
    if any(s in url for s in _BLOCKED_URL_SUBSTRINGS):
        return route.abort()
    return route.continue_()

# --- HAVUZ (toplu tarama icin) ---
# Playwright'in browser process baslatma maliyeti yuksek; toplu taramada
# (reuse=True) her thread kendi browser'ini canli tutar, her cagride sadece
# ucuz olan context/page yeniden olusturulur. Kisisel rezervasyon kontrolu
# (checker.py) reuse=False ile eskisi gibi her seferinde acip kapatir.
_RECYCLE_AFTER = 150
_thread_local = threading.local()
_pool_lock = threading.Lock()
_pooled_browsers = []  # (context_manager, browser) ciftleri


def _launch_browser():
    cm = Stealth().use_sync(sync_playwright())
    p = cm.__enter__()
    browser = p.chromium.launch(
        headless=True,
        args=["--window-size=1400,1000", "--window-position=-32000,-32000", "--disable-gpu"])
    return cm, browser


def _close_browser_entry(entry):
    cm, browser = entry
    try:
        browser.close()
    except Exception:
        pass
    try:
        cm.__exit__(None, None, None)
    except Exception:
        pass


def _get_browser(reuse):
    """(context_manager, browser, pooled_mi) dondurur. pooled=False ise
    cagiran taraf browser'i kendisi kapatmali."""
    if not reuse:
        cm, browser = _launch_browser()
        return cm, browser, False

    nav_count = getattr(_thread_local, "nav_count", 0)
    entry = getattr(_thread_local, "entry", None)
    if entry is not None and nav_count < _RECYCLE_AFTER:
        _thread_local.nav_count = nav_count + 1
        return entry[0], entry[1], True

    if entry is not None:
        _close_browser_entry(entry)
        with _pool_lock:
            if entry in _pooled_browsers:
                _pooled_browsers.remove(entry)

    cm, browser = _launch_browser()
    entry = (cm, browser)
    _thread_local.entry = entry
    _thread_local.nav_count = 1
    with _pool_lock:
        _pooled_browsers.append(entry)
    return cm, browser, True


def quit_pooled_browser():
    """Tarama bitince (basarili/iptal/hata farketmeksizin) havuzdaki TUM
    Touristica browser'larini kapatir. hotel_checker._run_scan'in finally'inde cagrilir."""
    with _pool_lock:
        entries = list(_pooled_browsers)
        _pooled_browsers.clear()
    for entry in entries:
        _close_browser_entry(entry)


def get_price(url, giris, cikis, yetiskin, cocuk, cocuk_yaslari, oda_tipi, reuse_driver=False):
    """giris/cikis: 'YYYY-MM-DD'. Touristica otelinden guncel oda fiyatini dondurur.
    reuse_driver=True: toplu tarama sirasinda thread'e ait browser yeniden kullanilir."""
    # Touristica tek odada en fazla 2 cocuk destekler (3. cocuk secenegi yok).
    # Yanlis doluluk fiyati vermemek icin 2'den fazla cocukta islem yapma.
    if int(cocuk or 0) > 2:
        print("[Touristica] Tek odada max 2 cocuk destekleniyor.")
        return scrape_result(status="no_availability")
    try:
        html = _search_and_get_html(url, giris, cikis, yetiskin, cocuk, cocuk_yaslari, reuse_driver)
    except Exception as e:
        print(f"[Touristica] {e}")
        return scrape_result(status="error")
    if html is None:
        return scrape_result(status="error")
    return _match_room_price(html, oda_tipi)


def _search_and_get_html(url, giris, cikis, yetiskin, cocuk, cocuk_yaslari, reuse_driver=False):
    ci = _fmt_date(giris)   # dd.MM.yyyy
    co = _fmt_date(cikis)
    adult = int(yetiskin or 2)
    child = int(cocuk or 0)
    ages = []
    if child > 0 and cocuk_yaslari:
        ages = [y.strip() for y in str(cocuk_yaslari).split(",") if y.strip().isdigit()]

    cm, browser, pooled = _get_browser(reuse_driver)
    try:
        ctx = browser.new_context(user_agent=_UA, locale="tr-TR",
                                  viewport={"width": 1400, "height": 1000})
        try:
            ctx.route("**/*", _route_filter)
            page = ctx.new_page()
            page.on("dialog", lambda d: d.accept())
            got = {"odalar": False}
            page.on("response", lambda r: got.__setitem__("odalar", got["odalar"] or "/odalar?" in r.url))

            page.goto(url, timeout=60000)
            # Sabit 7sn bekleme yerine arama formu (txtCheckInDate) DOM'a
            # girer girmez devam et — cogu zaman 7sn'den cok daha erken hazir olur.
            try:
                page.wait_for_selector("#txtCheckInDate", timeout=8000)
            except Exception:
                pass

            if redirected_away(url, page.url):
                print(f"[Touristica] Sayfa yonlendirildi ({page.url}) — link bozuk/otel kaldirilmis olabilir, GUVENSIZ.")
                return None

            # cerez: zorunlu olmayanlari reddet (gizlilik)
            page.evaluate("""() => { var b=document.getElementById('CybotCookiebotDialogBodyButtonDecline');
                if(b)b.click(); document.querySelectorAll('[id*=Cookiebot]').forEach(e=>e.remove()); }""")

            # tarih + kisi/cocuk ayarla
            page.evaluate(
                """(a) => {
                    function setField(id,v){var e=document.getElementById(id);
                        if(e){e.classList.remove('hidden'); e.value=v;
                        ['input','change','blur'].forEach(ev=>e.dispatchEvent(new Event(ev,{bubbles:true})));}}
                    function setSel(id,v){var e=document.getElementById(id);
                        if(e){e.value=String(v); e.dispatchEvent(new Event('change',{bubbles:true}));}}
                    setField('txtCheckInDate', a.ci);
                    setField('txtCheckOutDate', a.co);
                    setSel('ddlAdultCount', a.adult);
                    setSel('ddlChildCount', a.child);
                    var ids=['ddlFirstChildAge','ddlSecondChildAge','ddlThirdChildAge'];
                    for(var i=0;i<ids.length;i++){ setSel(ids[i], (a.ages[i]!==undefined)?a.ages[i]:0); }
                }""",
                {"ci": ci, "co": co, "adult": adult, "child": child, "ages": ages},
            )
            page.evaluate("""() => { var b=document.querySelector('button.search-button'); if(b)b.click(); }""")

            # /odalar fragmani gelene kadar bekle (max ~12sn), kucuk aralikla yokla
            for _ in range(48):
                page.wait_for_timeout(250)
                if got["odalar"]:
                    break
            # DOM render otursun — fiyat elemani gorununce hemen devam et
            try:
                page.wait_for_selector('.price', timeout=1500)
            except Exception:
                pass
            return page.content()
        finally:
            ctx.close()
    finally:
        if not pooled:
            _close_browser_entry((cm, browser))


def _match_room_price(html, oda_tipi):
    # Tum oda basliklari (accommodation-type)
    names = [(m.start(), re.sub(r'\s+', ' ', _html.unescape(re.sub(r'<[^>]+>', '', m.group(1))).strip()))
             for m in re.finditer(r'accommodation-type[^>]*>\s*([^<]{3,50})', html)]
    # Satis fiyatlari: <div class="price"> 86.260 <small>TL
    prices = [(m.start(), _to_float(m.group(1)))
              for m in re.finditer(r'class="price"[^>]*>\s*([\d.]+)\s*<small', html)]

    all_names = [n for _, n in names]
    if not all_names:
        return scrape_result(status="no_availability")

    # Her fiyati kendinden onceki en yakin oda basligiyla esle
    room_prices = {}
    for ppos, price in prices:
        if not price:
            continue
        prev = [n for n in names if n[0] < ppos]
        if prev:
            room_prices.setdefault(prev[-1][1], price)

    if oda_tipi:
        best_name = max(all_names, key=lambda n: room_score(n, oda_tipi))
        best_score = room_score(best_name, oda_tipi)
        if best_score < MATCH_THRESHOLD:
            print(f"[Touristica] Oda tipi bulunamadi: '{oda_tipi}' (skor {best_score:.2f}). Mevcut: {all_names}")
            return scrape_result(status="no_room", rooms=all_names)
        price = room_prices.get(best_name)
        if price is None:
            print(f"[Touristica] '{best_name}' bu tarihte musait degil.")
            return scrape_result(status="no_availability")
        return scrape_result(price=price)

    if not room_prices:
        return scrape_result(status="no_availability")
    best_name = min(room_prices, key=room_prices.get)
    return scrape_result(price=room_prices[best_name], oda_adi=best_name)


def _to_float(s):
    """'86.260' -> 86260.0 (Turkce binlik ayraci nokta)"""
    try:
        v = float(s.replace(".", ""))
        return v if v > 500 else None
    except Exception:
        return None


def _fmt_date(iso):
    """'2026-07-21' -> '21.07.2026'"""
    try:
        y, m, d = iso.split("-")
        return f"{d}.{m}.{y}"
    except Exception:
        return iso
