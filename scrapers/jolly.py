import re, time, threading, requests
import html as _html
from urllib.parse import urlparse
from .base import room_score, scrape_result, MATCH_THRESHOLD, redirected_away

# Onceden Jolly icin temiz bir API bulunamamis, tam sayfa Selenium navigasyonu +
# XHR sniffing kullaniliyordu (~5-9sn/istek). Sayfanin kendi JS paketi
# (hotel-detail.min.js) incelenince asil cagrinin gercek payload'i ortaya
# cikti: sayfa yuklenirken /hotel/GetReservationCompletePartial'a
# {id, startDate, endDate, rooms, originType:"Zone", hotelType:"Domestic",
# searchType:"Product", customerTrackId:"00000000-...", ...} govdesiyle POST
# atiliyor; 'id' de HTML'de <input name="hotelId" value="..."> olarak DUZ
# METIN halinde geliyor (JS calistirmaya bile gerek yok). ETS'deki gibi artik
# SADECE bu iki HTTP cagrisi (GET sayfa -> hotelId, POST fiyat) yapiliyor,
# tam tarayici GEREKMIYOR (~0.5-2sn/istek).
#
# ONEMLI SINIRLAMA: Jolly'nin es zamanlilik/hiz siniri IP BAZLI DEGIL,
# ZAMAN PENCERELI bir hiz siniri gibi davraniyor — hem yuksek concurrency
# (N=80) hem de sürdürülen orta concurrency (N=8, 80 istek uzerinden) belli
# bir noktadan sonra TUM istekleri (yenileri dahil) aninda basarisiz kilan
# bir bloklama tetikliyor (~30-60sn sonra kendiliginden acikiyor). Bu yuzden
# ONCEKI Selenium-tabanli kodun SERI kilidi (asagida) KORUNUYOR — sadece
# istek YONTEMI hafifletildi, es zamanlilik varsayimi DEGISTIRILMEDI. Guvenli
# bir hiz siniri (ornegin sabit bir bekleme ile N istek/sn) ileride ayrica
# olculup eklenebilir; simdilik seri + hafif istek = hem hizli hem guvenli.
_JOLLY_LOCK = threading.Lock()

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_HOTEL_ID_RE = re.compile(r"""name=['"]hotelId['"]\s+value=['"](\d+)['"]""")

# hotelId bir otel icin tum donemlerde AYNI — ETS'deki gibi process ici cache'lenir,
# ayni otelin 10 donemi taranirken 10 kez sayfa GET'i atilmasin diye.
_HOTEL_ID_CACHE = {}

_thread_local = threading.local()

# Hafif istekler eskisinden (Selenium, ~5-9sn/istek) COK daha hizli ates
# edebiliyor; olcumlerde suslu (concurrency degil, TOPLAM istek/zaman) yuksek
# hiz bir noktadan sonra TUM istekleri (yenileri dahil) topluca basarisiz
# kilan bir bloklama tetikliyordu. Bu yuzden SERI kilit icinde bile istekler
# arasina bilincli bir taban bekleme konur — 5 saatlik bir taramada binlerce
# istek atilacagi icin gorulen bozulma esiginin (~1-2 istek/sn) belirgin
# altinda, guvenlik payi birakilarak seciliyor.
_MIN_INTERVAL = 0.8
_last_request_at = [0.0]
_pace_lock = threading.Lock()


def _pace():
    with _pace_lock:
        wait = _last_request_at[0] + _MIN_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_request_at[0] = time.time()


def _get_session():
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": _UA})
        _thread_local.session = s
    return s


def get_price(url, giris, cikis, yetiskin, cocuk, cocuk_yaslari, oda_tipi, reuse_driver=False):
    """giris/cikis: 'YYYY-MM-DD'. Belirtilen oda tipinin guncel fiyatini dondurur.
    reuse_driver: thread'e ait requests.Session'in yeniden kullanilip
    kullanilmayacagini belirler (baglanti havuzu icin — artik gercek bir
    tarayici gerekmedigi icin isim eski API ile uyumluluk amacli korunuyor)."""
    with _JOLLY_LOCK:
        for attempt, bekleme in enumerate((0, 6, 15)):
            if bekleme:
                # ONEMLI (2026-07-21, gercek tarama analizi): "hotelId
                # bulunamadi" hatasi veren linkler TEK TEK test edildiginde
                # SORUNSUZ calisiyordu — yani cogu 'bozuk link' degil, sitenin
                # surdurulen istek hacminde tetikledigi GECICI (~30-60sn) bir
                # bloklama. Onceki retry SADECE _pace()'in ~0.8sn'lik bekleme
                # payi kadar sonra tekrar deniyordu — bu, blok penceresinin
                # COK altinda kaliyor, retry pratikte hicbir sey kazandirmiyordu.
                # Simdi blok suresini asacak kadar (12sn, sonra 25sn) bekleyip
                # tekrar deniyoruz.
                time.sleep(bekleme)
                print(f"[Jolly] hata sonrasi {bekleme}sn bekleyip tekrar deneniyor...")
            sonuc = _get_price_once(url, giris, cikis, yetiskin, cocuk, cocuk_yaslari, oda_tipi, reuse_driver)
            if sonuc["status"] != "error" or attempt == 2:
                return sonuc
    return sonuc


def _get_price_once(url, giris, cikis, yetiskin, cocuk, cocuk_yaslari, oda_tipi, reuse_driver):
    session = _get_session() if reuse_driver else requests.Session()
    if not reuse_driver:
        session.headers.update({"User-Agent": _UA})
    try:
        hotel_id = _HOTEL_ID_CACHE.get(url)
        if hotel_id is None:
            _pace()
            r1 = session.get(url, timeout=15)
            if redirected_away(url, r1.url):
                print(f"[Jolly] Sayfa yonlendirildi ({r1.url}) — link bozuk/otel kaldirilmis olabilir, GUVENSIZ.")
                return scrape_result(status="error")
            m = _HOTEL_ID_RE.search(r1.text)
            if not m:
                print("[Jolly] hotelId bulunamadi — link bozuk/otel kaldirilmis olabilir.")
                return scrape_result(status="error")
            hotel_id = m.group(1)
            _HOTEL_ID_CACHE[url] = hotel_id

        rooms = str(int(yetiskin or 2))
        # NOT: sitenin JS'i cocuk yaslarini ayri alanlarla gonderiyor
        # (firstChildAge vb.), burada su an sadece yetiskin sayisi tasiniyor —
        # mevcut kullanim (cocuk=0) icin yeterli, cocuklu arama eklenirse
        # bu alanlar genisletilmeli.
        payload = {
            "id": hotel_id, "startDate": _fmt_date(giris), "endDate": _fmt_date(cikis), "rooms": rooms,
            "originId": "", "originType": "Zone", "originName": "", "hotelType": "Domestic",
            "searchType": "Product", "customerTrackId": "00000000-0000-0000-0000-000000000000",
            "packageSearchType": "", "trivagoReferenceId": "", "neredeKalReferenceId": "",
            "hadsKey": "", "utmSource": "",
        }
        _pace()
        r2 = session.post(
            "https://www.jollytur.com/hotel/GetReservationCompletePartial",
            data=payload, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=15,
        )
        try:
            data = r2.json()
        except Exception:
            print(f"[Jolly] JSON parse hatasi (HTTP {r2.status_code})")
            return scrape_result(status="error")

        if data.get("isAvailableHotel") is False:
            print("[Jolly] otel bu tarih/kisi icin musait degil")
            return scrape_result(status="no_availability")

        html = data.get("html", "")
        if not html:
            return scrape_result(status="no_availability")

        return _match_room_price(html, oda_tipi)

    except Exception as e:
        print(f"[Jolly] {e}")
        return scrape_result(status="error")
    finally:
        if not reuse_driver:
            session.close()


def _match_room_price(html, oda_tipi):
    """Her oda blogu icin indirimli toplam fiyati bul, en yuksek skorlu odayi sec.

    Blok metni (tag'siz) su desende:
      'N Gece Toplam %40 Indirim <eski>,00TL <indirimli> ,00TL Tum Vergiler Dahil'
    Fiyat = 'Tum Vergiler Dahil' oncesindeki indirimli tutar.
    """
    titles = list(re.finditer(r'room-title[^>]*>\s*([^<]{3,60})', html))
    all_names = []          # tum oda basliklari (musait olsun olmasin)
    room_prices = {}        # oda_adi -> fiyat (SADECE fiyatli/musait odalar)
    for i, m in enumerate(titles):
        name = re.sub(r'\s+', ' ', _html.unescape(m.group(1)).strip())
        all_names.append(name)
        start = m.start()
        end = titles[i + 1].start() if i + 1 < len(titles) else start + 8000
        block = _html.unescape(re.sub(r'<[^>]+>', ' ', html[start:end]))
        block = re.sub(r'\s+', ' ', block)
        # 'Tum Vergiler Dahil' oncesindeki fiyat (indirimli toplam)
        pm = re.search(r'([\d][\d.]*)\s*,\d{2}\s*TL\s+\S+\s+Vergiler\s+Dahil', block)
        if not pm:
            # indirim yoksa: 'Gece Toplam' sonrasi ilk toplam fiyat
            pm = re.search(r'Gece Toplam\D*([\d][\d.]*)\s*,\d{2}\s*TL', block)
        if pm:
            price = _to_float(pm.group(1) + ",00")
            if price:
                room_prices.setdefault(name, price)

    if not all_names:
        return scrape_result(status="no_availability")

    if oda_tipi:
        # Once ISME gore tum odalar icinden en iyi eslesen, SONRA fiyati var mi bak
        best_name = max(all_names, key=lambda n: room_score(n, oda_tipi))
        best_score = room_score(best_name, oda_tipi)
        if best_score < MATCH_THRESHOLD:
            print(f"[Jolly] Oda tipi bulunamadi: '{oda_tipi}' (en iyi skor {best_score:.2f}). "
                  f"Mevcut: {all_names}")
            return scrape_result(status="no_room", rooms=all_names)
        price = room_prices.get(best_name)
        if price is None:
            print(f"[Jolly] '{best_name}' bu tarihte musait degil.")
            return scrape_result(status="no_availability")
        return scrape_result(price=price)

    if not room_prices:
        return scrape_result(status="no_availability")
    best_name = min(room_prices, key=room_prices.get)
    return scrape_result(price=room_prices[best_name], oda_adi=best_name)


def _to_float(s):
    """Turkce format '37.400,00' -> 37400.0"""
    try:
        v = float(s.replace(".", "").replace(",", "."))
        return v if v > 500 else None
    except Exception:
        return None


def _fmt_date(iso):
    """'2026-07-09' -> '09.07.2026'"""
    try:
        y, m, d = iso.split("-")
        return f"{d}.{m}.{y}"
    except Exception:
        return iso
