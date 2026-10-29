#!/usr/bin/env python3
"""Medusa D-006 – napi volatilitás-előrejelzések naplózása (csak standard könyvtár).

Minden futáskor pótolja a hiányzó előrejelzéseket 2026-10-28-as kiinduló naptól a tegnapi (lezárt) UTC-napig,
a rögzített paraméterekkel (data/d006/medusa_d006_params.json), két forrásból:
  kraken  – elsődleges, döntő (EUR, data/ohlc/60 és data/ohlc/1440)
  binance – másodlagos (USDT, data/binance/1h)
Kimenet: data/d006/forecasts.csv (hozzáfűzés). A git-commit ideje igazolja, mikor készült az előrejelzés.
'late' = 1, ha az előrejelzés az első cél-nap vége után készült (utólag számolt sor, E-006).
Az előrejelzés csak a kiinduló nap zárásáig ismert adatot használja: a bemeneteket a kiinduló napra vágjuk.
"""
import csv, datetime as dt, os, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import medusa_vol_har as m  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PARAMS = os.path.join(ROOT, "data", "d006", "medusa_d006_params.json")
OUT = os.path.join(ROOT, "data", "d006", "forecasts.csv")
START = "2026-10-28"
SOURCES = {
    "kraken": {"BTC": ("data/ohlc/60/XBTEUR.csv", "data/ohlc/1440/XBTEUR.csv"),
               "ETH": ("data/ohlc/60/ETHEUR.csv", "data/ohlc/1440/ETHEUR.csv")},
    "binance": {"BTC": ("data/binance/1h/BTCUSDT.csv", None),
                "ETH": ("data/binance/1h/ETHUSDT.csv", None)},
}
HDR = ["created_utc", "late", "source", "origin_day", "target_days", "asset", "h", "HAR", "B1", "B2", "B3", "model_version"]


def main():
    import json
    params = json.load(open(PARAMS))
    now = dt.datetime.now(dt.timezone.utc)
    yesterday = (now.date() - dt.timedelta(days=1)).isoformat()
    done = set()
    if os.path.exists(OUT):
        with open(OUT, newline="") as f:
            for r in csv.DictReader(f):
                done.add((r["source"], r["origin_day"], r["asset"], r["h"]))
    rows = []
    for src, assets in SOURCES.items():
        for asset, (p1h, p1d) in assets.items():
            p1h = os.path.join(ROOT, p1h)
            if not os.path.exists(p1h):
                continue
            hourly_all = m.load_hourly(p1h)
            daily_all = m.daily_closes_from_daily(os.path.join(ROOT, p1d)) if p1d else None
            t = START
            while t <= yesterday:
                if all((src, t, asset, str(h)) in done for h in m.H):
                    t = m.shift(t, 1)
                    continue
                hourly = [x for x in hourly_all if x[0][:10] <= t]  # csak a kiinduló nap zárásáig
                rv = m.valid_rv(m.daily_rv(hourly))
                closes = {d: c for d, c in daily_all.items() if d <= t} if daily_all else m.daily_closes_from_hourly(hourly)
                gf = m.garch_filter(params["garch"][asset], m.daily_returns_pct(closes))
                fc = m.forecasts(params, asset, rv, gf, t)
                if fc is not None:
                    late = int(now.date().isoformat() > m.shift(t, 1))
                    for h in m.H:
                        rows.append([now.strftime("%Y-%m-%dT%H:%M:%SZ"), late, src, t, f"{m.shift(t, 1)}..{m.shift(t, h)}", asset, h]
                                    + [f"{fc[h][k]:.6e}" for k in ("HAR", "B1", "B2", "B3")] + [params["version"]])
                else:
                    print(f"{src} {asset} {t}: nincs elég érvényes adat (kihagyva)", file=sys.stderr)
                t = m.shift(t, 1)
    if rows:
        os.makedirs(os.path.dirname(OUT), exist_ok=True)
        new = not os.path.exists(OUT)
        with open(OUT, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(HDR)
            w.writerows(rows)
    print(f"{len(rows)} új előrejelzés-sor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
