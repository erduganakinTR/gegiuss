import re, time, json
from .base import get_driver, room_score, scrape_result, MATCH_THRESHOLD, redirected_away

API_PATH = "/hotel/calculate-room-price"

# Resim/font/css/analitik kaynaklarini engelleyince sayfa yuklemesi olculebilir
# sekilde hizlaniyor (10 otelde A/B: ort. 1.37sn -> 1.20sn, ~%12, tum hotelId'ler
# dogru okundu). Jolly kadar dramatik degil (Tatilbudur zaten hizli render
# ediyor, XHR sonrasi beklemesi yok) ama guvenli ve bedava bir kazanc.
_BLOCKED_RESOURCE_PATTERNS = [
    "*.jpg", "*.jpeg", "*.png", "*.gif", "*.webp", "*.svg", "*.ico",
    "*.woff", "*.woff2", "*.ttf", "*.css",
    "*google-analytics*", "*googletagmanager*", "*doubleclick*",
    "*facebook.net*", "*googleadservices*",
]

# Tarayicinin KENDI fetch'i ile POST atar -> gercek TLS parmak izi, WAF/403 asilir.
_FETCH_JS = r"""
const done = arguments[arguments.length - 1];
const params = arguments[0];
try {
    const tokenEl = document.querySelector('input[name=_token]') ||
                    document.querySelector('meta[name=csrf-token]');
    const token = tokenEl ? (tokenEl.value || tokenEl.getAttribute('content')) : '';
    const body = new URLSearchParams({
        _token: token, productType:'hotel', hotelId: params.hotelId,
        selectedRoom:'', selectedPricingId:'', selectedMealType:'', hotelOldId:'',
        productLoc: params.productLoc, productTypeId:'', code:'', alertRoom:'0',
        actionPricingId:'', currencyId:'', 'actions[]':'0', selectedActionCategory:'',
        hidePrice:'0', googleRemarketingCategory:'', autoPost:'0',
        isFlightPackage:'0', adult: params.adult, child: params.child,
        isCyprusPackageManual:'0', priceConfig:'', loyaltyPoint:'0',
        'daterange-1': params.daterange,
        checkInDate: params.checkIn, checkOutDate: params.checkOut,
        quickPersonCount: params.quickPerson
    });
    if (params.childAges) {
        // Tatilbudur formu cocuk yaslarini 'childAge[]' (tekrarli) olarak yollar
        params.childAges.forEach(function(age){ body.append('childAge[]', age); });
    }
    fetch(params.api, {
        method:'POST',
        headers:{'X-Requested-With':'XMLHttpRequest',
                 'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8'},
        body: body.toString()
    }).then(r => r.text()).then(t => done({ok:true, text:t}))
      .catch(e => done({ok:false, text:String(e)}));
} catch(e) { done({ok:false, text:String(e)}); }
"""


def get_price(url, giris, cikis, yetiskin, cocuk, cocuk_yaslari, oda_tipi, reuse_driver=False):
    """giris/cikis: 'YYYY-MM-DD'. Belirtilen oda tipinin guncel fiyatini dondurur.
    reuse_driver=True: toplu tarama sirasinda thread'e ait tarayici yeniden kullanilir."""
    driver = get_driver(reuse=reuse_driver)
    try:
        # Kaynak engelleme driver-genelinde bir CDP ayari, bu yuzden tarayici
        # basina BIR KERE uygulanir (Jolly'deki sniffer-ready deseniyle ayni).
        # Regex/JSON tabanli parse gorsel/CSS'e bagli olmadigi icin diger agir
        # (Selenium) acentelerle ayni driver paylasilsa da GUVENLIDIR.
        if not getattr(driver, "_resource_blocking_ready", False):
            driver.execute_cdp_cmd("Network.enable", {})
            driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": _BLOCKED_RESOURCE_PATTERNS})
            driver._resource_blocking_ready = True

        driver.get(url)

        if redirected_away(url, driver.current_url):
            print(f"[Tatilbudur] Sayfa yonlendirildi ({driver.current_url}) — link bozuk/otel kaldirilmis olabilir, GUVENSIZ.")
            return scrape_result(status="error")

        # hotelId/productLoc/token sunucu tarafinda render edilen HTML'de
        # (JS ile SONRADAN eklenmiyor), bu yuzden sabit 4sn beklemek yerine
        # kisa bir "hazir mi" dongusu yeterli — cok daha hizli, ayni guvenilirlik.
        hotel_id = _poll_hotel_id(driver)
        product_loc = _find_product_loc(driver)

        adult = int(yetiskin or 2)
        child = int(cocuk or 0)
        quick_person = f"{adult} Yetişkin "
        if child > 0:
            quick_person += f"{child} Çocuk "

        child_ages = []
        if child > 0 and cocuk_yaslari:
            child_ages = [y.strip() for y in str(cocuk_yaslari).split(",") if y.strip()]

        params = {
            "api": API_PATH,
            "hotelId": hotel_id or "",
            "productLoc": product_loc or "",
            "adult": str(adult),
            "child": str(child),
            "daterange": _fmt_daterange(giris, cikis),
            "checkIn": _fmt_date_dot(giris),
            "checkOut": _fmt_date_dot(cikis),
            "quickPerson": quick_person,
            "childAges": child_ages,
        }

        driver.set_script_timeout(45)
        res = driver.execute_async_script(_FETCH_JS, params)
        if not res or not res.get("ok"):
            print(f"[Tatilbudur] fetch hatasi: {res.get('text','')[:200] if res else 'yok'}")
            return scrape_result(status="error")

        try:
            html = json.loads(res["text"]).get("view", "")
        except Exception:
            print("[Tatilbudur] JSON parse hatasi")
            return scrape_result(status="error")
        if not html:
            return scrape_result(status="error")

        return _match_room_price(html, oda_tipi)

    except Exception as e:
        print(f"[Tatilbudur] {e}")
        return scrape_result(status="error")
    finally:
        if not reuse_driver:
            driver.quit()


def _match_room_price(html, oda_tipi):
    """Oda adi -> sell-price eslestir, istenen odanin fiyatini dondur."""
    titles = [(m.start(), re.sub(r'<[^>]+>', '', m.group(1)).strip())
              for m in re.finditer(r'room-type-title">([^<]+)', html)]
    sells = [(m.start(), _to_float(m.group(1)))
             for m in re.finditer(r'sell-price"[^>]*>\s*([\d.,]+)', html)]

    # Tum oda basliklari (musait olsun olmasin). Baslik var ama sell-price yoksa
    # o oda o tarihte sold-out demektir.
    all_names = [name for _, name in titles]
    if not all_names:
        # Hic oda parse edilemedi = o tarih/kisi icin musaitlik yok
        return scrape_result(status="no_availability")

    # Her fiyati kendinden onceki en yakin oda basligiyla esle
    room_prices = {}  # oda_adi -> fiyat (SADECE musait/fiyatli odalar)
    for spos, price in sells:
        if price is None:
            continue
        prev = [t for t in titles if t[0] < spos]
        if not prev:
            continue  # ilk fiyat = "Baslangic Fiyati" ozeti, atla
        name = prev[-1][1]
        room_prices.setdefault(name, price)

    if oda_tipi:
        # Once ISME gore tum odalar icinden en iyi eslesen, SONRA fiyati var mi bak
        best_name = max(all_names, key=lambda n: room_score(n, oda_tipi))
        best_score = room_score(best_name, oda_tipi)
        if best_score < MATCH_THRESHOLD:
            print(f"[Tatilbudur] Oda tipi bulunamadi: '{oda_tipi}' (en iyi skor {best_score:.2f}). "
                  f"Mevcut: {all_names}")
            return scrape_result(status="no_room", rooms=all_names)
        price = room_prices.get(best_name)
        if price is None:
            # Oda listede var ama bu tarih icin fiyat/musaitlik yok (sold-out)
            print(f"[Tatilbudur] '{best_name}' bu tarihte musait degil.")
            return scrape_result(status="no_availability")
        return scrape_result(price=price)

    # Oda tipi belirtilmemisse en dusuk musait fiyat
    if not room_prices:
        return scrape_result(status="no_availability")
    best_name = min(room_prices, key=room_prices.get)
    return scrape_result(price=room_prices[best_name], oda_adi=best_name)


def _to_float(s):
    """'95.940' -> 95940.0"""
    try:
        v = float(s.replace(".", "").replace(",", ""))
        return v if v > 500 else None
    except Exception:
        return None


def _poll_hotel_id(driver, max_wait=1.5, interval=0.15):
    """hotelId genelde sayfa DOM'u hazir olur olmaz mevcuttur; yine de nadir
    gec render durumlarina karsi kisa bir dongu ile birkac kez dener —
    sabit 4sn beklemek yerine cogu zaman ~0.1-0.3sn'de biter."""
    waited = 0.0
    while waited < max_wait:
        hid = _find_hotel_id(driver)
        if hid:
            return hid
        time.sleep(interval)
        waited += interval
    return _find_hotel_id(driver)


def _find_hotel_id(driver):
    # 1) Formun kullandigi yetkili kaynak: gizli input[name=hotelId]
    #    (regex ile sayfadan cekmek yanlis sayi yakalayabiliyor)
    hid = driver.execute_script(
        "try{var i=document.querySelector('input[name=hotelId]');"
        "return (i&&i.value)?i.value:null;}catch(e){return null;}")
    if hid:
        return str(hid)
    # 2) JS degiskeni
    hid = driver.execute_script(
        "try{return window.hotelId||window.hotel_id||null;}catch(e){return null;}")
    if hid:
        return str(hid)
    # 3) Son care: sayfa kaynagindan regex
    src = driver.page_source
    for pat in [r'"hotelId"\s*[:\s=]+["\s]*(\d+)', r'hotelId["\']?\s*[:=]\s*["\']?(\d+)',
                r'hotelId=(\d+)', r'data-hotel-id=["\'](\d+)["\']']:
        m = re.search(pat, src)
        if m:
            return m.group(1)
    return ""


def _find_product_loc(driver):
    loc = driver.execute_script(
        "try{var i=document.querySelector('input[name=productLoc]');return i?i.value:null;}catch(e){return null;}")
    if loc:
        return loc
    m = re.search(r'productLoc["\']?\s*[:=]\s*["\']([^"\']+)["\']', driver.page_source)
    return m.group(1).strip() if m else ""


def _fmt_date_dot(iso):
    """'2026-07-08' -> '8.7.2026'"""
    try:
        y, m, d = iso.split("-")
        return f"{int(d)}.{int(m)}.{int(y)}"
    except Exception:
        return iso


def _fmt_daterange(giris_iso, cikis_iso):
    """-> '08/07/2026 - 13/07/2026'"""
    try:
        gy, gm, gd = giris_iso.split("-")
        cy, cm, cd = cikis_iso.split("-")
        return f"{gd}/{gm}/{gy} - {cd}/{cm}/{cy}"
    except Exception:
        return ""
