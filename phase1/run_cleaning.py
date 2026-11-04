"""Phase 1 - House 1 raw REFIT inspection and cleaning pipeline.

Run from anywhere:
    python3 phase1/run_cleaning.py              # writes Phase 1 outputs
    python3 phase1/run_cleaning.py --out DIR    # writes the same outputs under DIR (staging / review)

Inputs (read only, never modified): Data/REFIT_RAW_081116/RAW_House1_Part1.csv and _Part2.csv.
Outputs: outputs/tables/phase1/, outputs/figures/phase1/, Data/processed/phase1/.
Nothing is written until every validation assertion has passed.

Methodology in one place (the notebook imports these functions, so both use identical code):
  1. Timestamps. In both raw files `Time` is the UK wall clock (Europe/London) and `Unix` is that wall-clock
     value read as if it were UTC (Unix - naive(Time) is 0 or -1 s on every row). Canonical UTC is therefore
     Unix minus the local UTC offset (3600 s in BST, 0 s in GMT). The repeated autumn hour is resolved by file
     order when the file shows the clock going back; if the repeated hour is interleaved and cannot be split,
     its rows are removed (no invented correction).
  2. Readings above physical limits (appliance > 4000 W, aggregate > 25000 W) or below 0 W are set to missing.
  3. Part 1 is used up to the handoff instant and Part 2 after it.
  4. Several rows in the same UTC second are merged per channel, keeping the last non-missing reading.
  5. Each valid reading is held until the channel's next valid reading, for at most a channel-specific limit
     (60 s or 120 s). Time not covered by a held reading is missing; long outages are never filled.
  6. 1-minute values are exact time-weighted means: each held interval is split at minute boundaries.
     A minute is kept only if at least 30 s of it is covered.
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd

os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl_cache")
os.makedirs(os.environ["MPLCONFIGDIR"], exist_ok=True)
import matplotlib  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RAW_FILES = {1: ROOT / "Data/REFIT_RAW_081116/RAW_House1_Part1.csv",
             2: ROOT / "Data/REFIT_RAW_081116/RAW_House1_Part2.csv"}
TZ = "Europe/London"

CHANNELS = ["Aggregate"] + [f"Appliance{i}" for i in range(1, 10)]
# Channel labels as listed for House 1 in the REFIT readme shipped with the data (docs/CLEAN_READ_ME_081116.txt).
CHANNEL_NAMES = {
    "Aggregate": "Mains Whole-House", "Appliance1": "Fridge", "Appliance2": "Freezer (1)",
    "Appliance3": "Freezer (2)", "Appliance4": "Washer Dryer", "Appliance5": "Washing Machine",
    "Appliance6": "Dishwasher", "Appliance7": "Computer", "Appliance8": "Television Site",
    "Appliance9": "Electric Heater",
}
# Physical plausibility limits: an IAM plug is rated ~13 A (~3 kW), so > 4 kW is a sensor error (the official
# REFIT cleaning uses the same 4 kW figure); 25 kW is above a 100 A single-phase domestic supply.
LIMIT_W = {ch: (4000.0 if ch.startswith("Appliance") else 25000.0) for ch in CHANNELS}
# Project's conservative hold (forward-fill) limits, in seconds since the reading was taken.
HOLD_LIMIT_S = {"Aggregate": 60, "Appliance1": 120, "Appliance2": 120, "Appliance3": 120, "Appliance4": 60,
                "Appliance5": 60, "Appliance6": 60, "Appliance7": 120, "Appliance8": 120, "Appliance9": 120}
MIN_COVERAGE_S = 30.0

# Handoff instant chosen by the original pipeline: 2014-09-27 14:51:12 on the UK clock (BST). Both raw files carry
# the same signals around it (table 12), so it is kept as a project choice and expressed in canonical UTC here.
HANDOFF_LOCAL = "2014-09-27 14:51:12"
HANDOFF_UTC = int(pd.Timestamp(HANDOFF_LOCAL).tz_localize(TZ).timestamp())

# Energy-validation windows: half-open UTC intervals [start, end).
ENERGY_WINDOWS = [
    ("Aggregate", "48 h household load", "2013-11-15 00:00", "2013-11-17 00:00"),
    ("Appliance5", "Washing-machine cycle", "2013-10-23 05:45", "2013-10-23 08:00"),
    ("Appliance1", "12 h fridge cycling", "2013-10-12 00:00", "2013-10-12 12:00"),
]
# Raw-side windows used by the original validation (Unix seconds, inclusive). Kept only to decompose the old result:
# the washing-machine raw window started at 04:45 UTC, one hour earlier than its cleaned window.
LEGACY_RAW_WINDOWS = {"Aggregate": (1384473600, 1384646400), "Appliance5": (1382503500, 1382511600),
                      "Appliance1": (1381536000, 1381579200)}
ENERGY_TOLERANCE_PCT = 1.0  # practical tolerance for cleaned vs raw energy in a validation window

COLORS = {"raw": "#eb6834", "clean": "#2a78d6", "p1": "#2a78d6", "p2": "#eb6834", "accent": "#6250d6",
          "outage": "#d7191c", "fill": "#1baf7a"}


def rel(path) -> str:
    """Path relative to the project root for printing (avoids local absolute paths in logs)."""
    try:
        return str(Path(path).resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def unix(ts: str) -> int:
    return int(pd.Timestamp(ts, tz="UTC").timestamp())


def out_paths(out_root: Path | str = ROOT) -> dict:
    out_root = Path(out_root)
    p = {"tables": out_root / "outputs/tables/phase1", "figures": out_root / "outputs/figures/phase1",
         "data": out_root / "Data/processed/phase1"}
    return p


# ----------------------------------------------------------------------------------------------------------------
# 1. Load and inventory
# ----------------------------------------------------------------------------------------------------------------
def load_raw() -> dict:
    cols = ["Time", "Unix"] + CHANNELS
    return {p: pd.read_csv(f, usecols=cols, dtype={c: "float64" for c in CHANNELS}) for p, f in RAW_FILES.items()}


def raw_inventory(raw: dict, norm: dict) -> pd.DataFrame:
    rows = []
    for p, d in raw.items():
        n = norm[p]
        kept = n.loc[n.keep, "utc"]
        rows.append({
            "File": RAW_FILES[p].name, "Size_MB": round(RAW_FILES[p].stat().st_size / 2**20, 2),
            "Total_Rows": len(d), "Start_Time_Local": d.Time.iloc[0], "End_Time_Local": d.Time.iloc[-1],
            "Start_UTC": str(pd.to_datetime(kept.iloc[0], unit="s")), "End_UTC": str(pd.to_datetime(kept.iloc[-1], unit="s")),
            "Duplicate_Unix_Rows": int(d.Unix.duplicated().sum()),
            "Raw_Unix_Backward_Steps": int((d.Unix.diff() < 0).sum()),
            "Canonical_UTC_Backward_Steps": int((kept.diff() < 0).sum()),
        })
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------------------------------------------
# 2. Timestamp normalisation
# ----------------------------------------------------------------------------------------------------------------
def normalise_timestamps(df: pd.DataFrame) -> tuple[pd.DataFrame, list]:
    """Return a frame with canonical UTC seconds (`utc`), the applied offset and a `keep` flag, plus notes on
    every ambiguous (repeated) local hour found. Raises if any local time does not exist on the UK clock."""
    local = pd.to_datetime(df["Time"])
    naive = local.astype("int64") // 10**9
    nonexistent = local.dt.tz_localize(TZ, ambiguous=np.ones(len(df), bool), nonexistent="NaT").isna().to_numpy()
    if nonexistent.any():
        raise ValueError(f"{int(nonexistent.sum())} local times do not exist on the UK clock; no correction invented")
    ambiguous = local.dt.tz_localize(TZ, ambiguous="NaT", nonexistent="raise").isna().to_numpy()

    is_dst = np.ones(len(df), bool)
    keep = np.ones(len(df), bool)
    notes = []
    amb_pos = np.flatnonzero(ambiguous)
    for day in pd.unique(local.dt.date.to_numpy()[amb_pos]):
        pos = amb_pos[local.dt.date.to_numpy()[amb_pos] == day]
        back = np.flatnonzero(np.diff(naive.to_numpy()[pos]) < -1800)
        contiguous = pos[-1] - pos[0] + 1 == len(pos)
        if len(back) == 1 and contiguous:
            # Clock visibly goes back once inside a contiguous block: rows before the step are BST, after are GMT.
            is_dst[pos[back[0] + 1:]] = False
            notes.append({"date": str(day), "rows": len(pos), "resolution": "resolved by file order",
                          "rows_removed": 0})
        else:
            # Both copies of the hour are interleaved (no backward step): they cannot be told apart.
            keep[pos] = False
            notes.append({"date": str(day), "rows": len(pos), "resolution": "unresolvable - rows removed",
                          "rows_removed": len(pos)})
    utc_ts = local.dt.tz_localize(TZ, ambiguous=is_dst, nonexistent="raise")
    offset = (naive - utc_ts.astype("int64") // 10**9).to_numpy()
    out = pd.DataFrame({"Time": df["Time"].to_numpy(), "unix_raw": df["Unix"].to_numpy(),
                        "utc_offset_s": offset, "utc": df["Unix"].to_numpy() - offset, "keep": keep})
    for c in CHANNELS:
        out[c] = df[c].to_numpy()
    return out, notes


def clock_change_dates(start_utc: int, end_utc: int) -> list:
    days = pd.date_range(pd.to_datetime(start_utc, unit="s").normalize(), pd.to_datetime(end_utc, unit="s").normalize(), freq="D")
    out = []
    for d in days:
        o0 = pd.Timestamp(d).tz_localize(TZ).utcoffset()
        o1 = (pd.Timestamp(d) + pd.Timedelta(days=1)).tz_localize(TZ).utcoffset()
        if o0 != o1:
            out.append((d, "autumn (clock back)" if o1 < o0 else "spring (clock forward)"))
    return out


def clock_change_table(norm: dict, notes: dict) -> pd.DataFrame:
    rows = []
    for p, n in norm.items():
        for day, kind in clock_change_dates(int(n.utc.min()), int(n.utc.max())):
            t = pd.to_datetime(n.Time)
            k = ((t >= day) & (t < day + pd.Timedelta(hours=3))).to_numpy()
            if k.sum() < 2:
                rows.append({"Part": p, "Date": str(day.date()), "Change": kind, "Rows_00_03_Local": int(k.sum()),
                             "Treatment": "no rows on this date (file outage or outside file range)"})
                continue
            w = n[k]
            naive_step = (pd.to_datetime(w.Time).astype("int64") // 10**9).diff()
            i = naive_step.abs().idxmax()
            wk = w[w.keep]
            utc_step = wk.utc.diff()
            note = [x for x in notes[p] if x["date"] == str(day.date())]
            if kind.startswith("spring"):
                treat = "local clock jumps forward; canonical UTC continuous (no outage)"
            elif note:
                treat = note[0]["resolution"]
            else:
                treat = "no repeated hour present"
            rows.append({
                "Part": p, "Date": str(day.date()), "Change": kind, "Rows_00_03_Local": int(k.sum()),
                "Largest_Local_Step_From": n.Time[i - 1] if (i - 1) in n.index else "", "Largest_Local_Step_To": n.Time[i],
                "Largest_Local_Step_s": int(naive_step[i]), "Largest_Canonical_UTC_Step_s": int(utc_step.max()),
                "Canonical_UTC_Backward_Steps": int((utc_step < 0).sum()),
                "Ambiguous_Rows": int(note[0]["rows"]) if note else 0,
                "Rows_Removed": int(note[0]["rows_removed"]) if note else 0, "Treatment": treat,
            })
    rows.append({"Part": "1->2", "Date": HANDOFF_LOCAL[:10], "Change": "Part 1 to Part 2 handoff",
                 "Treatment": f"Part 1 rows with UTC <= {pd.to_datetime(HANDOFF_UTC, unit='s')} UTC, Part 2 after "
                              f"(= {HANDOFF_LOCAL} UK clock)"})
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------------------------------------------
# 3. Glitches, handoff, de-duplication
# ----------------------------------------------------------------------------------------------------------------
def outlier_table(raw: dict) -> pd.DataFrame:
    rows = []
    for ch in CHANNELS:
        r = {"Channel": ch, "Appliance_Name": CHANNEL_NAMES[ch], "Physical_Limit_Watts": LIMIT_W[ch]}
        for p, d in raw.items():
            v = d[ch].dropna()
            r[f"Part{p}_Max_Watts"] = float(v.max())
            r[f"Part{p}_Negative_Count"] = int((v < 0).sum())
            r[f"Part{p}_Above_Limit_Count"] = int((v > LIMIT_W[ch]).sum())
        r["Total_Flagged_Glitches"] = sum(r[f"Part{p}_Above_Limit_Count"] + r[f"Part{p}_Negative_Count"] for p in raw)
        rows.append(r)
    return pd.DataFrame(rows)


def apply_glitch_rules(n: pd.DataFrame) -> pd.DataFrame:
    n = n.copy()
    for ch in CHANNELS:
        bad = (n[ch] > LIMIT_W[ch]) | (n[ch] < 0)
        n.loc[bad, ch] = np.nan  # physically impossible reading -> missing (official REFIT sets IAM spikes to 0)
    return n


def select_and_dedup(norm_clean: dict) -> tuple[pd.DataFrame, dict, dict]:
    sel = {1: norm_clean[1][norm_clean[1].keep & (norm_clean[1].utc <= HANDOFF_UTC)],
           2: norm_clean[2][norm_clean[2].keep & (norm_clean[2].utc > HANDOFF_UTC)]}
    parts, stats = [], {}
    for p, s in sel.items():
        g = s.groupby("utc")[CHANNELS]
        dd = g.last()  # last non-missing reading per channel within the second (keeps complementary channels)
        size = g.size()
        stats[p] = {"rows_selected": len(s), "unique_seconds": len(dd), "seconds_with_multiple_rows": int((size > 1).sum()),
                    "nonnull_cells_before": int((g.count() > 0).to_numpy().sum()),
                    "nonnull_cells_after": int(dd.notna().to_numpy().sum())}
        parts.append(dd)
    combined = pd.concat(parts).sort_index()
    return combined, stats, sel


# ----------------------------------------------------------------------------------------------------------------
# 4. Held intervals and exact 1-minute integration
# ----------------------------------------------------------------------------------------------------------------
def held_intervals(s: pd.Series, hold_s: float):
    """Each valid reading holds until the channel's next valid reading, for at most `hold_s` seconds."""
    v = s.dropna()
    t = v.index.to_numpy(dtype="int64").astype(float)
    x = v.to_numpy(dtype=float)
    nxt = np.append(t[1:], np.inf)
    end = np.minimum(nxt, t + hold_s)
    end[np.isinf(end)] = t[np.isinf(end)]  # last reading with no hold limit: no defined duration
    return t, end, x


def cumulative_at(edges: np.ndarray, t: np.ndarray, end: np.ndarray, x: np.ndarray):
    """Exact cumulative energy (W*s) and covered time (s) of the held step signal at each edge."""
    dur = end - t
    cum_e = np.concatenate([[0.0], np.cumsum(x * dur)])
    cum_c = np.concatenate([[0.0], np.cumsum(dur)])
    i = np.searchsorted(t, edges, side="right") - 1
    ok = i >= 0
    ii = np.where(ok, i, 0)
    part = np.clip(edges - t[ii], 0.0, dur[ii])
    e = np.where(ok, cum_e[ii] + x[ii] * part, 0.0)
    c = np.where(ok, cum_c[ii] + part, 0.0)
    return e, c


def resample_1min(combined: pd.DataFrame):
    t0 = int(combined.index.min()) // 60 * 60
    # Grid ends at the minute that contains the end of the last held interval, so no held energy falls outside it.
    t1 = -(-(int(combined.index.max()) + max(HOLD_LIMIT_S.values())) // 60) * 60
    edges = np.arange(t0, t1 + 60, 60, dtype=float)
    idx = pd.to_datetime(edges[:-1].astype("int64"), unit="s", utc=True)
    mean, cov, energy = {}, {}, {}
    for ch in CHANNELS:
        t, end, x = held_intervals(combined[ch], HOLD_LIMIT_S[ch])
        e, c = cumulative_at(edges, t, end, x)
        de, dc = np.diff(e), np.diff(c)
        m = np.full(len(de), np.nan)
        ok = dc >= MIN_COVERAGE_S
        m[ok] = de[ok] / dc[ok]
        mean[ch], cov[ch], energy[ch] = m, dc, de
    return (pd.DataFrame(mean, index=idx), pd.DataFrame(cov, index=idx), pd.DataFrame(energy, index=idx))


def arithmetic_1min(combined: pd.DataFrame, index: pd.DatetimeIndex) -> pd.DataFrame:
    minute = pd.to_datetime((combined.index.to_numpy() // 60) * 60, unit="s", utc=True)
    return combined.groupby(minute).mean().reindex(index)


def resampling_comparison(arith: pd.DataFrame, weighted: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for ch in CHANNELS:
        diff = (arith[ch] - weighted[ch]).abs().dropna()
        rows.append({"Channel": ch, "Appliance_Name": CHANNEL_NAMES[ch], "Minutes_Compared": len(diff),
                     "Mean_Absolute_Diff_W": round(float(diff.mean()), 3), "P95_Absolute_Diff_W": round(float(diff.quantile(0.95)), 3),
                     "P99_Absolute_Diff_W": round(float(diff.quantile(0.99)), 3), "Max_Absolute_Diff_W": round(float(diff.max()), 3)})
    return pd.DataFrame(rows)


def availability_table(m: pd.DataFrame) -> pd.DataFrame:
    rows, total = [], len(m)
    for ch in CHANNELS:
        s = m[ch]
        valid, zero, active = int(s.notna().sum()), int((s == 0).sum()), int((s > 0).sum())
        rows.append({"Channel": ch, "Appliance_Name": CHANNEL_NAMES[ch], "Total_Minutes": total, "Valid_Minutes": valid,
                     "Missing_Minutes": total - valid, "Availability_Pct": round(100 * valid / total, 2),
                     "Zero_Pct": round(100 * zero / total, 2), "Active_Pct": round(100 * active / total, 2),
                     "Mean_Active_Power_W": round(float(s[s > 0].mean()), 1) if active else 0.0,
                     "Max_Power_W": round(float(s.max()), 1)})
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------------------------------------------
# 5. Quality summaries
# ----------------------------------------------------------------------------------------------------------------
def gap_stats(seconds: np.ndarray) -> pd.Series:
    return pd.Series(np.diff(np.unique(seconds)).astype(float))


def anomalies_table(raw: dict, norm: dict) -> pd.DataFrame:
    dts = {p: gap_stats(norm[p].loc[norm[p].keep, "utc"].to_numpy()) for p in norm}
    raw_dts = {p: gap_stats(raw[p].Unix.to_numpy()) for p in raw}

    def row(name, f):
        a, b = f(1), f(2)
        return {"Metric": name, "Part1": a, "Part2": b, "Combined": (max(a, b) if "Maximum" in name else a + b)}
    rows = [row("Total Rows", lambda p: len(raw[p])),
            row("Missing Values (NaN cells across all channels)", lambda p: int(raw[p][CHANNELS].isna().sum().sum())),
            row("Duplicate Unix Timestamps (rows)", lambda p: int(raw[p].Unix.duplicated().sum())),
            row("Rows Removed: Unresolvable Repeated Local Hour", lambda p: int((~norm[p].keep).sum()))]
    for lab, thr in [("2 Minutes (120s)", 120), ("15 Minutes (900s)", 900), ("1 Hour (3600s)", 3600), ("24 Hours (86400s)", 86400)]:
        rows.append(row(f"Gaps > {lab}, canonical UTC", lambda p, thr=thr: int((dts[p] > thr).sum())))
    rows.append(row("Gaps > 1 Hour (3600s), raw Unix as stored", lambda p: int((raw_dts[p] > 3600).sum())))
    rows.append(row("Maximum Outage Duration (Hours), canonical UTC", lambda p: round(float(dts[p].max()) / 3600, 2)))
    return pd.DataFrame(rows)


def outage_table(combined: pd.DataFrame, min_hours: float = 1.0) -> pd.DataFrame:
    t = combined["Aggregate"].dropna().index.to_numpy()
    d = np.diff(t)
    k = np.flatnonzero(d > min_hours * 3600)
    return pd.DataFrame({"Start_UTC": pd.to_datetime(t[k], unit="s").astype(str),
                         "End_UTC": pd.to_datetime(t[k + 1], unit="s").astype(str),
                         "Duration_Hours": np.round(d[k] / 3600, 2), "Source_Part": np.where(t[k] <= HANDOFF_UTC, 1, 2)})


def cleaning_checks(sel: dict, combined: pd.DataFrame, dstats: dict, cov: pd.DataFrame) -> pd.DataFrame:
    rows = []
    add = lambda area, metric, value, note="": rows.append({"Area": area, "Metric": metric, "Value": value, "Note": note})
    for p in (1, 2):
        s = dstats[p]
        add("De-duplication", f"Part {p}: rows used / unique UTC seconds", f"{s['rows_selected']} / {s['unique_seconds']}")
        add("De-duplication", f"Part {p}: seconds holding more than one row", s["seconds_with_multiple_rows"])
        add("De-duplication", f"Part {p}: (second, channel) cells with a reading before / after merge",
            f"{s['nonnull_cells_before']} / {s['nonnull_cells_after']}", "equal = no complementary reading lost")
    for ch in CHANNELS:
        r = combined[ch].dropna()
        gaps = np.diff(r.index.to_numpy()).astype(float)
        L = HOLD_LIMIT_S[ch]
        short = gaps[(gaps > L) & (gaps <= 900)]
        long_ = gaps[gaps > 900]
        add("Hold limit", f"{ch}: short gaps {L}-900 s left partly unfilled (count; minutes not filled)",
            f"{len(short)}; {round(float((short - L).sum()) / 60, 1)}", "decided by the hold limit")
        add("Hold limit", f"{ch}: gaps > 900 s, i.e. outages (count; hours missing)",
            f"{len(long_)}; {round(float((long_ - L).sum()) / 3600, 1)}", "missing under any short hold limit")
        p2 = sel[2][ch]
        p2_valid = sel[2].loc[p2.notna(), "utc"].to_numpy()
        g2 = np.diff(p2_valid)
        add("Part 2 telemetry", f"{ch}: Part 2 rows without a reading (%); median / 99th pct gap between readings (s)",
            f"{round(100 * p2.isna().mean(), 1)}; {int(np.median(g2))} / {int(np.percentile(g2, 99))}")
    for ch in CHANNELS:
        left = int(((combined[ch] > LIMIT_W[ch]) | (combined[ch] < 0)).sum())
        add("Glitch rule", f"{ch}: readings outside [0, {int(LIMIT_W[ch])}] W remaining after cleaning", left)
    partial = ((cov > 0) & (cov < MIN_COVERAGE_S)).sum()
    add("Coverage rule", "Minutes with 0 < coverage < 30 s set to missing (Aggregate)", int(partial["Aggregate"]))
    add("Coverage rule", "Minutes kept with 30-59 s coverage (Aggregate)", int(((cov["Aggregate"] >= MIN_COVERAGE_S) & (cov["Aggregate"] < 60)).sum()))
    return pd.DataFrame(rows)


def handoff_agreement(sel_all: dict) -> pd.DataFrame:
    """Compare Part 1 and Part 2 1-minute means on the same canonical UTC minutes, before and after the handoff."""
    def minute_means(n):
        return n[CHANNELS].groupby((n.utc // 60) * 60).mean()
    a, b = minute_means(sel_all[1]), minute_means(sel_all[2])
    rows = []
    for lab, s, e in [("7 days before handoff", HANDOFF_UTC - 7 * 86400, HANDOFF_UTC),
                      ("after handoff to end of Part 1", HANDOFF_UTC, int(sel_all[1].utc.max()))]:
        x = a[(a.index >= s) & (a.index < e)]
        y = b[(b.index >= s) & (b.index < e)]
        j = x.join(y, lsuffix="_p1", rsuffix="_p2", how="inner")
        for ch in CHANNELS:
            q = j[[f"{ch}_p1", f"{ch}_p2"]].dropna()
            diff = (q.iloc[:, 0] - q.iloc[:, 1]).abs()
            rows.append({"Period": lab, "Channel": ch, "Minutes_Both": len(q),
                         "Pct_Minutes_Within_5W": round(100 * float((diff <= 5).mean()), 1) if len(q) else np.nan,
                         "Part1_Distinct_Values": int(x[ch].round().nunique()), "Part2_Distinct_Values": int(y[ch].round().nunique()),
                         "Part1_Max_W": float(x[ch].max()), "Part2_Max_W": float(y[ch].max())})
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------------------------------------------
# 6. Energy validation
# ----------------------------------------------------------------------------------------------------------------
def window_energy_raw(combined: pd.DataFrame, ch: str, a: int, b: int, hold_s: float):
    t, end, x = held_intervals(combined[ch], hold_s)
    e, c = cumulative_at(np.array([a, b], float), t, end, x)
    return float(e[1] - e[0]), float(c[1] - c[0])


def energy_validation(combined, m, cov, energy) -> pd.DataFrame:
    rows = []

    def record(ch, desc, a, b):
        raw_ws, raw_cov = window_energy_raw(combined, ch, a, b, HOLD_LIMIT_S[ch])
        raw_unl, _ = window_energy_raw(combined, ch, a, b, np.inf)
        k = (m.index >= pd.to_datetime(a, unit="s", utc=True)) & (m.index < pd.to_datetime(b, unit="s", utc=True))
        mm, cc = m.loc[k, ch], cov.loc[k, ch]
        kept = mm.notna()
        clean60 = float((mm[kept] * 60).sum())
        cleancov = float((mm[kept] * cc[kept]).sum())
        dropped_ws = float(energy.loc[k, ch][~kept].sum())
        pct = lambda v: round(100 * (v - raw_ws) / raw_ws, 3) if raw_ws else np.nan
        rows.append({
            "Channel": ch, "Window": desc,
            "Window_UTC": f"[{pd.to_datetime(a, unit='s')}, {pd.to_datetime(b, unit='s')})",
            "Raw_Coverage_Pct": round(100 * raw_cov / (b - a), 2),
            "Raw_Held_kWh": round(raw_ws / 3.6e6, 4),
            "Cleaned_kWh": round(clean60 / 3.6e6, 4), "Pct_Diff": pct(clean60),
            "Cleaned_Coverage_Weighted_kWh": round(cleancov / 3.6e6, 4), "Pct_Diff_Coverage_Weighted": pct(cleancov),
            "Energy_In_Dropped_Minutes_kWh": round(dropped_ws / 3.6e6, 4),
            "Like_For_Like_Residual_Ws": round(raw_ws - cleancov - dropped_ws, 6),
            "Minutes_Kept": int(kept.sum()), "Minutes_Partial_Dropped": int(((cc > 0) & ~kept).sum()),
            "Raw_No_Hold_Limit_kWh": round(raw_unl / 3.6e6, 4),
            "Hold_Limit_Effect_Pct": round(100 * (raw_ws - raw_unl) / raw_unl, 3) if raw_unl else np.nan,
        })

    for ch, desc, s, e in ENERGY_WINDOWS:
        record(ch, desc, unix(s), unix(e))
    a, b = int(m.index[0].timestamp()), int(m.index[-1].timestamp()) + 60
    for ch in CHANNELS:
        record(ch, "Full record", a, b)
    out = pd.DataFrame(rows)
    out["Within_Tolerance"] = out["Pct_Diff"].abs() <= ENERGY_TOLERANCE_PCT
    return out


def legacy_window_minutes(p1_rows: pd.DataFrame, a: int, b: int) -> pd.DataFrame:
    """Re-implements the ORIGINAL resampling (forward-fill on the row index, weight = time to next row capped at
    60 s, whole weight given to the starting minute). Used only to explain the old validation numbers."""
    w = p1_rows[(p1_rows.utc >= a - 7200) & (p1_rows.utc <= b + 7200)]
    dd = w.groupby("utc")[CHANNELS].last()
    ix = dd.index.to_numpy()
    filled = dd.copy()
    for ch in CHANNELS:
        s = dd[ch]
        last = pd.Series(np.where(s.notna(), ix, np.nan), index=s.index).ffill()
        f = s.ffill()
        f[(ix - last) > HOLD_LIMIT_S[ch]] = np.nan
        filled[ch] = f
    idx = pd.to_datetime(ix, unit="s", utc=True)
    filled.index = idx
    wts = pd.Series(pd.Series(idx).diff().shift(-1).dt.total_seconds().clip(lower=0, upper=60).fillna(8.0).to_numpy(), index=idx)
    out = {}
    for ch in CHANNELS:
        v = filled[ch]
        wv = wts.where(v.notna(), 0.0)
        num = (v.fillna(0.0) * wv).resample("1min").sum()
        den = wv.resample("1min").sum()
        r = num / den
        r[den < 30.0] = np.nan
        out[ch] = r
    return pd.DataFrame(out)


def energy_decomposition(sel: dict, combined, m) -> pd.DataFrame:
    """Step-by-step explanation of the old raw-vs-cleaned differences (table 06 in the original pipeline)."""
    p1 = sel[1]
    rows = []
    for ch, desc, s, e in ENERGY_WINDOWS:
        a, b = unix(s), unix(e)
        la, lb = LEGACY_RAW_WINDOWS[ch]
        legacy = legacy_window_minutes(p1, a, b)

        def raw_rows(x0, x1, closed=True):
            r = p1[(p1.utc >= x0) & ((p1.utc <= x1) if closed else (p1.utc < x1))]
            return r

        def backward(r):
            dt = r.utc.diff().clip(lower=0, upper=60).fillna(8.0)
            return float((r[ch] * dt).sum()) / 3.6e6

        def forward(r):
            dt = (-r.utc.diff(-1)).clip(lower=0, upper=60).fillna(8.0)
            return float((r[ch] * dt).sum()) / 3.6e6

        def legacy_clean(closed=True):
            k = (legacy.index >= pd.to_datetime(a, unit="s", utc=True)) & (
                (legacy.index <= pd.to_datetime(b, unit="s", utc=True)) if closed else (legacy.index < pd.to_datetime(b, unit="s", utc=True)))
            return float((legacy.loc[k, ch].dropna() * 60).sum()) / 3.6e6

        raw_exact, _ = window_energy_raw(combined, ch, a, b, HOLD_LIMIT_S[ch])
        raw_unl, _ = window_energy_raw(combined, ch, a, b, np.inf)
        k = (m.index >= pd.to_datetime(a, unit="s", utc=True)) & (m.index < pd.to_datetime(b, unit="s", utc=True))
        new_clean = float((m.loc[k, ch].dropna() * 60).sum()) / 3.6e6
        steps = [
            ("A", "Original calculation: raw rows in the original raw window (inclusive), weighted by time since previous row; cleaned minutes inclusive of end minute",
             backward(raw_rows(la, lb)), legacy_clean(True)),
            ("B", "A, with the raw window moved to the same UTC interval as the cleaned window",
             backward(raw_rows(a, b)), legacy_clean(True)),
            ("C", "B, with each raw reading held until the next row (as the resampler does)",
             forward(raw_rows(a, b)), legacy_clean(True)),
            ("D", "C, with half-open windows [start, end) on both sides (end minute excluded)",
             forward(raw_rows(a, b, closed=False)), legacy_clean(False)),
            ("E", "Revised method: exact held-interval integral (channel hold limit) vs revised 1-minute means x 60 s",
             raw_exact / 3.6e6, new_clean),
            ("F", "Sensitivity: raw held with no hold limit (until next reading) vs revised 1-minute means x 60 s",
             raw_unl / 3.6e6, new_clean),
        ]
        for code, what, r, c in steps:
            rows.append({"Channel": ch, "Window": desc, "Step": code, "What_Changed": what, "Raw_kWh": round(r, 4),
                         "Cleaned_kWh": round(c, 4), "Diff_kWh": round(c - r, 4),
                         "Pct_Diff": round(100 * (c - r) / r, 2) if r else np.nan})
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------------------------------------------------
# 7. Assertions
# ----------------------------------------------------------------------------------------------------------------
def run_assertions(norm, notes, combined, dstats, m, cov, energy, changes, outages, ev) -> pd.DataFrame:
    res = []

    def check(name, ok, detail=""):
        res.append({"Check": name, "Passed": bool(ok), "Detail": str(detail)})

    for p, n in norm.items():
        k = n[n.keep]
        check(f"Part {p}: UTC offset is 0 or 3600 s on every row", set(np.unique(n.utc_offset_s)) <= {0, 3600},
              sorted(set(np.unique(n.utc_offset_s))))
        check(f"Part {p}: canonical UTC never decreases in file order", (np.diff(k.utc.to_numpy()) >= 0).all(),
              f"backward steps = {int((np.diff(k.utc.to_numpy()) < 0).sum())}")
        check(f"Part {p}: every repeated local hour resolved or removed", all(x["resolution"] for x in notes[p]), notes[p])
    sp = changes[changes.Change.astype(str).str.startswith("spring") & changes.Largest_Canonical_UTC_Step_s.notna()]
    check("Spring clock changes: canonical UTC step < 60 s", (sp.Largest_Canonical_UTC_Step_s < 60).all(),
          sp[["Part", "Date", "Largest_Local_Step_s", "Largest_Canonical_UTC_Step_s"]].to_dict("records"))
    au = changes[changes.Change.astype(str).str.startswith("autumn") & changes.Canonical_UTC_Backward_Steps.notna()]
    check("Autumn clock changes: no backward step in canonical UTC", (au.Canonical_UTC_Backward_Steps == 0).all())
    check("Combined index unique and strictly increasing", combined.index.is_unique and combined.index.is_monotonic_increasing)
    check("Handoff: Part 1 ends at or before, Part 2 starts after the handoff",
          dstats["p1_max"] <= HANDOFF_UTC < dstats["p2_min"], f"{dstats['p1_max']} <= {HANDOFF_UTC} < {dstats['p2_min']}")
    for p in (1, 2):
        s = dstats[p]
        check(f"Part {p}: de-duplication keeps every (second, channel) reading", s["nonnull_cells_before"] == s["nonnull_cells_after"])
    for ch in CHANNELS:
        check(f"{ch}: no readings outside physical limits", not ((combined[ch] > LIMIT_W[ch]) | (combined[ch] < 0)).any())
    check("Coverage per minute within [0, 60] s", ((cov >= -1e-9) & (cov <= 60 + 1e-9)).all().all())
    check("Minutes with < 30 s coverage are missing", m.where(cov < MIN_COVERAGE_S).isna().all().all())
    check("Minutes with >= 30 s coverage have a value", m.where(cov >= MIN_COVERAGE_S).notna().sum().sum() == (cov >= MIN_COVERAGE_S).sum().sum())
    big = outages[outages.Duration_Hours >= 24]
    inside = 0
    for _, o in big.iterrows():
        k = (m.index >= pd.Timestamp(o.Start_UTC, tz="UTC") + pd.Timedelta(minutes=2)) & (m.index < pd.Timestamp(o.End_UTC, tz="UTC") - pd.Timedelta(minutes=1))
        inside += int(m.loc[k, "Aggregate"].notna().sum())
    check("Outages >= 24 h stay missing (no Aggregate values inside)", inside == 0, f"{len(big)} outages, {inside} values inside")
    for ch in CHANNELS:
        t, end, x = held_intervals(combined[ch], HOLD_LIMIT_S[ch])
        total = float((x * (end - t)).sum())
        kept = m[ch].notna()
        recon = float((m.loc[kept, ch] * cov.loc[kept, ch]).sum() + energy.loc[~kept, ch].sum())
        check(f"{ch}: minute integration conserves held-signal energy exactly", abs(recon - total) <= 1e-9 * max(total, 1.0),
              f"relative error {abs(recon - total) / max(total, 1.0):.2e}")
    check("Energy windows: like-for-like residual ~ 0", (ev.Like_For_Like_Residual_Ws.abs() < 1e-3).all())
    return pd.DataFrame(res)


# ----------------------------------------------------------------------------------------------------------------
# 8. Figures
# ----------------------------------------------------------------------------------------------------------------
def _style():
    plt.style.use("seaborn-v0_8-whitegrid" if "seaborn-v0_8-whitegrid" in plt.style.available else "default")
    matplotlib.rcParams.update({"font.size": 10, "axes.titlesize": 12, "axes.labelsize": 11})


def _before_after(combined, m, ch, s, e, ylim, title, fname, figdir):
    a, b = unix(s), unix(e)
    r = combined.loc[(combined.index >= a) & (combined.index < b), ch].dropna()
    c = m.loc[(m.index >= pd.Timestamp(s, tz="UTC")) & (m.index < pd.Timestamp(e, tz="UTC")), ch]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 6), sharex=True)
    ax1.plot(pd.to_datetime(r.index, unit="s", utc=True), r.values, color=COLORS["raw"], lw=0.8,
             label="Raw readings (canonical UTC, glitches removed)")
    ax1.set_title(f"House 1 {title}: raw readings")
    ax2.plot(c.index, c.values, color=COLORS["clean"], lw=1.2, label="Cleaned 1-minute time-weighted mean")
    ax2.set_title(f"House 1 {title}: cleaned 1-minute series")
    for ax in (ax1, ax2):
        ax.set_ylabel("Active power (W)")
        ax.set_ylim(*ylim)
        ax.legend(loc="upper right")
    ax2.set_xlabel("Time (UTC)")
    fig.tight_layout()
    path = figdir / fname
    fig.savefig(path, dpi=200)
    plt.close(fig)
    return path


def make_figures(norm, combined, m, outages, figdir: Path) -> list:
    _style()
    paths = [
        _before_after(combined, m, "Aggregate", "2013-11-15 00:00", "2013-11-17 00:00", (0, 5000),
                      "aggregate power, 15-16 Nov 2013", "01_aggregate_power_before_after.png", figdir),
        _before_after(combined, m, "Appliance5", "2013-10-23 05:45", "2013-10-23 08:00", (0, 2600),
                      "washing machine (Appliance 5), 23 Oct 2013", "02_washing_machine_before_after.png", figdir),
        _before_after(combined, m, "Appliance1", "2013-10-12 00:00", "2013-10-12 12:00", (0, 150),
                      "fridge (Appliance 1), 12 Oct 2013", "03_fridge_before_after.png", figdir),
    ]
    fig, ax = plt.subplots(figsize=(10, 5))
    bins = np.linspace(0, 30, 61)
    for p, col in ((1, COLORS["p1"]), (2, COLORS["p2"])):
        dt = gap_stats(norm[p].loc[norm[p].keep, "utc"].to_numpy())
        med = dt.median()
        ax.hist(dt[dt <= 30], bins=bins, density=True, alpha=0.55, color=col, label=f"Part {p} intervals")
        ax.axvline(med, color=col, ls="--", lw=1.5, label=f"Part {p} median = {med:.1f} s")
    ax.set_title("House 1 raw sampling intervals between distinct UTC seconds (Part 1 vs Part 2)")
    ax.set_xlabel("Interval between consecutive readings (s)")
    ax.set_ylabel("Probability density (1/s)")
    ax.set_xlim(0, 30)
    ax.legend(loc="upper right")
    fig.tight_layout()
    p4 = figdir / "04_sampling_interval_distribution.png"
    fig.savefig(p4, dpi=200)
    plt.close(fig)
    paths.append(p4)

    daily = m["Aggregate"].notna().resample("1D").mean() * 100
    fig, ax = plt.subplots(figsize=(14, 4.5))
    ax.plot(daily.index, daily.values, color=COLORS["clean"], lw=1.2, label="Daily Aggregate availability")
    ax.fill_between(daily.index, 0, daily.values, color=COLORS["fill"], alpha=0.25)
    longest = outages.loc[outages.Duration_Hours.idxmax()]
    ax.axvspan(pd.Timestamp(longest.Start_UTC, tz="UTC"), pd.Timestamp(longest.End_UTC, tz="UTC"), color=COLORS["outage"], alpha=0.3,
               label=f"Longest outage: {longest.Start_UTC[:10]} to {longest.End_UTC[:10]} ({longest.Duration_Hours / 24:.1f} days)")
    ax.axvline(pd.to_datetime(HANDOFF_UTC, unit="s", utc=True), color=COLORS["accent"], ls="--", lw=1.5,
               label=f"Part 1 to Part 2 handoff ({pd.to_datetime(HANDOFF_UTC, unit='s'):%Y-%m-%d %H:%M} UTC)")
    ax.set_title("House 1 daily share of minutes with a valid Aggregate value (outages kept as missing)")
    ax.set_xlabel("Date (UTC)")
    ax.set_ylabel("Minutes with data (%)")
    ax.set_ylim(-5, 105)
    ax.legend(loc="lower left", frameon=True)
    fig.tight_layout()
    p5 = figdir / "05_data_availability_timeline.png"
    fig.savefig(p5, dpi=200)
    plt.close(fig)
    paths.append(p5)
    return paths


# ----------------------------------------------------------------------------------------------------------------
# 9. Orchestration
# ----------------------------------------------------------------------------------------------------------------
def export_frame(m: pd.DataFrame) -> pd.DataFrame:
    out = m.copy()
    out.index.name = "DateTime_UTC"
    out = out.reset_index()
    out.insert(1, "Unix", out["DateTime_UTC"].astype("int64") // 10**9)
    out.insert(0, "Time", out["DateTime_UTC"].dt.strftime("%Y-%m-%d %H:%M:%S"))
    return out.drop(columns=["DateTime_UTC"])


def build(verbose: bool = True) -> dict:
    """Run every step in memory (nothing written). Returns all tables and intermediate frames."""
    say = print if verbose else (lambda *a, **k: None)
    t0 = time.time()
    raw = load_raw()
    say(f"Loaded Part 1 ({len(raw[1]):,} rows) and Part 2 ({len(raw[2]):,} rows)")
    norm, notes = {}, {}
    for p in raw:
        norm[p], notes[p] = normalise_timestamps(raw[p])
        say(f"Part {p}: canonical UTC built; repeated local hours: {notes[p]}")
    changes = clock_change_table(norm, notes)
    outliers = outlier_table(raw)
    norm_clean = {p: apply_glitch_rules(n) for p, n in norm.items()}
    combined, dstats, sel = select_and_dedup(norm_clean)
    dstats["p1_max"], dstats["p2_min"] = int(sel[1].utc.max()), int(sel[2].utc.min())
    say(f"Combined series: {len(combined):,} unique UTC seconds")
    m, cov, energy = resample_1min(combined)
    arith = arithmetic_1min(combined, m.index)
    outages = outage_table(combined)
    ev = energy_validation(combined, m, cov, energy)
    res = {
        "raw": raw, "norm": norm, "notes": notes, "sel": sel, "combined": combined, "m": m, "cov": cov, "energy": energy,
        "t01_inventory": raw_inventory(raw, norm),
        "t02_anomalies": anomalies_table(raw, norm),
        "t03_outliers": outliers,
        "t04_availability": availability_table(m),
        "t05_resampling": resampling_comparison(arith, m),
        "t06_energy": ev,
        "t07_clock_changes": changes,
        "t10_energy_decomposition": energy_decomposition(sel, combined, m),
        "t11_cleaning_checks": cleaning_checks(sel, combined, dstats, cov),
        "t12_handoff_agreement": handoff_agreement({p: norm_clean[p][norm_clean[p].keep] for p in norm_clean}),
        "t13_outages": outages,
    }
    res["t14_assertions"] = run_assertions(norm, notes, combined, dstats, m, cov, energy, changes, outages, ev)
    res["dstats"] = dstats
    say(f"Built in {time.time() - t0:.1f}s; assertions passed: {int(res['t14_assertions'].Passed.sum())}/{len(res['t14_assertions'])}")
    return res


TABLE_FILES = {
    "t01_inventory": "01_house1_raw_file_inventory.csv",
    "t02_anomalies": "02_data_quality_anomalies_summary.csv",
    "t03_outliers": "03_outlier_flagging_summary.csv",
    "t04_availability": "04_cleaned_dataset_availability_summary.csv",
    "t05_resampling": "05_arithmetic_vs_timeweighted_comparison.csv",
    "t06_energy": "06_energy_preservation_comparison.csv",
    "t07_clock_changes": "07_timestamp_clock_changes.csv",
    "t10_energy_decomposition": "10_energy_validation_decomposition.csv",
    "t11_cleaning_checks": "11_cleaning_decision_checks.csv",
    "t12_handoff_agreement": "12_handoff_overlap_agreement.csv",
    "t13_outages": "13_aggregate_outages_over_1h.csv",
    "t14_assertions": "14_pipeline_assertions.csv",
}


def write_outputs(res: dict, out_root: Path | str = ROOT, verbose: bool = True) -> dict:
    """Write tables, figures and the cleaned dataset. Refuses to write if any assertion failed."""
    failed = res["t14_assertions"][~res["t14_assertions"].Passed]
    if len(failed):
        raise AssertionError("Validation failed; nothing written:\n" + failed.to_string())
    p = out_paths(out_root)
    for d in p.values():
        d.mkdir(parents=True, exist_ok=True)
    written = []
    for key, fname in TABLE_FILES.items():
        res[key].to_csv(p["tables"] / fname, index=False)
        written.append(p["tables"] / fname)
    final = export_frame(res["m"])
    final.to_parquet(p["data"] / "house1_cleaned_1min.parquet", index=False)
    final.to_csv(p["data"] / "house1_cleaned_1min.csv.gz", index=False, compression="gzip")
    written += [p["data"] / "house1_cleaned_1min.parquet", p["data"] / "house1_cleaned_1min.csv.gz"]
    res["figs"] = make_figures(res["norm"], res["combined"], res["m"], res["t13_outages"], p["figures"])
    written += res["figs"]
    if verbose:
        for w in written:
            print("Saved", rel(w))
    return {"written": written, "final": final}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT), help="output root (default: project root)")
    ap.add_argument("--skip-audit", action="store_true", help="do not run the timestamp audit (tables 08-09)")
    args = ap.parse_args(argv)
    plt.switch_backend("Agg")
    res = build()
    write_outputs(res, args.out)
    if not args.skip_audit:
        import final_tz_audit  # same folder
        final_tz_audit.run(out_root=args.out, norm=res["norm"])
    print("Phase 1 pipeline completed.")


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
