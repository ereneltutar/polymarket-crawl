#!/usr/bin/env python3
"""
Calibration Drift Scanner — Favorite-Longshot Bias Detector
--------------------------------------------------------------
Bu script Polymarket'in GECMISTE KAPANMIS (sonuclanmis) market'lerini
tarayip, "piyasanin fiyatladigi olasilik" ile "gercekte gerceklesen
oran" arasinda sistematik bir sapma olup olmadigini olcer.

Bilinen fenomen (favorite-longshot bias): dusuk olasilikli sonuclar
genelde gercek frekanslarindan daha PAHALI fiyatlanir, yuksek olasilikli
sonuclar ise hafifce UCUZ kalir. Bu script bu sapmanin Polymarket'te
var olup olmadigini, hangi fiyat araliklarinda ve ne buyuklukte oldugunu
istatistiksel olarak (Wilson skor guven araligi ile) olcer.

KRITIK METODOLOJIK NOKTA:
  Kapanmis bir market'in outcomePrices'i SONUCUN KENDISIDIR (0'a/1'e
  yakin) — bunu referans fiyat olarak kullanmak, cevabi cevaptan tahmin
  etmek olur (bu hata baska gelistiricilerde gercekten gorulmus ve
  sahte "%93 dogruluk" sonuclarina yol acmis). Bu yuzden referans
  fiyati, kapanistan LEAD_DAYS gun ONCEKI gercek fiyat olarak aliyoruz
  (CLOB /prices-history endpoint'i uzerinden).

BILINEN ALTYAPI RISKI:
  Polymarket'in /prices-history endpoint'i bazi token'lar icin bos
  veri donduruyor (Polymarket'in kendi GitHub repo'sunda raporlanmis,
  dogrulanmis bir sorun). Bu script bu basarisizliklari sessizce
  yutmuyor — kac market'in basariyla kullanildigini, kacinin
  veri eksikligi yuzunden atlandigini acikca raporluyor.

  AYRICA: Polymarket'in kimliksiz (unauthenticated) API erisimi icin
  belgelenmis bir oran siniri var (~dakikada 60 istek). Bu script
  market basina bir CLOB cagrisi yaptigi icin, bu siniri asarsa
  cogu cagri 429 (Too Many Requests) ile geri doner - bu, gercek bir
  "veri yok" durumuyla AYNI sekilde basarisizlik gibi gorunur ama
  kok nedeni tamamen farklidir (yavaslatip tekrar denemek yeterlidir).
  Bu yuzden fetch_reference_price() 429'u ozel olarak tespit edip
  backoff ile yeniden deniyor, ve kac basarisizligin rate-limit
  kaynakli oldugunu ayri sayiyor.

Cikti: docs/calibration.json
"""

import datetime
import json
import math
import sys
import time
from pathlib import Path

import requests

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

# --- Ayarlanabilir parametreler -------------------------------------------
LEAD_DAYS = 7                 # referans fiyati kapanistan kac gun once alalim
MIN_DURATION_DAYS = 3         # bundan daha kisa surmus market'leri ele (15dk BTC up/down gibi)
BIN_WIDTH = 0.05              # kova genisligi (0.05 = 20 kova)
MIN_SAMPLE_PER_BUCKET = 30    # bu sayidan az ornegi olan kovaya istatistiksel guven yok
RESOLUTION_THRESHOLD = 0.98   # outcomePrices bunun ustunde/altinda degilse "net sonuclanmamis" sayip ele
PAGE_LIMIT = 500
MAX_PAGES = 40                # guvenlik siniri (closed market taramasi icin)
MAX_MARKETS_TO_ANALYZE = 2200 # CLOB price-history cagrisi yapilacak max market sayisi (rate-limit guvenligi - asagidaki SLEEP ile carptiginda 60dk timeout'u asmamali)
REQUEST_TIMEOUT = 20
SLEEP_BETWEEN_CALLS = 1.15    # ~52 istek/dk - Polymarket'in ~60/dk siniri altinda guvenli pay birakir
MAX_RETRIES_ON_429 = 2        # rate-limit hatasinda kac kere tekrar denensin
BACKOFF_SECONDS_ON_429 = 8    # her 429'da bu kadar bekleyip tekrar dene
# ----------------------------------------------------------------------------

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "calibration.json"


def parse_iso(date_str):
    if not date_str:
        return None
    try:
        return datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def fetch_closed_markets() -> list:
    """Gamma API'den kapanmis market'leri sayfalayarak ceker."""
    markets = []
    offset = 0
    for _ in range(MAX_PAGES):
        params = {"closed": "true", "limit": PAGE_LIMIT, "offset": offset}
        try:
            resp = requests.get(f"{GAMMA_BASE}/markets", params=params, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            batch = resp.json()
        except requests.RequestException as exc:
            print(f"Gamma API hatasi (offset={offset}): {exc}", file=sys.stderr)
            break
        if not isinstance(batch, list) or not batch:
            break
        markets.extend(batch)
        offset += len(batch)
        time.sleep(0.15)
    return markets


def qualifies(market: dict) -> bool:
    """Bir kapanmis market'in kalibrasyon analizine girmeye uygun olup olmadigini kontrol eder."""
    start = parse_iso(market.get("startDate"))
    end = parse_iso(market.get("endDate"))
    if not start or not end:
        return False
    duration_days = (end - start).total_seconds() / 86400

    # KRITIK: target_ts = end - LEAD_DAYS. Market'in suresi LEAD_DAYS'tan kisaysa,
    # target_ts market'in baslangicindan ONCEYE denk gelir ve fetch_reference_price()
    # garanti basarisiz olur (history'de o tarihten once hic veri yoktur). Bu yuzden
    # MIN_DURATION_DAYS yerine dogrudan LEAD_DAYS + kucuk bir tampon kullaniyoruz.
    min_required_duration = max(MIN_DURATION_DAYS, LEAD_DAYS + 1)
    if duration_days < min_required_duration:
        return False  # cok kisa sureli market (orn. 15dk crypto up/down, ya da LEAD_DAYS'tan kisa yasamis)

    raw_prices = market.get("outcomePrices")
    if not raw_prices:
        return False
    try:
        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
        yes_price = float(prices[0])
    except (ValueError, TypeError, IndexError, json.JSONDecodeError):
        return False

    # Net sonuclanmamis (orn. iptal/draw/belirsiz) market'leri ele - sadece acik YES/NO sonuclar
    if not (yes_price >= RESOLUTION_THRESHOLD or yes_price <= (1 - RESOLUTION_THRESHOLD)):
        return False

    raw_tokens = market.get("clobTokenIds")
    if not raw_tokens:
        return False
    try:
        tokens = json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
        if not tokens or not tokens[0]:
            return False
    except (json.JSONDecodeError, TypeError):
        return False

    return True


def fetch_reference_price(yes_token_id: str, target_ts: int):
    """CLOB /prices-history'den, target_ts'den ONCEKI en yakin fiyati ceker.

    Basarisizlikta (price=None, reason) doner - reason, ust seviyede HANGI
    turde basarisizlik oldugunu ayirt edebilmemiz icin var (rate_limited /
    empty_history / no_candidate / request_error). Bu ayrim onemli: 429
    (rate limit) "veri yok" ile AYNI sonucu (None) verir ama kok nedeni
    tamamen farklidir ve farkli bir cozumu gerektirir."""
    attempt = 0
    while True:
        try:
            resp = requests.get(
                f"{CLOB_BASE}/prices-history",
                params={"market": yes_token_id, "interval": "max", "fidelity": 720},  # 720 dk = 12 saat
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException:
            return None, "request_error"

        if resp.status_code == 429:
            if attempt >= MAX_RETRIES_ON_429:
                return None, "rate_limited"
            attempt += 1
            time.sleep(BACKOFF_SECONDS_ON_429 * attempt)
            continue

        try:
            resp.raise_for_status()
            history = resp.json().get("history") or []
        except (requests.RequestException, ValueError):
            return None, "request_error"
        break

    if not history:
        return None, "empty_history"

    # target_ts'den ONCEKI (lookahead-leak yapmayan) en yakin noktayi bul
    candidates = [pt for pt in history if pt.get("t") is not None and pt["t"] <= target_ts]
    if not candidates:
        return None, "no_candidate"
    closest = max(candidates, key=lambda pt: pt["t"])
    try:
        return float(closest["p"]), None
    except (TypeError, ValueError, KeyError):
        return None, "request_error"


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

    print("Kapanmis market'ler cekiliyor...")
    all_closed = fetch_closed_markets()
    print(f"{len(all_closed)} kapanmis market bulundu (Gamma API).")

    qualifying = [m for m in all_closed if qualifies(m)]
    print(f"{len(qualifying)} market filtreyi gecti (min {MIN_DURATION_DAYS} gun surmus, net sonuclanmis).")

    if len(qualifying) > MAX_MARKETS_TO_ANALYZE:
        qualifying = qualifying[:MAX_MARKETS_TO_ANALYZE]
        print(f"Runtime guvenligi icin {MAX_MARKETS_TO_ANALYZE} market ile sinirlandi.")

    samples = []          # her biri: {reference_price, resolved_yes}
    failure_reasons = {"rate_limited": 0, "empty_history": 0, "no_candidate": 0, "request_error": 0}

    for i, market in enumerate(qualifying):
        end = parse_iso(market.get("endDate"))
        tokens = json.loads(market["clobTokenIds"]) if isinstance(market["clobTokenIds"], str) else market["clobTokenIds"]
        yes_token_id = tokens[0]
        target_ts = int((end - datetime.timedelta(days=LEAD_DAYS)).timestamp())

        ref_price, reason = fetch_reference_price(yes_token_id, target_ts)
        time.sleep(SLEEP_BETWEEN_CALLS)

        if ref_price is None:
            failure_reasons[reason] = failure_reasons.get(reason, 0) + 1
            continue

        raw_prices = market["outcomePrices"]
        prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
        resolved_yes = float(prices[0]) >= RESOLUTION_THRESHOLD

        samples.append({"reference_price": ref_price, "resolved_yes": resolved_yes})

        if (i + 1) % 200 == 0:
            done_failures = sum(failure_reasons.values())
            print(f"  ... {i + 1}/{len(qualifying)} islendi ({len(samples)} basarili, {done_failures} basarisiz)")

    history_failures = sum(failure_reasons.values())
    print(f"Toplam {len(samples)} market icin gecmis fiyat basariyla cekildi.")
    print(f"{history_failures} market basarisiz oldu - detay: {failure_reasons}")
    if failure_reasons["rate_limited"] > history_failures * 0.2:
        print("UYARI: basarisizliklarin onemli bir kismi rate-limit (429) kaynakli. "
              "SLEEP_BETWEEN_CALLS'i artirmayi veya MAX_RETRIES_ON_429'u yukseltmeyi dusun.", file=sys.stderr)

    # --- Kovalama ve istatistik ---
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
            # "Anlamli" = guven araligi, piyasanin kendi fiyatini (midpoint) icermiyor
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
        "lead_days": LEAD_DAYS,
        "min_duration_days": MIN_DURATION_DAYS,
        "bin_width": BIN_WIDTH,
        "min_sample_per_bucket": MIN_SAMPLE_PER_BUCKET,
        "closed_markets_scanned": len(all_closed),
        "markets_qualifying": len(qualifying),
        "markets_with_history": len(samples),
        "history_fetch_failures": history_failures,
        "failure_breakdown": failure_reasons,
        "bins": bins,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Yazildi -> {OUTPUT_PATH}")
    sig_count = sum(1 for b in bins if b["significant"])
    print(f"{sig_count} kovada istatistiksel olarak anlamli sapma bulundu.")


if __name__ == "__main__":
    main()
