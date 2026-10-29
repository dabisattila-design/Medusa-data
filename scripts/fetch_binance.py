#!/usr/bin/env python3
"""Medusa D-006 – Binance nyilvános archívum (data.binance.vision), spot 1h klines.

Csak a Python standard könyvtárát használja. Kulcs nem kell.
Eszközök: BTCUSDT, ETHUSDT (D-006, 1. pont). Kezdet: 2018-01 (D-006, 2. pont).

Mit csinál:
  - minden lezárt hónapra a havi zip-et tölti le; ha az még nincs közzétéve, az adott hónap napi zip-jeit;
  - a folyó hónapra a tegnapig lezárt napok napi zip-jeit;
  - minden fájlt a hozzá tartozó .CHECKSUM (SHA-256) alapján ellenőriz; eltérésnél nem menti;
  - a már letöltött fájlokat a manifest alapján kihagyja (ismételt futtatás biztonságos);
  - az eredményt eszközönként egy CSV-be fésüli: data/binance/1h/<SYMBOL>.csv
  - napló: data/binance/manifest.csv (fájl, sha256, sorok, lekérés ideje), hibák: data/binance/errors.log
Időbélyeg: a Binance 2025-től a spot adatban mikroszekundumot is használhat; a szkript mindkettőt kezeli, a kimenet ms + ISO UTC.
Kilépési kód: 1, ha bármilyen hiba volt (a sikeres részek ekkor is mentésre kerülnek).
"""
import csv, datetime as dt, hashlib, io, os, sys, time, urllib.error, urllib.request, zipfile

BASE = "https://data.binance.vision/data/spot"
SYMBOLS = ["BTCUSDT", "ETHUSDT"]
INTERVAL = "1h"
START = dt.date(2018, 1, 1)
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "binance")
UA = {"User-Agent": "medusa-research-data/1.0 (academic, public archive only)"}
HDR = ["open_time_ms", "time_utc", "open", "high", "low", "close", "volume", "quote_volume", "trades"]
errors = []


def fetch(url):
    """Visszaadja a bájtokat; 404 esetén None. 3 próbálkozás."""
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            last = e
        except Exception as e:  # noqa: BLE001
            last = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{url}: {last}")


def get_verified(path):
    """path: pl. monthly/klines/BTCUSDT/1h/BTCUSDT-1h-2018-01.zip -> (bájtok, sha256) vagy None, ha nincs közzétéve."""
    data = fetch(f"{BASE}/{path}")
    if data is None:
        return None
    chk = fetch(f"{BASE}/{path}.CHECKSUM")
    if chk is None:
        raise RuntimeError(f"{path}: hiányzó CHECKSUM")
    expected = chk.decode().split()[0].lower()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise RuntimeError(f"{path}: SHA-256 eltérés ({actual} != {expected})")
    return data, actual


def to_ms(x):
    v = int(float(x))
    if v > 10**14:      # mikroszekundum
        v //= 1000
    return v


def parse(zbytes):
    rows = []
    with zipfile.ZipFile(io.BytesIO(zbytes)) as z:
        for name in z.namelist():
            if not name.endswith(".csv"):
                continue
            for r in csv.reader(io.TextIOWrapper(z.open(name), encoding="utf-8")):
                if not r or not r[0].strip().lstrip("-").isdigit():
                    continue  # esetleges fejléc
                ms = to_ms(r[0])
                iso = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                rows.append([str(ms), iso, r[1], r[2], r[3], r[4], r[5], r[7], r[8]])
    return rows


def load_manifest(path):
    done = set()
    if os.path.exists(path):
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                done.add(r["file"])
    return done


def months(start, end_excl):
    y, m = start.year, start.month
    while (y, m) < (end_excl.year, end_excl.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def main():
    today = dt.datetime.now(dt.timezone.utc).date()
    os.makedirs(os.path.join(ROOT, INTERVAL), exist_ok=True)
    man_path = os.path.join(ROOT, "manifest.csv")
    done = load_manifest(man_path)
    new_manifest = []
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for sym in SYMBOLS:
        out_path = os.path.join(ROOT, INTERVAL, f"{sym}.csv")
        store = {}
        if os.path.exists(out_path):
            with open(out_path, newline="") as f:
                for r in csv.DictReader(f):
                    store[int(r["open_time_ms"])] = [r[h] for h in HDR]
        before = len(store)

        def take(path):
            if path in done:
                return True
            try:
                res = get_verified(path)
            except Exception as e:  # noqa: BLE001
                errors.append(f"{stamp} {path}: {e}")
                return True  # hiba, de ne próbáljon helyette napi fájlokat
            if res is None:
                return False
            data, sha = res
            rows = parse(data)
            for r in rows:
                store[int(r[0])] = r
            new_manifest.append([path, sha, len(rows), stamp])
            done.add(path)
            return True

        first_of_month = today.replace(day=1)
        for y, m in months(START, first_of_month):
            tag = f"{y:04d}-{m:02d}"
            if take(f"monthly/klines/{sym}/{INTERVAL}/{sym}-{INTERVAL}-{tag}.zip"):
                continue
            # a havi fájl még nincs közzétéve -> napi fájlok
            d = dt.date(y, m, 1)
            while d.month == m:
                take(f"daily/klines/{sym}/{INTERVAL}/{sym}-{INTERVAL}-{d.isoformat()}.zip")
                d += dt.timedelta(days=1)
        d = first_of_month
        while d < today:  # csak lezárt napok
            take(f"daily/klines/{sym}/{INTERVAL}/{sym}-{INTERVAL}-{d.isoformat()}.zip")
            d += dt.timedelta(days=1)

        tmp = out_path + ".tmp"
        with open(tmp, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(HDR)
            for k in sorted(store):
                w.writerow(store[k])
        os.replace(tmp, out_path)
        print(f"{sym}: {before} -> {len(store)} sor")

    if new_manifest:
        new = not os.path.exists(man_path)
        with open(man_path, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["file", "sha256", "rows", "fetched_utc"])
            w.writerows(new_manifest)
    if errors:
        with open(os.path.join(ROOT, "errors.log"), "a") as f:
            f.write("\n".join(errors) + "\n")
        print("\n".join(errors[:20]), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
