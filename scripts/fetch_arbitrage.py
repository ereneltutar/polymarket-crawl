#!/usr/bin/env python3
"""
Polymarket Arbitrage Watch
---------------------------
Her sabah otomatik calisir, Polymarket Gamma API'sinden acik (kapanmamis)
event'leri ceker, deadline'i belirli bir pencere icinde olanlari filtreler
ve iki ayri mantikla firsat arar:

1) RISKSIZ ARBITRAJ (negRisk gruplarinda):
   Bir event'te N tane birbirini dislayan ve TAMAMI kapsayan secenek varsa
   (ornegin "Kim kazanir?" tipi bir yarisma), her secenegin "Yes" tarafini
   en iyi satis (ask) fiyatindan satin alip elinde tutarsan, olaylardan
   TAM OLARAK BIRI gerceklesecegi icin garanti $1 odeme alirsin.
   Eger butun "ask" fiyatlarinin toplami $1'in altindaysa, aradaki fark
   teorik bir risksiz kazanc (gercek hayatta islem ucreti, kayma/slippage,
   likidite yetersizligi ve infaz riski bu farki azaltabilir/yok edebilir).

   Bu script sadece "negRisk" gruplarini tarar (binary tek soru-cevap
   marketlerde NO tarafinin gercek ask fiyati Gamma API'de ayri bir alan
   olarak gelmiyor, CLOB book endpoint'i de bilinen bir "stale data" sorunu
   tasidigi icin v1'de bilerek disarida tutuldu).

2) KALIBRASYON SAPMASI (istatistiksel, RISKSIZ DEGIL):
   scripts/calibration_scan.py (haftalik, ayri bir is) Polymarket'in
   GECMIS kapanmis market'lerini tarayip "piyasa fiyati" ile "gercekte
   gerceklesen oran" arasinda sistematik bir sapma (favorite-longshot
   bias) olup olmadigini olcer ve docs/calibration.json'a yazar.
   Bu script o tabloyu okuyup BUGUNUN acik market'lerini bu tabloyla
   karsilastirir: fiyati, gecmiste istatistiksel olarak anlamli sapma
   gosterilmis bir araliga denk gelen market'leri "calibration_signals"
   olarak isaretler. Bu, TEK bir bahiste kazanc garantisi DEGILDIR -
   cok sayida tekrarlanan pozisyonda beklenen degeri (+EV) lehine ceken
   istatistiksel bir egilimdir.

Cikti: docs/results.json (statik dashboard bu dosyayi okuyor)
"""

import datetime
import json
import sys
import time
from pathlib import Path

import requests

GAMMA_BASE = "https://gamma-api.polymarket.com"

# --- Ayarlanabilir parametreler -------------------------------------------
DAYS_AHEAD = 30          # deadline'i bugunden en fazla kac gun sonra olan event'ler
MIN_EDGE_PCT = 0.5       # bu yuzdenin altindaki "firsatlari" gosterme (gurultu filtresi)
MIN_LIQUIDITY_USD = 50   # her bacakta en az bu kadar likidite olmali (cok ince kitaplari ele)
PAGE_LIMIT = 500
MAX_PAGES = 60           # guvenlik siniri
REQUEST_TIMEOUT = 30

# Momentum / hacim anomalisi taramasi icin:
MOMENTUM_MIN_VOLUME_24H = 200     # bu tutarin altindaki gunluk hacmi olan marketleri atla (gurultu)
MOMENTUM_VOLUME_MULTIPLIER = 3.0  # son 24s hacmi, son 1 haftalik gunluk ortalamanin en az kac kati olmali
MOMENTUM_MIN_PRICE_MOVE = 0.05    # son 24s (yoksa son 1s) fiyat hareketi en az kac puan olmali (0.05 = 5 puan)
MAX_MOMENTUM_SIGNALS = 15         # dashboard'da gosterilecek max sinyal sayisi

# Kalibrasyon sapmasi (favorite-longshot bias) capraz kontrolu icin:
MIN_CALIBRATION_EDGE_PCT = 2.0    # bu yuzdenin altindaki kalibrasyon farkini gosterme (gurultu)
MIN_CALIBRATION_LIQUIDITY_USD = 100  # bu likiditenin altindaki market'leri ele
MAX_CALIBRATION_SIGNALS = 20      # dashboard'da gosterilecek max sinyal sayisi
# ----------------------------------------------------------------------------

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "results.json"
CALIBRATION_PATH = Path(__file__).resolve().parent.parent / "docs" / "calibration.json"


def fetch_all_events() -> list:
    """Gamma API /events endpoint'inden aktif ve kapanmamis tum event'leri sayfalayarak ceker.

    Not: API'nin sayfa basina dondurdugu gercek eleman sayisi, "limit" ile
    istenenden daha az olabilir (sunucu kendi ust siniri uygulayabilir).
    Bu yuzden "bos sayfa gelene kadar devam et, offset'i gercek alinan
    miktar kadar ilerlet" mantigini kullaniyoruz; "alinan miktar < istenen
    limit" durumunu "veri bitti" sanmiyoruz.
    """
    events = []
    offset = 0
    for _ in range(MAX_PAGES):
        params = {
            "active": "true",
            "closed": "false",
            "limit": PAGE_LIMIT,
            "offset": offset,
        }
        resp = requests.get(f"{GAMMA_BASE}/events", params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        batch = resp.json()
        if not isinstance(batch, list) or not batch:
            break
        events.extend(batch)
        offset += len(batch)
        time.sleep(0.2)  # API'ye nazik davran
    return events


def parse_iso(date_str):
    if not date_str:
        return None
    try:
        return datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def find_opportunity(event: dict, now: datetime.datetime):
    """Bir event icin negRisk arbitraj firsati varsa dict, yoksa None doner."""
    markets = event.get("markets") or []
    neg_risk_markets = [
        m for m in markets
        if m.get("negRisk")
        and m.get("acceptingOrders")
        and m.get("bestAsk") is not None
    ]
    if len(neg_risk_markets) < 2:
        return None

    legs = []
    total_ask = 0.0
    for m in neg_risk_markets:
        try:
            ask = float(m["bestAsk"])
        except (TypeError, ValueError):
            return None
        if ask <= 0 or ask >= 1:
            return None
        liquidity = float(m.get("liquidityNum") or 0)
        total_ask += ask
        legs.append({
            "outcome": m.get("groupItemTitle") or m.get("question") or "?",
            "ask": round(ask, 4),
            "liquidity": round(liquidity, 2),
        })

    if total_ask <= 0:
        return None

    edge_pct = (1 - total_ask) / total_ask * 100
    if edge_pct < MIN_EDGE_PCT:
        return None

    min_liquidity = min(leg["liquidity"] for leg in legs)
    if min_liquidity < MIN_LIQUIDITY_USD:
        return None

    end_date = parse_iso(event.get("endDate"))
    days_left = (end_date - now).days if end_date else None

    return {
        "event_title": event.get("title") or event.get("ticker") or "Bilinmeyen event",
        "slug": event.get("slug"),
        "url": f"https://polymarket.com/event/{event.get('slug')}" if event.get("slug") else None,
        "end_date": event.get("endDate"),
        "days_left": days_left,
        "num_outcomes": len(legs),
        "total_cost": round(total_ask, 4),
        "edge_pct": round(edge_pct, 2),
        "min_outcome_liquidity": round(min_liquidity, 2),
        "legs": sorted(legs, key=lambda x: x["ask"]),
    }


def load_calibration():
    """docs/calibration.json'i okur. Henuz hic kalibrasyon taramasi calismadiysa
    (dosya yok) None doner - bu, gunluk script'in cokmesine sebep olmamali,
    sadece kalibrasyon sinyallerini bos gecer."""
    if not CALIBRATION_PATH.exists():
        return None
    try:
        return json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def find_bin(price: float, bins: list):
    """Verilen fiyatin dustugu kalibrasyon kovasini bulur."""
    for b in bins:
        lo, hi = b["range"]
        if lo <= price < hi or (hi >= 1.0 and price == 1.0):
            return b
    return None


def find_calibration_signal(market: dict, event: dict, bins: list, now: datetime.datetime):
    """Bir market'in fiyati, gecmiste istatistiksel olarak anlamli kalibrasyon
    sapmasi gosterilmis bir kovaya denk geliyorsa sinyal dondurur, yoksa None.

    ONEMLI: Bu RISKSIZ DEGIL. Tek bir bahis icin kazanc garantisi vermiyor;
    cok sayida tekrarlanan, BAGIMSIZ pozisyonda beklenen degeri (+EV) lehine
    cektigi varsayilan istatistiksel bir egilim."""
    try:
        price = float(market.get("lastTradePrice") or market.get("bestAsk") or 0)
    except (TypeError, ValueError):
        return None
    if price <= 0 or price >= 1:
        return None

    liquidity = float(market.get("liquidityNum") or 0)
    if liquidity < MIN_CALIBRATION_LIQUIDITY_USD:
        return None

    bucket = find_bin(price, bins)
    if not bucket or not bucket.get("significant"):
        return None

    bias_pct = bucket["bias_pct"]
    actual_rate = bucket["resolved_yes_rate"]

    if bias_pct > 0:
        side, cost, true_rate = "YES", price, actual_rate
    else:
        side, cost, true_rate = "NO", 1 - price, 1 - actual_rate

    if cost <= 0:
        return None

    edge_pct = (true_rate - cost) / cost * 100
    if edge_pct < MIN_CALIBRATION_EDGE_PCT:
        return None

    end_date = parse_iso(event.get("endDate"))
    days_left = (end_date - now).days if end_date else None

    return {
        "market_question": market.get("question") or event.get("title") or "?",
        "slug": event.get("slug"),
        "url": f"https://polymarket.com/event/{event.get('slug')}" if event.get("slug") else None,
        "days_left": days_left,
        "recommended_side": side,
        "current_price": round(price, 4),
        "implied_cost": round(cost, 4),
        "bucket_range": bucket["range"],
        "bucket_sample_size": bucket["sample_size"],
        "bucket_historical_rate": round(actual_rate, 4),
        "edge_pct": round(edge_pct, 2),
        "liquidity": round(liquidity, 2),
    }


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now + datetime.timedelta(days=DAYS_AHEAD)

    try:
        events = fetch_all_events()
    except requests.RequestException as exc:
        print(f"Polymarket API hatasi: {exc}", file=sys.stderr)
        events = []

    calibration = load_calibration()
    calibration_bins = calibration["bins"] if calibration else None

    opportunities = []
    calibration_signals = []

    for event in events:
        end_date = parse_iso(event.get("endDate"))
        if not end_date or end_date < now or end_date > cutoff:
            continue

        opp = find_opportunity(event, now)
        if opp:
            opportunities.append(opp)

        if calibration_bins:
            for market in (event.get("markets") or []):
                sig = find_calibration_signal(market, event, calibration_bins, now)
                if sig:
                    calibration_signals.append(sig)

    opportunities.sort(key=lambda o: o["edge_pct"], reverse=True)
    calibration_signals.sort(key=lambda s: s["edge_pct"], reverse=True)
    calibration_signals = calibration_signals[:MAX_CALIBRATION_SIGNALS]

    output = {
        "generated_at": now.isoformat(),
        "days_ahead_filter": DAYS_AHEAD,
        "min_edge_pct_filter": MIN_EDGE_PCT,
        "scanned_events": len(events),
        "opportunities": opportunities,
        "calibration_signals": calibration_signals,
        "calibration_table_generated_at": calibration["generated_at"] if calibration else None,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(events)} event tarandi, {len(opportunities)} arbitraj firsati, "
          f"{len(calibration_signals)} kalibrasyon sinyali bulundu -> {OUTPUT_PATH}")
    if not calibration:
        print("Not: docs/calibration.json henuz yok - 'Weekly Calibration Scan' workflow'u "
              "en az bir kez calismadan kalibrasyon sinyalleri uretilemez.", file=sys.stderr)


if __name__ == "__main__":
    main()
