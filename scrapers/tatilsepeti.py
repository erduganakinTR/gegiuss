"""Tatilsepeti scraper.

Onceden "Cloudflare yok, tam Selenium gerekli" sanilirdi. Bu oturumda
yeniden test edildi: sayfada GERCEKTEN bir Cloudflare "challenge-platform"
script'i var, AMA fiyat verisi plain requests.Session ile (GET+POST) sorunsuz
cekilebiliyor — WAF/challenge asilmiyor cunku tetiklenmiyor. Sayfanin kendi
JS'i, ayni URL'e ('ara' parametresiyle) bir POST atip {"roomList": "<html>"}
donen bir JSON aliyor; bu fragment AYNEN eski Selenium page_source'unda
kullanilan regex ile parse edilebiliyor (_match_room_price DEGISTIRILMEDI,
5/5 otelde eski koda BIREBIR AYNI sonuc verdi).

ONEMLI SINIRLAMA: es zamanlilik testinde (20 otel/20 worker) hicbir sorun
yokken, SURDURULEN yukte (80-100 otel, hem 20-worker patlama hem 4/sn
paced) agir 429 orani (%50-60) goruldu — ETS/Jolly'deki gibi kisa testler
yaniltici. Tam guvenli esik bu oturumda netlestirilemedi (zaman kisiti),
bu yuzden TEDBIRLI bir sabit hiz sinirlayici (asagida) kullanilir; ETS'in
olculmus 4/sn'sinden cok daha yavas (1/sn) — gelecekte daha dikkatli
bir esik-bulma calismasi ile yukseltilebilir. 45 otelde (20 worker, 1/sn
global pace) 0 adet 429 — 40 ok, 3 gercek no_availability, 2 gercek 404
(eski .aspx formatinda kalmis linkler, taramayla ilgisiz veri sorunu).

DENENDI VE BASARISIZ OLDU: "GET sadece Cloudflare cerezi icin, otel'e ozgu
degil, thread basina bir kez yeter" varsayimi test edildi — YANLIS cikti.
GET'in query string'indeki 'ara=' parametresi sunucu tarafinda ARAMA
BAGLAMINI (tarih/kisi sayisi) o oteldeki oturuma kaydediyor; bu adim
atlanip sadece POST atilinca sunucu "musait degil"/"baska tarih sec"
kartlari donduruyor — roomList DOLU gorunuyor (regex hata vermiyor) ama
GERCEK FIYAT YOK, sessizce yanlis 'no_availability' sonucu uretiyor. Bu
yuzden GET+POST cifti HER OTEL icin ZORUNLU, kod bu haliyle BIRAKILDI.

URL formati:
  /{slug}?ara=oda:<yetiskin>[,<yas1>,<yas2>...];tarih:<gg.aa.yyyy>,<gg.aa.yyyy>
Odalar <h3> basliginda, satis fiyati 'all-prices__discount' sinifinda
(ustu cizili 'default' fiyat degil, kosullu 'Axess sepet' fiyati da degil).
"""
import re, time, json, threading, html as _html
from urllib.parse import urlparse, urlunparse
import requests
from .base import room_score, scrape_result, MATCH_THRESHOLD, redirected_away

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Guvenli esik bu oturumda tam netlesmedi (bkz. yukaridaki not) — 1 istek/sn
# tedbirli bir baslangic degeri, ETS/Jolly'deki _pace() ile ayni desen.
_MIN_INTERVAL = 1.0
_last_request_at = [0.0]
_pace_lock = threading.Lock()


def _pace():
    with _pace_lock:
        wait = _last_request_at[0] + _MIN_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_request_at[0] = time.time()


_thread_local = threading.local()


def _get_session():
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": _UA})
        _thread_local.session = s
    return s


def get_price(url, giris, cikis, yetiskin, cocuk, cocuk_yaslari, oda_tipi, reuse_driver=False):
    """giris/cikis: 'YYYY-MM-DD'. Tatilsepeti otelinden guncel oda fiyatini dondurur.
    reuse_driver: thread'e ait requests.Session'in yeniden kullanilip
    kullanilmayacagini belirler (eski Selenium API'siyle uyumluluk icin isim korunuyor)."""
    session = _get_session() if reuse_driver else requests.Session()
    if not reuse_driver:
        session.headers.update({"User-Agent": _UA})
    try:
        ara = _build_ara(giris, cikis, yetiskin, cocuk, cocuk_yaslari)
        target = _build_url(url, ara)

        _pace()
        r0 = session.get(target, timeout=20)
        if redirected_away(url, r0.url):
            print(f"[Tatilsepeti] Sayfa yonlendirildi ({r0.url}) — link bozuk/otel kaldirilmis olabilir, GUVENSIZ.")
            return scrape_result(status="error")
        if r0.status_code != 200:
            print(f"[Tatilsepeti] HTTP {r0.status_code} (sayfa)")
            return scrape_result(status="error")

        _pace()
        r1 = session.post(url, data={"ara": ara},
                           headers={"X-Requested-With": "XMLHttpRequest"}, timeout=20)
        if r1.status_code != 200:
            print(f"[Tatilsepeti] HTTP {r1.status_code} (fiyat)")
            return scrape_result(status="error")

        try:
            data = json.loads(r1.text)
        except Exception:
            print("[Tatilsepeti] JSON parse hatasi")
            return scrape_result(status="error")
        if "roomList" not in data:
            print("[Tatilsepeti] roomList alani yok — link bozuk/kategori sayfasina dusmus olabilir, GUVENSIZ.")
            return scrape_result(status="error")

        return _match_room_price(data["roomList"], oda_tipi)
    except Exception as e:
        print(f"[Tatilsepeti] {e}")
        return scrape_result(status="error")
    finally:
        if not reuse_driver:
            session.close()


def _build_ara(giris, cikis, yetiskin, cocuk, cocuk_yaslari):
    # oda:<yetiskin>[-<yas1>-<yas2>...]  (cocuk yaslari TIRE ile ayrilir;
    # virgul 'birden fazla oda' anlamina gelir, kullanma)
    oda = str(int(yetiskin or 2))
    if cocuk and int(cocuk) > 0 and cocuk_yaslari:
        for yas in str(cocuk_yaslari).split(","):
            yas = yas.strip()
            if yas.isdigit():
                oda += f"-{yas}"
    return f"oda:{oda};tarih:{_fmt_date(giris)},{_fmt_date(cikis)}"


def _build_url(url, ara):
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", f"ara={ara}", ""))


def _match_room_price(html, oda_tipi):
    # Oda basliklari: SINIFSIZ <h3> (bolum basliklari <h3 class="detail-title">,
    # oda adlari ise sade <h3>). 'Family Triplex','Junior Royal Suit' gibi
    # anahtar kelimesiz odalar da boylece yakalanir.
    names = [(m.start(), re.sub(r'\s+', ' ', _html.unescape(m.group(1)).strip()))
             for m in re.finditer(r'<h3>\s*([^<]{4,60})</h3>', html)]
    # satis fiyati: all-prices__discount
    prices = [(m.start(), _to_float(m.group(1)))
              for m in re.finditer(r'all-prices__discount[^"]*"[^>]*>\s*([\d.]+)', html)]

    all_names = [n for _, n in names]
    if not all_names:
        return scrape_result(status="no_availability")

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
            print(f"[Tatilsepeti] Oda tipi bulunamadi: '{oda_tipi}' (skor {best_score:.2f}). Mevcut: {all_names}")
            return scrape_result(status="no_room", rooms=all_names)
        price = room_prices.get(best_name)
        if price is None:
            print(f"[Tatilsepeti] '{best_name}' bu tarihte musait degil.")
            return scrape_result(status="no_availability")
        return scrape_result(price=price)

    if not room_prices:
        return scrape_result(status="no_availability")
    best_name = min(room_prices, key=room_prices.get)
    return scrape_result(price=room_prices[best_name], oda_adi=best_name)


def _to_float(s):
    """'138.000' -> 138000.0 (Turkce binlik ayraci nokta)"""
    try:
        v = float(s.replace(".", ""))
        return v if v > 500 else None
    except Exception:
        return None


def _fmt_date(iso):
    """'2026-08-17' -> '17.08.2026'"""
    try:
        y, m, d = iso.split("-")
        return f"{d}.{m}.{y}"
    except Exception:
        return iso
