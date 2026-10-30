#!/usr/bin/env python3
"""Medusa – Kraken nyilvános piaci adatok gyűjtése (API-kulcs nélkül).

Óránként fut (GitHub Actions). Csak a Python standard könyvtárát használja.
Gyűjtött adatok (data/ alatt):
  ohlc/1440/<PÁR>.csv  napi gyertyák (csak lezártak), összefésülve, időben visszamenőleg bővül
  ohlc/60/<PÁR>.csv    órás gyertyák (csak lezártak), összefésülve
  ticker/ÉÉÉÉ-HH.csv   pillanatkép: bid, ask, spread, utolsó ár, 24 órás forgalom
  depth/ÉÉÉÉ-HH.csv    ajánlati könyv: legjobb bid/ask, likviditás (EUR) a középár ±0,5%-os sávjában
  errors.log           sikertelen lekérések
Kilépési kód: 1, ha bármelyik OHLC-lekérés sikertelen volt (a már letöltött adatok ekkor is mentésre kerülnek).
A Ticker/Depth hibák (pl. "EGeneral:No data") csak az errors.log-ba kerülnek, hibakódot nem okoznak.
"""
import csv, json, os, sys, time, urllib.request, urllib.error, datetime as dt

API = "https://api.kraken.com/0/public/"
PAIRS = ["ETHEUR", "SOLEUR", "XRPEUR", "XBTEUR"]   # XBTEUR: csak tájékoztató (D-001)
INTERVALS = [1440, 60]
DEPTH_BAND = 0.005
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
UA = {"User-Agent": "medusa-research-data/1.0 (academic, public endpoints only)"}
errors = []        # minden hiba (errors.log)
ohlc_errors = []   # csak ezek okoznak 1-es kilépési kódot

def get(endpoint, **params):
    """Kraken nyilvános végpont hívása, 3 próbálkozással. A 'result' objektum egyetlen párkulcsának értékét adja vissza."""
    q = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{API}{endpoint}?{q}"
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20) as r:
                j = json.load(r)
            if j.get("error"):
                raise RuntimeError(";".join(j["error"]))
            res = j["result"]
            keys = [k for k in res if k != "last"]
            return keys[0], res[keys[0]]
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{endpoint} {q}: {last}")

def now():
    return dt.datetime.now(dt.timezone.utc)

def append(path, header, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerows(rows)

OHLC_HDR = ["ts", "time_utc", "open", "high", "low", "close", "vwap", "volume", "count"]

def merge_ohlc(path, candles, interval, t_now):
    """Csak lezárt gyertyák; meglévő fájllal összefésül időbélyeg szerint (az új érték felülír)."""
    rows = {}
    if os.path.exists(path):
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                rows[int(r["ts"])] = [r[h] for h in OHLC_HDR]
    cutoff = t_now.timestamp()
    for c in candles:
        ts = int(c[0])
        if ts + interval * 60 > cutoff:
            continue  # még nyitott gyertya
        iso = dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows[ts] = [str(ts), iso] + [str(x) for x in c[1:8]]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f); w.writerow(OHLC_HDR)
        for ts in sorted(rows):
            w.writerow(rows[ts])
    os.replace(tmp, path)
    return len(rows)

def run():
    t = now(); stamp = t.strftime("%Y-%m-%dT%H:%M:%SZ"); month = t.strftime("%Y-%m")
    ticker_rows, depth_rows = [], []
    for p in PAIRS:
        try:
            name, tk = get("Ticker", pair=p)
            bid, ask = float(tk["b"][0]), float(tk["a"][0]); mid = (bid + ask) / 2
            ticker_rows.append([stamp, p, name, bid, ask, mid, round((ask - bid) / mid * 100, 5),
                                tk["c"][0], tk["p"][1], tk["v"][1]])
        except Exception as e:  # noqa: BLE001
            errors.append(f"{stamp} Ticker {p}: {e}")
        try:
            name, ob = get("Depth", pair=p, count=100)
            bids = [(float(x[0]), float(x[1])) for x in ob["bids"]]
            asks = [(float(x[0]), float(x[1])) for x in ob["asks"]]
            mid = (bids[0][0] + asks[0][0]) / 2
            bd = sum(px * v for px, v in bids if px >= mid * (1 - DEPTH_BAND))
            ad = sum(px * v for px, v in asks if px <= mid * (1 + DEPTH_BAND))
            depth_rows.append([stamp, p, bids[0][0], asks[0][0],
                               round((asks[0][0] - bids[0][0]) / mid * 100, 5), round(bd, 2), round(ad, 2)])
        except Exception as e:  # noqa: BLE001
            errors.append(f"{stamp} Depth {p}: {e}")
        for iv in INTERVALS:
            try:
                name, candles = get("OHLC", pair=p, interval=iv)
                merge_ohlc(os.path.join(ROOT, "ohlc", str(iv), f"{p}.csv"), candles, iv, t)
            except Exception as e:  # noqa: BLE001
                msg = f"{stamp} OHLC {p} {iv}: {e}"
                errors.append(msg); ohlc_errors.append(msg)
    if ticker_rows:
        append(os.path.join(ROOT, "ticker", f"{month}.csv"),
               ["fetched_utc", "pair", "kraken_key", "bid", "ask", "mid", "spread_pct", "last", "vwap_24h", "volume_24h"],
               ticker_rows)
    if depth_rows:
        append(os.path.join(ROOT, "depth", f"{month}.csv"),
               ["fetched_utc", "pair", "best_bid", "best_ask", "spread_pct", "bid_depth_eur_0.5pct", "ask_depth_eur_0.5pct"],
               depth_rows)
    if errors:
        append_err = os.path.join(ROOT, "errors.log")
        os.makedirs(ROOT, exist_ok=True)
        with open(append_err, "a") as f:
            f.write("\n".join(errors) + "\n")
        print("\n".join(errors), file=sys.stderr)
    if ohlc_errors:
        return 1
    print(f"OK {stamp}: {len(ticker_rows)} ticker, {len(depth_rows)} depth")
    return 0

if __name__ == "__main__":
    sys.exit(run())
