#!/usr/bin/env python3
"""
Calibration Drift Scanner — Favorite-Longshot Bias Detector (v2: ileriye-donuk)
--------------------------------------------------------------------------------
Bu script, "piyasanin fiyatladigi olasilik" ile "gercekte gerceklesen oran"
arasinda sistematik bir sapma (favorite-longshot bias) olup olmadigini olcer.

NEDEN GECMISE BAKMIYORUZ (v1'den v2'ye gecis nedeni):
  v1, Polymarket'in CLOB /prices-history endpoint'inden GECMISE donup
  "kapanistan N gun onceki fiyat ne idi" diye soruyordu. Canli testte
  (379 nitelikli market, 372 basarisiz) bu endpoint market'lerin %98'i
  icin BOS veri dondu. Bu rastlantisal bir hata degil - Polymarket'in
  kendi genel API'si sadece CANLI durumu guvenilir sekilde sunuyor;
  bagimsiz bir ucuncu taraf sirketin ("PolymarketData.co") sirf bu
  yuzden ayrica ucretli bir gecmis-veri arsivi satmasi da bunu
  dogruluyor.

  Bu yuzden v2 GECMISE bakmiyor, ILERIYE bakiyor: scripts/fetch_arbitrage.py
  (her sabah calisan ana script), kapanisina yaklasik 7 gun kalan her
  market'in su anki fiyatini docs/price_log.jsonl'e ekliyor (append-only).
  Bu script o log'u okuyor, Gamma API'den (guvenilir, sorunsuz) o
  market'lerin ARTIK kapanip kapanmadigini kontrol ediyor, kapananlari
  gercek sonucla eslestirip ayni kovalama/Wilson-araligi istatistigini
  uyguluyor.

  BEDELI: Bu yontem aninda sonuc vermez. Ilk anlamli sinyaller icin
  (kova basina 30+ ornek) gercekci olarak haftalar/aylar gerekir - cunku
  hem dogru zamanda loglanmis hem de sonradan kapanmis market birikmesi
  gerekiyor. Ama bu, Polymarket'in guvenilmez gecmis-veri altyapisina
  hic bagimli olmayan TEK saglam yontem.

Girdi: docs/price_log.jsonl (fetch_arbitrage.py tarafindan yaziliyor)
Cikti: docs/calibration.json (sekli v1 ile AYNI - fetch_arbitrage.py'nin
       find_calibration_signal() fonksiyonu bu degisiklikten habersiz,
       hicbir kod degisikligi gerekmedi)
"""

import datetime
import json
import math
import sys
import time
from pathlib import Path

import requests

GAMMA_BASE = "https://gamma-api.polymarket.com"

# --- Ayarlanabilir parametreler -------------------------------------------
BIN_WIDTH = 0.05              # kova genisligi (0.05 = 20 kova)
MIN_SAMPLE_PER_BUCKET = 30    # bu sayidan az ornegi olan kovaya istatistiksel guven yok
RESOLUTION_THRESHOLD = 0.98   # outcomePrices bunun ustunde/altinda degilse "net sonuclanmamis" sayip ele
MAX_LOG_ENTRIES_TO_CHECK = 2500  # runtime/rate-limit guvenligi (Gamma API ~60 istek/dk)
REQUEST_TIMEOUT = 20
SLEEP_BETWEEN_CALLS = 1.15    # ~52 istek/dk - Gamma API'nin ~60/dk siniri altinda guvenli pay
MAX_RETRIES_ON_429 = 2
BACKOFF_SECONDS_ON_429 = 8
# ----------------------------------------------------------------------------

PRICE_LOG_PATH = Path(__file__).resolve().parent.parent / "docs" / "price_log.jsonl"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "calibration.json"


def load_price_log() -> list:
    """docs/price_log.jsonl'i okur. Dosya yoksa (henuz hic loglanmamissa) bos liste doner."""
    entries = []
    if not PRICE_LOG_PATH.exists():
        return entries
    with PRICE_LOG_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def fetch_market_resolution(market_id: str):
    """Gamma API'den tek bir market'in SU ANKI durumunu ceker (guvenilir,
    /prices-history gibi bir sorun bilinmiyor). Kapanip net sonuclanmissa
    (True=Yes, False=No) doner; hala aciksa, henuz kapanmamissa, ya da
    belirsiz sonuclanmissa (orn. 50-50/iptal) None doner - "henuz bilinmiyor"
    anlaminda, hata degil."""
    attempt = 0
    while True:
        try:
            resp = requests.get(f"{GAMMA_BASE}/markets/{market_id}", timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            return None
        if resp.status_code == 429:
            if attempt >= MAX_RETRIES_ON_429:
                return None
            attempt += 1
            time.sleep(BACKOFF_SECONDS_ON_429 * attempt)
            continue
        try:
            resp.raise_for_status()
            market = resp.json()
        except (requests.RequestException, ValueError):
            return None
        break

    if not market.get("closed"):
        return None

    raw_prices = market.get("outcomePrices")
    if not raw_prices:
        return None
    try:
        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
        yes_price = float(prices[0])
    except (ValueError, TypeError, IndexError, json.JSONDecodeError):
        return None

    if yes_price >= RESOLUTION_THRESHOLD:
        return True
    if yes_price <= (1 - RESOLUTION_THRESHOLD):
        return False
    return None  # belirsiz / 50-50 / iptal - kullanma


def wilson_interval(k: int, n: int, z: float = 1.96):
    """95% Wilson skor guven araligi (kucuk orneklemde normal yaklasimdan daha guvenilir)."""
    if n == 0:
        return (0.0, 0.0)
    p_hat = k / n
    denom = 1 + (z ** 2) / n
    center = (p_hat + (z ** 2) / (2 * n)) / denom
    margin = (z / denom) * math.sqrt((p_hat * (1 - p_hat) / n) + (z ** 2) / (4 * n ** 2))
    return (max(0.0, center - margin), min(1.0, center + margin))


def main():
    now = datetime.datetime.now(datetime.timezone.utc)

    log_entries = load_price_log()
    print(f"{len(log_entries)} loglanmis market bulundu (docs/price_log.jsonl).")

    # En eski loglanan market'ler kapanmaya en yakin olduklari icin once onlara bakmak,
    # her calistirmada en yuksek "sonuclanmis market" verimini almamizi saglar.
    log_entries.sort(key=lambda e: e.get("logged_at", ""))
    if len(log_entries) > MAX_LOG_ENTRIES_TO_CHECK:
        log_entries = log_entries[:MAX_LOG_ENTRIES_TO_CHECK]
        print(f"Runtime guvenligi icin {MAX_LOG_ENTRIES_TO_CHECK} kayit ile sinirlandi.")

    samples = []
    still_open_or_unclear = 0

    for i, entry in enumerate(log_entries):
        resolved_yes = fetch_market_resolution(entry["market_id"])
        time.sleep(SLEEP_BETWEEN_CALLS)

        if resolved_yes is None:
            still_open_or_unclear += 1
            continue

        samples.append({"reference_price": entry["price"], "resolved_yes": resolved_yes})

        if (i + 1) % 200 == 0:
            print(f"  ... {i + 1}/{len(log_entries)} kontrol edildi "
                  f"({len(samples)} sonuclanmis, {still_open_or_unclear} hala acik/belirsiz)")

    print(f"Toplam {len(samples)} market sonuclanmis ve kullanilabilir.")
    print(f"{still_open_or_unclear} market hala acik, henuz kapanmamis, ya da belirsiz sonuclanmis.")

    # --- Kovalama ve istatistik (v1 ile birebir ayni mantik) ---
    num_bins = int(round(1 / BIN_WIDTH))
    bins = []
    for b in range(num_bins):
        lo = b * BIN_WIDTH
        hi = lo + BIN_WIDTH
        bucket_samples = [s for s in samples if lo <= s["reference_price"] < hi or (b == num_bins - 1 and s["reference_price"] == 1.0)]
        n = len(bucket_samples)
        k = sum(1 for s in bucket_samples if s["resolved_yes"])
        midpoint = (lo + hi) / 2

        entry = {
            "range": [round(lo, 2), round(hi, 2)],
            "midpoint": round(midpoint, 3),
            "sample_size": n,
        }

        if n >= MIN_SAMPLE_PER_BUCKET:
            actual_rate = k / n
            ci_low, ci_high = wilson_interval(k, n)
            bias_pct = (actual_rate - midpoint) * 100
            significant = not (ci_low <= midpoint <= ci_high)
            entry.update({
                "resolved_yes_rate": round(actual_rate, 4),
                "ci_95_low": round(ci_low, 4),
                "ci_95_high": round(ci_high, 4),
                "bias_pct": round(bias_pct, 2),
                "significant": significant,
            })
        else:
            entry.update({
                "resolved_yes_rate": None,
                "ci_95_low": None,
                "ci_95_high": None,
                "bias_pct": None,
                "significant": False,
            })

        bins.append(entry)

    output = {
        "generated_at": now.isoformat(),
        "bin_width": BIN_WIDTH,
        "min_sample_per_bucket": MIN_SAMPLE_PER_BUCKET,
        "logged_markets_total": len(load_price_log()),  # checked sayisindan once, sinirlamadan once
        "logged_markets_checked": len(log_entries),
        "markets_resolved": len(samples),
        "markets_still_open_or_unclear": still_open_or_unclear,
        "bins": bins,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Yazildi -> {OUTPUT_PATH}")
    sig_count = sum(1 for b in bins if b["significant"])
    print(f"{sig_count} kovada istatistiksel olarak anlamli sapma bulundu.")
    if len(samples) < MIN_SAMPLE_PER_BUCKET:
        print(f"Not: toplam {len(samples)} sonuclanmis market var, en az bir kovanin "
              f"anlamli olabilmesi icin bile {MIN_SAMPLE_PER_BUCKET} gerekiyor - "
              f"daha fazla market kapanana kadar bekleniyor.", file=sys.stderr)


if __name__ == "__main__":
    main()
