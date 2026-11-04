# Phase 3 Data Documentation: Modelling Dataset for Washing-Machine Disaggregation

Scope: which households, periods and minutes are used to train and evaluate the washing-machine
disaggregation model, and why. All numbers come from [`wm_nilm.py`](wm_nilm.py) and are saved in
[`outputs/tables/phase3/`](../outputs/tables/phase3/); the file is named next to each table.

---

## 1. Source and preparation

| Item | Value |
|---|---|
| Source | Official cleaned REFIT release, `Data/CLEAN_REFIT_081116/CLEAN_House{N}.csv` |
| Input (X) | Whole-house **aggregate** active power, 1-minute mean (W) |
| Target (y) | **Washing-machine** sub-meter active power, 1-minute mean (W) |
| Preparation | Phase 2's rules, reused read-only from `phase2/wm_eda.py`: sort, drop duplicate timestamps, mask frozen readings (an identical value held for more than 60 min), 1-minute arithmetic means, no gap filling. Phase 2 outputs are read if present, otherwise rebuilt in memory. Nothing in Phase 2 is written |
| Time zone | Data are in UTC. Hour-of-day features use Europe/London local time |

> **Assumption:** the 1-minute mean of a sub-meter is the "true" washing-machine power. Sub-meters and the
> aggregate clamp are not synchronised (6–8 s polling), so small timing offsets exist between the two at 1-minute scale.

---

## 2. Household selection

[`01_household_selection.csv`](../outputs/tables/phase3/01_household_selection.csv)

| House | Used | Reason |
|---|---|---|
| 2 | ✅ | Dedicated WM channel, consistent with the aggregate |
| 5 | ❌ | WM channel is probably a **washer-dryer** (model AEG L99695HWD; 5–9.5 h wash-and-dry periods, see Phase 2 DATA.md §2). As a training target it would teach the model that drying is washing. As a test target it would score the model against the wrong appliance |
| 7 | ✅ | Dedicated WM channel, consistent with the aggregate |
| 8 | ✅ | Dedicated WM channel. The house also has a separate washer-dryer (`Appliance3`), a known source of confusion |
| 9 | ✅ | Dedicated WM channel. Unreliable aggregate periods are excluded (§3). The house also has a separate washer-dryer (`Appliance2`) |
| 15 | ✅ | Dedicated WM channel, consistent with the aggregate |

"Consistent with the aggregate" means the WM reading exceeds the whole-house reading in less than 0.1% of
minutes in each selected house.

---

## 3. Minute-level exclusions (same rules for every house)

A minute is **usable** only if all of the following hold:

1. **Both channels are present.** There is a 1-minute value for the aggregate and for the WM after
   frozen-value masking.
2. **The day is not an "unreliable day".** Any UTC day with **≥ 60 minutes of frozen aggregate** is dropped
   entirely, for both input and target.
3. **The minute is not inconsistent.** Minutes where the WM reads more than 100 W **above** the aggregate are
   dropped, because a sub-meter cannot exceed the house total.

### Why whole days are dropped: the House 9 investigation

House 9's aggregate is stuck at one value from **2014-03-13 to 2014-04-01** (≈ 18 days) and from
**2013-12-19 to 2013-12-24**. Throughout these periods the WM channel keeps "reporting", but it reads 0–1 W
the whole time ([`02_house9_aggregate_freeze.png`](../outputs/figures/phase3/02_house9_aggregate_freeze.png)).
The other houses behave the same way whenever their aggregate is frozen
([`04_frozen_aggregate_wm_evidence.csv`](../outputs/tables/phase3/04_frozen_aggregate_wm_evidence.csv)):

| House | Frozen-aggregate minutes | WM exactly 0 W while frozen | WM exactly 0 W normally | WM active while frozen | WM active normally |
|---|---|---|---|---|---|
| 2 | 6,502 | 100.0% | 94.4% | 0.00% | 4.69% |
| 7 | 4,827 | 99.4% | 74.7% | 0.31% | 8.33% |
| 8 | 7,935 | 79.5% | 84.1% | 1.54% | 3.36% |
| 9 | 38,443 | 97.9% | 90.2% | 0.00% | 2.83% |
| 15 | 6,568 | 100.0% | 96.2% | 0.00% | 2.87% |

While the aggregate is frozen, the WM channel reads exactly zero more often than usual, and almost never shows
activity. In House 7, for example, the normal 0.4 W standby reading disappears. The data logger itself appears
to stall. These zeros are **not evidence that the machine was off**, so they are not used as training or test
targets. House 8 is the one exception and the evidence there is weaker (79.5% vs 84.1%). The same rule is
applied anyway, for consistency.

[`03_excluded_unreliable_days.csv`](../outputs/tables/phase3/03_excluded_unreliable_days.csv) lists every
excluded day. Several (2014-08-01/02 and 2014-08-30 to 09-01) occur in **all five houses at once**, which
points to a study-wide logging fault.

---

## 4. Usable coverage and activity balance

[`02_feasibility_and_split.csv`](../outputs/tables/phase3/02_feasibility_and_split.csv)

| House | Record (UTC) | Days with both channels | Unreliable days excluded | Minutes on those days | Inconsistent minutes | **Usable days** | **Active minutes (> 20 W)** |
|---|---|---|---|---|---|---|---|
| 2 | 2013-09-17 → 2015-05-28 | 460.9 | 13 | 11,442 | 53 | **452.9** | 4.69% |
| 7 | 2013-11-01 → 2015-07-08 | 532.6 | 11 | 10,427 | 503 | **525.1** | 8.33% |
| 8 | 2013-11-01 → 2015-05-10 | 492.6 | 23 | 24,049 | 36 | **475.8** | 3.36% |
| 9 | 2013-12-17 → 2015-07-08 | 454.0 | 36 | 12,802 | 25 | **445.1** | 2.83% |
| 15 | 2013-12-17 → 2015-07-08 | 485.1 | 8 | 4,630 | 158 | **481.8** | 2.87% |

**Class balance.** The washing machine is active in only **2.8–8.3%** of usable minutes. As a result:
- an "always off" prediction is correct about 92–97% of the time and has a deceptively low MAE
- metrics that focus on active periods (RMSE, MAE on active minutes, F1, cycle detection) are needed

---

## 5. Chronological split

Each house's **usable** minutes are split in time order, with no random mixing:
- the first 60% go to train
- the next 20% go to validation
- the last 20% go to test

A **1-day buffer** is dropped after each cut. Features look at most 60 min either side of a minute, so no
feature window spans two splits.

| House | Train (UTC) | Days | Active | Validation (UTC) | Days | Active | Test (UTC) | Days | Active |
|---|---|---|---|---|---|---|---|---|---|
| 2 | 2013-09-17 → 2014-10-27 | 271.7 | 4.67% | 2014-10-28 → 2015-02-09 | 90.0 | 4.64% | 2015-02-10 → 2015-05-28 | 89.6 | 4.73% |
| 7 | 2013-11-01 → 2014-11-08 | 315.0 | 7.93% | 2014-11-09 → 2015-02-22 | 104.0 | 7.91% | 2015-02-23 → 2015-07-08 | 104.0 | 9.78% |
| 8 | 2013-11-01 → 2014-10-12 | 285.5 | 3.32% | 2014-10-13 → 2015-01-25 | 94.2 | 3.27% | 2015-01-26 → 2015-05-10 | 94.2 | 3.66% |
| 9 | 2013-12-17 → 2014-12-17 | 267.0 | 2.68% | 2014-12-18 → 2015-03-17 | 88.0 | 3.05% | 2015-03-18 → 2015-07-08 | 88.0 | 3.15% |
| 15 | 2013-12-17 → 2014-11-26 | 289.1 | 2.83% | 2014-11-27 → 2015-03-09 | 95.4 | 2.83% | 2015-03-10 → 2015-07-08 | 95.4 | 3.10% |

Figure: [`01_split_timeline.png`](../outputs/figures/phase3/01_split_timeline.png).

**How each split is used:**
- **Train:** fits the model and the hour-of-day baseline.
- **Validation:** sets the LightGBM early-stopping point and chooses the "on" threshold τ (within-house model:
  all five houses' validation periods).
- **Test:** used once, for the reported results.

**Two evaluation settings use the same test windows:**
1. **Within-house (unseen time):** one model is trained on all five houses' training periods and scored on each
   house's later test period.
2. **Leave-one-house-out (unseen household):** for each house, a model is trained on the other four houses'
   training periods, early-stopped on *their* validation periods, and given its own τ chosen on *those four*
   validation periods only. It is then scored on the held-out house's test period, so the held-out house's data
   is used only for scoring. Only this setting tests generalisation to a new home. The five fold thresholds are in
   [`05b_loho_threshold_selection.csv`](../outputs/tables/phase3/05b_loho_threshold_selection.csv).

---

## 6. Remaining data concerns

> **Limitation:** frozen stretches shorter than 60 min cannot be detected and remain in the data.

> **Limitation:** the `Issues` flag (all sub-meters together exceed the aggregate) is **not** used to exclude
> minutes. It involves the other sub-meters too, and the WM-specific inconsistency is handled by rule 3. Flagged
> minutes are 1.6–4.7% of valid minutes in the selected houses (Phase 2 DATA.md §5).

> **Limitation:** some aggregates contain very large loads that no sub-meter records. In usable minutes,
> Houses 2 and 8 exceed 10 kW for 3,600 and 2,190 minutes respectively, with a 1-minute maximum of 23.8 kW in
> House 8. These are probably electric showers or storage/immersion heating. They are kept as genuine input,
> because they are the kind of load a deployed model would face.

> **Limitation:** all test periods fall in late winter to summer 2015, while training covers autumn 2013 to
> autumn 2014. Seasonal change is part of the test, which is realistic, but it cannot be separated from model error.

> **Limitation:** other sub-meters are used **only** to explain false detections after the fact, never as model
> input. They cover at most 9 appliances per home, so many loads stay unmetered.
