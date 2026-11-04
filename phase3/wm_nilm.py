"""
Phase 3 - Washing-machine disaggregation (NILM) from whole-house aggregate power.

Input  : 1-minute mean whole-house aggregate power (W), official cleaned REFIT data.
Target : 1-minute mean washing-machine power (W) from the house's WM sub-meter.

Data preparation reuses the Phase 2 loading/frozen-value rules (phase2/wm_eda.py) read-only;
nothing in Phase 2 is modified. Run end-to-end with:
    python3 phase3/wm_nilm.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "phase2"))
import wm_eda as p2  # noqa: E402  (Phase 2 loading + cycle rules, used read-only)

import lightgbm as lgb  # noqa: E402
import matplotlib  # noqa: E402  (backend already set to Agg by wm_eda)
import matplotlib.pyplot as plt  # noqa: E402

PROC_DIR = ROOT / "Data" / "processed" / "phase3"
FIG_DIR = ROOT / "outputs" / "figures" / "phase3"
TAB_DIR = ROOT / "outputs" / "tables" / "phase3"
MODEL_DIR = ROOT / "outputs" / "models" / "phase3"

# ----------------------------------------------------------------------------
# Household selection (see DATA.md for reasons)
# ----------------------------------------------------------------------------
MODEL_HOUSES = [2, 7, 8, 9, 15]
EXCLUDED_HOUSES = {5: "WM channel is probably a washer-dryer (model AEG L99695HWD; 5-9.5 h wash+dry periods). "
                      "Training on it would teach the model that drying is washing."}

# Other sub-metered appliances (official readme) - used only to explain errors, never as model input.
APPLIANCE_NAMES = {
    2: {1: "Fridge-Freezer", 2: "Washing Machine", 3: "Dishwasher", 4: "Television Site", 5: "Microwave",
        6: "Toaster", 7: "Hi-Fi", 8: "Kettle", 9: "Overhead Fan"},
    7: {1: "Fridge", 2: "Freezer(1)", 3: "Freezer(2)", 4: "Tumble Dryer", 5: "Washing Machine",
        6: "Dishwasher", 7: "Television Site", 8: "Toaster", 9: "Kettle"},
    8: {1: "Fridge", 2: "Freezer", 3: "Washer Dryer", 4: "Washing Machine", 5: "Toaster", 6: "Computer",
        7: "Television Site", 8: "Microwave", 9: "Kettle"},
    9: {1: "Fridge-Freezer", 2: "Washer Dryer", 3: "Washing Machine", 4: "Dishwasher", 5: "Television Site",
        6: "Microwave", 7: "Kettle", 8: "Hi-Fi", 9: "Electric Heater"},
    15: {1: "Fridge-Freezer", 2: "Tumble Dryer", 3: "Washing Machine", 4: "Dishwasher", 5: "Computer Site",
         6: "Television Site", 7: "Microwave", 8: "Hi-Fi", 9: "Toaster"},
}

# ----------------------------------------------------------------------------
# Data-quality and split parameters
# ----------------------------------------------------------------------------
ACTIVE_W = p2.ACTIVE_W               # 20 W: same "active" definition as Phase 2
BAD_DAY_FROZEN_MIN = 60              # a UTC day with >= 60 frozen-aggregate minutes is excluded entirely
INCONSISTENT_MARGIN_W = 100.0        # WM > aggregate + 100 W is physically impossible -> excluded
SPLIT_FRACTIONS = (0.6, 0.2, 0.2)    # train / validation / test, chronological, per house
BUFFER_DAYS = 1                      # dropped between consecutive splits (no window can straddle them)
HALF_WINDOW = 60                     # features see +/-60 min of aggregate -> predictions available 60 min late
MATCH_IOU = 0.5                      # cycle matching: one-to-one, temporal IoU >= 0.5 (fixed before evaluation)
MATCH_IOU_SENSITIVITY = 1e-9         # sensitivity check: one-to-one, any overlap (IoU > 0)
SEED = 42


def ensure_dirs() -> None:
    for d in (PROC_DIR, FIG_DIR, TAB_DIR, MODEL_DIR):
        d.mkdir(parents=True, exist_ok=True)


# ============================================================================
# 1. Data loading and reliability masks
# ============================================================================
def load_minute(house: int) -> pd.DataFrame:
    """1-minute aggregate + WM frame with Phase 2's preparation (sort, de-duplicate,
    frozen-value masking, 1-minute means). Uses the Phase 2 parquet if present,
    otherwise rebuilds it in memory (Phase 2 outputs are never written from here)."""
    path = p2.PROC_DIR / f"house{house}_wm_1min.parquet"
    if path.exists():
        return pd.read_parquet(path)
    samples, _ = p2.load_house(house)
    return p2.resample_1min(samples)


def reliability_masks(m: pd.DataFrame) -> pd.DataFrame:
    """Per-minute flags explaining why a minute can or cannot be used.

    - agg_frozen: aggregate masked by the frozen-value rule while the WM channel still reports.
    - bad_day: whole UTC day dropped when it has >= 60 frozen-aggregate minutes. Reason: in House 9
      the WM channel reads exactly 0 W throughout every frozen-aggregate stretch - the logger itself
      stalled - so the WM 'zeros' on those days are not real 'off' readings either.
    - inconsistent: WM exceeds the aggregate by > 100 W (a sub-meter cannot exceed the house total).
    """
    f = pd.DataFrame(index=m.index)
    f["agg_valid"] = m["aggregate_w"].notna()
    f["wm_valid"] = m["wm_w"].notna()
    f["agg_frozen"] = ~f["agg_valid"] & f["wm_valid"]
    frozen_per_day = f["agg_frozen"].groupby(m.index.floor("D")).transform("sum")
    f["bad_day"] = frozen_per_day >= BAD_DAY_FROZEN_MIN
    f["inconsistent"] = (m["wm_w"] - m["aggregate_w"]) > INCONSISTENT_MARGIN_W
    f["usable"] = f["agg_valid"] & f["wm_valid"] & ~f["bad_day"] & ~f["inconsistent"]
    return f


def bad_day_table(flags: dict) -> pd.DataFrame:
    rows = []
    for h, f in flags.items():
        days = f.loc[f["bad_day"]].index.floor("D").unique()
        for d in days:
            day = f.loc[d: d + pd.Timedelta(hours=23, minutes=59)]
            rows.append({"house": h, "day_utc": d.date(), "frozen_agg_minutes": int(day["agg_frozen"].sum()),
                         "wm_valid_minutes": int(day["wm_valid"].sum())})
    return pd.DataFrame(rows)


# ============================================================================
# 2. Chronological split
# ============================================================================
def split_labels(f: pd.DataFrame) -> tuple[pd.Series, dict]:
    """Chronological 60/20/20 split of each house's *usable* minutes, with a 1-day
    buffer dropped after each cut. Returns a label per minute ('train','val','test',
    'buffer' or '') and the cut timestamps."""
    usable = f["usable"].to_numpy()
    cum = np.cumsum(usable) / usable.sum()
    idx = f.index
    c1 = idx[np.searchsorted(cum, SPLIT_FRACTIONS[0])]
    c2 = idx[np.searchsorted(cum, SPLIT_FRACTIONS[0] + SPLIT_FRACTIONS[1])]
    buf = pd.Timedelta(days=BUFFER_DAYS)
    lab = np.full(len(idx), "", dtype=object)
    lab[idx < c1] = "train"
    lab[(idx >= c1) & (idx < c1 + buf)] = "buffer"
    lab[(idx >= c1 + buf) & (idx < c2)] = "val"
    lab[(idx >= c2) & (idx < c2 + buf)] = "buffer"
    lab[idx >= c2 + buf] = "test"
    lab[~usable] = ""
    return pd.Series(lab, index=idx, name="split"), {"cut_train_val": c1, "cut_val_test": c2}


def feasibility_table(minute: dict, flags: dict, splits: dict, cuts: dict) -> pd.DataFrame:
    rows = []
    for h in minute:
        m, f, s = minute[h], flags[h], splits[h]
        both = f["agg_valid"] & f["wm_valid"]
        row = {"house": h, "record_start_utc": m.index[0], "record_end_utc": m.index[-1],
               "both_channels_valid_days": round(both.sum() / 1440, 1),
               "bad_days_excluded": int(f.loc[f["bad_day"]].index.floor("D").nunique()),
               "bad_day_minutes_excluded": int((f["bad_day"] & both).sum()),
               "inconsistent_minutes_excluded": int((f["inconsistent"] & both & ~f["bad_day"]).sum()),
               "usable_days": round(f["usable"].sum() / 1440, 1),
               "usable_active_pct": round(100 * (m.loc[f["usable"], "wm_w"] > ACTIVE_W).mean(), 2),
               "usable_agg_max_w": round(float(m.loc[f["usable"], "aggregate_w"].max()), 0),
               "usable_agg_minutes_gt_10kw": int((m.loc[f["usable"], "aggregate_w"] > 10000).sum())}
        for part in ("train", "val", "test"):
            sel = s == part
            row[f"{part}_start_utc"] = m.index[sel.to_numpy()][0]
            row[f"{part}_end_utc"] = m.index[sel.to_numpy()][-1]
            row[f"{part}_days"] = round(sel.sum() / 1440, 1)
            row[f"{part}_active_pct"] = round(100 * (m.loc[sel, "wm_w"] > ACTIVE_W).mean(), 2)
        rows.append(row)
    return pd.DataFrame(rows)


def prepare(houses=MODEL_HOUSES) -> dict:
    minute, flags, splits, cuts = {}, {}, {}, {}
    for h in houses:
        minute[h] = load_minute(h)
        flags[h] = reliability_masks(minute[h])
        splits[h], cuts[h] = split_labels(flags[h])
    return dict(minute=minute, flags=flags, splits=splits, cuts=cuts)


def frozen_wm_evidence(d: dict) -> pd.DataFrame:
    """What the WM channel reads while the aggregate is frozen, vs normal usable minutes.
    Evidence for excluding whole frozen days rather than only the aggregate."""
    rows = []
    for h in d["minute"]:
        m, f = d["minute"][h], d["flags"][h]
        fr, ok = m.loc[f["agg_frozen"], "wm_w"], m.loc[f["usable"], "wm_w"]
        rows.append({"house": h, "frozen_agg_minutes": len(fr),
                     "wm_exact_zero_pct_during_frozen": round(100 * (fr == 0).mean(), 1),
                     "wm_exact_zero_pct_usable": round(100 * (ok == 0).mean(), 1),
                     "wm_active_pct_during_frozen": round(100 * (fr > ACTIVE_W).mean(), 2),
                     "wm_active_pct_usable": round(100 * (ok > ACTIVE_W).mean(), 2)})
    return pd.DataFrame(rows)


# ============================================================================
# 3. Features (aggregate only - the WM channel is never an input)
# ============================================================================
def make_features(m: pd.DataFrame, f: pd.DataFrame, causal: bool = False) -> pd.DataFrame:
    """Features from the aggregate signal around each minute.

    centered (default): uses aggregate from t-60 to t+60 min, so the estimate for minute t
        is available at t+60 min. Suitable for after-the-fact disaggregation (bills,
        feedback, programme evaluation), not for instant control.
    causal: uses only t-60 .. t (a real-time variant, reported as a check).
    Unreliable aggregate minutes (frozen or on excluded days) are NaN, so they never
    feed a feature; LightGBM handles the NaNs natively.
    """
    a = m["aggregate_w"].where(f["agg_valid"] & ~f["bad_day"])
    F = {"agg": a, "d_prev": a - a.shift(1)}
    for w in (5, 15, 30, 60):
        r = a.rolling(w + 1, min_periods=1)
        F[f"past{w}_mean"], F[f"past{w}_std"] = r.mean(), r.std()
        F[f"past{w}_max"], F[f"past{w}_min"] = r.max(), r.min()
    if not causal:
        F["d_next"] = a.shift(-1) - a
        rev = a.iloc[::-1]
        for w in (5, 15, 30, 60):
            r = rev.rolling(w + 1, min_periods=1)
            F[f"fut{w}_mean"], F[f"fut{w}_max"] = r.mean().iloc[::-1], r.max().iloc[::-1]
            c = a.rolling(2 * w + 1, center=True, min_periods=1)
            F[f"cen{w}_mean"], F[f"cen{w}_std"] = c.mean(), c.std()
            F[f"cen{w}_max"], F[f"cen{w}_min"] = c.max(), c.min()
        base = F["cen60_min"]
        heat = ((a - base) > 1500).astype(float).where(a.notna())
        F["heat_minutes_cen60"] = heat.rolling(2 * HALF_WINDOW + 1, center=True, min_periods=1).sum()
    else:
        base = F["past60_min"]
        heat = ((a - base) > 1500).astype(float).where(a.notna())
        F["heat_minutes_past60"] = heat.rolling(HALF_WINDOW + 1, min_periods=1).sum()
    # Load above the local baseline: a ~2 kW washing-machine heater shows up as a step above it
    F["excess_over_base"] = a - base
    loc = m.index.tz_convert(p2.LOCAL_TZ)
    F["hour_sin"] = np.sin(2 * np.pi * loc.hour / 24)
    F["hour_cos"] = np.cos(2 * np.pi * loc.hour / 24)
    F["is_weekend"] = (loc.dayofweek >= 5).astype(float)
    return pd.DataFrame(F, index=m.index)


def build_table(d: dict, causal: bool = False) -> pd.DataFrame:
    """Stack all houses: features + target + split, usable minutes in train/val/test only."""
    parts = []
    for h in d["minute"]:
        m, f, s = d["minute"][h], d["flags"][h], d["splits"][h]
        X = make_features(m, f, causal=causal)
        keep = s.isin(["train", "val", "test"]).to_numpy()
        t = X.loc[keep].copy()
        t["house"] = h
        t["split"] = s[keep].to_numpy()
        t["y"] = m.loc[keep, "wm_w"].to_numpy()
        t["hour_local"] = t.index.tz_convert(p2.LOCAL_TZ).hour
        parts.append(t)
    return pd.concat(parts)


NON_FEATURES = {"house", "split", "y", "hour_local"}


def feature_cols(tab: pd.DataFrame) -> list[str]:
    return [c for c in tab.columns if c not in NON_FEATURES]


# ============================================================================
# 4. Baselines and model
# ============================================================================
LGB_PARAMS = dict(objective="regression", learning_rate=0.05, num_leaves=63, min_child_samples=200,
                  subsample=0.8, subsample_freq=1, colsample_bytree=0.8, reg_lambda=1.0,
                  n_estimators=2000, random_state=SEED, n_jobs=-1, verbose=-1)


def hour_profile(train: pd.DataFrame) -> pd.Series:
    """Mean WM power by local hour of day over the training rows."""
    return train.groupby("hour_local")["y"].mean()


def fit_lgbm(train: pd.DataFrame, val: pd.DataFrame, cols: list[str]) -> lgb.LGBMRegressor:
    """LightGBM with early stopping on the chronologically later validation split
    (never on random rows, which would leak neighbouring minutes)."""
    model = lgb.LGBMRegressor(**LGB_PARAMS)
    model.fit(train[cols], train["y"], eval_set=[(val[cols], val["y"])], eval_metric="l1",
              callbacks=[lgb.early_stopping(50, verbose=False)])
    return model


def predict(model, tab: pd.DataFrame, cols: list[str]) -> np.ndarray:
    """Clip to [0, aggregate]: an appliance cannot draw negative power or more than the house."""
    yhat = model.predict(tab[cols], num_iteration=model.best_iteration_)
    return np.clip(yhat, 0, tab["agg"].to_numpy())


# ============================================================================
# 5. Metrics
# ============================================================================
def _prf(true_on: np.ndarray, pred_on: np.ndarray) -> tuple[float, float, float]:
    tp = np.sum(true_on & pred_on)
    p = tp / pred_on.sum() if pred_on.sum() else np.nan
    r = tp / true_on.sum() if true_on.sum() else np.nan
    f1 = 2 * p * r / (p + r) if (p == p and r == r and p + r > 0) else 0.0
    return p, r, f1


def minute_metrics(y: np.ndarray, yhat: np.ndarray, tau: float) -> dict:
    err = yhat - y
    on = y > ACTIVE_W
    p, r, f1 = _prf(on, yhat > tau)
    return {"minutes": len(y), "MAE_W": np.mean(np.abs(err)), "RMSE_W": np.sqrt(np.mean(err ** 2)),
            "MAE_active_W": np.mean(np.abs(err[on])) if on.any() else np.nan,
            "energy_true_kWh": y.sum() / 60000, "energy_pred_kWh": yhat.sum() / 60000,
            "energy_error_pct": 100 * (yhat.sum() - y.sum()) / y.sum(),
            "precision": p, "recall": r, "F1": f1}


def choose_threshold(val: pd.DataFrame, yhat_val: np.ndarray) -> pd.DataFrame:
    """Pick the predicted-power level that counts as 'machine on' by maximising
    minute-level F1 on the validation split (the test split is never used for this)."""
    rows = []
    for tau in (10, 20, 50, 100, 150, 200, 300, 500):
        p, r, f1 = _prf(val["y"].to_numpy() > ACTIVE_W, yhat_val > tau)
        rows.append({"tau_W": tau, "precision": p, "recall": r, "F1": f1})
    return pd.DataFrame(rows)


def _series(index: pd.DatetimeIndex, tab: pd.DataFrame, values: np.ndarray) -> pd.DataFrame:
    """Rebuild a gap-aware minute series (NaN outside the evaluated rows) for cycle detection."""
    s = pd.Series(np.nan, index=index)
    s.loc[tab.index] = values
    return pd.DataFrame({"wm_w": s})


def detect_cycles(index, tab, values, house, active_w) -> pd.DataFrame:
    a = p2.detect_activity(_series(index, tab, values), house, active_w=active_w)
    return p2.classify_cycles(a) if len(a) else a


def _overlaps(a: pd.DataFrame, b: pd.DataFrame) -> np.ndarray:
    """For each interval in a, does any interval in b overlap it?"""
    if len(a) == 0 or len(b) == 0:
        return np.zeros(len(a), dtype=bool)
    bs, be = b["start_utc"].to_numpy(), b["end_utc"].to_numpy()
    order = np.argsort(bs)
    bs, be = bs[order], be[order]
    be_cummax = np.maximum.accumulate(be)
    out = []
    for s, e in zip(a["start_utc"].to_numpy(), a["end_utc"].to_numpy()):
        k = np.searchsorted(bs, e, side="right")          # b intervals starting no later than a's end
        out.append(k > 0 and be_cummax[k - 1] >= s)
    return np.array(out)


def match_cycles(true_c: pd.DataFrame, pred_c: pd.DataFrame,
                 min_iou: float) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """One-to-one matching of predicted to actual cycles.

    Temporal IoU = (minutes in both) / (minutes in either), using each cycle's inclusive
    [start, end]. Candidate pairs need IoU >= min_iou; pairs are then taken greedily from the
    highest IoU down, and each actual and each predicted cycle can be used at most once. So one
    long predicted period cannot 'detect' several washes, and several fragments cannot all count
    as correct for one wash. Returns (true_matched, pred_matched, iou of each true cycle's match,
    best IoU of each true cycle with any predicted cycle, number of predicted cycles overlapping it)."""
    nt, npred = len(true_c), len(pred_c)
    t_m, p_m, t_iou = np.zeros(nt, bool), np.zeros(npred, bool), np.zeros(nt)
    if nt == 0 or npred == 0:
        return t_m, p_m, t_iou, np.zeros(nt), np.zeros(nt, int)
    def to_min(col: pd.Series) -> np.ndarray:   # minutes since epoch (UTC)
        return (pd.to_datetime(col, utc=True).dt.tz_localize(None).to_numpy()
                .astype("datetime64[m]").astype(np.int64))
    ts, te_ = to_min(true_c["start_utc"]), to_min(true_c["end_utc"])
    ps, pe = to_min(pred_c["start_utc"]), to_min(pred_c["end_utc"])
    inter = np.clip(np.minimum(te_[:, None], pe[None, :]) - np.maximum(ts[:, None], ps[None, :]) + 1, 0, None)
    union = (te_ - ts + 1)[:, None] + (pe - ps + 1)[None, :] - inter
    iou = inter / union
    ii, jj = np.nonzero((iou >= min_iou) & (inter > 0))
    for k in np.argsort(-iou[ii, jj], kind="stable"):
        i, j = ii[k], jj[k]
        if not t_m[i] and not p_m[j]:
            t_m[i], p_m[j], t_iou[i] = True, True, iou[i, j]
    return t_m, p_m, t_iou, iou.max(axis=1), (inter > 0).sum(axis=1)


def event_eval(d: dict, tab: pd.DataFrame, yhat: np.ndarray, house: int, tau: float,
               min_iou: float = MATCH_IOU) -> tuple[dict, pd.DataFrame, pd.DataFrame]:
    """Cycle-level evaluation. Phase 2's cycle rule is applied to the actual series (20 W) and to
    the predicted series (tau), then likely cycles are matched one-to-one (match_cycles).
    precision = matched / predicted cycles, recall = matched / actual cycles.
    Unmatched predicted cycles are split into those touching some real WM activity (misaligned
    or fragmented estimates) and 'pure' false detections that overlap no WM activity at all."""
    idx = d["minute"][house].index
    sel = (tab["house"] == house).to_numpy()
    t = tab.loc[sel]
    true_all = detect_cycles(idx, t, t["y"].to_numpy(), house, ACTIVE_W)
    true_c = true_all[true_all["likely_cycle"]].copy()
    pred_all = detect_cycles(idx, t, yhat[sel], house, tau)
    pred_c = pred_all[pred_all["likely_cycle"]].copy() if len(pred_all) else pd.DataFrame(
        columns=["start_utc", "end_utc", "energy_kwh", "peak_w", "duration_min", "house"])
    t_m, p_m, t_iou, best_iou, n_ovl = match_cycles(true_c, pred_c, min_iou)
    true_c["matched"], true_c["match_iou"] = t_m, t_iou
    true_c["best_iou_any_pred"], true_c["n_overlapping_pred"] = best_iou, n_ovl
    true_c["status"] = np.select([t_m, n_ovl > 0], ["matched", "overlapped, below match IoU"], "no overlap at all")
    pred_c["matched"] = p_m
    pred_c["overlaps_wm_activity"] = _overlaps(pred_c, true_all)
    # Predicted energy over each true cycle's window (energy-estimation quality per cycle)
    ps = pd.Series(yhat[sel], index=t.index)
    true_c["pred_energy_kwh"] = [ps.loc[a:b].sum() / 60000 for a, b in zip(true_c["start_utc"], true_c["end_utc"])]
    n_true, n_pred, n_match = len(true_c), len(pred_c), int(t_m.sum())
    prec = n_match / n_pred if n_pred else np.nan
    rec = n_match / n_true if n_true else np.nan
    f1 = 2 * prec * rec / (prec + rec) if n_pred and n_match else 0.0
    unmatched = pred_c[~pred_c["matched"]]
    return ({"house": house, "tau_W": tau, "match_min_iou": min_iou, "true_cycles": n_true, "pred_cycles": n_pred,
             "matched_cycles": n_match, "missed_true_cycles": n_true - n_match,
             "unmatched_pred_cycles": n_pred - n_match,
             "unmatched_pred_touching_wm": int(unmatched["overlaps_wm_activity"].sum()),
             "pure_false_detections": int((~unmatched["overlaps_wm_activity"]).sum()),
             "event_precision": prec, "event_recall": rec, "event_F1": f1},
            true_c, pred_c)


def predict_gated(model, tab: pd.DataFrame, cols: list[str], tau: float) -> np.ndarray:
    """Model output with everything at or below the validation-chosen 'on' level set to 0.
    Reason: a squared-error regressor leaves a small positive smear during the ~95% of
    minutes when the machine is off; zeroing it removes false 'on' minutes and lowers MAE."""
    yhat = predict(model, tab, cols)
    return np.where(yhat > tau, yhat, 0.0)


# ============================================================================
# 6. Error analysis helpers
# ============================================================================
def load_other_appliances(house: int, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """1-minute means of the house's other sub-metered appliances (not the WM) in a window.
    Used only to explain false detections - never as model input."""
    names = APPLIANCE_NAMES[house]
    wm_ch = int(p2.HOUSES[house]["channel"].replace("Appliance", ""))
    cols = [f"Appliance{i}" for i in names if i != wm_ch]
    raw = pd.read_csv(p2.CLEAN_DIR / f"CLEAN_House{house}.csv", usecols=["Unix"] + cols)
    raw = raw[(raw["Unix"] >= start.timestamp() - 3600) & (raw["Unix"] <= end.timestamp() + 3600)]
    raw.index = pd.to_datetime(raw["Unix"], unit="s", utc=True)
    out = raw[cols].resample("1min").mean()
    out.columns = [names[int(c.replace("Appliance", ""))] for c in cols]
    return out


def attribute_false_detections(fp: pd.DataFrame, other: dict) -> pd.DataFrame:
    """For each false detection, name the other sub-metered appliance responsible - but only if
    that appliance's own metered energy in the window is at least half of the (false) predicted
    WM energy. Otherwise the cause is 'unmetered or unclear' (e.g. a shower, cooker or heater
    without a sub-meter), so a small dishwasher run is not blamed for a long unmetered load."""
    rows = []
    for _, r in fp.iterrows():
        o = other[r["house"]].loc[r["start_utc"]: r["end_utc"]]
        e = o.sum() / 60000
        top, top_kwh = (e.idxmax(), float(e.max())) if len(e) else ("none", 0.0)
        explained = top_kwh >= 0.05 and top_kwh >= 0.5 * r["energy_kwh"]
        rows.append({"house": r["house"], "start_utc": r["start_utc"], "duration_min": r["duration_min"],
                     "pred_energy_kwh": r["energy_kwh"], "pred_peak_w": r["peak_w"],
                     "largest_metered_appliance": top, "largest_metered_kwh": round(top_kwh, 3),
                     "attributed_to": top if explained else "unmetered or unclear"})
    return pd.DataFrame(rows)


def error_by_band(te: pd.DataFrame, preds: dict) -> pd.DataFrame:
    """MAE and bias by what the machine was really doing, and (for active minutes) by how much
    other load was running at the same time."""
    y = te["y"].to_numpy()
    other = te["agg"].to_numpy() - y
    bands = {"off (<=20 W)": y <= ACTIVE_W,
             "low-power tail (20-1000 W)": (y > ACTIVE_W) & (y <= p2.HEATING_W),
             "heating (>1000 W)": y > p2.HEATING_W,
             "active, other load <500 W": (y > ACTIVE_W) & (other < 500),
             "active, other load 500-1500 W": (y > ACTIVE_W) & (other >= 500) & (other < 1500),
             "active, other load >=1500 W": (y > ACTIVE_W) & (other >= 1500)}
    rows = []
    for name, sel in bands.items():
        for mname, yhat in preds.items():
            rows.append({"band": name, "model": mname, "minutes": int(sel.sum()),
                         "share_of_test_pct": round(100 * sel.mean(), 2),
                         "MAE_W": np.mean(np.abs(yhat[sel] - y[sel])), "bias_W": np.mean(yhat[sel] - y[sel])})
    return pd.DataFrame(rows)


# ============================================================================
# 7. Figures
# ============================================================================
C_PRED, C_TRUE, C_AGG_BG = "#2a78d6", "#eb6834", "#b5b4ae"
C_NEUTRAL = p2.C_NEUTRAL
MODEL_COLORS = {"LightGBM (centered ±60 min)": "#2a78d6", "Hour-of-day profile": "#eb6834",
                "Always off": "#1baf7a", "LightGBM (causal, past 60 min)": "#eda100"}


def _save(fig, name: str) -> Path:
    path = FIG_DIR / name
    fig.savefig(path, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


def fig_split_timeline(d: dict) -> Path:
    p2._style()
    fig, ax = plt.subplots(figsize=(11, 3.2))
    cols = {"train": "#2a78d6", "val": "#eb6834", "test": "#1baf7a"}
    for i, h in enumerate(d["minute"]):
        s, f = d["splits"][h], d["flags"][h]
        for part, c in cols.items():
            st, en = p2._runs((s == part).to_numpy())
            idx = s.index
            for a, b in zip(st, en):
                ax.barh(i, (idx[b] - idx[a]).total_seconds() / 86400 + 1 / 1440, left=matplotlib.dates.date2num(idx[a]),
                        height=0.6, color=c, lw=0, label=part if (i == 0 and a == st[0]) else None)
        bad = f.loc[f["bad_day"]].index.floor("D").unique()
        ax.scatter(matplotlib.dates.date2num(bad + pd.Timedelta(hours=12)), np.full(len(bad), i + 0.42),
                   marker="v", s=14, color="#e34948", label="excluded unreliable day" if i == 0 else None, zorder=3)
    ax.set_yticks(range(len(d["minute"])), [f"House {h}" for h in d["minute"]])
    ax.invert_yaxis()
    ax.xaxis_date()
    ax.set_xlabel("Date (UTC)")
    ax.set_ylabel("Household")
    ax.set_title("Chronological train / validation / test split of usable minutes (gaps = missing or excluded data)")
    handles, labels = ax.get_legend_handles_labels()
    order = [labels.index(k) for k in ("train", "val", "test", "excluded unreliable day")]
    ax.legend([handles[k] for k in order], ["Train (60%)", "Validation (20%)", "Test (20%)", "Excluded unreliable day"],
              loc="upper center", bbox_to_anchor=(0.5, -0.22), ncol=4)
    return _save(fig, "01_split_timeline.png")


def fig_house9_investigation() -> Path:
    """Raw (un-masked) House 9 aggregate and WM around the March 2014 freeze."""
    p2._style()
    ch = p2.HOUSES[9]["channel"]
    raw = pd.read_csv(p2.CLEAN_DIR / "CLEAN_House9.csv", usecols=["Unix", "Aggregate", ch])
    t0, t1 = pd.Timestamp("2014-03-06", tz="UTC"), pd.Timestamp("2014-04-12", tz="UTC")
    raw = raw[(raw["Unix"] >= t0.timestamp()) & (raw["Unix"] < t1.timestamp())]
    raw.index = pd.to_datetime(raw["Unix"], unit="s", utc=True)
    m = raw[["Aggregate", ch]].resample("1min").mean()
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 5.5), sharex=True)
    a1.plot(m.index, m["Aggregate"], color=C_PRED, lw=0.6, label="Aggregate (raw cleaned file)")
    a2.plot(m.index, m[ch], color=C_TRUE, lw=0.6, label=f"Washing machine ({ch})")
    for ax in (a1, a2):
        ax.axvspan(pd.Timestamp("2014-03-13", tz="UTC"), pd.Timestamp("2014-04-02", tz="UTC"), color="#e34948",
                   alpha=0.08, lw=0, label="Excluded days (frozen aggregate)")
        ax.set_ylabel("Active power (W)")
        ax.legend(loc="center")
    a1.set_title("House 9, March 2014: aggregate frozen at one value for ~18 days while the WM channel reads 0-1 W")
    a2.set_xlabel("Date (UTC)")
    return _save(fig, "02_house9_aggregate_freeze.png")


def fig_actual_vs_predicted(te: pd.DataFrame, yhat: np.ndarray) -> Path:
    """Minute-level actual vs predicted (test split), one panel per house, log-count hexbin."""
    p2._style()
    fig, axes = plt.subplots(1, len(MODEL_HOUSES), figsize=(15, 3.6), sharex=True, sharey=True)
    for ax, h in zip(axes, MODEL_HOUSES):
        s = (te["house"] == h).to_numpy()
        hb = ax.hexbin(te["y"].to_numpy()[s], yhat[s], gridsize=45, bins="log", cmap=p2.SEQ_BLUE,
                       extent=(0, 2600, 0, 2600), mincnt=1, linewidths=0)
        ax.plot([0, 2600], [0, 2600], color=C_NEUTRAL, lw=0.8, ls="--", label="Perfect prediction")
        ax.set_title(f"House {h}")
        ax.set_xlabel("Actual WM power (W)")
        ax.grid(False)
    axes[0].set_ylabel("Predicted WM power (W)")
    axes[0].legend(loc="upper left", fontsize=7)
    cb = fig.colorbar(hb, ax=axes, pad=0.01)
    cb.set_label("Minutes (log scale)")
    fig.suptitle("Actual vs predicted washing-machine power, test period (1-minute values, LightGBM centered model)",
                 fontweight="bold", fontsize=11, y=1.03)
    return _save(fig, "03_actual_vs_predicted_minutes.png")


def fig_cycle_energy(true_cycles: pd.DataFrame) -> Path:
    p2._style()
    fig, axes = plt.subplots(1, len(MODEL_HOUSES), figsize=(15, 3.6), sharex=True, sharey=True)
    for ax, h in zip(axes, MODEL_HOUSES):
        c = true_cycles[true_cycles["house"] == h]
        ax.scatter(c["energy_kwh"], c["pred_energy_kwh"], s=12, color=C_PRED, alpha=0.6, linewidths=0,
                   label="Test-period cycle")
        ax.plot([0, 3], [0, 3], color=C_NEUTRAL, lw=0.8, ls="--", label="Perfect estimate")
        mape = np.median(np.abs(c["pred_energy_kwh"] - c["energy_kwh"]) / c["energy_kwh"]) * 100
        ax.set_title(f"House {h}\n{len(c)} cycles, median abs. error {mape:.0f}%", fontsize=9)
        ax.set_xlabel("Actual cycle energy (kWh)")
    axes[0].set_ylabel("Predicted energy in same window (kWh)")
    axes[0].legend(loc="upper left", fontsize=7)
    fig.suptitle("Per-cycle energy: actual vs predicted over each true test-period wash cycle",
                 fontweight="bold", fontsize=11, y=1.03)
    return _save(fig, "04_cycle_energy_actual_vs_predicted.png")


def _plot_window(ax, d, h, te_h, yhat_h, t0, t1, title):
    m = d["minute"][h].loc[t0:t1]
    pred = pd.Series(yhat_h, index=te_h.index).reindex(m.index)
    ax.plot(m.index, m["aggregate_w"], color=C_AGG_BG, lw=0.9, label="Aggregate (model input)")
    ax.plot(m.index, m["wm_w"], color=C_TRUE, lw=1.4, label="Actual washing machine")
    ax.plot(pred.index, pred, color=C_PRED, lw=1.4, label="Predicted washing machine")
    ax.set_title(title, fontsize=9)
    ax.set_ylabel("Active power (W)")
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%H:%M", tz=p2.LOCAL_TZ))


def pick_typical_days(d, te, yhat) -> pd.DataFrame:
    """Per house: among test days (local) with >=95% evaluated minutes and >=1 true active
    minute, the day whose daily MAE is closest to the median - typical, not best-case."""
    rows = []
    for h in MODEL_HOUSES:
        s = (te["house"] == h).to_numpy()
        t = te.loc[s, ["y"]].copy()
        t["err"] = np.abs(yhat[s] - t["y"].to_numpy())
        t["day"] = t.index.tz_convert(p2.LOCAL_TZ).normalize()
        g = t.groupby("day").agg(n=("y", "size"), active=("y", lambda v: (v > ACTIVE_W).sum()), mae=("err", "mean"))
        g = g[(g["n"] >= 0.95 * 1440) & (g["active"] > 0)]
        day = (g["mae"] - g["mae"].median()).abs().idxmin()
        rows.append({"house": h, "day_local": day, "day_MAE_W": round(g.loc[day, "mae"], 1),
                     "median_day_MAE_W": round(g["mae"].median(), 1), "candidate_days": len(g)})
    return pd.DataFrame(rows)


def fig_typical_days(d, te, yhat, days: pd.DataFrame) -> Path:
    p2._style()
    fig, axes = plt.subplots(len(days), 1, figsize=(11, 2.6 * len(days)))
    for ax, (_, r) in zip(axes, days.iterrows()):
        h = r["house"]
        s = (te["house"] == h).to_numpy()
        t0 = r["day_local"].tz_convert("UTC")
        _plot_window(ax, d, h, te.loc[s], yhat[s], t0, t0 + pd.Timedelta(hours=23, minutes=59),
                     f"House {h}, {r['day_local']:%a %d %b %Y} (day MAE {r['day_MAE_W']} W, closest to the house's "
                     f"median test day)")
        ax.set_ylim(0, 3000)
    axes[0].legend(loc="upper left", ncol=3, fontsize=8)
    axes[-1].set_xlabel("Local time (Europe/London)")
    fig.suptitle("Representative test days: actual vs predicted washing-machine power (aggregate clipped at 3 kW)",
                 fontweight="bold", fontsize=11, y=1.0)
    fig.tight_layout()
    return _save(fig, "05_typical_test_days.png")


def fig_metrics(metrics: pd.DataFrame, events: pd.DataFrame, loho: pd.DataFrame) -> Path:
    p2._style()
    mm = metrics[(metrics["split"] == "test") & (metrics["house"] != "pooled")]
    models = list(MODEL_COLORS)
    hs = [str(h) for h in MODEL_HOUSES]
    x = np.arange(len(hs))
    fig, axes = plt.subplots(2, 2, figsize=(13, 7.5))
    for ax, metric, ylab in ((axes[0, 0], "RMSE_W", "RMSE (W)"), (axes[0, 1], "MAE_W", "MAE (W)"),
                             (axes[1, 0], "F1", "Minute-level F1 (on/off)")):
        w = 0.8 / len(models)
        for i, mname in enumerate(models):
            v = mm[mm["model"] == mname].set_index("house").reindex(hs)[metric].fillna(0)
            ax.bar(x + (i - (len(models) - 1) / 2) * w, v, width=w * 0.92, color=MODEL_COLORS[mname], label=mname)
        ax.set_xticks(x, [f"House {h}" for h in hs])
        ax.set_ylabel(ylab)
        ax.set_title(f"{ylab.split(' (')[0]} by house, test period (lower is better)" if metric != "F1"
                     else "Minute-level F1 by house, test period (higher is better)")
    axes[0, 0].legend(fontsize=8, loc="upper right")
    ax = axes[1, 1]
    ep = events[events["criterion"] == "one-to-one IoU>=0.5"]
    e_in = ep[ep["setting"] == "within-house"].set_index("house").reindex(MODEL_HOUSES)
    e_lo = ep[ep["setting"] == "leave-one-house-out"].set_index("house").reindex(MODEL_HOUSES)
    ax.bar(x - 0.2, e_in["event_F1"], width=0.37, color="#2a78d6", label="Within-house (unseen time)")
    ax.bar(x + 0.2, e_lo["event_F1"], width=0.37, color="#6250d6", label="Leave-one-house-out (unseen house)")
    ax.set_xticks(x, [f"House {h}" for h in hs])
    ax.set_ylabel("Cycle-level F1")
    ax.set_ylim(0, 1)
    ax.set_title("Wash-cycle F1, one-to-one matching at IoU ≥ 0.5 (higher is better)")
    ax.legend(fontsize=8, loc="upper left")
    fig.tight_layout()
    return _save(fig, "06_metrics_by_house.png")


def fig_failures(d, te, yhat, cases: list[tuple[str, int, pd.Timestamp, pd.Timestamp]]) -> Path:
    p2._style()
    fig, axes = plt.subplots(len(cases), 1, figsize=(11, 2.8 * len(cases)))
    for ax, (title, h, s0, e0) in zip(axes, cases):
        s = (te["house"] == h).to_numpy()
        _plot_window(ax, d, h, te.loc[s], yhat[s], s0 - pd.Timedelta(minutes=90), e0 + pd.Timedelta(minutes=90),
                     f"{title} - House {h}, {s0.tz_convert(p2.LOCAL_TZ):%d %b %Y %H:%M} local")
        ax.axvspan(s0, e0, color="#e34948", alpha=0.07, lw=0, label="Period in question")
    axes[0].legend(loc="upper left", ncol=4, fontsize=8)
    axes[-1].set_xlabel("Local time (Europe/London)")
    fig.suptitle("Failure cases on the test period (chosen by rule, see RESULTS.md)", fontweight="bold", fontsize=11)
    fig.tight_layout()
    return _save(fig, "07_failure_cases.png")


def fig_error_bands(bands: pd.DataFrame) -> Path:
    p2._style()
    order = bands["band"].unique()
    models = [m for m in MODEL_COLORS if m in bands["model"].unique()]
    fig, ax = plt.subplots(figsize=(12, 4))
    x = np.arange(len(order))
    w = 0.8 / len(models)
    for i, mname in enumerate(models):
        v = bands[bands["model"] == mname].set_index("band").reindex(order)["MAE_W"]
        ax.bar(x + (i - (len(models) - 1) / 2) * w, v, width=w * 0.92, color=MODEL_COLORS[mname], label=mname)
    ax.set_xticks(x, [b.replace(", ", ",\n") for b in order], fontsize=8)
    ax.set_ylabel("MAE (W)")
    ax.set_xlabel("What the washing machine was actually doing (test minutes, all houses pooled)")
    ax.set_title("Where the errors come from: MAE by actual washing-machine state and concurrent other load")
    ax.legend(fontsize=8)
    return _save(fig, "08_error_by_state_and_overlap.png")


# ============================================================================
# 8. End-to-end run
# ============================================================================
def _metrics_rows(tab, preds: dict, split: str, tau: float) -> list[dict]:
    rows = []
    for mname, yhat in preds.items():
        for h in MODEL_HOUSES + ["pooled"]:
            s = np.ones(len(tab), bool) if h == "pooled" else (tab["house"] == h).to_numpy()
            r = minute_metrics(tab["y"].to_numpy()[s], yhat[s], tau)
            rows.append({"split": split, "model": mname, "house": str(h), **r})
    return rows


def run_all(verbose: bool = True) -> dict:
    ensure_dirs()
    t_start = time.time()
    d = prepare()
    feas = feasibility_table(d["minute"], d["flags"], d["splits"], d["cuts"])
    bad = bad_day_table(d["flags"])
    evid = frozen_wm_evidence(d)

    # --- features and split -------------------------------------------------------------
    tab = build_table(d)
    cols = feature_cols(tab)
    tr, va, te = (tab[tab["split"] == s] for s in ("train", "val", "test"))
    tab_c = build_table(d, causal=True)
    cols_c = feature_cols(tab_c)
    tr_c, va_c, te_c = (tab_c[tab_c["split"] == s] for s in ("train", "val", "test"))

    # --- main model, threshold chosen on validation -----------------------------------
    model = fit_lgbm(tr, va, cols)
    thr = choose_threshold(va, predict(model, va, cols))
    tau = float(thr.loc[thr["F1"].idxmax(), "tau_W"])
    model_c = fit_lgbm(tr_c, va_c, cols_c)

    def baselines(train, target):
        prof = hour_profile(train)
        return {"Hour-of-day profile": target["hour_local"].map(prof).to_numpy(),
                "Always off": np.zeros(len(target))}

    def house_profile(target):
        out = np.zeros(len(target))
        for h in MODEL_HOUSES:
            s = (target["house"] == h).to_numpy()
            out[s] = target.loc[s, "hour_local"].map(hour_profile(tr[tr["house"] == h])).to_numpy()
        return out

    preds = {}
    for split, t, tc in (("val", va, va_c), ("test", te, te_c)):
        preds[split] = {"LightGBM (centered ±60 min)": predict_gated(model, t, cols, tau),
                        "LightGBM (causal, past 60 min)": predict_gated(model_c, tc, cols_c, tau),
                        "Hour-of-day profile": house_profile(t), "Always off": np.zeros(len(t))}
    metrics = pd.DataFrame(_metrics_rows(va, preds["val"], "val", tau) + _metrics_rows(te, preds["test"], "test", tau))

    # --- leave-one-household-out: a genuine unseen-household test -----------------------
    loho_rows, loho_pred, loho_tau, loho_thr = [], np.zeros(len(te)), {}, []
    best_iters = {"within_house": int(model.best_iteration_), "causal": int(model_c.best_iteration_)}
    for h in MODEL_HOUSES:
        tr_o, va_o = tr[tr["house"] != h], va[va["house"] != h]
        assert h not in set(tr_o["house"]) | set(va_o["house"]), "held-out house leaked into its fold"
        mdl = fit_lgbm(tr_o, va_o, cols)
        best_iters[f"loho_holdout_{h}"] = int(mdl.best_iteration_)
        # Fold-specific 'on' threshold: chosen on the four training houses' validation data only.
        # The held-out house contributes nothing to model fitting, early stopping or tau.
        thr_h = choose_threshold(va_o, predict(mdl, va_o, cols)).assign(held_out_house=h)
        tau_h = float(thr_h.loc[thr_h["F1"].idxmax(), "tau_W"])
        thr_h["selected"] = thr_h["tau_W"] == tau_h
        loho_thr.append(thr_h)
        loho_tau[h] = tau_h
        s = (te["house"] == h).to_numpy()
        yh = predict_gated(mdl, te.loc[s], cols, tau_h)
        loho_pred[s] = yh
        prof = hour_profile(tr_o)
        for mname, yy in (("LightGBM (centered ±60 min)", yh),
                          ("Hour-of-day profile (other houses)", te.loc[s, "hour_local"].map(prof).to_numpy()),
                          ("Always off", np.zeros(s.sum()))):
            loho_rows.append({"setting": "leave-one-house-out", "held_out_house": h, "model": mname, "tau_W": tau_h,
                              **minute_metrics(te.loc[s, "y"].to_numpy(), yy, tau_h)})
    loho = pd.DataFrame(loho_rows)
    loho_thr = pd.concat(loho_thr, ignore_index=True)[["held_out_house", "tau_W", "precision", "recall", "F1", "selected"]]

    # --- cycle-level evaluation ---------------------------------------------------------
    yhat = preds["test"]["LightGBM (centered ±60 min)"]
    ev_rows, true_all, pred_all = [], [], []
    runs = (("within-house", yhat, lambda h: tau),
            ("leave-one-house-out", loho_pred, lambda h: loho_tau[h]),
            ("within-house causal", preds["test"]["LightGBM (causal, past 60 min)"], lambda h: tau),
            ("within-house hour profile", preds["test"]["Hour-of-day profile"], lambda h: tau))
    for criterion, min_iou in (("one-to-one IoU>=0.5", MATCH_IOU), ("one-to-one any overlap", MATCH_IOU_SENSITIVITY)):
        for setting, yy, tau_of in runs:
            for h in MODEL_HOUSES:
                ev, tc, pc = event_eval(d, te, yy, h, tau_of(h), min_iou)
                ev_rows.append({"criterion": criterion, "setting": setting, **ev})
                if setting == "within-house" and min_iou == MATCH_IOU:
                    true_all.append(tc)
                    pred_all.append(pc)
    events = pd.DataFrame(ev_rows)
    true_c = pd.concat(true_all, ignore_index=True)
    pred_c = pd.concat(pred_all, ignore_index=True)

    # --- error analysis -----------------------------------------------------------------
    bands = error_by_band(te, {k: preds["test"][k] for k in ("LightGBM (centered ±60 min)", "Hour-of-day profile",
                                                             "Always off")})
    other = {h: load_other_appliances(h, te[te["house"] == h].index.min(), te[te["house"] == h].index.max())
             for h in MODEL_HOUSES}
    # 'Pure' false detections: unmatched predicted cycles that overlap no real WM activity at all
    fp = attribute_false_detections(pred_c[~pred_c["matched"] & ~pred_c["overlaps_wm_activity"]], other)
    cyc_stats = (true_c.assign(abs_pct_err=100 * (true_c["pred_energy_kwh"] - true_c["energy_kwh"]).abs()
                               / true_c["energy_kwh"])
                 .groupby("house").agg(true_cycles=("energy_kwh", "size"),
                                       actual_kwh_total=("energy_kwh", "sum"),
                                       pred_kwh_total=("pred_energy_kwh", "sum"),
                                       median_abs_pct_err=("abs_pct_err", "median")).round(3).reset_index())
    # Energy the model assigns to false detections (explains why total energy can look right
    # while each true cycle is underestimated)
    fp_kwh = fp.groupby("house")["pred_energy_kwh"].sum() if len(fp) else pd.Series(dtype=float)
    cyc_stats["false_detection_pred_kwh"] = cyc_stats["house"].map(fp_kwh).fillna(0).round(3)
    cyc_stats["total_pred_kwh_test"] = cyc_stats["house"].map(
        {h: yhat[(te["house"] == h).to_numpy()].sum() / 60000 for h in MODEL_HOUSES}).round(3)
    # Overlap: mean other load during each true cycle
    te_other = (te["agg"] - te["y"])
    true_c["mean_other_load_w"] = [te_other[(te["house"] == r.house).to_numpy()].loc[r.start_utc:r.end_utc].mean()
                                   for r in true_c.itertuples()]
    missed = true_c[~true_c["matched"]].copy()
    missed_vs_detected = (true_c.groupby("status").agg(cycles=("energy_kwh", "size"),
                                                 median_energy_kwh=("energy_kwh", "median"),
                                                 heated_pct=("heated", lambda v: 100 * v.mean()),
                                                 median_duration_min=("duration_min", "median"),
                                                 median_other_load_w=("mean_other_load_w", "median"),
                                                 median_best_iou=("best_iou_any_pred", "median"),
                                                 median_overlapping_pred=("n_overlapping_pred", "median"))
                          .round(3).reset_index())

    # --- failure cases chosen by rule ---------------------------------------------------
    cases = []
    none = missed[missed["status"] == "no overlap at all"]
    if len(none):
        r = none.loc[none["energy_kwh"].idxmax()]
        cases.append(("Missed cycle (largest-energy true cycle with no overlapping prediction)", r["house"],
                      r["start_utc"], r["end_utc"]))
    low = missed[missed["status"] == "overlapped, below match IoU"]
    if len(low):
        r = low.loc[low["energy_kwh"].idxmax()]
        cases.append((f"Found but badly aligned (largest-energy true cycle with best IoU {r['best_iou_any_pred']:.2f} < 0.5)",
                      r["house"], r["start_utc"], r["end_utc"]))
    if len(fp):
        r = fp.loc[fp["pred_energy_kwh"].idxmax()]
        cases.append((f"False detection (largest with no WM activity; attributed to: {r['attributed_to']})", r["house"],
                      r["start_utc"], r["start_utc"] + pd.Timedelta(minutes=int(r["duration_min"]) - 1)))
    r = true_c.loc[true_c["mean_other_load_w"].idxmax()]
    cases.append(("Overlapping loads (true cycle with highest concurrent other load)", r["house"], r["start_utc"], r["end_utc"]))

    # --- figures ------------------------------------------------------------------------
    days = pick_typical_days(d, te, yhat)
    figs = [fig_split_timeline(d), fig_house9_investigation(), fig_actual_vs_predicted(te, yhat),
            fig_cycle_energy(true_c), fig_typical_days(d, te, yhat, days),
            fig_metrics(metrics, events, loho), fig_failures(d, te, yhat, cases), fig_error_bands(bands)]

    # --- save ---------------------------------------------------------------------------
    model.booster_.save_model(str(MODEL_DIR / "lgbm_centered.txt"), num_iteration=model.best_iteration_)
    run_info = {"tau_W": tau, "loho_tau_W": {str(k): v for k, v in loho_tau.items()},
                "cycle_matching": f"one-to-one greedy by temporal IoU, IoU >= {MATCH_IOU} (sensitivity: any overlap)",
                "best_iterations": best_iters, "lgb_params": LGB_PARAMS, "features_centered": cols,
                "features_causal": cols_c, "half_window_min": HALF_WINDOW, "split_fractions": SPLIT_FRACTIONS,
                "buffer_days": BUFFER_DAYS, "lightgbm_version": lgb.__version__,
                "runtime_s": round(time.time() - t_start, 1)}
    (MODEL_DIR / "run_info.json").write_text(json.dumps(run_info, indent=2, default=str))
    out_pred = te[["house", "agg", "y"]].copy()
    out_pred["pred_centered"] = yhat
    out_pred["pred_loho"] = loho_pred
    out_pred["pred_causal"] = preds["test"]["LightGBM (causal, past 60 min)"]
    out_pred["pred_hour_profile"] = preds["test"]["Hour-of-day profile"]
    out_pred.to_parquet(PROC_DIR / "test_predictions.parquet")

    imp = pd.DataFrame({"feature": cols, "gain": model.booster_.feature_importance("gain")})
    imp["gain_share_pct"] = (100 * imp["gain"] / imp["gain"].sum()).round(2)
    selection = pd.DataFrame([{"house": h, "used": h in MODEL_HOUSES,
                               "reason": "WM channel reliable and aligned with aggregate" if h in MODEL_HOUSES
                               else EXCLUDED_HOUSES[h]} for h in sorted(MODEL_HOUSES + list(EXCLUDED_HOUSES))])
    tables = {"01_household_selection.csv": selection, "02_feasibility_and_split.csv": feas,
              "03_excluded_unreliable_days.csv": bad, "04_frozen_aggregate_wm_evidence.csv": evid,
              "05_threshold_selection_val.csv": thr, "05b_loho_threshold_selection.csv": loho_thr,
              "06_metrics_within_house.csv": metrics,
              "07_metrics_leave_one_house_out.csv": loho, "08_event_metrics.csv": events,
              "09_cycle_energy_by_house.csv": cyc_stats, "10_test_true_cycles.csv": true_c,
              "11_false_detections_attributed.csv": fp, "12_missed_cycles.csv": missed,
              "12b_missed_vs_detected.csv": missed_vs_detected,
              "13_error_by_state_and_overlap.csv": bands, "14_feature_importance.csv": imp.sort_values("gain", ascending=False),
              "15_typical_days.csv": days,
              "16_failure_cases.csv": pd.DataFrame(cases, columns=["case", "house", "start_utc", "end_utc"])}
    for name, t in tables.items():
        t.to_csv(TAB_DIR / name, index=False)

    res = dict(d=d, feas=feas, bad=bad, evid=evid, tab=tab, cols=cols, model=model, tau=tau, thr=thr,
               loho_tau=loho_tau, loho_thr=loho_thr,
               metrics=metrics, loho=loho, events=events, true_c=true_c, pred_c=pred_c, fp=fp, missed=missed,
               cyc_stats=cyc_stats, missed_vs_detected=missed_vs_detected, bands=bands, days=days, cases=cases, figs=figs, run_info=run_info)
    kn = key_numbers(res)
    kn.to_csv(TAB_DIR / "17_key_numbers.csv", index=False)
    if verbose:
        pd.set_option("display.width", 250)
        print(f"tau = {tau} W; LOHO taus {loho_tau}; best iterations {best_iters}; runtime {run_info['runtime_s']} s")
        print(metrics[metrics.split == "test"].round(3).to_string())
        print(loho.round(3).to_string())
        print(events.round(3).to_string())
        print(kn.to_string())
    return res


def key_numbers(res: dict) -> pd.DataFrame:
    m = res["metrics"]
    t = m[(m["split"] == "test") & (m["house"] == "pooled")].set_index("model")
    ev = res["events"]
    rows = [("tau_W", res["tau"])]
    rows += [(f"loho_tau_W__held_out_house_{h}", v) for h, v in res["loho_tau"].items()]
    for mname in t.index:
        for k in ("MAE_W", "RMSE_W", "F1", "energy_error_pct"):
            rows.append((f"pooled_test_{k}__{mname}", round(t.loc[mname, k], 3)))
    lo = res["loho"][res["loho"]["model"] == "LightGBM (centered ±60 min)"]
    y_true = lo["energy_true_kWh"].sum()
    rows.append(("loho_pooled_energy_error_pct", round(100 * (lo["energy_pred_kWh"].sum() - y_true) / y_true, 3)))
    for (crit, setting), e in ev.groupby(["criterion", "setting"], sort=False):
        n_true, n_pred, n_m = e["true_cycles"].sum(), e["pred_cycles"].sum(), e["matched_cycles"].sum()
        p = n_m / n_pred if n_pred else np.nan
        r = n_m / n_true
        f1 = 2 * p * r / (p + r) if n_pred and n_m else 0.0
        for k, v in (("matched", n_m), ("true", n_true), ("pred", n_pred), ("precision", p), ("recall", r), ("F1", f1)):
            rows.append((f"cycles_pooled_{k}__{setting}__{crit}", round(float(v), 3) if v == v else np.nan))
    fp = res["fp"]
    for k, v in fp["attributed_to"].value_counts().items():
        rows.append((f"pure_false_detections_attributed__{k}", int(v)))
    rows.append(("pure_false_detections_total", len(fp)))
    rows.append(("missed_cycles_total", len(res["missed"])))
    return pd.DataFrame(rows, columns=["metric", "value"])


if __name__ == "__main__":
    run_all()
