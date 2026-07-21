"""GitHub Actions runner'inda calisir: data/batch.json'daki kombinasyonlardan
kendi shard'ina duseni (index % shard_count == shard) tarar, sonucu
results_<shard>.json olarak yazar (workflow bunu artifact olarak yukler).

Runner 2 vCPU oldugu icin agir (Selenium/Playwright) acenteler kucuk bir
havuzda, hafif acenteler biraz daha genis bir havuzda calisir — mantik
ana projedeki hotel_checker.py'nin hafif/agir ayrimiyla ayni ruhta,
kucuk olcege indirgenmis hali.

Kullanim: python run_shard.py <batch.json> <shard> <shard_count>
"""
import sys, json, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

def main():
    batch_path, shard, shard_count = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])

    with open(batch_path, encoding="utf-8") as f:
        combos = json.load(f)

    mine = [c for i, c in enumerate(combos) if i % shard_count == shard]
    print(f"Shard {shard}/{shard_count}: {len(mine)} kombinasyon", flush=True)

    from scrapers import ets, tatilbudur, jolly, setur, touristica, coral, gezinomi
    SCRAPERS = {
        "ETS Tur": ets.get_price, "Tatilbudur": tatilbudur.get_price, "Jolly": jolly.get_price,
        "Setur": setur.get_price, "Touristica": touristica.get_price,
        "Coral": coral.get_price, "Gezinomi": gezinomi.get_price,
    }
    HEAVY = {"Tatilbudur", "Touristica"}  # Selenium/Playwright, gercek tarayici
    JOLLY = {"Jolly"}  # kendi ic kilidi zaten seri, ayri kucuk havuz yeterli

    results = []
    results_lock = threading.Lock()

    def work(c):
        t0 = time.time()
        try:
            scraper = SCRAPERS[c["acente"]]
            r = scraper(c["url"], c["giris"], c["cikis"], 2, 0, "", "", reuse_driver=True)
        except Exception as e:
            r = {"price": None, "status": "error", "oda_adi": str(e)[:200]}
        sure_ms = int((time.time() - t0) * 1000)
        row = {
            "hotel_id": c["hotel_id"], "period_id": c["period_id"], "acente": c["acente"],
            "fiyat": r.get("price"), "oda_adi": r.get("oda_adi"), "status": r.get("status", "ok"),
            "hata_mesaj": r.get("oda_adi") if r.get("status") == "error" else None,
            "sure_ms": sure_ms,
        }
        print(f"[{c['acente']}] {c.get('hotel_name','')} -> {r.get('status')} {r.get('price')} ({sure_ms}ms)", flush=True)
        with results_lock:
            results.append(row)

    heavy = [c for c in mine if c["acente"] in HEAVY]
    jolly_combos = [c for c in mine if c["acente"] in JOLLY]
    light = [c for c in mine if c["acente"] not in HEAVY and c["acente"] not in JOLLY]

    # 2 vCPU'luk runner icin tedbirli havuz boyutlari: agir 2, hafif 8, jolly 1
    # (Jolly'nin kendi ic kilidi zaten tek seferde 1 istege izin veriyor).
    with ThreadPoolExecutor(max_workers=2) as heavy_pool, \
         ThreadPoolExecutor(max_workers=8) as light_pool, \
         ThreadPoolExecutor(max_workers=1) as jolly_pool:
        futs = []
        futs += [heavy_pool.submit(work, c) for c in heavy]
        futs += [light_pool.submit(work, c) for c in light]
        futs += [jolly_pool.submit(work, c) for c in jolly_combos]
        for fut in as_completed(futs):
            fut.result()  # istisnalari yukselt, work() zaten kendi icinde yakaliyor

    with open(f"results_{shard}.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False)
    print(f"Yazildi: results_{shard}.json ({len(results)} sonuc)", flush=True)

if __name__ == "__main__":
    main()
