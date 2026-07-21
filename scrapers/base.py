from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from urllib.parse import urlparse
import re, sys, subprocess, threading

# Windows'ta chromedriver'in konsol penceresini gizlemek icin
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

def _new_driver():
    opts = Options()
    # 'eager': sadece DOM hazir olunca (DOMContentLoaded) devam et, resim/font/
    # reklam gibi alt kaynaklarin tam yuklenmesini BEKLEME. Fiyat/hotelId gibi
    # bilgiler zaten sunucu tarafinda render edilen HTML'de oldugu icin bu
    # yeterli — sayfa basina saniyeler mertebesinde kazanc saglar.
    opts.page_load_strategy = "eager"
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    # Pencereyi ekran disina konumlandir ki headless baslarken beyaz cakma olmasin
    opts.add_argument("--window-position=-32000,-32000")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    service = Service(ChromeDriverManager().install())
    # chromedriver.exe konsol penceresi flash'ini engelle
    if _NO_WINDOW:
        service.creation_flags = _NO_WINDOW
    return webdriver.Chrome(service=service, options=opts)


# --- HAVUZ (toplu tarama icin) ---
# Kisisel rezervasyon kontrolu (checker.py) her cagride yeni tarayici acip
# kapatmaya devam eder (reuse=False, varsayilan) — davranisi degismedi.
# Toplu otel taramasi (hotel_checker.py) reuse=True kullanir: her worker
# thread'i kendi Chrome'unu ThreadPoolExecutor omru boyunca canli tutar,
# boylece binlerce kombinasyonda her seferinde Chrome acip kapatma
# maliyetinden (~1-3sn/kombinasyon) kacinilir. Cok uzun oturumlarda bellek
# birikmesini onlemek icin thread basina _RECYCLE_AFTER navigasyondan
# sonra tarayici otomatik yenilenir.
_RECYCLE_AFTER = 150
_thread_local = threading.local()
_pool_lock = threading.Lock()
_pooled_drivers = []  # tum havuzdaki tarayicilar (kapatma icin)


def get_driver(reuse=False):
    if not reuse:
        return _new_driver()

    nav_count = getattr(_thread_local, "nav_count", 0)
    driver = getattr(_thread_local, "driver", None)
    if driver is not None and nav_count < _RECYCLE_AFTER:
        _thread_local.nav_count = nav_count + 1
        return driver

    if driver is not None:
        _retire_driver(driver)

    driver = _new_driver()
    _thread_local.driver = driver
    _thread_local.nav_count = 1
    with _pool_lock:
        _pooled_drivers.append(driver)
    return driver


def _retire_driver(driver):
    try:
        driver.quit()
    except Exception:
        pass
    with _pool_lock:
        if driver in _pooled_drivers:
            _pooled_drivers.remove(driver)


def quit_all_pooled_drivers():
    """Tarama bitince (basarili/iptal/hata farketmeksizin) havuzdaki
    TUM tarayicilari kapatir. hotel_checker._run_scan'in finally'inde cagrilir."""
    with _pool_lock:
        drivers = list(_pooled_drivers)
        _pooled_drivers.clear()
    for d in drivers:
        try:
            d.quit()
        except Exception:
            pass
    # Not: _thread_local sadece BU cagriyi yapan thread'i etkiler; worker
    # thread'leri ThreadPoolExecutor kapanirken zaten sonlandirilir, o
    # yuzden onlarin thread-local referanslarini ayrica temizlemeye gerek yok.

def safe_float(text):
    if not text:
        return None
    cleaned = re.sub(r'[^\d,.]', '', str(text).replace('\xa0', ''))
    # Türkçe format: 1.234,56 → 1234.56
    if ',' in cleaned and '.' in cleaned:
        cleaned = cleaned.replace('.', '').replace(',', '.')
    elif ',' in cleaned:
        cleaned = cleaned.replace(',', '.')
    try:
        val = float(cleaned)
        return val if val > 10 else None
    except:
        return None

def redirected_away(original_url, current_url):
    """Site, gecersiz/kaldirilmis otel sayfasini genel bir listeleme/ana
    sayfaya yonlendirdiyse True doner. Boyle bir durumda sayfadan cekilen
    HERHANGI bir veri (hotelId, fiyat, oda adi) BASKA bir otele ait
    olabilir ve GUVENILMEZ — cagiran scraper bu durumda 'error' donmeli,
    yanlis otelin fiyatini dogru otel gibi RAPORLAMAMALI.

    Kontrol: orijinal URL'nin path'indeki ilk parca (slug), yonlendirme
    sonrasi gelinen URL'nin path'inde hala geciyor mu? Gecmiyorsa
    yonlendirilmis demektir (orn. '/Vogue-Hotel-Bodrum' -> site kok
    domain'ine ya da '/bodrum-otelleri' listelemesine dusmus olabilir)."""
    orig_slug = urlparse(original_url).path.strip("/").split("/")[0].lower()
    if not orig_slug:
        return False
    cur_path = urlparse(current_url).path.strip("/").lower()
    return orig_slug not in cur_path


def extract_prices_from_text(text):
    """Sayfa metninden TL fiyatlarını çıkar."""
    pattern = r'(\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})?)\s*(?:TL|₺)'
    matches = re.findall(pattern, text)
    prices = []
    for m in matches:
        p = safe_float(m)
        if p and 500 < p < 50_000_000:
            prices.append(p)
    return sorted(set(prices))

# Oda adi eslesme esigi. Yuksek tutulur cunku 'Ana Bina' vs 'Park Bina' ya da
# 'Deniz' vs 'Kara' gibi TEK kelime farki farkli odalari gosterir; bunlar
# ~0.71-0.83 skor alir ve KABUL EDILMEMELIDIR (musteriye yanlis fiyat gitmesin).
# Musterinin gercek odasi birebir yazildiginda zaten 1.0 skor alir.
MATCH_THRESHOLD = 0.85


def scrape_result(price=None, status="ok", rooms=None, oda_adi=None):
    """Scraper donus tipi.

    status: 'ok'  -> price dolu
            'no_room'         -> otel musait ama istenen oda tipi yok
            'no_availability' -> o tarih/kisi icin hic musaitlik yok
            'error'           -> teknik hata (fetch/parse)
            'unsupported'     -> bu acente icin scraper henuz yok
    rooms: no_room durumunda mevcut oda adlari (kullaniciya gostermek icin)
    oda_adi: oda_tipi verilmeden (en ucuz oda modu) cekildiginde, o fiyata
             karsilik gelen odanin adi
    """
    return {"price": price, "status": status, "rooms": rooms or [], "oda_adi": oda_adi}


# Turkce karakterleri ascii karsiligina cevir. ONEMLI: bunu yapmazsak 'ı,ş,ç,ğ,ü,ö'
# regex ile silinir ve 'Manzaralı'->'manzaral' olurken ascii 'Manzarali'->'manzarali'
# ile eslesmez. Bu, tum oda eslestirmesini sessizce bozan bir hataydi.
_TR_ASCII = str.maketrans("ıİşŞçÇğĞüÜöÖ", "iissccgguuoo")

def normalize_room(name):
    """Oda adını karşılaştırma için normalize et (Türkçe->ascii, küçük harf, sade)."""
    if not name:
        return ""
    import unicodedata
    s = name.translate(_TR_ASCII)
    nfkd = unicodedata.normalize('NFKD', s.lower())
    ascii_str = ''.join(c for c in nfkd if not unicodedata.combining(c))
    # Noktalama/parantez vb. bosluga cevir ki '(2+1)' gibi ekler ayri token olsun
    ascii_str = re.sub(r'[^a-z0-9]+', ' ', ascii_str)
    return re.sub(r'\s+', ' ', ascii_str).strip()

def room_match(scraped_name, target_name, threshold=0.6):
    """İki oda adının benzerliğini kontrol et (bool)."""
    return room_score(scraped_name, target_name) >= threshold


def room_score(scraped_name, target_name):
    """İki oda adının benzerlik skorunu döndür (0-1, Jaccard).

    Tam eşleşen oda en yüksek skoru alır; böylece
    'Club Aile Odası Kara Manzaralı' aranırken
    'Club Kara Manzaralı' gibi kısmi eşleşmeler geride kalır.
    """
    s = normalize_room(scraped_name)
    t = normalize_room(target_name)
    if not s or not t:
        return 0.0
    if s == t:
        return 1.0
    s_words = set(s.split())
    t_words = set(t.split())
    if not s_words or not t_words:
        return 0.0
    inter = len(s_words & t_words)
    union = len(s_words | t_words)
    return inter / union if union else 0.0
