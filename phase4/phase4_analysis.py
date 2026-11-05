"""
Phase 4 - Practical implications: supporting analysis for the peak-shifting recommendation.

Read-only with respect to Phases 1-3: imports their functions and reads their outputs
(Phase 2/3 1-minute data, Phase 3 test predictions and run settings). Writes only to
outputs/tables/phase4/ and outputs/figures/phase4/. No model is trained or tuned here.

Run from anywhere:  python3 phase4/phase4_analysis.py
"""
from __future__ import annotations

import json
import sys

sys.dont_write_bytecode = True          # do not create __pycache__ inside phase2/ or phase3/
from pathlib import Path  # noqa: E402

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "phase3"))
import wm_nilm as p3  # noqa: E402  (Phase 3 functions, read-only; imports Phase 2's wm_eda too)

p2 = p3.p2
import matplotlib  # noqa: E402,F401
import matplotlib.pyplot as plt  # noqa: E402

TAB_DIR = ROOT / "outputs" / "tables" / "phase4"
FIG_DIR = ROOT / "outputs" / "figures" / "phase4"
P3_PRED = ROOT / "Data" / "processed" / "phase3" / "test_predictions.parquet"
P3_RUN_INFO = ROOT / "outputs" / "models" / "phase3" / "run_info.json"

HOUSES = p3.MODEL_HOUSES               # 2, 7, 8, 9, 15 (House 5 excluded: probable washer-dryer)
LOCAL_TZ = p2.LOCAL_TZ                 # Europe/London: local clock time, DST handled by tz conversion
WINDOW_START_H, WINDOW_END_H = 16, 19  # chosen study window: weekdays 16:00-18:59 local time
WINDOW_HOURS = WINDOW_END_H - WINDOW_START_H
WEEKDAYS_PER_YEAR = 261                # 52.18 weeks x 5; bank holidays not removed (stated assumption)
ACTIVE_W = p3.ACTIVE_W
RESOLUTIONS = (1, 15, 30)              # minutes


def ensure_dirs() -> None:
    for d in (TAB_DIR, FIG_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ============================================================================
# Time helpers - everything is decided on the household's local clock
# ============================================================================
def local(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    return idx.tz_convert(LOCAL_TZ)


def weekday_mask(idx: pd.DatetimeIndex) -> np.ndarray:
    return (local(idx).dayofweek < 5)


def window_mask(idx: pd.DatetimeIndex) -> np.ndarray:
    loc = local(idx)
    return (loc.dayofweek < 5) & (loc.hour >= WINDOW_START_H) & (loc.hour < WINDOW_END_H)


def starts_in_window(start_utc: pd.Series) -> np.ndarray:
    return window_mask(pd.DatetimeIndex(start_utc))


def starts_on_weekday(start_utc: pd.Series) -> np.ndarray:
    return weekday_mask(pd.DatetimeIndex(start_utc))


def dst_check(d: dict) -> pd.DataFrame:
    """The window is defined on the local clock, so in UTC it must sit at 16:00-18:59 under
    GMT (UTC+0) and at 15:00-17:59 under BST (UTC+1), and every complete weekday must hold
    exactly 180 window minutes - including the weekdays right after each clock change.
    (UK clocks change on Sundays, so the change days themselves contain no window minutes.)"""
    rows = []
    for h in HOUSES:
        idx = d["minute"][h].index
        loc = local(idx)
        day = loc.normalize()
        win = window_mask(idx)
        per_day = pd.Series(win, index=idx).groupby(day).sum()
        n_min = pd.Series(1, index=idx).groupby(day).sum()
        sel = (per_day.index.dayofweek < 5) & (n_min.reindex(per_day.index).to_numpy() == 1440)
        offset_h = np.array([t.utcoffset().total_seconds() / 3600 for t in loc[win]])
        utc_h = idx[win].hour
        gmt_hours = sorted(set(utc_h[offset_h == 0])), sorted(set(utc_h[offset_h == 1]))
        off_day = pd.Series([t.utcoffset().total_seconds() for t in loc], index=idx).groupby(day).first()
        # First local day whose first minute carries the new UTC offset. UK clocks change at 01:00 UTC on a
        # Sunday, so the Sunday still starts on the old offset and this is the Monday after the change.
        mondays = list(off_day.index[1:][off_day.to_numpy()[1:] != off_day.to_numpy()[:-1]])
        assert all(x.dayofweek == 0 for x in mondays), "expected the Monday after each clock change"
        change_days = mondays
        rows.append({"house": h, "complete_weekdays": int(sel.sum()),
                     "weekdays_with_180_window_minutes": int((per_day[sel] == 180).sum()),
                     "window_utc_hours_when_GMT": ",".join(map(str, gmt_hours[0])),
                     "window_utc_hours_when_BST": ",".join(map(str, gmt_hours[1])),
                     "clock_changes_in_record": len(change_days),
                     "window_minutes_on_monday_after_each_change": ";".join(
                         f"{x:%Y-%m-%d}={int(per_day.get(x, 0))}" for x in mondays)})
    return pd.DataFrame(rows)


# ============================================================================
# 1. Peak-window exposure (full usable record, sub-metered ground truth)
# ============================================================================
def true_cycles_full(d: dict, h: int) -> pd.DataFrame:
    """Likely wash cycles on the house's full usable record (Phase 3 usability mask,
    Phase 2 cycle rule) - the same definitions used throughout the project."""
    m, f = d["minute"][h], d["flags"][h]
    t = m.loc[f["usable"]]
    c = p3.detect_cycles(m.index, t, t["wm_w"].to_numpy(), h, ACTIVE_W)
    return c[c["likely_cycle"]].copy()


def peak_exposure(d: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    rows, cyc_rows = [], []
    for h in HOUSES:
        m, f = d["minute"][h], d["flags"][h]
        u = f["usable"].to_numpy()
        wm = m["wm_w"].to_numpy()
        wk, win = weekday_mask(m.index) & u, window_mask(m.index) & u
        c = true_cycles_full(d, h)
        c_wk = c[starts_on_weekday(c["start_utc"])]
        c_in = c_wk[starts_in_window(c_wk["start_utc"])]

        # Attribute every window minute's WM energy to where its cycle started
        cat = np.full(len(m), "outside any likely cycle", dtype=object)
        idx_ns = m.index.asi8
        for r in c.itertuples():
            a = np.searchsorted(idx_ns, r.start_utc.value)
            b = np.searchsorted(idx_ns, r.end_utc.value, side="right")
            st_loc = r.start_utc.tz_convert(LOCAL_TZ)
            if st_loc.dayofweek < 5 and WINDOW_START_H <= st_loc.hour < WINDOW_END_H:
                cat[a:b] = "cycle started in window"
            else:
                cat[a:b] = "cycle started before/outside window"
        e_win = np.nansum(wm[win]) / 60000
        e_wk = np.nansum(wm[wk]) / 60000
        comp = {k: np.nansum(wm[win & (cat == k)]) / 60000 for k in
                ("cycle started in window", "cycle started before/outside window", "outside any likely cycle")}

        # For cycles that start in the window: what fraction of their energy actually falls inside it?
        frac_inside = []
        for r in c_in.itertuples():
            a = np.searchsorted(idx_ns, r.start_utc.value)
            b = np.searchsorted(idx_ns, r.end_utc.value, side="right")
            e_all = np.nansum(wm[a:b])
            frac_inside.append(np.nansum(wm[a:b][win[a:b]]) / e_all if e_all > 0 else np.nan)
            cyc_rows.append({"house": h, "start_local": r.start_utc.tz_convert(LOCAL_TZ), "duration_min": r.duration_min,
                             "energy_kwh": r.energy_kwh, "frac_energy_inside_window": frac_inside[-1]})
        mean_w_win = np.nanmean(wm[win])
        rows.append({
            "house": h,
            "observed_weekday_days": round(wk.sum() / 1440, 1),
            "observed_window_hours": round(win.sum() / 60, 1),
            "weekday_likely_cycles": len(c_wk),
            "weekday_cycles_starting_in_window": len(c_in),
            "pct_weekday_starts_in_window": 100 * len(c_in) / len(c_wk) if len(c_wk) else np.nan,
            "weekday_wm_kwh_observed": e_wk,
            "window_wm_kwh_observed": e_win,
            "pct_weekday_wm_energy_in_window": 100 * e_win / e_wk if e_wk else np.nan,
            "mean_wm_w_in_window": mean_w_win,
            "mean_wm_w_weekday_outside_window": np.nanmean(wm[wk & ~win]),
            "window_kwh_from_cycles_started_in_window": comp["cycle started in window"],
            "window_kwh_from_cycles_started_earlier": comp["cycle started before/outside window"],
            "window_kwh_outside_likely_cycles": comp["outside any likely cycle"],
            "median_frac_energy_inside_window_for_window_starts": np.nanmedian(frac_inside) if frac_inside else np.nan,
            "annual_wm_kwh_in_window_est": mean_w_win * WINDOW_HOURS * WEEKDAYS_PER_YEAR / 1000,
            "mean_aggregate_kw_in_window": np.nanmean(m["aggregate_w"].to_numpy()[win]) / 1000,
            "window_aggregate_kwh_observed": np.nansum(m["aggregate_w"].to_numpy()[win]) / 60000,
            "outside_window_weekday_hours": round((wk & ~win).sum() / 60, 1),
        })
    t = pd.DataFrame(rows)
    pooled = {"house": "pooled", "observed_weekday_days": t["observed_weekday_days"].sum(),
              "observed_window_hours": t["observed_window_hours"].sum(),
              "weekday_likely_cycles": t["weekday_likely_cycles"].sum(),
              "weekday_cycles_starting_in_window": t["weekday_cycles_starting_in_window"].sum()}
    pooled["pct_weekday_starts_in_window"] = 100 * pooled["weekday_cycles_starting_in_window"] / pooled["weekday_likely_cycles"]
    for k in ("weekday_wm_kwh_observed", "window_wm_kwh_observed", "window_kwh_from_cycles_started_in_window",
              "window_kwh_from_cycles_started_earlier", "window_kwh_outside_likely_cycles",
              "window_aggregate_kwh_observed", "outside_window_weekday_hours"):
        pooled[k] = t[k].sum()
    pooled["pct_weekday_wm_energy_in_window"] = 100 * pooled["window_wm_kwh_observed"] / pooled["weekday_wm_kwh_observed"]
    # Pooled mean power = total window energy / total observed window hours (household-average W)
    pooled["mean_wm_w_in_window"] = 1000 * pooled["window_wm_kwh_observed"] / pooled["observed_window_hours"]
    pooled["annual_wm_kwh_in_window_est"] = pooled["mean_wm_w_in_window"] * WINDOW_HOURS * WEEKDAYS_PER_YEAR / 1000
    pooled["mean_wm_w_weekday_outside_window"] = (1000 * (pooled["weekday_wm_kwh_observed"] - pooled["window_wm_kwh_observed"])
                                                  / pooled["outside_window_weekday_hours"])
    pooled["mean_aggregate_kw_in_window"] = pooled["window_aggregate_kwh_observed"] / pooled["observed_window_hours"]
    cyc = pd.DataFrame(cyc_rows)
    pooled["median_frac_energy_inside_window_for_window_starts"] = cyc["frac_energy_inside_window"].median()
    t = pd.concat([t, pd.DataFrame([pooled])], ignore_index=True)
    t["wm_share_of_window_aggregate_pct"] = 100 * t["window_wm_kwh_observed"] / t["window_aggregate_kwh_observed"]
    return t, cyc


def heating_share(d: dict) -> pd.DataFrame:
    """Context for the cold-wash idea: share of sub-metered WM energy drawn while the 1-minute
    power exceeds 1 kW (heater on). Measured data, NOT a model output and NOT a savings estimate."""
    rows = []
    for h in HOUSES:
        m, f = d["minute"][h], d["flags"][h]
        wm = m.loc[f["usable"], "wm_w"]
        rows.append({"house": h, "wm_kwh": wm.sum() / 60000,
                     "pct_wm_energy_while_heating": 100 * wm[wm > p2.HEATING_W].sum() / wm.sum()})
    t = pd.DataFrame(rows)
    tot = t["wm_kwh"].sum()
    t.loc[len(t)] = {"house": "pooled", "wm_kwh": tot,
                     "pct_wm_energy_while_heating": (t["wm_kwh"] * t["pct_wm_energy_while_heating"]).sum() / tot}
    return t


# ============================================================================
# 2. Can the model identify peak-window washing? (Phase 3 test periods only)
# ============================================================================
def load_phase3_predictions() -> tuple[pd.DataFrame, dict]:
    if not P3_PRED.exists():
        raise FileNotFoundError(f"{P3_PRED} is missing - run `python3 phase3/wm_nilm.py` first (it is git-ignored).")
    return pd.read_parquet(P3_PRED), json.loads(P3_RUN_INFO.read_text())


def model_peak_comparison(d: dict) -> pd.DataFrame:
    """Actual vs predicted peak-window start shares on exactly the Phase 3 test rows.
    Both series use the same rows (Phase 3 test split, usable minutes), the same cycle rule
    (actual at 20 W, predicted at that setting's tau), and the same weekday/window definition."""
    preds, info = load_phase3_predictions()
    settings = (("within-house", "pred_centered", lambda h: info["tau_W"]),
                ("leave-one-house-out", "pred_loho", lambda h: info["loho_tau_W"][str(h)]))
    rows = []
    for criterion, min_iou in (("one-to-one IoU>=0.5", p3.MATCH_IOU), ("one-to-one any overlap (sensitivity)",
                                                                      p3.MATCH_IOU_SENSITIVITY)):
        for setting, col, tau_of in settings:
            for h in HOUSES:
                t = preds[preds["house"] == h]
                idx = d["minute"][h].index
                ta = p3.detect_cycles(idx, t, t["y"].to_numpy(), h, ACTIVE_W)
                ta = ta[ta["likely_cycle"]].reset_index(drop=True)
                tp = p3.detect_cycles(idx, t, t[col].to_numpy(), h, tau_of(h))
                tp = tp[tp["likely_cycle"]].reset_index(drop=True) if len(tp) else tp
                t_m, p_m, *_ = p3.match_cycles(ta, tp, min_iou)
                a_wk, p_wk = starts_on_weekday(ta["start_utc"]), starts_on_weekday(tp["start_utc"])
                a_in, p_in = a_wk & starts_in_window(ta["start_utc"]), p_wk & starts_in_window(tp["start_utc"])
                win = window_mask(t.index)
                rows.append({
                    "criterion": criterion, "setting": setting, "house": h, "tau_W": tau_of(h),
                    "actual_weekday_cycles": int(a_wk.sum()), "actual_window_starts": int(a_in.sum()),
                    "actual_pct_window_starts": 100 * a_in.sum() / a_wk.sum() if a_wk.sum() else np.nan,
                    "pred_weekday_cycles": int(p_wk.sum()), "pred_window_starts": int(p_in.sum()),
                    "pred_pct_window_starts": 100 * p_in.sum() / p_wk.sum() if p_wk.sum() else np.nan,
                    "actual_window_starts_matched": int((a_in & t_m).sum()),
                    "pred_window_starts_matched": int((p_in & p_m).sum()),
                    "actual_mean_wm_w_in_window": t.loc[win, "y"].mean(),
                    "pred_mean_wm_w_in_window": t.loc[win, col].mean(),
                    "total_true_cycles_check": len(ta),
                })
    r = pd.DataFrame(rows)
    r["error_pct_points"] = r["pred_pct_window_starts"] - r["actual_pct_window_starts"]
    r["window_recall"] = r["actual_window_starts_matched"] / r["actual_window_starts"]
    r["window_precision"] = r["pred_window_starts_matched"] / r["pred_window_starts"]
    r["mean_w_error_w"] = r["pred_mean_wm_w_in_window"] - r["actual_mean_wm_w_in_window"]
    pooled = []
    for (crit, setting), g in r.groupby(["criterion", "setting"], sort=False):
        s = g.sum(numeric_only=True)
        row = {"criterion": crit, "setting": setting, "house": "pooled",
               **{k: s[k] for k in ("actual_weekday_cycles", "actual_window_starts", "pred_weekday_cycles",
                                    "pred_window_starts", "actual_window_starts_matched", "pred_window_starts_matched",
                                    "total_true_cycles_check")}}
        row["actual_pct_window_starts"] = 100 * row["actual_window_starts"] / row["actual_weekday_cycles"]
        row["pred_pct_window_starts"] = 100 * row["pred_window_starts"] / row["pred_weekday_cycles"]
        row["error_pct_points"] = row["pred_pct_window_starts"] - row["actual_pct_window_starts"]
        row["window_recall"] = row["actual_window_starts_matched"] / row["actual_window_starts"]
        row["window_precision"] = row["pred_window_starts_matched"] / row["pred_window_starts"]
        row["mean_abs_house_error_pct_points"] = g["error_pct_points"].abs().mean()
        pooled.append(row)
    return pd.concat([r, pd.DataFrame(pooled)], ignore_index=True)


# ============================================================================
# 3. Information loss at 15 / 30 minutes (illustration only - no model is trained)
# ============================================================================
def _resample(s: pd.Series, r: int) -> pd.Series:
    """Clock-aligned r-minute means; an interval is kept only if all r minutes are present."""
    if r == 1:
        return s
    g = s.resample(f"{r}min")
    out = g.mean()
    return out.where(g.count() == r)


def resolution_illustration(d: dict) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """How visible is the WM heating step at 1, 15 and 30 minutes?

    For each true heated test-period cycle (Phase 3 test rows, same cycle rule):
      signal = highest interval-mean WM power during the cycle at resolution r
               (what the heater adds to the aggregate in its best interval)
      noise  = standard deviation of interval-to-interval changes of the *other* load
               (aggregate minus WM) over WM-off intervals of that house's test period
      visibility ratio = signal / noise.
    Also: share of WM-off intervals whose other-load change is at least as large as the
    house's median heater signal - how often something else looks like a heater step."""
    preds, _ = load_phase3_predictions()
    cyc_rows, house_rows, examples = [], [], {}
    for h in HOUSES:
        t = preds[preds["house"] == h]
        idx = d["minute"][h].index
        y = pd.Series(np.nan, index=idx)
        y.loc[t.index] = t["y"].to_numpy()
        agg = pd.Series(np.nan, index=idx)
        agg.loc[t.index] = t["agg"].to_numpy()
        tc = p3.detect_cycles(idx, t, t["y"].to_numpy(), h, ACTIVE_W)
        tc = tc[tc["likely_cycle"] & tc["heated"] & ~tc["gap_affected"]]
        for r in RESOLUTIONS:
            yr, ar = _resample(y, r), _resample(agg, r)
            other = ar - yr
            d_other = other.diff()
            off = (yr <= ACTIVE_W) & (yr.shift(1) <= ACTIVE_W)
            noise = d_other[off].std()
            sig = []
            for c in tc.itertuples():
                seg = yr.loc[c.start_utc.floor(f"{r}min"): c.end_utc]
                sig.append(seg.max())
            sig = np.array(sig, dtype=float)
            med_sig = np.nanmedian(sig)
            for c, s in zip(tc.itertuples(), sig):
                cyc_rows.append({"house": h, "resolution_min": r, "start_utc": c.start_utc,
                                 "signal_w": s, "noise_w": noise, "visibility_ratio": s / noise})
            house_rows.append({"house": h, "resolution_min": r, "heated_complete_cycles": len(tc),
                               "median_signal_w": med_sig, "noise_std_w": noise,
                               "median_visibility_ratio": np.nanmedian(sig / noise),
                               "pct_cycles_ratio_ge_2": 100 * np.nanmean(sig / noise >= 2),
                               "pct_off_intervals_other_step_ge_median_signal":
                                   100 * (d_other[off].abs() >= med_sig).mean()})
            examples[(h, r)] = (yr, ar)
    cyc = pd.DataFrame(cyc_rows)
    hs = pd.DataFrame(house_rows)
    pooled = (cyc.groupby("resolution_min").agg(heated_complete_cycles=("signal_w", "size"),
                                                median_signal_w=("signal_w", "median"),
                                                median_visibility_ratio=("visibility_ratio", "median"),
                                                pct_cycles_ratio_ge_2=("visibility_ratio", lambda v: 100 * (v >= 2).mean()))
              .reset_index().assign(house="pooled"))
    return pd.concat([hs, pooled], ignore_index=True), cyc, examples


# ============================================================================
# 4. Illustrative pilot sample size (NOT a power calculation for a real programme)
# ============================================================================
def illustrative_sample_size(exposure: pd.DataFrame) -> pd.DataFrame:
    """Households per arm needed to detect a drop in average 16:00-19:00 whole-house kW,
    for a simple two-arm comparison of household means (alpha 0.05 two-sided, 80% power):
        n = 2 * (1.96 + 0.84)^2 * sigma^2 / delta^2
    sigma = between-household SD of mean window kW in these 5 REFIT homes (very rough);
    delta = an assumed share of the pooled mean WM power in the window. The effects are
    hypothetical; the 100% row is a ceiling (every washing-machine watt removed)."""
    hh = exposure[exposure["house"] != "pooled"]
    sigma_kw = hh["mean_aggregate_kw_in_window"].std(ddof=1)
    wm_kw = exposure.loc[exposure["house"] == "pooled", "mean_wm_w_in_window"].iloc[0] / 1000
    rows = []
    for frac in (1.0, 0.5, 0.25):
        delta = wm_kw * frac
        rows.append({"assumed_reduction_share_of_wm_window_power": frac, "assumed_reduction_kw": delta,
                     "sigma_between_household_kw": sigma_kw,
                     "households_per_arm": int(np.ceil(2 * (1.96 + 0.84) ** 2 * sigma_kw ** 2 / delta ** 2))})
    return pd.DataFrame(rows)


# ============================================================================
# 5. Figures
# ============================================================================
C1, C2, C3, CN = "#2a78d6", "#eb6834", "#1baf7a", p2.C_NEUTRAL


def _save(fig, name):
    path = FIG_DIR / name
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


def fig_exposure(exp: pd.DataFrame) -> Path:
    p2._style()
    e = exp[exp["house"] != "pooled"]
    x = np.arange(len(e))
    labels = [f"House {h}" for h in e["house"]]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 4))
    a1.bar(x - 0.2, e["pct_weekday_starts_in_window"], 0.37, color=C1, label="Weekday wash starts in window")
    a1.bar(x + 0.2, e["pct_weekday_wm_energy_in_window"], 0.37, color=C2, label="Weekday WM energy in window")
    a1.axhline(100 * WINDOW_HOURS / 24, color=CN, ls="--", lw=1, label="Window share of weekday time (12.5%)")
    a1.set_xticks(x, labels)
    a1.set_ylabel("Share of weekday total (%)")
    a1.set_title("Washing in the 16:00–19:00 weekday study window")
    a1.legend(fontsize=8, loc="upper left")
    a2.bar(x - 0.2, e["mean_wm_w_in_window"], 0.37, color=C1, label="16:00–19:00 weekdays")
    a2.bar(x + 0.2, e["mean_wm_w_weekday_outside_window"], 0.37, color=C3, label="Other weekday hours")
    a2.set_xticks(x, labels)
    a2.set_ylabel("Average washing-machine power (W)")
    a2.set_title("Average washing-machine power, inside vs outside the window")
    a2.legend(fontsize=8, loc="upper left")
    fig.tight_layout()
    return _save(fig, "01_peak_window_exposure.png")


def fig_model_vs_actual(cmp: pd.DataFrame) -> Path:
    p2._style()
    c = cmp[(cmp["criterion"] == "one-to-one IoU>=0.5") & (cmp["house"] != "pooled")]
    a = c[c["setting"] == "within-house"].set_index("house")
    lo = c[c["setting"] == "leave-one-house-out"].set_index("house")
    x = np.arange(len(HOUSES))
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 4))
    a1.bar(x - 0.27, a.loc[HOUSES, "actual_pct_window_starts"], 0.26, color=C2, label="Actual (sub-meter)")
    a1.bar(x, a.loc[HOUSES, "pred_pct_window_starts"], 0.26, color=C1, label="Model, within-house")
    a1.bar(x + 0.27, lo.loc[HOUSES, "pred_pct_window_starts"], 0.26, color="#6250d6", label="Model, leave-one-house-out")
    a1.set_xticks(x, [f"House {h}" for h in HOUSES])
    a1.set_ylabel("Weekday cycle starts in window (%)")
    a1.set_title("Share of weekday wash starts in 16:00–19:00, Phase 3 test periods")
    a1.legend(fontsize=8)
    a2.bar(x - 0.2, a.loc[HOUSES, "window_recall"], 0.37, color=C1, label="Within-house")
    a2.bar(x + 0.2, lo.loc[HOUSES, "window_recall"], 0.37, color="#6250d6", label="Leave-one-house-out")
    a2.set_ylim(0, 1.12)
    for i, h in enumerate(HOUSES):
        n = int(a.loc[h, "actual_window_starts"])
        a2.text(i, 1.04, f"n={n}" if n else "n=0 (no actual\nwindow starts)", ha="center", va="bottom", fontsize=7,
                color=CN)
    a2.set_xticks(x, [f"House {h}" for h in HOUSES])
    a2.set_ylabel("Share of actual window starts matched")
    a2.set_title("Actual window-start cycles matched one-to-one (IoU ≥ 0.5)")
    a2.legend(fontsize=8)
    fig.tight_layout()
    return _save(fig, "02_model_vs_actual_window_starts.png")


def fig_resolution(res_t: pd.DataFrame, cyc: pd.DataFrame, examples: dict) -> tuple[Path, dict]:
    """Example = pooled heated test cycle whose 1-minute visibility ratio is closest to the median."""
    p2._style()
    c1 = cyc[cyc["resolution_min"] == 1].reset_index(drop=True)
    ex = c1.loc[(c1["visibility_ratio"] - c1["visibility_ratio"].median()).abs().idxmin()]
    h, s0 = int(ex["house"]), ex["start_utc"]
    t0, t1 = s0 - pd.Timedelta(hours=2), s0 + pd.Timedelta(hours=4)
    fig, axes = plt.subplots(1, 4, figsize=(16, 3.8), gridspec_kw={"width_ratios": [1, 1, 1, 1.1]})
    for ax, r in zip(axes[:3], RESOLUTIONS):
        yr, ar = examples[(h, r)]
        yr, ar = yr.loc[t0:t1], ar.loc[t0:t1]
        ax.step(ar.index, ar / 1000, where="post", color="#b5b4ae", lw=1.2, label="Aggregate (meter)")
        ax.step(yr.index, yr / 1000, where="post", color=C2, lw=1.4, label="Washing machine (sub-meter)")
        ax.set_title(f"{r}-minute averages")
        ax.set_ylabel("Average power (kW)")
        ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%H:%M", tz=LOCAL_TZ))
        ax.tick_params(axis="x", labelrotation=45)
        ax.set_xlabel("Local time")
    axes[0].legend(fontsize=7, loc="upper right")
    ax = axes[3]
    pooled = res_t[res_t["house"] == "pooled"].set_index("resolution_min")
    ax.bar([str(r) for r in RESOLUTIONS], pooled.loc[list(RESOLUTIONS), "median_visibility_ratio"], color=C1, width=0.55,
           label="Median heater visibility ratio")
    ax.axhline(2, color=CN, ls="--", lw=1, label="Ratio = 2")
    ax.set_xlabel("Meter resolution (min)")
    ax.set_ylabel("Heater signal ÷ background step SD")
    ax.set_title("Heater step visibility (all houses)")
    ax.legend(fontsize=7)
    fig.suptitle(f"Information loss at coarser resolution - House {h}, cycle starting "
                 f"{s0.tz_convert(LOCAL_TZ):%d %b %Y %H:%M} local (illustration, no model)", fontweight="bold", fontsize=11, y=1.04)
    fig.tight_layout()
    return _save(fig, "03_resolution_information_loss.png"), {"house": h, "start_utc": s0}


# ============================================================================
# 6. End-to-end
# ============================================================================
def run_all(verbose: bool = True) -> dict:
    ensure_dirs()
    d = p3.prepare()                       # read-only: Phase 2/3 1-minute data and Phase 3 masks
    dst = dst_check(d)
    exposure, win_cycles = peak_exposure(d)
    heat = heating_share(d)
    cmp = model_peak_comparison(d)
    res_t, res_cyc, examples = resolution_illustration(d)
    ss = illustrative_sample_size(exposure)
    figs = [fig_exposure(exposure), fig_model_vs_actual(cmp)]
    p, ex = fig_resolution(res_t, res_cyc, examples)
    figs.append(p)

    tables = {"01_dst_window_check.csv": dst, "02_peak_window_exposure.csv": exposure,
              "03_window_start_cycles.csv": win_cycles, "04_heating_energy_share.csv": heat,
              "05_model_vs_actual_window_starts.csv": cmp, "06_resolution_visibility.csv": res_t,
              "07_resolution_visibility_by_cycle.csv": res_cyc, "08_illustrative_sample_size.csv": ss,
              "09_resolution_example.csv": pd.DataFrame([ex])}
    for name, t in tables.items():
        t.to_csv(TAB_DIR / name, index=False)
    res = dict(d=d, dst=dst, exposure=exposure, win_cycles=win_cycles, heat=heat, cmp=cmp, res_t=res_t,
               res_cyc=res_cyc, ss=ss, figs=figs, example=ex)
    if verbose:
        pd.set_option("display.width", 250)
        for k in ("dst", "exposure", "heat", "cmp", "res_t", "ss"):
            print(f"--- {k}")
            print(res[k].round(3).to_string())
    return res


if __name__ == "__main__":
    run_all()
