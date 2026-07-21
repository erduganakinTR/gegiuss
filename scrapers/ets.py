import time
import threading
import requests
from urllib.parse import urlparse
from .base import room_score, scrape_result, MATCH_THRESHOLD

API_PATH = "https://www.etstur.com/services/api/room"
HOTEL_DETAIL_API = "https://www.etstur.com/services/api/hotel/detail/"

# Onceden "cimplak requests WAF tarafindan bloklaniyor" sanilip TUM istekler
# tarayicinin kendi fetch()'iyle (Selenium icinden) atiliyordu. Bu iddia bu
# oturumda YENIDEN test edildi ve YANLIS cikti — plain requests.Session ile
# hem hotel-detail hem fiyat API'si sorunsuz 200 donuyor, hicbir WAF/TLS
# engeli yok. Simdi ETS de Jolly gibi TAMAMEN tarayicisiz calisiyor.
#
# ETS Cloudflare arkasinda ve gercek sinir SESSION/COOKIE PAYLASIMINA bagli
# gibi gorunuyor: tek bir Selenium tarayicisinin (tek cookie/session) art
# arda istekleri es zamanli atmasi neredeyse aninda 429 tetikliyordu (N=2
# bile sürdürülen yukte %60 basari). AYRI requests.Session (ayri cerez)
# kullanildiginda ise durum COK farkli: N=12 es zamanli anlik patlamada 0
# adet 429; sadece YUKSEK SURDURULEN HIZDA (saniyede ~35+ istek) 429 baslıyor.
# Olculen esikler: 3-5 istek/sn TEMIZ (0/76, 0/100), 6/sn'de cizik basliyor
# (6/100), 8/sn'de belirgin (16/100). Guvenlik payi icin 4/sn PACED hiz
# sinirlayici kullanilir (kilit DEGIL — birden fazla istek ES ZAMANLI ucabilir,
# sadece YENI istek BASLATMA hizi sinirlanir).
_RATE_LIMIT_BACKOFF = 3.0
_MIN_INTERVAL = 0.25  # 4 istek/sn
_last_request_at = [0.0]
_pace_lock = threading.Lock()


def _pace():
    with _pace_lock:
        wait = _last_request_at[0] + _MIN_INTERVAL - time.time()
        if wait > 0:
            time.sleep(wait)
        _last_request_at[0] = time.time()


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# hotelId, bir otel icin tum donemlerde AYNI (checkIn/checkOut'a bagli degil).
# Slug -> hotelId process ici cache'lenir (ayni otelin 10 donemi taranirken
# 10 kez ayni sorgu atilmasin diye).
_HOTEL_ID_CACHE = {}

_thread_local = threading.local()


def _get_session():
    s = getattr(_thread_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": _UA, "Accept": "application/json, text/plain, */*"})
        _thread_local.session = s
    return s


def get_price(url, giris, cikis, yetiskin, cocuk, cocuk_yaslari, oda_tipi, reuse_driver=False):
    """giris/cikis: 'YYYY-MM-DD'. Belirtilen oda tipinin guncel fiyatini dondurur.
    reuse_driver: thread'e ait requests.Session'in yeniden kullanilip
    kullanilmayacagini belirler (eski Selenium API'siyle uyumluluk icin isim korunuyor)."""
    session = _get_session() if reuse_driver else requests.Session()
    if not reuse_driver:
        session.headers.update({"User-Agent": _UA, "Accept": "application/json, text/plain, */*"})
    try:
        slug = urlparse(url).path.strip("/").split("/")[-1]
        hotel_id = _HOTEL_ID_CACHE.get(slug)
        if hotel_id is None:
            hotel_id = _fetch_hotel_id(session, slug)
            if hotel_id:
                _HOTEL_ID_CACHE[slug] = hotel_id
        if not hotel_id:
            print(f"[ETS] hotelId bulunamadi ({slug}) — link bozuk/otel kaldirilmis olabilir.")
            return scrape_result(status="error")

        child_count = int(cocuk or 0)
        child_ages = []
        if child_count > 0 and cocuk_yaslari:
            child_ages = [int(y.strip()) for y in str(cocuk_yaslari).split(",")
                          if y.strip().isdigit()]

        payload = {
            "hotelId": hotel_id,
            "checkIn": giris,
            "checkOut": cikis,
            "room": {
                "adultCount": int(yetiskin or 2),
                "childCount": child_count,
                "childAges": child_ages,
                "infantCount": 0,
            },
        }

        _pace()
        r = session.post(API_PATH, json=payload,
                          headers={"Content-Type": "application/json"}, timeout=15)
        if r.status_code == 429:
            print("[ETS] fiyat istegi 429 (hiz siniri) — kisa bekleme sonrasi tek tekrar deneniyor...")
            time.sleep(_RATE_LIMIT_BACKOFF)
            _pace()
            r = session.post(API_PATH, json=payload,
                              headers={"Content-Type": "application/json"}, timeout=15)
        if r.status_code != 200:
            print(f"[ETS] fiyat istegi hatasi (HTTP {r.status_code})")
            return scrape_result(status="error")

        try:
            data = r.json()
        except Exception:
            print("[ETS] JSON parse hatasi")
            return scrape_result(status="error")

        result = data.get("result") or {}
        # noDates=True => istenen tarihlerde musaitlik yok, gelen fiyatlar
        # alternatif tarihler icin. Yanlis fiyat vermemek icin no_availability don.
        if result.get("noDates") is True:
            print("[ETS] Bu tarihlerde musaitlik yok (noDates).")
            return scrape_result(status="no_availability")

        rooms = result.get("rooms") or []
        if not rooms:
            print("[ETS] Bu tarih/kisi icin oda yok.")
            return scrape_result(status="no_availability")

        nights = _nights(giris, cikis)
        return _match_room_price(rooms, oda_tipi, nights)

    except Exception as e:
        print(f"[ETS] {e}")
        return scrape_result(status="error")
    finally:
        if not reuse_driver:
            session.close()


def _room_total(room, nights):
    """Bir odanin en dusuk toplam (konaklama) fiyatini dondur.

    SADECE musait (availability.type == 'AVAILABLE') board'lari sayar;
    stop-sale / talep-uzerine board'lar fiyat tasisa da atlanir ki
    musait olmayan tarihte yanlis fiyat verilmesin.
    """
    totals = []
    for sb in room.get("subBoards") or []:
        av = sb.get("availability") or {}
        av_type = av.get("type")
        if av_type is not None and av_type != "AVAILABLE":
            continue
        pr = sb.get("price") or {}
        val = pr.get("discountedPrice") or pr.get("amount")
        if val and val > 500:
            totals.append(float(val))
    return min(totals) if totals else None


def _match_room_price(rooms, oda_tipi, nights):
    """Once ISME gore en iyi eslesen odayi bul, SONRA o odanin musait olup
    olmadigina bak. Boylece kullanicinin musait OLMAYAN odasi yerine benzer
    ama farkli bir musait oda yanlislikla eslesip fiyat vermez.

    _room_total musait board yoksa None doner (fiyat tasisa da NOT_AVAILABLE atlanir).
    """
    # (isim, musait_fiyat_veya_None) - tum odalar
    all_rooms = [(r.get("roomName", ""), _room_total(r, nights))
                 for r in rooms if r.get("roomName")]
    if not all_rooms:
        return scrape_result(status="no_availability")

    if oda_tipi:
        best_name, best_price = max(all_rooms, key=lambda x: room_score(x[0], oda_tipi))
        best_score = room_score(best_name, oda_tipi)
        if best_score < MATCH_THRESHOLD:
            # Kullanicinin odasi satis listesinde yok
            print(f"[ETS] Oda tipi bulunamadi: '{oda_tipi}' (en iyi skor {best_score:.2f}). "
                  f"Mevcut: {[n for n, _ in all_rooms]}")
            return scrape_result(status="no_room", rooms=[n for n, _ in all_rooms])
        if best_price is None:
            # Oda listede var ama bu tarih/kisi icin musait degil
            print(f"[ETS] '{best_name}' bu tarihte musait degil.")
            return scrape_result(status="no_availability")
        return scrape_result(price=best_price)

    # oda_tipi yoksa: en ucuz MUSAIT oda
    avail = [(n, p) for n, p in all_rooms if p]
    if not avail:
        return scrape_result(status="no_availability")
    best_name, best_price = min(avail, key=lambda x: x[1])
    return scrape_result(price=best_price, oda_adi=best_name)


def _fetch_hotel_id(session, slug):
    """hotelId'yi tam sayfa yuklemeden, dogrudan hafif bir API cagrisiyla
    alir. Slug gecersiz/otel kaldirilmis ise API net bir hata doner
    (sessizce yanlis otele dusme riski yok — /services/api/hotel/detail
    slug'i tam eslestirir, bulamazsa success=false doner)."""
    _pace()
    r = session.get(HOTEL_DETAIL_API + slug, timeout=15)
    if r.status_code == 429:
        print("[ETS] hotelId istegi 429 (hiz siniri) — kisa bekleme sonrasi tek tekrar deneniyor...")
        time.sleep(_RATE_LIMIT_BACKOFF)
        _pace()
        r = session.get(HOTEL_DETAIL_API + slug, timeout=15)
    if r.status_code != 200:
        return ""
    try:
        data = r.json()
    except Exception:
        return ""
    if not data.get("success"):
        return ""
    return str((data.get("result") or {}).get("hotelId") or "")


def _nights(giris, cikis):
    try:
        from datetime import date
        gy, gm, gd = map(int, giris.split("-"))
        cy, cm, cd = map(int, cikis.split("-"))
        return (date(cy, cm, cd) - date(gy, gm, gd)).days
    except Exception:
        return 0
