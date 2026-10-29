#!/usr/bin/env python3
"""Medusa D-006 – volatilitás-előrejelző modul (log-HAR) és benchmarkok.

Hiteles leírás: claude/medusa-d006-tervezet.md (jóváhagyva 2026-10-29).
A 'forecast' és az 'evaluate' mód csak a Python standard könyvtárát használja (GitHub Actions-ben is fut);
a 'fit' mód numpy-t és scipy-t igényel, és csak egyszer, a tanulómintán fut.

Használat:
  python medusa_vol_har.py fit      <BTC_1h.csv> <ETH_1h.csv> <params_out.json>
  python medusa_vol_har.py forecast <params.json> <BTC_1h.csv> <ETH_1h.csv> <BTC_1d.csv|-> <ETH_1d.csv|-> <origin YYYY-MM-DD|last>
  python medusa_vol_har.py evaluate <params.json> <BTC_1h.csv> <ETH_1h.csv> <BTC_1d.csv|-> <ETH_1d.csv|-> <első cél-nap> <utolsó cél-nap>
Az 1h CSV-ben 'time_utc' és 'close' oszlop kell (Binance és Kraken formátum is). Az 1d CSV (Kraken napi) a GARCH napi
hozamaihoz; '-' esetén a napi záróár az órás adatból (a nap utolsó órás gyertyájának záróára) számolódik.

Megvalósítási részletek (a D-006 végrehajtási pontosításai, eredmény megtekintése előtt rögzítve, v1.0):
  - órás hozam: log(záró_i / záró_{i-1}) egymást követő gyertyák között, a későbbi gyertya napjához rendelve;
  - érvényes nap: legalább 22 órás gyertya (D-006 1. pont);
  - HAR-bemenetek a t. napon: RV(t) (t-nek érvényesnek kell lennie); RV7 = az érvényes napok átlaga t-6..t (≥ 5 érvényes);
    RV30 = az érvényes napok átlaga t-29..t (≥ 25 érvényes);
  - h = 7 cél: az érvényes napok RV-átlaga t+1..t+7 (≥ 5 érvényes);
  - GARCH(1,1): napi záróár-log-hozam ×100, nulla várható érték, normális likelihood; h-lépéses előrejelzés a
    feltétel nélküli varianciához tartó képlettel; h = 7 esetén az 1..7 lépéses előrejelzések átlaga;
  - QLIKE = RV/F − ln(RV/F) − 1; MSE = (RV − F)²;
  - Diebold–Mariano: d = L(benchmark) − L(HAR), egyoldali, Newey–West (Bartlett), késleltetés = h − 1.
"""
import csv, datetime as dt, json, math, sys

VERSION = "v1.0 (2026-10-29)"
H = (1, 7)


def load_hourly(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append((r["time_utc"], float(r["close"])))
    rows.sort()
    return rows


def daily_rv(hourly):
    """{nap: (rv, gyertyaszám)}"""
    acc = {}
    prev = None
    for t, c in hourly:
        d = t[:10]
        a = acc.setdefault(d, [0.0, 0])
        a[1] += 1
        if prev is not None and prev > 0 and c > 0:
            a[0] += math.log(c / prev) ** 2
        prev = c
    return {d: (v[0], v[1]) for d, v in acc.items()}


def valid_rv(rvd):
    return {d: v[0] for d, v in rvd.items() if v[1] >= 22 and v[0] > 0}


def day(s):
    return dt.date.fromisoformat(s)


def shift(d, k):
    return (day(d) + dt.timedelta(days=k)).isoformat()


def har_inputs(rv, t):
    if t not in rv:
        return None
    w = [rv[x] for x in (shift(t, -k) for k in range(7)) if x in rv]
    m = [rv[x] for x in (shift(t, -k) for k in range(30)) if x in rv]
    if len(w) < 5 or len(m) < 25:
        return None
    return rv[t], sum(w) / len(w), sum(m) / len(m)


def target(rv, t, h):
    if h == 1:
        return rv.get(shift(t, 1))
    v = [rv[x] for x in (shift(t, k) for k in range(1, 8)) if x in rv]
    return sum(v) / len(v) if len(v) >= 5 else None


def daily_closes_from_hourly(hourly):
    out = {}
    for t, c in hourly:
        out[t[:10]] = c  # rendezett, így a nap utolsó gyertyája marad
    return out


def daily_closes_from_daily(path):
    out = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            out[r["time_utc"][:10]] = float(r["close"])
    return out


def daily_returns_pct(closes):
    ds = sorted(closes)
    out = {}
    for a, b in zip(ds, ds[1:]):
        if (day(b) - day(a)).days == 1:
            out[b] = 100 * math.log(closes[b] / closes[a])
    return out


def garch_filter(p, rets):
    """{nap t: sigma²(t+1) előrejelzés a t. nap zárásakor}, %² egységben."""
    w, a, b = p["omega"], p["alpha"], p["beta"]
    v = w / (1 - a - b)
    s2 = v
    out = {}
    for d in sorted(rets):
        s2 = w + a * rets[d] ** 2 + b * s2  # sigma²(d+1)
        out[d] = s2
    return out


def garch_h(p, s2_next, h):
    w, a, b = p["omega"], p["alpha"], p["beta"]
    v = w / (1 - a - b)
    f = [v + (a + b) ** (k - 1) * (s2_next - v) for k in range(1, h + 1)]
    return sum(f) / len(f) / 10000.0  # %² -> log-hozam²


def har_forecast(c, s2, x):
    rv1, rv7, rv30 = x
    z = c[0] + c[1] * math.log(rv1) + c[2] * math.log(rv7) + c[3] * math.log(rv30)
    return math.exp(z + 0.5 * s2)


def forecasts(params, asset, rv, gf, t):
    x = har_inputs(rv, t)
    if x is None or t not in gf:
        return None
    out = {}
    for h in H:
        m = params["har"][asset][str(h)]
        out[h] = {
            "HAR": har_forecast(m["coef"], m["s2"], x),
            "B1": x[0] if h == 1 else x[1],
            "B2": x[2],
            "B3": garch_h(params["garch"][asset], gf[t], h),
        }
    return out


def qlike(y, f):
    r = y / f
    return r - math.log(r) - 1


def dm_test(d, lag):
    n = len(d)
    mu = sum(d) / n
    e = [x - mu for x in d]
    s = sum(x * x for x in e) / n
    for k in range(1, lag + 1):
        g = sum(e[i] * e[i - k] for i in range(k, n)) / n
        s += 2 * (1 - k / (lag + 1)) * g
    stat = mu / math.sqrt(s / n)
    p = 0.5 * math.erfc(stat / math.sqrt(2))  # egyoldali: H1 mean(d) > 0
    return stat, p, n


# ---------- fit (numpy/scipy) ----------
def fit(btc, eth, out):
    import numpy as np
    from scipy.optimize import minimize
    T0, T1 = "2018-01-01", "2024-10-31"
    params = {"version": VERSION, "training": [T0, T1], "har": {}, "garch": {}, "strongest_benchmark": {}, "training_qlike": {}}
    for asset, path in (("BTC", btc), ("ETH", eth)):
        hourly = [x for x in load_hourly(path) if T0 <= x[0][:10] <= T1]
        rv = valid_rv(daily_rv(hourly))
        # GARCH
        rets = {d: r for d, r in daily_returns_pct(daily_closes_from_hourly(hourly)).items() if T0 <= d <= T1}
        R = np.array([rets[d] for d in sorted(rets)])

        def nll(th):
            w, a, b = th
            if w <= 0 or a < 0 or b < 0 or a + b >= 0.9999:
                return 1e12
            s2 = np.empty_like(R)
            s2[0] = R.var()
            for i in range(1, len(R)):
                s2[i] = w + a * R[i - 1] ** 2 + b * s2[i - 1]
            return 0.5 * np.sum(np.log(2 * np.pi * s2) + R ** 2 / s2)

        best = None
        for start in ((0.1 * R.var(), 0.1, 0.85), (0.05 * R.var(), 0.05, 0.9), (0.2 * R.var(), 0.15, 0.7)):
            res = minimize(nll, start, method="Nelder-Mead", options={"maxiter": 4000, "xatol": 1e-8, "fatol": 1e-8})
            if best is None or res.fun < best.fun:
                best = res
        w, a, b = (float(x) for x in best.x)
        params["garch"][asset] = {"omega": w, "alpha": a, "beta": b, "nll": float(best.fun), "n": int(len(R))}
        gf = garch_filter(params["garch"][asset], rets)
        params["har"][asset] = {}
        params["strongest_benchmark"][asset] = {}
        params["training_qlike"][asset] = {}
        allkeys = {}
        for h in H:
            X, Y, keys = [], [], []
            for t in sorted(rv):
                if shift(t, h) > T1:
                    continue
                x = har_inputs(rv, t)
                y = target(rv, t, h)
                if x is None or y is None:
                    continue
                X.append([1.0, math.log(x[0]), math.log(x[1]), math.log(x[2])])
                Y.append(math.log(y))
                keys.append(t)
            X, Y = np.array(X), np.array(Y)
            coef, *_ = np.linalg.lstsq(X, Y, rcond=None)
            resid = Y - X @ coef
            s2 = float(resid @ resid / (len(Y) - 4))
            params["har"][asset][str(h)] = {"coef": [float(c) for c in coef], "s2": s2, "n": int(len(Y)),
                                            "r2_log": float(1 - resid.var() / Y.var()), "first_origin": keys[0], "last_origin": keys[-1]}
            allkeys[h] = keys
        for h in H:
            keys = allkeys[h]
            losses = {"HAR": [], "B1": [], "B2": [], "B3": []}
            for t in keys:
                if t not in gf:
                    continue
                fc = forecasts(params, asset, rv, gf, t)[h]
                y = target(rv, t, h)
                for k in losses:
                    losses[k].append(qlike(y, fc[k]))
            mean = {k: sum(v) / len(v) for k, v in losses.items()}
            params["training_qlike"][asset][str(h)] = {**mean, "n": len(losses["HAR"])}
            params["strongest_benchmark"][asset][str(h)] = min(("B1", "B2", "B3"), key=lambda k: mean[k])
    with open(out, "w") as f:
        json.dump(params, f, indent=2, ensure_ascii=False)
    print(json.dumps(params, indent=2, ensure_ascii=False))


def prep(params, asset, p1h, p1d):
    hourly = load_hourly(p1h)
    rv = valid_rv(daily_rv(hourly))
    closes = daily_closes_from_daily(p1d) if p1d != "-" else daily_closes_from_hourly(hourly)
    gf = garch_filter(params["garch"][asset], daily_returns_pct(closes))
    return rv, gf


def main(argv):
    mode = argv[1]
    if mode == "fit":
        return fit(argv[2], argv[3], argv[4])
    params = json.load(open(argv[2]))
    data = {"BTC": prep(params, "BTC", argv[3], argv[5]), "ETH": prep(params, "ETH", argv[4], argv[6])}
    if mode == "forecast":
        w = csv.writer(sys.stdout)
        w.writerow(["origin_day", "target_days", "asset", "h", "HAR", "B1", "B2", "B3", "model_version"])
        for asset, (rv, gf) in data.items():
            t = argv[7] if argv[7] != "last" else max(rv)
            fc = forecasts(params, asset, rv, gf, t)
            if fc is None:
                print(f"# {asset} {t}: nincs elég érvényes adat", file=sys.stderr)
                continue
            for h in H:
                w.writerow([t, f"{shift(t, 1)}..{shift(t, h)}", asset, h] + [f"{fc[h][k]:.6e}" for k in ("HAR", "B1", "B2", "B3")] + [params["version"]])
        return 0
    if mode == "evaluate":
        first, last = argv[7], argv[8]
        res = {}
        for asset, (rv, gf) in data.items():
            for h in H:
                L = {"HAR": [], "B1": [], "B2": [], "B3": []}
                M = {"HAR": [], "B1": [], "B2": [], "B3": []}
                t = shift(first, -1)
                while shift(t, h) <= last:
                    fc = forecasts(params, asset, rv, gf, t)
                    y = target(rv, t, h)
                    if fc is not None and y is not None:
                        for k in L:
                            L[k].append(qlike(y, fc[h][k]))
                            M[k].append((y - fc[h][k]) ** 2)
                    t = shift(t, 1)
                sb = params["strongest_benchmark"][asset][str(h)]
                d = [b - a for a, b in zip(L["HAR"], L[sb])]
                stat, p, n = dm_test(d, h - 1)
                res[f"{asset} h={h}"] = {"n": n, "qlike": {k: sum(v) / len(v) for k, v in L.items()},
                                         "mse": {k: sum(v) / len(v) for k, v in M.items()},
                                         "strongest": sb, "dm_stat": stat, "p_one_sided": p}
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
