import re, threading, requests
from datetime import date
from urllib.parse import urlparse, urlunparse
from .base import room_score, scrape_result, MATCH_THRESHOLD, redirected_away

# Onceden "fiyatlar sadece client-side hydration ile geliyor, SSR'de yok"
# sanilip tam Selenium tarayicisi kullaniliyordu. Bu iddia bu oturumda
# YENIDEN test edildi (ETS/Tatilbudur/Jolly'de oldugu gibi) ve YANLIS
# cikti — en ucuz odanin fiyati GERCEKTEN sunucu tarafinda render ediliyor
# (styled-components "style__Whole-sc-..." span'i), plain requests.get()
# ile hicbir WAF/Cloudflare engeli olmadan dogrudan okunabiliyor. Mevcut
# _match_room_price regex'i (asagida, DEGISTIRILMEDI) hem eski Selenium
# page_source'unda hem de ham SSR HTML'inde AYNI sekilde calisiyor —
# dogrulandi: 10/10 otelde dogru sonuc, 4/4 otelde eski (Selenium) koduyla
# BIREBIR AYNI fiyat. Es zamanlilik testi de TEMIZ: 100 otel/20 worker'da
# hic 429/bloklanma yok (5.3sn'de tamamlandi) — ETS/Jolly'nin aksine
# Setur icin herhangi bir hiz sinirlayici/kilit GEREKMIYOR.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_thread_local = threading.local()


def _get_session():
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": _UA})
        _thread_local.session = s
    return s


def get_price(url, giris, cikis, yetiskin, cocuk, cocuk_yaslari, oda_tipi, reuse_driver=False):
    """giris/cikis: 'YYYY-MM-DD'. Setur otelinden guncel oda fiyatini dondurur.
    reuse_driver: thread'e ait requests.Session'in yeniden kullanilip
    kullanilmayacagini belirler (eski Selenium API'siyle uyumluluk icin isim korunuyor)."""
    session = _get_session() if reuse_driver else requests.Session()
    if not reuse_driver:
        session.headers.update({"User-Agent": _UA})
    try:
        target = _build_url(url, giris, cikis, yetiskin, cocuk, cocuk_yaslari)
        r = session.get(target, timeout=20)

        if redirected_away(url, r.url):
            print(f"[Setur] Sayfa yonlendirildi ({r.url}) — link bozuk/otel kaldirilmis olabilir, GUVENSIZ.")
            return scrape_result(status="error")
        if r.status_code != 200:
            print(f"[Setur] HTTP {r.status_code}")
            return scrape_result(status="error")

        return _match_room_price(r.text, oda_tipi)

    except Exception as e:
        print(f"[Setur] {e}")
        return scrape_result(status="error")
    finally:
        if not reuse_driver:
            session.close()


def _build_url(url, giris, cikis, yetiskin, cocuk, cocuk_yaslari):
    """Otel path'i + in/out/room query parametreleriyle URL kur."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")

    room = str(int(yetiskin or 2))
    child_count = int(cocuk or 0)
    if child_count > 0 and cocuk_yaslari:
        for yas in str(cocuk_yaslari).split(","):
            yas = yas.strip()
            if yas.isdigit():
                room += f"_{_birthdate_for_age(giris, int(yas))}"

    query = f"in={giris}&out={cikis}&room={room}"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", query, ""))


def _birthdate_for_age(giris_iso, age):
    """Cocugun giris tarihinde verilen yasta olacagi tahmini dogum tarihi."""
    try:
        y, m, d = map(int, giris_iso.split("-"))
        return date(y - age, m, d).isoformat()
    except ValueError:
        # 29 Subat gibi gecersiz kombinasyonlar icin guvenli fallback
        return date(y - age, m, 28).isoformat()
    except Exception:
        return giris_iso


def _match_room_price(html, oda_tipi):
    titles = [(m.start(), re.sub(r'\s+', ' ', m.group(1)).strip())
              for m in re.finditer(r'RoomListCardTitle[^>]*>([^<]+)<', html)]
    prices = [(m.start(), _to_float(m.group(1)))
              for m in re.finditer(r'style__Whole-sc-[a-z0-9]+-\d+[^"]*">([\d.]+)<', html)]

    all_names = [name for _, name in titles]
    if not all_names:
        return scrape_result(status="no_availability")

    # Her odanin fiyat penceresi: kendi basligindan bir sonraki basliga kadar.
    # O pencuredeki ILK fiyat = toplam konaklama fiyati (sonrakiler gecelik kirilim).
    room_prices = {}
    for i, (tpos, name) in enumerate(titles):
        end = titles[i + 1][0] if i + 1 < len(titles) else len(html)
        window_prices = [p for ppos, p in prices if tpos < ppos < end]
        if window_prices and window_prices[0] is not None:
            room_prices.setdefault(name, window_prices[0])

    if oda_tipi:
        best_name = max(all_names, key=lambda n: room_score(n, oda_tipi))
        best_score = room_score(best_name, oda_tipi)
        if best_score < MATCH_THRESHOLD:
            print(f"[Setur] Oda tipi bulunamadi: '{oda_tipi}' (en iyi skor {best_score:.2f}). "
                  f"Mevcut: {all_names}")
            return scrape_result(status="no_room", rooms=all_names)
        price = room_prices.get(best_name)
        if price is None:
            print(f"[Setur] '{best_name}' bu tarihte musait degil.")
            return scrape_result(status="no_availability")
        return scrape_result(price=price)

    if not room_prices:
        return scrape_result(status="no_availability")
    best_name = min(room_prices, key=room_prices.get)
    return scrape_result(price=room_prices[best_name], oda_adi=best_name)


def _to_float(s):
    """'128.750' -> 128750.0 (Turkce binlik ayraci nokta)"""
    try:
        v = float(s.replace(".", ""))
        return v if v > 500 else None
    except Exception:
        return None
