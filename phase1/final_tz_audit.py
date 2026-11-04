"""Phase 1 - House 1 timestamp audit (evidence for the canonical UTC conversion in run_cleaning.py).

Run:  python3 phase1/final_tz_audit.py [--out DIR]
Writes outputs/tables/phase1/08_timestamp_alignment_checks.csv, 09_part1_part2_daily_clock_offset.csv and
15_official_clean_dst_signatures.csv (reads the official cleaned files; never modifies them).

Method: 1-minute mean Aggregate power is built for (a) the official REFIT cleaned file, (b) each raw file on its
canonical UTC axis and (c) each raw file on its stored Unix axis. The lag (in minutes) that maximises the correlation
between two series shows how their clocks relate. Best_Lag_Min = L means series A at label t matches series B at
label t - L, i.e. A's clock reads L minutes later than B's for the same moment. Lag 0 means the same clock.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_cleaning as rc  # noqa: E402

PROJECT = rc.ROOT
CLEAN_PATH = PROJECT / "Data/CLEAN_REFIT_081116/CLEAN_House1.csv"


def minute_series(seconds: np.ndarray, values: np.ndarray, t0: int, n: int) -> np.ndarray:
    """Dense 1-minute mean array on the grid t0 + 60*k (NaN where no reading)."""
    k = (seconds.astype("int64") - t0) // 60
    ok = (k >= 0) & (k < n) & ~np.isnan(values)
    s = np.bincount(k[ok], weights=values[ok], minlength=n)
    c = np.bincount(k[ok], minlength=n)
    out = np.full(n, np.nan)
    out[c > 0] = s[c > 0] / c[c > 0]
    return out


def corr_at_lag(x: np.ndarray, y: np.ndarray, i0: int, i1: int, lag: int) -> tuple[float, int]:
    """Correlation of x[i] with y[i - lag] for i in [i0, i1)."""
    a = x[i0:i1]
    b = y[i0 - lag:i1 - lag]
    ok = ~np.isnan(a) & ~np.isnan(b)
    if ok.sum() < 300:
        return np.nan, int(ok.sum())
    return float(np.corrcoef(a[ok], b[ok])[0, 1]), int(ok.sum())


def best_lag(x, y, i0, i1, lags):
    r = [(L, *corr_at_lag(x, y, i0, i1, L)) for L in lags]
    r = [t for t in r if not np.isnan(t[1])]
    if not r:
        return None
    L, c, n = max(r, key=lambda t: t[1])
    d = {L_: c_ for L_, c_, _ in r}
    return {"Best_Lag_Min": L, "Corr_Best": round(c, 3), "Corr_Lag0": round(d.get(0, np.nan), 3),
            "Corr_Lag_Plus60": round(d.get(60, np.nan), 3), "Corr_Lag_Minus60": round(d.get(-60, np.nan), 3), "Minutes": n}


OFFICIAL_HOUSES = [1, 2, 5, 7, 8, 9, 15]  # House 1 plus the houses used in Phases 2-4 (read only)


def official_dst_signatures() -> pd.DataFrame:
    """Rows per UTC hour in the official cleaned files around each UK clock change. On a true UTC axis every hour
    has normal density. A UK-local axis shows an empty 01:00 UTC hour on the spring date and a doubled 01:00 UTC
    hour on the autumn date."""
    events = [("2013-10-27", "autumn"), ("2014-03-30", "spring"), ("2014-10-26", "autumn"), ("2015-03-29", "spring")]
    rows = []
    for h in OFFICIAL_HOUSES:
        u = pd.read_csv(PROJECT / f"Data/CLEAN_REFIT_081116/CLEAN_House{h}.csv", usecols=["Unix"]).Unix.to_numpy()
        for d, kind in events:
            a = int(pd.Timestamp(d, tz="UTC").timestamp())
            cnt = {H: int(((u >= a + H * 3600) & (u < a + (H + 1) * 3600)).sum()) for H in (-1, 0, 1, 2)}
            normal = np.median([cnt[-1], cnt[2]])
            if normal < 100:
                sig = "no data"
            elif cnt[1] < 0.1 * normal:
                sig = "empty 01:00 UTC hour (local-clock spring jump)"
            elif cnt[1] > 1.5 * normal:
                sig = "doubled 01:00 UTC hour (local-clock repeated hour)"
            elif cnt[0] < 0.1 * normal:
                sig = "empty 00:00 UTC hour"
            else:
                sig = "normal density (consistent with UTC)"
            rows.append({"House": h, "Date": d, "Change": kind, "Rows_23h_prev_day": cnt[-1], "Rows_00h_UTC": cnt[0],
                         "Rows_01h_UTC": cnt[1], "Rows_02h_UTC": cnt[2], "Signature": sig})
    return pd.DataFrame(rows)


def run(out_root=PROJECT, norm: dict | None = None, verbose: bool = True, write: bool = True) -> dict:
    if norm is None:
        raw = rc.load_raw()
        norm = {p: rc.normalise_timestamps(raw[p])[0] for p in raw}
    clean = pd.read_csv(CLEAN_PATH, usecols=["Unix", "Aggregate"])
    t0 = (int(min(n.utc.min() for n in norm.values())) // 60) * 60 - 7200
    t1 = int(max(n.unix_raw.max() for n in norm.values())) + 7200
    n_min = (t1 - t0) // 60 + 1
    off = clean.Unix.to_numpy()
    ser = {"official": minute_series(off, clean.Aggregate.to_numpy(float), t0, n_min)}
    for p, n in norm.items():
        k = n.keep.to_numpy()
        ser[f"p{p}_utc"] = minute_series(n.utc.to_numpy()[k], n.Aggregate.to_numpy(float)[k], t0, n_min)
        ser[f"p{p}_stored"] = minute_series(n.unix_raw.to_numpy(), n.Aggregate.to_numpy(float), t0, n_min)
    idx = lambda ts: (int(pd.Timestamp(ts, tz="UTC").timestamp()) - t0) // 60
    lags = range(-90, 91)

    rows = []
    months = pd.date_range("2013-10-01", "2015-07-01", freq="MS")
    for ms in months:
        me = ms + pd.offsets.MonthBegin(1)
        part = 1 if me.tz_localize("UTC").timestamp() <= rc.HANDOFF_UTC + 86400 * 5 else 2
        i0, i1 = max(idx(ms), 200), min(idx(me), n_min - 200)
        for axis in ("utc", "stored"):
            b = best_lag(ser["official"], ser[f"p{part}_{axis}"], i0, i1, lags)
            if b:
                rows.append({"Comparison": f"Official CLEAN_House1 vs raw Part {part} ({'canonical UTC' if axis == 'utc' else 'stored Unix'})",
                             "Period": ms.strftime("%Y-%m"), "Clock_Regime": "BST" if pd.Timestamp(ms + pd.Timedelta(days=14)).tz_localize(rc.TZ).utcoffset().seconds else "GMT",
                             **b})
    ov_s, ov_e = "2014-06-28", "2014-10-01"
    for s, e in [("2014-06-28", "2014-07-15"), ("2014-07-15", "2014-08-01"), ("2014-08-01", "2014-08-28"),
                 ("2014-09-01", "2014-09-27"), ("2014-09-27", "2014-10-01")]:
        b = best_lag(ser["p1_utc"], ser["p2_utc"], idx(s), idx(e), lags)
        if b:
            rows.append({"Comparison": "Raw Part 1 vs raw Part 2 (both canonical UTC)", "Period": f"{s} to {e}",
                         "Clock_Regime": "BST", **b})
    t08 = pd.DataFrame(rows)

    daily = []
    for day in pd.date_range(ov_s, ov_e, inclusive="left"):
        b = best_lag(ser["p1_utc"], ser["p2_utc"], idx(day), idx(day + pd.Timedelta(days=1)), range(-15, 16))
        if b and b["Minutes"] >= 600:
            daily.append({"Date": str(day.date()), "Part1_Minus_Part2_Clock_Min": b["Best_Lag_Min"],
                          "Corr_Best": b["Corr_Best"], "Corr_Lag0": b["Corr_Lag0"], "Minutes": b["Minutes"]})
    t09 = pd.DataFrame(daily)

    t15 = official_dst_signatures()

    if not write:
        return {"t08": t08, "t09": t09, "t15": t15}
    p = rc.out_paths(out_root)["tables"]
    p.mkdir(parents=True, exist_ok=True)
    t08.to_csv(p / "08_timestamp_alignment_checks.csv", index=False)
    t09.to_csv(p / "09_part1_part2_daily_clock_offset.csv", index=False)
    t15.to_csv(p / "15_official_clean_dst_signatures.csv", index=False)
    if verbose:
        print("Saved", rc.rel(p / "15_official_clean_dst_signatures.csv"))
        print("Saved", rc.rel(p / "08_timestamp_alignment_checks.csv"))
        print("Saved", rc.rel(p / "09_part1_part2_daily_clock_offset.csv"))
    return {"t08": t08, "t09": t09, "t15": t15}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="House 1 timestamp audit")
    ap.add_argument("--out", default=str(PROJECT))
    a = ap.parse_args()
    r = run(a.out)
    pd.set_option("display.width", 220)
    print(r["t08"].to_string(index=False))
    print(r["t09"].to_string(index=False))
