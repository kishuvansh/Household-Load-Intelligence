"""Evidence for the clock correction applied to the official REFIT cleaned files (phase2/wm_eda.py).

Run after Phases 2-4:  python3 audit/official_timestamp_check.py
Reads the official and raw REFIT files and the Phase 2-4 outputs; changes nothing. Writes outputs/tables/audit/:
  01_official_vs_raw_clock_by_month.csv  how the PUBLISHED official files relate to the raw UK wall clock, per month
  02_corrected_share_by_phase.csv        share of each phase's analysed data that the correction re-timed
  03_part2_switch_verification.csv       the per-house switch instants used by the correction, re-derived from raw data
  04_behaviour_check.csv                 daily load profile, BST 2015 vs BST 2014, as published vs corrected
  05_event_trace.csv                     one washing-machine heater switch-on traced through raw, official and both conversions

Method for 01: 1-minute mean Aggregate power of the official file is cross-correlated with the raw file on its stored
clock. Lag 0 = official timestamps equal the raw local clock; -60 min = converted to UTC.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
for sub in ("phase1", "phase2", "phase4"):
    sys.path.insert(0, str(ROOT / sub))
import final_tz_audit as fa  # noqa: E402  (minute_series, best_lag)
import wm_eda as p2  # noqa: E402  (PART2_SWITCH_UTC, correct_official_clock)

HOUSES = [2, 5, 7, 8, 9, 15]
OUT = ROOT / "outputs/tables/audit"
BST_2014_END_UTC = pd.Timestamp("2014-10-26 01:00", tz="UTC")
BST_2015_START_UTC = pd.Timestamp("2015-03-29 01:00", tz="UTC")
U = lambda s: int(pd.Timestamp(s, tz="UTC").timestamp())  # noqa: E731


def raw_path(h: int, part: int) -> Path:
    d = ROOT / "Data/REFIT_RAW_081116"
    p = d / f"RAW_House{h}_Part{part}.csv"
    return p if p.exists() else d / f"RAW_House_{h}_Part{part}.csv"


def official(h: int, cols=("Unix", "Aggregate")) -> pd.DataFrame:
    return pd.read_csv(ROOT / f"Data/CLEAN_REFIT_081116/CLEAN_House{h}.csv", usecols=list(cols))


def corrected_periods(h: int) -> list:
    """Periods (in corrected UTC) whose timestamps the correction changed: BST data built from raw Part 2."""
    s = pd.Timestamp(p2.PART2_SWITCH_UTC[h], tz="UTC")
    return [(s, BST_2014_END_UTC), (BST_2015_START_UTC, pd.Timestamp("2016-01-01", tz="UTC"))]


def in_periods(ts, periods) -> np.ndarray:
    ts = pd.DatetimeIndex(ts)
    return np.any([(ts >= a) & (ts < b) for a, b in periods], axis=0)


def clock_by_month() -> pd.DataFrame:
    rows = []
    for h in HOUSES:
        off = official(h)
        switch = pd.Timestamp(p2.PART2_SWITCH_UTC[h], tz="UTC")
        t0 = (int(off.Unix.min()) // 60) * 60 - 7200
        n = (int(off.Unix.max()) - t0) // 60 + 7200
        so = fa.minute_series(off.Unix.to_numpy(), off.Aggregate.to_numpy(float), t0, n)
        sr = {}
        for part in (1, 2):
            r = pd.read_csv(raw_path(h, part), usecols=["Unix", "Aggregate"])
            sr[part] = fa.minute_series(r.Unix.to_numpy(), r.Aggregate.to_numpy(float), t0, n)
        idx = lambda ts: (int(ts.timestamp()) - t0) // 60  # noqa: E731
        for ms in pd.date_range(pd.to_datetime(t0, unit="s", utc=True).normalize().replace(day=1),
                                pd.to_datetime(int(off.Unix.max()), unit="s", utc=True), freq="MS"):
            me = ms + pd.offsets.MonthBegin(1)
            part = 1 if me <= switch + pd.Timedelta(days=1) else 2
            b = fa.best_lag(so, sr[part], max(idx(ms), 200), min(idx(me), n - 200), range(-90, 91))
            if not b or b["Minutes"] < 1440:
                continue
            bst = (ms + pd.Timedelta(days=14)).tz_convert(p2.LOCAL_TZ).utcoffset().seconds > 0
            lag = b["Best_Lag_Min"]
            rows.append({"House": h, "Month": ms.strftime("%Y-%m"), "Raw_Part": part, "Clock_Regime": "BST" if bst else "GMT",
                         "Official_Minus_Raw_Stored_Min": lag, "Corr_Best": b["Corr_Best"], "Minutes": b["Minutes"],
                         "Published_Official_Timestamps_Are": ("UTC (converted)" if lag == -60 else
                                                               ("local = UTC in GMT" if not bst and lag == 0 else
                                                                ("UK local clock, not UTC" if lag == 0 else "unclear")))})
    return pd.DataFrame(rows)


def switch_verification() -> pd.DataFrame:
    rows = []
    for h in HOUSES:
        last_p1 = int(pd.read_csv(raw_path(h, 1), usecols=["Unix"]).Unix.max())
        derived = pd.to_datetime(last_p1 - 3600, unit="s")
        used = pd.Timestamp(p2.PART2_SWITCH_UTC[h])
        rows.append({"House": h, "Last_Raw_Part1_Label_UK_Clock": str(pd.to_datetime(last_p1, unit="s")),
                     "Derived_Switch_UTC": str(derived), "Used_In_Correction": str(used), "Match": derived == used})
    return pd.DataFrame(rows)


def mean_profile(t_utc: np.ndarray, agg: np.ndarray, start: str, end: str) -> np.ndarray:
    k = (t_utc >= U(start)) & (t_utc < U(end)) & ~np.isnan(t_utc)
    loc = pd.to_datetime(t_utc[k], unit="s", utc=True).tz_convert(p2.LOCAL_TZ)
    m = pd.Series(agg[k]).groupby(loc.hour * 60 + loc.minute).mean().reindex(range(1440)).interpolate()
    return m.rolling(31, center=True, min_periods=1).mean().to_numpy()


def profile_lag(ref: np.ndarray, x: np.ndarray) -> tuple[int, float]:
    lags = list(range(-120, 121, 5))
    r = [np.corrcoef(ref, np.roll(x, -L))[0, 1] for L in lags]
    i = int(np.nanargmax(r))
    return lags[i], round(float(r[i]), 3)


def behaviour_check() -> pd.DataFrame:
    """Household routines follow the local clock. BST 2014 (built from Part 1, converted correctly) is the reference:
    a correct BST 2015 profile should line up with it at lag ~0, a one-hour-late one at ~+60 min."""
    rows = []
    for h in HOUSES:
        d = official(h)
        a = d.Aggregate.to_numpy(float)
        pub = d.Unix.to_numpy(float)
        cor = p2.correct_official_clock(d.Unix.to_numpy(), h)
        ref = mean_profile(pub, a, "2014-04-01", "2014-07-01")
        lp, rp = profile_lag(ref, mean_profile(pub, a, "2015-04-01", "2015-07-01"))
        lc, rc = profile_lag(ref, mean_profile(cor, a, "2015-04-01", "2015-07-01"))
        rows.append({"House": h, "Lag_As_Published_Min": lp, "Lag_Corrected_Min": lc, "Profile_Corr": rp,
                     "Rows_Dropped_By_Correction": int(np.isnan(cor).sum()),
                     "Reading": ("weak profile, inconclusive" if rp < 0.85 else
                                 ("supports correction: about +60 min as published, about 0 corrected"
                                  if abs(lp - 60) <= 10 and abs(lc) <= 10 else
                                  "correction moves the profile by 60 min as predicted, but a residual offset remains"))})
    return pd.DataFrame(rows)


def event_trace() -> pd.DataFrame:
    """House 7: first washing-machine heater sample (> 1 kW) on a Part 1-era day and a Part 2-era day."""
    h, ch = 7, p2.HOUSES[7]["channel"]
    off = official(h, ("Unix", ch))
    rows = []
    for day, part in (("2014-08-20", 1), ("2015-04-24", 2)):
        r = pd.read_csv(raw_path(h, part), usecols=["Time", "Unix", ch])
        row = r[r.Time.str.startswith(day) & (r[ch] > 1000)].iloc[0]
        o = off[(off[ch] > 1000) & ((off.Unix - row.Unix).abs() <= 4000)].iloc[0]
        cu = p2.correct_official_clock(np.array([o.Unix]), h)[0]
        to_local = lambda s: pd.to_datetime(s, unit="s", utc=True).tz_convert(p2.LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")  # noqa: E731
        rows.append({"House": h, "Raw_Part": part, "Raw_Time_UK_Clock": row.Time, "WM_W": float(row[ch]),
                     "Official_Unix_Label": str(pd.to_datetime(o.Unix, unit="s")),
                     "As_Published_Read_As_UTC_To_Local": to_local(o.Unix),
                     "Corrected_UTC": str(pd.to_datetime(cu, unit="s")), "Corrected_To_Local": to_local(cu)})
    return pd.DataFrame(rows)


def corrected_share() -> pd.DataFrame:
    """Share of each phase's analysed data (now on corrected UTC) that lies in re-timed periods."""
    import phase4_analysis as a  # imports Phase 3 and Phase 2 modules (read only)
    rows = []
    ap = pd.read_csv(ROOT / "outputs/tables/phase2/05_activity_periods_all.csv")
    lk = ap[ap["likely_cycle"]].copy()
    lk["f"] = [bool(in_periods([t], corrected_periods(h))[0]) for t, h in
               zip(pd.to_datetime(lk["start_utc"], utc=True), lk["house"])]
    rows.append({"Phase": 2, "Quantity": "likely cycles", "House": "all", "Total": len(lk),
                 "In_Corrected_Periods": int(lk.f.sum()), "Pct": round(100 * lk.f.mean(), 1)})
    for h, g in lk.groupby("house"):
        rows.append({"Phase": 2, "Quantity": "likely cycles", "House": h, "Total": len(g),
                     "In_Corrected_Periods": int(g.f.sum()), "Pct": round(100 * g.f.mean(), 1)})
    p = pd.read_parquet(ROOT / "Data/processed/phase3/test_predictions.parquet")
    f = np.zeros(len(p), bool)
    for h in p["house"].unique():
        s = (p["house"] == h).to_numpy()
        f[s] = in_periods(p.index[s], corrected_periods(int(h)))
    rows.append({"Phase": 3, "Quantity": "test minutes", "House": "all", "Total": len(p),
                 "In_Corrected_Periods": int(f.sum()), "Pct": round(100 * f.mean(), 1)})
    d = a.p3.prepare()
    tot = aff = 0
    for h in a.HOUSES:
        idx = d["minute"][h].index[d["flags"][h]["usable"].to_numpy()]
        w = a.window_mask(idx) & (idx.tz_convert(p2.LOCAL_TZ).dayofweek < 5)
        k = w & in_periods(idx, corrected_periods(h))
        tot, aff = tot + int(w.sum()), aff + int(k.sum())
        rows.append({"Phase": 4, "Quantity": "weekday 16:00-19:00 window minutes (full record)", "House": h,
                     "Total": int(w.sum()), "In_Corrected_Periods": int(k.sum()), "Pct": round(100 * k.sum() / w.sum(), 1)})
    rows.append({"Phase": 4, "Quantity": "weekday 16:00-19:00 window minutes (full record)", "House": "all",
                 "Total": tot, "In_Corrected_Periods": aff, "Pct": round(100 * aff / tot, 1)})
    return pd.DataFrame(rows)


def run() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    old = OUT / "02_mislabelled_share_by_phase.csv"   # superseded by 02_corrected_share_by_phase.csv
    if old.exists():
        old.unlink()
    res = {"01_official_vs_raw_clock_by_month.csv": clock_by_month(),
           "02_corrected_share_by_phase.csv": corrected_share(),
           "03_part2_switch_verification.csv": switch_verification(),
           "04_behaviour_check.csv": behaviour_check(),
           "05_event_trace.csv": event_trace()}
    assert res["03_part2_switch_verification.csv"]["Match"].all(), "switch constants do not match the raw files"
    for name, t in res.items():
        t.to_csv(OUT / name, index=False)
        print("Saved outputs/tables/audit/" + name)
    return res


if __name__ == "__main__":
    r = run()
    pd.set_option("display.width", 220)
    t1 = r["01_official_vs_raw_clock_by_month.csv"]
    print(t1.groupby(["Raw_Part", "Clock_Regime", "Published_Official_Timestamps_Are"]).size().to_string())
    for k in ("02_corrected_share_by_phase.csv", "03_part2_switch_verification.csv", "04_behaviour_check.csv", "05_event_trace.csv"):
        print(f"--- {k}\n" + r[k].to_string(index=False))
