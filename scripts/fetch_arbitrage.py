#!/usr/bin/env python3
"""
Polymarket Arbitrage Watch
---------------------------
Her sabah otomatik calisir, Polymarket Gamma API'sinden acik (kapanmamis)
event'leri ceker, deadline'i belirli bir pencere icinde olanlari filtreler
ve "negRisk" (birbirini dislayan, coklu secenekli) event gruplarinda
teorik risksiz arbitraj firsati olup olmadigini hesaplar.

Mantik (multi-outcome arbitraj):
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
DAYS_AHEAD = 14          # deadline'i bugunden en fazla kac gun sonra olan event'ler
MIN_EDGE_PCT = 0.5       # bu yuzdenin altindaki "firsatlari" gosterme (gurultu filtresi)
MIN_LIQUIDITY_USD = 50   # her bacakta en az bu kadar likidite olmali (cok ince kitaplari ele)
PAGE_LIMIT = 500
MAX_PAGES = 60           # guvenlik siniri
REQUEST_TIMEOUT = 30
# ----------------------------------------------------------------------------

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "results.json"


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


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now + datetime.timedelta(days=DAYS_AHEAD)

    try:
        events = fetch_all_events()
    except requests.RequestException as exc:
        print(f"Polymarket API hatasi: {exc}", file=sys.stderr)
        events = []

    opportunities = []
    for event in events:
        end_date = parse_iso(event.get("endDate"))
        if not end_date or end_date < now or end_date > cutoff:
            continue
        opp = find_opportunity(event, now)
        if opp:
            opportunities.append(opp)

    opportunities.sort(key=lambda o: o["edge_pct"], reverse=True)

    output = {
        "generated_at": now.isoformat(),
        "days_ahead_filter": DAYS_AHEAD,
        "min_edge_pct_filter": MIN_EDGE_PCT,
        "scanned_events": len(events),
        "opportunities": opportunities,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(events)} event tarandi, {len(opportunities)} firsat bulundu -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
