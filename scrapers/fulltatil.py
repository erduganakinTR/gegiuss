"""Fulltatil scraper — otellerin kendi rezervasyon motoru (fulltatil.com platformu).

Cloudflare yok. Sayfada calentim datepicker + arama formu var. Musterinin
tarih/kisi bilgisiyle formu doldurup 'Ara'ya basinca yeni SearchResults sayfasi
(yeni token) uretiliyor; odalar 'fs-18 fw-semibold' div'inde, satis fiyati
'new-price' sinifinda (TRY). Her *.fulltatil.com oteli icin calisir.
"""
import re, time, html as _html
from .base import get_driver, room_score, scrape_result, MATCH_THRESHOLD

# Tarih: calentim config'inden set edilir (input.value yetmiyor).
# Misafir: +/- stepper butonlariyla ayarlanir (input.value handler'i tetiklemiyor).
_SET_SEARCH_JS = r"""
var a = arguments[0];
// --- Tarihler (calentim) ---
try {
    var c = window.jQuery ? jQuery('#HotelCheckin').data('calentim') : null;
    if (c && c.config && window.moment) {
        c.config.startDate = moment(a.ci, 'DD-MM-YYYY');
        c.config.endDate   = moment(a.co, 'DD-MM-YYYY');
        if (c.drawCalendars) try { c.drawCalendars(); } catch(e){}
    }
} catch(e) {}
var ci=document.getElementById('HotelCheckin'); if(ci){ci.value=a.ci;}
var co=document.getElementById('HotelCheckout'); if(co){co.value=a.co;}
document.querySelectorAll('input[name="checkinDate"]').forEach(function(e){e.value=a.ci;});
document.querySelectorAll('input[name="checkoutDate"]').forEach(function(e){e.value=a.co;});
// --- Misafir sayilari (stepper) ---
var t=document.querySelector('.room-guest,[class*=room-guest]'); if(t) t.click();
function stepTo(name, target){
    var inp=document.querySelector('#guestDropdown input[name="'+name+'"]')||document.querySelector('input[name="'+name+'"]');
    if(!inp) return;
    var w=inp.closest('.qty')||inp.parentElement;
    var inc=w.querySelector('.increase'), dec=w.querySelector('.decrease');
    var cur=parseInt(inp.value)||0, g=0;
    while(cur<target && inc && g<15){ inc.click(); g++; cur=parseInt(inp.value); }
    while(cur>target && dec && g<15){ dec.click(); g++; cur=parseInt(inp.value); }
}
stepTo('Adult', a.adult);
stepTo('Child', a.child);
"""

# Cocuk yaslari: Child stepper'i artirinca beliren yas select'lerini doldur.
_SET_CHILD_AGES_JS = r"""
var ages = arguments[0];
var sels = Array.from(document.querySelectorAll('#guestDropdown select')).filter(function(s){
    return /age|yas|child/i.test((s.name||'')+(s.id||'')+(s.className||''));
});
if(!sels.length){ // yedek: guest dropdown icindeki tum select'ler
    sels = Array.from(document.querySelectorAll('#guestDropdown select'));
}
for(var i=0;i<sels.length && i<ages.length;i++){
    sels[i].value=String(ages[i]);
    sels[i].dispatchEvent(new Event('change',{bubbles:true}));
}
return sels.length;
"""

_CLICK_ARA_JS = r"""
var b = Array.from(document.querySelectorAll('button,a,input[type=submit]')).filter(function(e){
    return /^ara$/i.test((e.textContent||e.value||'').trim()) && e.offsetParent!==null; })[0];
if(b){ b.click(); return true; }
var f = document.querySelector('form[action*="Reservation/Search"]');
if(f){ f.submit(); return true; }
return false;
"""


def get_price(url, giris, cikis, yetiskin, cocuk, cocuk_yaslari, oda_tipi):
    """giris/cikis: 'YYYY-MM-DD'. Fulltatil otelinden guncel oda fiyatini dondurur."""
    driver = get_driver()
    try:
        driver.get(url)
        time.sleep(10)
        child = int(cocuk or 0)
        driver.execute_script(_SET_SEARCH_JS, {
            "ci": _fmt_date(giris), "co": _fmt_date(cikis),
            "adult": int(yetiskin or 2), "child": child,
        })
        time.sleep(1.2)
        # Cocuk varsa beliren yas select'lerini doldur
        if child > 0 and cocuk_yaslari:
            ages = [int(y.strip()) for y in str(cocuk_yaslari).split(",") if y.strip().isdigit()]
            if ages:
                driver.execute_script(_SET_CHILD_AGES_JS, ages)
                time.sleep(0.5)
        ok = driver.execute_script(_CLICK_ARA_JS)
        if not ok:
            return scrape_result(status="error")
        time.sleep(11)
        return _match_room_price(driver.page_source, oda_tipi)
    except Exception as e:
        print(f"[Fulltatil] {e}")
        return scrape_result(status="error")
    finally:
        driver.quit()


def _match_room_price(html, oda_tipi):
    # Oda adi: <div class="fs-18 fw-semibold ...">Oda Adi</div>
    names = [(m.start(), re.sub(r'\s+', ' ', _html.unescape(m.group(1)).strip()))
             for m in re.finditer(r'fs-18 fw-semibold[^"]*"[^>]*>\s*([^<]{4,50})', html)]
    # Sadece oda gibi gorunenler (fs-18 fw-semibold baska yerde de olabilir)
    names = [(p, n) for p, n in names if re.search(r'oda|suit|manzara|room', n, re.I)]
    # Satis fiyati: new-price (TRY)
    prices = [(m.start(), _to_float(m.group(1)))
              for m in re.finditer(r'new-price[^>]*>\s*([\d.,]+)', html)]

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
            print(f"[Fulltatil] Oda tipi bulunamadi: '{oda_tipi}' (skor {best_score:.2f}). Mevcut: {all_names}")
            return scrape_result(status="no_room", rooms=all_names)
        price = room_prices.get(best_name)
        if price is None:
            print(f"[Fulltatil] '{best_name}' bu tarihte musait degil.")
            return scrape_result(status="no_availability")
        return scrape_result(price=price)

    if not room_prices:
        return scrape_result(status="no_availability")
    return scrape_result(price=min(room_prices.values()))


def _to_float(s):
    """Turkce '57.000,00' -> 57000.0"""
    try:
        v = float(s.replace(".", "").replace(",", "."))
        return v if v > 500 else None
    except Exception:
        return None


def _fmt_date(iso):
    """'2026-08-20' -> '20-08-2026'"""
    try:
        y, m, d = iso.split("-")
        return f"{d}-{m}-{y}"
    except Exception:
        return iso
