"""
Phase 2 - Exploratory analysis of washing-machine (WM) use.

Source: official cleaned REFIT data (Data/CLEAN_REFIT_081116/CLEAN_HouseN.csv).
This module is independent of Phase 1 outputs. It never modifies the source CSVs;
processed copies are written to Data/processed/phase2/.

Run end-to-end from the repository root (or anywhere):
    python3 phase2/wm_eda.py
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

# Matplotlib needs a writable cache dir; keep it out of the repo.
os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "mpl_cache"))
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402

# ----------------------------------------------------------------------------
# Paths (resolved from this file, so the script works from any working dir)
# ----------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parents[1]
CLEAN_DIR = ROOT / "Data" / "CLEAN_REFIT_081116"
PROC_DIR = ROOT / "Data" / "processed" / "phase2"
FIG_DIR = ROOT / "outputs" / "figures" / "phase2"
TAB_DIR = ROOT / "outputs" / "tables" / "phase2"

# ----------------------------------------------------------------------------
# Household selection and channel mapping
# Taken from the official CLEAN_READ_ME_081116.txt appliance list (copy in
# docs/CLEAN_READ_ME_081116.txt). Only houses whose readme entry names a single,
# dedicated "Washing Machine" with no mid-study change note are used.
# ----------------------------------------------------------------------------
HOUSES = {
    2: {"channel": "Appliance2", "readme_label": "Washing Machine, LG, F1289TD", "occupancy": 4},
    5: {"channel": "Appliance3", "readme_label": "Washing Machine, AEG, L99695HWD", "occupancy": 4},
    7: {"channel": "Appliance5", "readme_label": "Washing Machine, Bosch, Unknown", "occupancy": 4},
    8: {"channel": "Appliance4", "readme_label": "Washing Machine, (no make/model given)", "occupancy": 2},
    9: {"channel": "Appliance3", "readme_label": "Washing Machine, Bosch, Classixx 6 1200 Express", "occupancy": 2},
    15: {"channel": "Appliance3", "readme_label": "Washing Machine, Beko, WMB91242LB", "occupancy": 1},
}

# Houses with a WM-like channel that were NOT selected, and why (documented in DATA.md).
EXCLUDED = {
    1: "Two laundry channels (Washer Dryer + Washing Machine); ambiguous which carries the main load",
    4: "Two washing machines (Appliance4 and Appliance5)",
    13: "Readme notes the washing machine 'Changes 25 Mar 2015'",
    12: "No washing machine listed (several channels labelled '???')",
}

# ----------------------------------------------------------------------------
# Analysis parameters (all documented in DATA.md / RESULTS.md)
# ----------------------------------------------------------------------------
LOCAL_TZ = "Europe/London"   # Unix column is UTC; behaviour is analysed in UK local time
ACTIVE_W = 20.0              # 1-min mean WM power above this = "active minute"
MERGE_GAP_MIN = 15           # active runs separated by <= this many minutes are one activity period
FROZEN_MIN = 60              # identical non-zero value held longer than this = frozen sensor
HEATING_W = 1000.0           # 1-min mean above this = water-heating element on

# Rule for calling an activity period a "likely wash cycle" (see detect_cycles)
CYCLE_MIN_DURATION_MIN = 10
CYCLE_MAX_DURATION_MIN = 300
CYCLE_MIN_ENERGY_KWH = 0.05
CYCLE_MIN_PEAK_W = 200.0

GOOD_DAY_MIN_AVAIL = 0.95    # a day needs >=95% valid minutes to be used as a "representative day"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# Reference palette (dataviz skill, light mode): slot 1 blue, slot 2 orange
C_AGG = "#2a78d6"
C_WM = "#eb6834"
C_NEUTRAL = "#52514e"
SEQ_BLUE = LinearSegmentedColormap.from_list(
    "seq_blue", ["#f4f8fd", "#cde2fb", "#86b6ef", "#3987e5", "#256abf", "#184f95", "#0d366b"]
)


def ensure_dirs() -> None:
    for d in (PROC_DIR, FIG_DIR, TAB_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ============================================================================
# 1. Loading and light-touch preparation
# ============================================================================
def _frozen_mask(t: np.ndarray, v: np.ndarray, min_value: float, max_minutes: float) -> np.ndarray:
    """True for samples that sit inside a run of one identical value (> min_value)
    lasting longer than `max_minutes`.

    Why: the cleaned files forward-fill NaN values, so a sensor that stops reporting
    can appear as one value repeated for hours (e.g. House 5 WM at exactly 2129 W for
    ~13 h). Real appliance power always fluctuates, so such runs are treated as
    missing - not as consumption and not as zero.
    """
    change = np.r_[True, v[1:] != v[:-1]]
    run_id = np.cumsum(change) - 1
    first_t = t[change]
    last_t = np.r_[t[np.flatnonzero(change)[1:] - 1], t[-1]]
    run_val = v[change]
    run_frozen = ((last_t - first_t) / 60.0 > max_minutes) & (run_val > min_value)
    return run_frozen[run_id]


def load_house(house: int) -> tuple[pd.DataFrame, dict]:
    """Read one cleaned REFIT file (only the columns needed) and apply minimal,
    documented preparation. Returns sample-level data and a dict of check results."""
    ch = HOUSES[house]["channel"]
    path = CLEAN_DIR / f"CLEAN_House{house}.csv"
    raw = pd.read_csv(path, usecols=["Unix", "Aggregate", ch, "Issues"])
    raw = raw.rename(columns={"Aggregate": "aggregate_w", ch: "wm_w", "Issues": "issues"})

    checks = {
        "house": house,
        "file": path.name,
        "raw_rows": len(raw),
        "nan_values": int(raw[["aggregate_w", "wm_w"]].isna().sum().sum()),
        "non_monotonic_steps": int((raw["Unix"].diff() < 0).sum()),
        "duplicate_unix": int(raw["Unix"].duplicated().sum()),
        "negative_values": int((raw[["aggregate_w", "wm_w"]] < 0).sum().sum()),
        "wm_max_w": float(raw["wm_w"].max()),
        "aggregate_max_w": float(raw["aggregate_w"].max()),
    }

    # Sort and de-duplicate. Rule: keep the first reading for a repeated Unix second
    # (the cleaned files are expected to be clean already; this is a safety net).
    raw = raw.sort_values("Unix", kind="stable").drop_duplicates("Unix", keep="first")

    t = raw["Unix"].to_numpy(dtype=float)
    # WM: only non-zero values above the activity threshold can be "frozen" -
    # a constant 0-3 W standby reading for hours is physically normal.
    wm_frozen = _frozen_mask(t, raw["wm_w"].to_numpy(dtype=float), ACTIVE_W, FROZEN_MIN)
    # Aggregate: a whole house never holds one exact wattage for an hour (fridges cycle),
    # so any long constant run, including 0, is treated as a frozen/forward-filled reading.
    agg_frozen = _frozen_mask(t, raw["aggregate_w"].to_numpy(dtype=float), -1.0, FROZEN_MIN)
    checks["wm_frozen_samples"] = int(wm_frozen.sum())
    checks["aggregate_frozen_samples"] = int(agg_frozen.sum())

    raw.loc[wm_frozen, "wm_w"] = np.nan
    raw.loc[agg_frozen, "aggregate_w"] = np.nan
    raw.index = pd.to_datetime(raw["Unix"], unit="s", utc=True)
    return raw.drop(columns="Unix"), checks


def resample_1min(samples: pd.DataFrame) -> pd.DataFrame:
    """1-minute arithmetic mean of the ~6-8 s readings.

    A minute with no valid reading stays NaN (missing) - it is never filled with 0,
    because missing data is not evidence that the machine was off. The index covers
    every minute from the first to the last reading so gaps are explicit.
    """
    m = samples[["aggregate_w", "wm_w"]].resample("1min").mean()
    m["issues"] = samples["issues"].resample("1min").max()
    m["n_samples"] = samples["wm_w"].resample("1min").count()
    return m


def prepare_all(save: bool = True) -> tuple[dict, pd.DataFrame]:
    """Load + resample every selected house. Returns {house: 1-min frame} and the
    integrity-check table."""
    ensure_dirs()
    minute, checks = {}, []
    for h in HOUSES:
        samples, chk = load_house(h)
        m = resample_1min(samples)
        minute[h] = m
        chk["wm_frozen_minutes"] = int((m["wm_w"].isna() & m["aggregate_w"].notna()).sum())
        checks.append(chk)
        if save:
            m.to_parquet(PROC_DIR / f"house{h}_wm_1min.parquet")
    return minute, pd.DataFrame(checks)


# ============================================================================
# 2. Availability and data quality
# ============================================================================
def _runs(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Start and end (inclusive) indices of consecutive True runs."""
    d = np.diff(np.r_[0, mask.astype(np.int8), 0])
    return np.flatnonzero(d == 1), np.flatnonzero(d == -1) - 1


def gap_table(m: pd.DataFrame, house: int, min_minutes: int = 60) -> pd.DataFrame:
    """All missing-WM stretches of at least `min_minutes` minutes."""
    s, e = _runs(m["wm_w"].isna().to_numpy())
    idx = m.index
    g = pd.DataFrame({"house": house, "gap_start_utc": idx[s], "gap_end_utc": idx[e],
                      "duration_min": e - s + 1})
    g["duration_days"] = (g["duration_min"] / 1440).round(2)
    return g[g["duration_min"] >= min_minutes].reset_index(drop=True)


def quality_summary(minute: dict) -> pd.DataFrame:
    rows = []
    for h, m in minute.items():
        wm = m["wm_w"]
        valid = wm.notna()
        s, e = _runs((~valid).to_numpy())
        glen = e - s + 1
        longest = int(glen.argmax()) if len(glen) else None
        both_valid = valid & m["aggregate_w"].notna()
        rows.append({
            "house": h,
            "wm_channel": HOUSES[h]["channel"],
            "start_utc": m.index[0], "end_utc": m.index[-1],
            "span_days": round(len(m) / 1440, 1),
            "expected_minutes": len(m),
            "valid_wm_minutes": int(valid.sum()),
            "missing_wm_minutes": int((~valid).sum()),
            "availability_pct": round(100 * valid.mean(), 2),
            "gaps_total": len(glen),
            "gaps_le_5min": int((glen <= 5).sum()),
            "gaps_gt_60min": int((glen > 60).sum()),
            "gaps_gt_1day": int((glen > 1440).sum()),
            "longest_gap_days": round(glen[longest] / 1440, 1) if longest is not None else 0,
            "longest_gap_start_utc": m.index[s[longest]] if longest is not None else pd.NaT,
            "issues_flag_pct_of_valid": round(100 * (m.loc[valid, "issues"] > 0).mean(), 2),
            "wm_exact_zero_pct_of_valid": round(100 * (wm[valid] == 0).mean(), 2),
            "wm_off_pct_of_valid": round(100 * (wm[valid] <= ACTIVE_W).mean(), 2),
            "wm_active_pct_of_valid": round(100 * (wm[valid] > ACTIVE_W).mean(), 2),
            "aggregate_missing_while_wm_valid_min": int((valid & m["aggregate_w"].isna()).sum()),
            "both_valid_minutes": int(both_valid.sum()),
        })
    return pd.DataFrame(rows)


def daily_availability(minute: dict) -> pd.DataFrame:
    """Fraction of valid WM minutes per UTC calendar day (houses as columns)."""
    return pd.DataFrame({h: m["wm_w"].notna().resample("D").mean() for h, m in minute.items()})


# ============================================================================
# 3. Activity periods and likely wash cycles
# ============================================================================
def detect_activity(m: pd.DataFrame, house: int, active_w: float = ACTIVE_W,
                    merge_gap: int = MERGE_GAP_MIN) -> pd.DataFrame:
    """Detect WM *activity periods* from 1-minute data.

    Rule (plain language):
      1. A minute is "active" when the machine's average power that minute is above
         `active_w` (20 W). Standby/off readings sit at 0-6 W in every selected house.
      2. Active minutes separated by a pause of `merge_gap` minutes (15) or less are
         joined into one period - a wash programme pauses briefly between fill,
         wash, rinse and spin, but separate uses are normally hours apart.
    Missing minutes count as "not active" for joining, but any period with missing
    data inside it, or within `merge_gap` minutes of either end, is flagged
    `gap_affected` because it may be truncated or wrongly split.
    """
    wm = m["wm_w"].to_numpy()
    active = np.nan_to_num(wm, nan=0.0) > active_w
    s, e = _runs(active)
    if len(s) == 0:
        return pd.DataFrame()
    # Merge runs whose separating pause is <= merge_gap minutes
    keep = np.r_[True, (s[1:] - e[:-1] - 1) > merge_gap]
    starts = s[keep]
    ends = np.r_[e[np.flatnonzero(keep)[1:] - 1], e[-1]]

    missing = np.isnan(wm)
    cum_missing = np.r_[0, np.cumsum(missing)]
    wm0 = np.nan_to_num(wm, nan=0.0)
    cum_energy = np.r_[0, np.cumsum(wm0)]
    cum_active = np.r_[0, np.cumsum(active)]
    cum_heat = np.r_[0, np.cumsum(wm0 > HEATING_W)]
    n = len(wm)
    lo = np.clip(starts - merge_gap, 0, n)
    hi = np.clip(ends + merge_gap + 1, 0, n)

    peak = np.array([wm0[a:b + 1].max() for a, b in zip(starts, ends)])
    out = pd.DataFrame({
        "house": house,
        "start_utc": m.index[starts],
        "end_utc": m.index[ends],
        "duration_min": ends - starts + 1,
        "active_minutes": cum_active[ends + 1] - cum_active[starts],
        "energy_kwh": (cum_energy[ends + 1] - cum_energy[starts]) / 60.0 / 1000.0,
        "peak_w": peak,
        "heating_minutes": cum_heat[ends + 1] - cum_heat[starts],
        "missing_minutes_in_span": cum_missing[ends + 1] - cum_missing[starts],
        "gap_affected": (cum_missing[hi] - cum_missing[lo]) > 0,
    })
    out["start_local"] = out["start_utc"].dt.tz_convert(LOCAL_TZ)
    return out


def classify_cycles(act: pd.DataFrame) -> pd.DataFrame:
    """Label each activity period as a 'likely wash cycle' or 'other activity'.

    A detected period is evidence of the machine drawing power - not proof of a
    complete physical wash. We call it a *likely wash cycle* only if it looks like a
    programme: lasts 10-300 min, uses >= 0.05 kWh and peaks above 200 W (drum motor
    or heater). Shorter/smaller periods are typically door-lock, pump-out or
    programme-selection blips; longer ones are usually back-to-back loads merged
    together or residual sensor problems.
    """
    a = act.copy()
    a["likely_cycle"] = (
        a["duration_min"].between(CYCLE_MIN_DURATION_MIN, CYCLE_MAX_DURATION_MIN)
        & (a["energy_kwh"] >= CYCLE_MIN_ENERGY_KWH)
        & (a["peak_w"] >= CYCLE_MIN_PEAK_W)
    )
    a["category"] = np.select(
        [a["likely_cycle"] & ~a["gap_affected"], a["likely_cycle"] & a["gap_affected"],
         a["duration_min"] > CYCLE_MAX_DURATION_MIN],
        ["likely cycle (complete)", "likely cycle (gap-affected)", "long period (>300 min)"],
        default="short/small activity",
    )
    a["heated"] = a["heating_minutes"] > 0
    return a


def detect_all(minute: dict, **kw) -> pd.DataFrame:
    return pd.concat([classify_cycles(detect_activity(m, h, **kw)) for h, m in minute.items()],
                     ignore_index=True)


def sensitivity_table(minute: dict) -> pd.DataFrame:
    """Likely-cycle counts under alternative thresholds, to show how much the
    headline numbers depend on the chosen rule."""
    rows = []
    for thr in (10.0, 20.0, 50.0):
        for gap in (5, 10, 15, 30):
            a = detect_all(minute, active_w=thr, merge_gap=gap)
            for h in HOUSES:
                rows.append({"active_w": thr, "merge_gap_min": gap, "house": h,
                             "likely_cycles": int(a.loc[a["house"] == h, "likely_cycle"].sum())})
    t = pd.DataFrame(rows).pivot_table(index=["active_w", "merge_gap_min"], columns="house",
                                       values="likely_cycles").reset_index()
    t.columns = ["active_w", "merge_gap_min"] + [f"house_{h}" for h in HOUSES]
    return t


# ============================================================================
# 4. Usage patterns (hour of day, weekday) - normalised by observed time
# ============================================================================
def _local_valid(m: pd.DataFrame) -> pd.Series:
    v = m["wm_w"].dropna()
    v.index = v.index.tz_convert(LOCAL_TZ)
    return v


def hourly_profile(minute: dict, act: pd.DataFrame) -> pd.DataFrame:
    """Per house and local hour: cycle starts per 100 observed days and mean WM power.

    Dividing by *observed* hours (not calendar hours) keeps outages from looking
    like 'no washing'."""
    rows = []
    for h, m in minute.items():
        v = _local_valid(m)
        obs_days = v.groupby(v.index.hour).size() / 60.0          # observed hours = observed days for that hour
        mean_w = v.groupby(v.index.hour).mean()
        cyc = act[(act["house"] == h) & act["likely_cycle"]]
        starts = cyc["start_local"].dt.hour.value_counts().reindex(range(24), fill_value=0)
        for hr in range(24):
            rows.append({"house": h, "hour_local": hr, "observed_days": round(obs_days.get(hr, 0), 1),
                         "cycle_starts": int(starts[hr]),
                         "starts_per_100_days": 100 * starts[hr] / obs_days.get(hr, np.nan),
                         "mean_wm_w": mean_w.get(hr, np.nan)})
    return pd.DataFrame(rows)


def weekday_profile(minute: dict, act: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for h, m in minute.items():
        v = _local_valid(m)
        obs_days = v.groupby(v.index.dayofweek).size() / 1440.0
        kwh_day = v.groupby(v.index.dayofweek).mean() * 24 / 1000
        cyc = act[(act["house"] == h) & act["likely_cycle"]]
        starts = cyc["start_local"].dt.dayofweek.value_counts().reindex(range(7), fill_value=0)
        for d in range(7):
            rows.append({"house": h, "weekday": WEEKDAYS[d], "observed_days": round(obs_days.get(d, 0), 1),
                         "cycle_starts": int(starts[d]),
                         "cycles_per_observed_day": starts[d] / obs_days.get(d, np.nan),
                         "wm_kwh_per_observed_day": kwh_day.get(d, np.nan)})
    return pd.DataFrame(rows)


# ============================================================================
# 5. Cross-household comparison
# ============================================================================
def cycle_summary(minute: dict, act: pd.DataFrame) -> pd.DataFrame:
    """Frequency counts use all likely cycles; duration/energy/peak statistics use
    only *complete* cycles (no missing data in or next to the period)."""
    rows = []
    for h, m in minute.items():
        wm = m["wm_w"]
        valid = wm.notna()
        obs_days = valid.sum() / 1440
        a = act[act["house"] == h]
        lk = a[a["likely_cycle"]]
        cp = a[a["category"] == "likely cycle (complete)"]
        both = valid & m["aggregate_w"].notna()
        wm_kwh_day = wm[valid].mean() * 24 / 1000
        rows.append({
            "house": h, "occupancy": HOUSES[h]["occupancy"],
            "observed_days": round(obs_days, 1),
            "activity_periods": len(a),
            "likely_cycles": len(lk),
            "complete_cycles": len(cp),
            "gap_affected_cycles": int((a["category"] == "likely cycle (gap-affected)").sum()),
            "short_small_periods": int((a["category"] == "short/small activity").sum()),
            "long_periods_gt300min": int((a["category"] == "long period (>300 min)").sum()),
            "cycles_per_week": round(len(lk) / obs_days * 7, 2),
            "cycles_per_week_per_occupant": round(len(lk) / obs_days * 7 / HOUSES[h]["occupancy"], 2),
            "duration_min_median": cp["duration_min"].median(),
            "duration_min_q25": cp["duration_min"].quantile(0.25),
            "duration_min_q75": cp["duration_min"].quantile(0.75),
            "energy_kwh_median": round(cp["energy_kwh"].median(), 3),
            "energy_kwh_q25": round(cp["energy_kwh"].quantile(0.25), 3),
            "energy_kwh_q75": round(cp["energy_kwh"].quantile(0.75), 3),
            "peak_w_median": round(cp["peak_w"].median(), 0),
            "heated_cycle_pct": round(100 * cp["heated"].mean(), 1),
            "heating_min_median": cp["heating_minutes"].median(),
            "active_min_per_observed_day": round((wm[valid] > ACTIVE_W).sum() / obs_days, 1),
            "wm_kwh_per_observed_day": round(wm_kwh_day, 3),
            "wm_kwh_per_year_est": round(wm_kwh_day * 365, 0),
            "pct_wm_energy_in_likely_cycles": round(
                100 * lk["energy_kwh"].sum() / (wm[valid].sum() / 60 / 1000), 1),
            "aggregate_kwh_per_observed_day": round(m["aggregate_w"].mean() * 24 / 1000, 2),
            "wm_share_of_aggregate_pct": round(
                100 * m.loc[both, "wm_w"].sum() / m.loc[both, "aggregate_w"].sum(), 2),
        })
    return pd.DataFrame(rows)


# ============================================================================
# 6. Figures
# ============================================================================
def _style() -> None:
    plt.rcParams.update({
        "figure.dpi": 110, "savefig.dpi": 150, "font.size": 9,
        "axes.titlesize": 10, "axes.titleweight": "bold", "axes.labelsize": 9,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.edgecolor": "#8a8984", "axes.labelcolor": "#0b0b0b",
        "xtick.color": C_NEUTRAL, "ytick.color": C_NEUTRAL,
        "axes.grid": True, "grid.color": "#e6e5e0", "grid.linewidth": 0.6,
        "legend.frameon": False, "lines.linewidth": 1.4, "axes.axisbelow": True,
    })


def _hlabel(h: int) -> str:
    return f"House {h}\n({HOUSES[h]['occupancy']} occ.)"


def _save(fig, name: str) -> Path:
    path = FIG_DIR / name
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


def fig_availability(daily: pd.DataFrame) -> Path:
    _style()
    fig, ax = plt.subplots(figsize=(11, 2.8))
    # NaN = day outside that house's recording period (shown grey); 0 = recorded period, no data
    data = np.ma.masked_invalid((daily.T * 100).to_numpy())
    cmap = SEQ_BLUE.copy()
    cmap.set_bad("#d9d8d3")
    x0, x1 = matplotlib.dates.date2num(daily.index[0]), matplotlib.dates.date2num(daily.index[-1])
    im = ax.imshow(data, aspect="auto", cmap=cmap, vmin=0, vmax=100,
                   extent=[x0, x1 + 1, len(daily.columns) - 0.5, -0.5], interpolation="nearest")
    ax.set_yticks(range(len(daily.columns)), [f"House {h}" for h in daily.columns])
    ax.xaxis_date()
    ax.grid(False)
    ax.set_xlabel("Date (UTC)")
    ax.set_ylabel("Household")
    ax.set_title("Washing-machine channel availability: share of valid 1-minute readings per day "
                 "(grey = outside the house's recording period)")
    cb = fig.colorbar(im, ax=ax, pad=0.01)
    cb.set_label("Valid minutes (% of day)")
    return _save(fig, "01_wm_daily_availability.png")


def pick_representative_day(minute: dict, act: pd.DataFrame, house: int) -> tuple[pd.Timestamp, pd.Series]:
    """Pick, by rule, a local calendar day with >=95% valid data on both channels and
    exactly one complete likely cycle whose energy is closest to the house median."""
    m = minute[house].copy()
    m.index = m.index.tz_convert(LOCAL_TZ)
    ok = (m["wm_w"].notna() & m["aggregate_w"].notna()).groupby(m.index.normalize()).mean()
    good_days = set(ok[ok >= GOOD_DAY_MIN_AVAIL].index)
    cp = act[(act["house"] == house) & (act["category"] == "likely cycle (complete)")].copy()
    cp["day"] = cp["start_local"].dt.normalize()
    one = cp.groupby("day").filter(lambda g: len(g) == 1)
    # whole cycle must end on the same local day so the trace shows it completely
    one = one[one["day"].isin(good_days) & (one["end_utc"].dt.tz_convert(LOCAL_TZ).dt.normalize() == one["day"])]
    med = cp["energy_kwh"].median()
    row = one.iloc[(one["energy_kwh"] - med).abs().argmin()]
    return row["day"], row


def fig_representative_day(minute: dict, act: pd.DataFrame, house: int) -> tuple[Path, dict]:
    _style()
    day, cyc = pick_representative_day(minute, act, house)
    m = minute[house].copy()
    m.index = m.index.tz_convert(LOCAL_TZ)
    d = m.loc[day: day + pd.Timedelta(hours=23, minutes=59)]
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 6), sharex=True,
                                 gridspec_kw={"height_ratios": [1.3, 1]})
    a1.plot(d.index, d["aggregate_w"], color=C_AGG, label="Whole-house aggregate")
    a1.plot(d.index, d["wm_w"], color=C_WM, label=f"Washing machine ({HOUSES[house]['channel']})")
    a1.set_ylabel("Active power (W)")
    a1.set_title(f"House {house}, {day:%a %d %b %Y}: 24-hour aggregate and washing-machine power "
                 "(1-minute means, UK local time)")
    a1.legend(loc="upper left")
    a2.plot(d.index, d["wm_w"], color=C_WM, label="Washing machine")
    a2.axhline(ACTIVE_W, color=C_NEUTRAL, ls="--", lw=1, label=f"Activity threshold ({ACTIVE_W:.0f} W)")
    s, e = cyc["start_utc"].tz_convert(LOCAL_TZ), cyc["end_utc"].tz_convert(LOCAL_TZ)
    a2.axvspan(s, e + pd.Timedelta(minutes=1), color=C_WM, alpha=0.12, lw=0,
               label=f"Detected cycle: {cyc['duration_min']} min, {cyc['energy_kwh']:.2f} kWh")
    a2.set_ylabel("Active power (W)")
    a2.set_xlabel("Local time (Europe/London)")
    a2.set_title("Washing-machine channel with activity threshold and detected cycle")
    a2.legend(loc="upper left")
    a2.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%H:%M", tz=d.index.tz))
    info = {"house": house, "day": str(day.date()), "cycle_start_local": str(s), "duration_min": int(cyc["duration_min"]),
            "energy_kwh": round(float(cyc["energy_kwh"]), 3), "peak_w": round(float(cyc["peak_w"]), 0),
            "day_aggregate_kwh": round(float(d["aggregate_w"].mean() * 24 / 1000), 2),
            "day_wm_kwh": round(float(d["wm_w"].mean() * 24 / 1000), 2)}
    return _save(fig, "02_representative_24h_aggregate_and_wm.png"), info


def representative_cycles(act: pd.DataFrame) -> pd.DataFrame:
    """For each house, the complete likely cycle whose energy is closest to that
    house's median (a 'typical' cycle, chosen by rule rather than by eye)."""
    out = []
    for h in HOUSES:
        cp = act[(act["house"] == h) & (act["category"] == "likely cycle (complete)")]
        out.append(cp.iloc[(cp["energy_kwh"] - cp["energy_kwh"].median()).abs().argmin()])
    return pd.DataFrame(out)


def fig_representative_cycles(minute: dict, act: pd.DataFrame) -> Path:
    _style()
    reps = representative_cycles(act)
    fig, axes = plt.subplots(2, 3, figsize=(12, 6), sharey=True)
    for ax, (_, r) in zip(axes.flat, reps.iterrows()):
        m = minute[r["house"]]
        seg = m.loc[r["start_utc"] - pd.Timedelta(minutes=10): r["end_utc"] + pd.Timedelta(minutes=10), "wm_w"]
        x = (seg.index - r["start_utc"]).total_seconds() / 60
        ax.plot(x, seg.to_numpy(), color=C_WM, label="Washing machine")
        ax.axhline(ACTIVE_W, color=C_NEUTRAL, ls="--", lw=1, label=f"{ACTIVE_W:.0f} W threshold")
        ax.axvspan(0, r["duration_min"], color=C_WM, alpha=0.10, lw=0, label="Detected period")
        ax.set_title(f"House {r['house']}  |  {r['start_local']:%d %b %Y %H:%M}\n"
                     f"{r['duration_min']} min, {r['energy_kwh']:.2f} kWh, peak {r['peak_w']:.0f} W",
                     fontsize=9)
        ax.set_xlabel("Minutes since cycle start (min)")
    for ax in axes[:, 0]:
        ax.set_ylabel("Active power (W)")
    axes.flat[0].legend(loc="upper right", fontsize=8)
    fig.suptitle("Representative detected washing-machine cycles (cycle closest to each house's median energy)",
                 fontweight="bold", fontsize=11)
    fig.tight_layout()
    return _save(fig, "03_representative_cycles_by_house.png")


def fig_period_classification(act: pd.DataFrame) -> Path:
    """Duration vs energy of every activity period - shows what the cycle rule keeps."""
    _style()
    cats = [("likely cycle (complete)", "#2a78d6", "o"),
            ("likely cycle (gap-affected)", "#eb6834", "^"),
            ("other activity (short/small or >300 min)", "#1baf7a", "s")]
    fig, axes = plt.subplots(2, 3, figsize=(12, 6.5), sharex=True, sharey=True)
    for ax, h in zip(axes.flat, HOUSES):
        a = act[act["house"] == h]
        groups = [a[a["category"] == cats[0][0]], a[a["category"] == cats[1][0]],
                  a[~a["likely_cycle"]]]
        for (lab, col, mk), g in zip(cats, groups):
            ax.scatter(g["duration_min"], g["energy_kwh"].clip(lower=1e-3), s=10, color=col, marker=mk,
                       alpha=0.6, linewidths=0, label=lab)
        ax.set_xscale("log"); ax.set_yscale("log")
        ax.axvline(CYCLE_MIN_DURATION_MIN, color=C_NEUTRAL, lw=0.8, ls=":")
        ax.axvline(CYCLE_MAX_DURATION_MIN, color=C_NEUTRAL, lw=0.8, ls=":")
        ax.axhline(CYCLE_MIN_ENERGY_KWH, color=C_NEUTRAL, lw=0.8, ls=":")
        ax.set_title(f"House {h}: {len(groups[0])} complete, {len(groups[1])} gap-affected, "
                     f"{len(groups[2])} other", fontsize=9)
    for ax in axes[1]:
        ax.set_xlabel("Period duration (min, log scale)")
    for ax in axes[:, 0]:
        ax.set_ylabel("Energy in period (kWh, log scale)")
    handles, labels = axes.flat[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=3, bbox_to_anchor=(0.5, -0.03), markerscale=1.6)
    fig.suptitle("Detected activity periods vs likely wash cycles (dotted lines = cycle-rule limits; "
                 "energies below 0.001 kWh drawn at 0.001)", fontweight="bold", fontsize=11)
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    return _save(fig, "04_activity_period_classification.png")


def _heatmap(ax, mat: pd.DataFrame, cbar_label: str, fmt: str | None, fig) -> None:
    im = ax.imshow(mat.to_numpy(), aspect="auto", cmap=SEQ_BLUE, vmin=0)
    ax.set_yticks(range(len(mat.index)), [f"House {h}" for h in mat.index])
    ax.set_xticks(range(len(mat.columns)), mat.columns)
    ax.grid(False)
    for s in ax.spines.values():
        s.set_visible(False)
    if fmt:
        vmax = np.nanmax(mat.to_numpy())
        for i in range(mat.shape[0]):
            for j in range(mat.shape[1]):
                val = mat.iat[i, j]
                ax.text(j, i, format(val, fmt), ha="center", va="center", fontsize=7,
                        color="white" if val > 0.55 * vmax else "#0b0b0b")
    cb = fig.colorbar(im, ax=ax, pad=0.01)
    cb.set_label(cbar_label)


def fig_hourly(hourly: pd.DataFrame) -> Path:
    _style()
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(12, 6.5))
    m1 = hourly.pivot(index="house", columns="hour_local", values="starts_per_100_days")
    m2 = hourly.pivot(index="house", columns="hour_local", values="mean_wm_w")
    _heatmap(a1, m1, "Cycle starts per 100 observed days", ".0f", fig)
    a1.set_title("Likely wash-cycle starts by hour of day (normalised by observed time)")
    a1.set_xlabel("Hour of day, UK local time (h)")
    a1.set_ylabel("Household")
    _heatmap(a2, m2, "Mean WM power (W)", ".0f", fig)
    a2.set_title("Average washing-machine power by hour of day (all valid minutes)")
    a2.set_xlabel("Hour of day, UK local time (h)")
    a2.set_ylabel("Household")
    fig.tight_layout()
    return _save(fig, "05_usage_by_hour_of_day.png")


def fig_weekday(weekly: pd.DataFrame) -> Path:
    _style()
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 3.6))
    m1 = weekly.pivot(index="house", columns="weekday", values="cycles_per_observed_day")[WEEKDAYS]
    m2 = weekly.pivot(index="house", columns="weekday", values="wm_kwh_per_observed_day")[WEEKDAYS]
    _heatmap(a1, m1, "Likely cycles per observed day", ".2f", fig)
    a1.set_title("Likely wash cycles per observed day, by weekday")
    a1.set_xlabel("Day of week (UK local time)")
    a1.set_ylabel("Household")
    _heatmap(a2, m2, "WM energy (kWh per observed day)", ".2f", fig)
    a2.set_title("Washing-machine energy per observed day, by weekday")
    a2.set_xlabel("Day of week (UK local time)")
    a2.set_ylabel("Household")
    fig.tight_layout()
    return _save(fig, "06_usage_by_weekday.png")


def fig_cross_house(summary: pd.DataFrame, act: pd.DataFrame) -> Path:
    _style()
    hs = list(HOUSES)
    labels = [_hlabel(h) for h in hs]
    cp = act[act["category"] == "likely cycle (complete)"]
    fig, axes = plt.subplots(2, 2, figsize=(12, 7.5))
    s = summary.set_index("house").loc[hs]

    ax = axes[0, 0]
    ax.bar(labels, s["cycles_per_week"], color=C_AGG, width=0.6)
    for i, v in enumerate(s["cycles_per_week"]):
        ax.text(i, v, f"{v:.1f}", ha="center", va="bottom", fontsize=8)
    ax.set_title("Cycle frequency")
    ax.set_ylabel("Likely cycles per observed week")

    ax = axes[0, 1]
    ax.bar(labels, s["wm_kwh_per_observed_day"], color=C_AGG, width=0.6)
    for i, v in enumerate(s["wm_kwh_per_observed_day"]):
        ax.text(i, v, f"{v:.2f}", ha="center", va="bottom", fontsize=8)
    ax.set_title("Washing-machine energy use")
    ax.set_ylabel("WM energy (kWh per observed day)")

    box_kw = dict(widths=0.5, patch_artist=True, showfliers=False,
                  medianprops=dict(color="#0b0b0b", lw=1.4),
                  boxprops=dict(facecolor="#cde2fb", edgecolor=C_AGG),
                  whiskerprops=dict(color=C_AGG), capprops=dict(color=C_AGG))
    ax = axes[1, 0]
    ax.boxplot([cp.loc[cp["house"] == h, "duration_min"] for h in hs], tick_labels=labels, **box_kw)
    ax.set_title("Cycle duration (complete likely cycles)")
    ax.set_ylabel("Duration (min)")

    ax = axes[1, 1]
    ax.boxplot([cp.loc[cp["house"] == h, "energy_kwh"] for h in hs], tick_labels=labels, **box_kw)
    ax.set_title("Energy per cycle (complete likely cycles)")
    ax.set_ylabel("Energy per cycle (kWh)")
    for ax in axes[1]:
        ax.set_xlabel("Household (occupants)")
    fig.suptitle("Cross-household comparison of washing-machine use "
                 "(boxes: interquartile range, line: median, whiskers: 1.5×IQR, outliers hidden)",
                 fontweight="bold", fontsize=11)
    fig.tight_layout()
    return _save(fig, "07_cross_house_comparison.png")


# ============================================================================
# 7. End-to-end run
# ============================================================================
REP_DAY_HOUSE = 8   # house used for the single representative 24-hour trace


def mapping_table() -> pd.DataFrame:
    rows = [{"house": h, "wm_channel": v["channel"], "readme_label": v["readme_label"],
             "occupancy": v["occupancy"], "selected": True, "note": ""} for h, v in HOUSES.items()]
    rows[list(HOUSES).index(5)]["note"] = (
        "Model AEG L99695HWD appears to be a washer-dryer; long ~2.2 kW periods suggest drying")
    rows += [{"house": h, "wm_channel": "", "readme_label": "", "occupancy": np.nan,
              "selected": False, "note": why} for h, why in EXCLUDED.items()]
    return pd.DataFrame(rows)


def save_tables(res: dict) -> list[Path]:
    """Write every Phase 2 summary table to outputs/tables/phase2/."""
    tables = {
        "01_household_channel_mapping.csv": mapping_table(),
        "02_integrity_checks.csv": res["checks"],
        "03_availability_quality_summary.csv": res["quality"],
        "04_gaps_over_1h.csv": res["gaps"],
        "05_activity_periods_all.csv": res["act"],
        "06_cycle_summary_by_house.csv": res["summary"],
        "07_hourly_profile.csv": res["hourly"],
        "08_weekday_profile.csv": res["weekly"],
        "09_threshold_sensitivity.csv": res["sens"],
        "10_representative_cycles.csv": res["reps"],
        "11_representative_day.csv": pd.DataFrame([res["rep_day"]]),
        "12_key_numbers.csv": key_numbers(res),
    }
    paths = []
    for name, t in tables.items():
        t.to_csv(TAB_DIR / name, index=False)
        paths.append(TAB_DIR / name)
    return paths


def key_numbers(res: dict) -> pd.DataFrame:
    """Headline numbers quoted in RESULTS.md, derived here so the docs can be checked."""
    s, q, a = res["summary"], res["quality"], res["act"]
    hourly = res["hourly"]
    peak_hour = hourly.loc[hourly.groupby("house")["starts_per_100_days"].idxmax(), ["house", "hour_local"]]
    wk = res["weekly"]
    wk_share = (wk.assign(weekend=wk["weekday"].isin(["Sat", "Sun"]))
                  .groupby(["house", "weekend"])["cycles_per_observed_day"].mean().unstack())
    rows = [
        ("houses_analysed", len(s)),
        ("total_activity_periods", len(a)),
        ("total_likely_cycles", int(a["likely_cycle"].sum())),
        ("total_complete_cycles", int((a["category"] == "likely cycle (complete)").sum())),
        ("availability_pct_min", q["availability_pct"].min()),
        ("availability_pct_max", q["availability_pct"].max()),
        ("cycles_per_week_min", s["cycles_per_week"].min()),
        ("cycles_per_week_max", s["cycles_per_week"].max()),
        ("median_cycle_kwh_min", s["energy_kwh_median"].min()),
        ("median_cycle_kwh_max", s["energy_kwh_median"].max()),
        ("median_duration_min_min", s["duration_min_median"].min()),
        ("median_duration_min_max", s["duration_min_median"].max()),
        ("wm_share_of_aggregate_pct_min", s["wm_share_of_aggregate_pct"].min()),
        ("wm_share_of_aggregate_pct_max", s["wm_share_of_aggregate_pct"].max()),
    ]
    rows += [(f"peak_start_hour_house_{r.house}", r.hour_local) for r in peak_hour.itertuples()]
    rows += [(f"weekend_to_weekday_cycle_rate_ratio_house_{h}", round(r[True] / r[False], 2))
             for h, r in wk_share.iterrows()]
    return pd.DataFrame(rows, columns=["metric", "value"])


def run_all(verbose: bool = True) -> dict:
    ensure_dirs()
    minute, checks = prepare_all(save=True)
    act = detect_all(minute)
    res = dict(minute=minute, checks=checks, quality=quality_summary(minute),
               gaps=pd.concat([gap_table(m, h) for h, m in minute.items()], ignore_index=True),
               daily=daily_availability(minute), act=act, sens=sensitivity_table(minute),
               hourly=hourly_profile(minute, act), weekly=weekday_profile(minute, act),
               summary=cycle_summary(minute, act), reps=representative_cycles(act))

    figs = [fig_availability(res["daily"])]
    p, res["rep_day"] = fig_representative_day(minute, act, REP_DAY_HOUSE)
    figs += [p, fig_representative_cycles(minute, act), fig_period_classification(act),
             fig_hourly(res["hourly"]), fig_weekday(res["weekly"]), fig_cross_house(res["summary"], act)]
    tabs = save_tables(res)

    if verbose:
        pd.set_option("display.width", 250)
        print(res["summary"].to_string())
        print(key_numbers(res).to_string())
        for f in figs + tabs:
            print("saved", f.relative_to(ROOT))
    return res


if __name__ == "__main__":
    run_all()
