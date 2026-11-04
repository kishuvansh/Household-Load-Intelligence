# Phase 3 Results: Washing-Machine Disaggregation from Whole-House Power

**Task:** estimate washing-machine (WM) power at 1-minute resolution from the whole-house aggregate only.
**Data:** official cleaned REFIT data for Houses 2, 7, 8, 9 and 15; the usable data and exclusions are described
in [DATA.md](DATA.md).
**Code:** [`wm_nilm.py`](wm_nilm.py) (`python3 phase3/wm_nilm.py`, about 2 min) or the notebook
[`03_wm_disaggregation.ipynb`](03_wm_disaggregation.ipynb). Both produce the same outputs.
**Numbers:** all values are in [`outputs/tables/phase3/`](../outputs/tables/phase3/). Headline values are in
[`17_key_numbers.csv`](../outputs/tables/phase3/17_key_numbers.csv).

---

## 1. Summary

- **The model finds most wash cycles, but often mis-times them and raises many false alarms.** Cycles are
  scored by one-to-one matching at temporal IoU ≥ 0.5 (§2). On unseen 2015 test periods:
  - **recall is 77.6%** (337 of 434 true cycles matched)
  - **precision is 40.6%** (337 of 831 predicted cycles), giving **cycle F1 0.53**
  - most unmatched true cycles *are* overlapped by a prediction that covers only the heater block (84 of 97)
  - most unmatched predictions (397 of 494) touch no washing at all and coincide with dishwashers, kettles,
    tumble dryers or unmetered loads
- **It clearly beats both baselines on the metrics that matter for an appliance that is usually off:**

  | Pooled test metric | Model | Always off | Hour-of-day profile |
  |---|---|---|---|
  | RMSE | **128 W** | 220 W | 217 W |
  | Minute-level F1 | **0.56** | 0.00 | 0.20 |

- **Plain MAE is misleading here.** Always-off has a slightly lower MAE than the model in Houses 2 and 9, because
  the machine is off about 95% of the time.
- **Total energy looks right for partly the wrong reason.**
  - Pooled test energy error is **−5.5%**.
  - But the model **under-estimates each true cycle**: it assigns only 48–75% of real cycle energy to the cycle
    window.
  - The gap is largely refilled by energy assigned to false detections.
- **On a never-seen house (leave-one-house-out), cycle recall falls to 68.9% and precision to 28.1% (F1 0.40).**
  Each fold's threshold is chosen without the held-out house (§6). Energy errors reach +46% to +48% in Houses 8
  and 9 and −50% in House 15. The model should not be assumed to transfer to new homes without local checks.

---

## 2. Set-up

| Item | Choice | Why |
|---|---|---|
| Target | 1-min mean WM power (W) | As required. Never used as an input: no lagged target values |
| Input | 1-min mean aggregate (W): values and rolling mean/std/min/max over past, future and centred 5/15/30/60 min; load above the local 2-hour minimum; count of > 1.5 kW step minutes in ±60 min; hour of day; weekend flag (48 features, [`run_info.json`](../outputs/models/phase3/run_info.json)) | The WM's clearest signature is a ~2 kW heater step lasting 10–20 min (Phase 2), which needs a window of context to recognise |
| Prediction timing | Uses aggregate up to **60 min after** the minute being estimated | The estimate exists 60 min after the fact. That suits billing, feedback and programme evaluation, not instant control. A **causal** variant (past 60 min only) is reported as a check |
| Model | LightGBM, squared-error loss, early stopping on validation | Fast (one fit takes about 10–15 s on ~2M training rows), handles missing values natively, easy to explain. The aim was a defensible model, not a high score |
| Post-processing | Clip to [0, aggregate]. Set predictions ≤ τ to 0, where τ = **50 W** is chosen on validation by maximising minute F1 ([`05_threshold_selection_val.csv`](../outputs/tables/phase3/05_threshold_selection_val.csv)). Leave-one-house-out folds choose their own τ (§6) | A WM cannot draw negative power or more than the whole house. Gating removes the small positive smear a squared-error model leaves on the ~95% of off minutes |
| Baselines | (1) **Always off** (0 W); (2) **Hour-of-day profile**: each house's mean WM power for each local hour, from its training period | The two simplest honest references: "no information", and "typical timing only" |
| Split | Per house, chronological 60/20/20 with 1-day buffers (DATA.md §5) | Prevents leakage between neighbouring minutes. The test is genuinely *later* than training |

**Metrics and why they are used**

| Metric | Meaning | Why it is useful here |
|---|---|---|
| MAE (W) | Mean absolute error per minute | Intuitive, but rewards predicting 0 for a mostly-off appliance |
| RMSE (W) | Root-mean-square error | Penalises missing a 2 kW heater block heavily, and those blocks hold most of the energy |
| MAE active (W) | MAE on minutes when the WM was truly on (> 20 W) | Isolates the minutes that matter |
| Energy error (%) | (predicted − actual) total WM energy over the test period | What a bill-level insight would use |
| Minute precision / recall / F1 | Predicted on (> τ) vs actually on (> 20 W) | Standard NILM on/off metrics |
| Cycle precision / recall / F1 | Phase 2's cycle rule is applied to the actual series (20 W) and the predicted series (τ). Cycles are then matched **one-to-one**: temporal IoU = minutes in both ÷ minutes in either, a pair is eligible only if IoU ≥ 0.5, and pairs are taken greedily from the highest IoU down with each cycle used at most once. Precision = matched ÷ predicted cycles; recall = matched ÷ true cycles. The criterion was fixed before evaluation. A looser one-to-one "any overlap" version is reported only as a sensitivity check | What a utility would act on ("did a wash happen, and when?"). One-to-one matching stops one long prediction from "detecting" several washes, and stops several fragments from all counting as correct |

---

## 3. Results on the test period: unseen time in the same households

Source: [`06_metrics_within_house.csv`](../outputs/tables/phase3/06_metrics_within_house.csv),
[`08_event_metrics.csv`](../outputs/tables/phase3/08_event_metrics.csv)

| House | Model | MAE (W) | RMSE (W) | MAE active (W) | Energy error | Minute F1 | Cycle recall | Cycle precision | Cycle F1 |
|---|---|---|---|---|---|---|---|---|---|
| 2 | **LightGBM** | 24.5 | **137.0** | **261.2** | +9.3% | **0.344** | 0.297 | 0.119 | **0.170** |
| | Hour profile | 33.7 | 178.0 | 385.1 | −14.1% | 0.093 | 0 | n/a | 0 |
| | Always off | **19.6** | 179.8 | 412.2 | −100% | 0 | 0 | n/a | 0 |
| 7 | **LightGBM** | **31.7** | **156.2** | **235.2** | −15.5% | **0.673** | 0.896 | 0.601 | **0.720** |
| | Hour profile | 67.8 | 267.9 | 429.1 | −29.8% | 0.268 | 0 | n/a | 0 |
| | Always off | 46.2 | 275.0 | 471.2 | −100% | 0 | 0 | n/a | 0 |
| 8 | **LightGBM** | **25.4** | **141.4** | **359.8** | +10.5% | **0.544** | 0.822 | 0.337 | **0.478** |
| | Hour profile | 56.2 | 254.5 | 838.6 | −13.8% | 0.198 | 0 | n/a | 0 |
| | Always off | 33.3 | 260.4 | 902.4 | −100% | 0 | 0 | n/a | 0 |
| 9 | **LightGBM** | 17.9 | **114.5** | **294.7** | +8.3% | **0.463** | 0.860 | 0.287 | **0.430** |
| | Hour profile | 31.0 | 174.7 | 529.4 | −13.8% | 0.064 | 0 | n/a | 0 |
| | Always off | **17.4** | 175.5 | 549.6 | −100% | 0 | 0 | n/a | 0 |
| 15 | **LightGBM** | **7.6** | **72.3** | **226.3** | −34.2% | **0.675** | 0.756 | 0.791 | **0.773** |
| | Hour profile | 33.2 | 177.3 | 566.7 | −11.1% | 0.000 | 0 | n/a | 0 |
| | Always off | 18.1 | 177.3 | 580.2 | −100% | 0 | 0 | n/a | 0 |
| **Pooled** | **LightGBM** | **21.6** | **128.3** | **264.0** | −5.5% | **0.556** | 0.776 | 0.406 | 0.533 |
| | Causal LightGBM | 25.5 | 149.4 | 312.9 | −11.5% | 0.505 | 0.737 | 0.349 | 0.474 |
| | Hour profile | 45.1 | 216.5 | 510.1 | −19.4% | 0.202 | 0 | n/a | 0 |
| | Always off | 27.5 | 220.3 | 546.6 | −100% | 0 | 0 | n/a | 0 |

Cycle columns use one-to-one matching at IoU ≥ 0.5. "n/a": the baseline never predicts a cycle, so precision is
undefined. The hour profile never exceeds τ long enough to form a cycle. Bold marks the best value per house where
the comparison is meaningful.

**Sensitivity of the cycle metrics to the matching criterion** (pooled within-house, from
[`17_key_numbers.csv`](../outputs/tables/phase3/17_key_numbers.csv)):

| Criterion | Matched | Recall | Precision | F1 |
|---|---|---|---|---|
| **One-to-one, IoU ≥ 0.5 (primary)** | 337 | 0.776 | 0.406 | 0.533 |
| One-to-one, any overlap (sensitivity) | 412 | 0.949 | 0.496 | 0.651 |

Of the 84 true cycles that are overlapped but below IoU 0.5 (§7.2), 75 are matched under the looser criterion.
The other 9 remain unmatched even then, because their only overlapping prediction is already matched to another
wash. That is exactly the double-counting one-to-one matching prevents. An earlier version of this
report used many-to-many overlap (97% recall, 52% precision). That method overstated detection, because one
prediction could count for several washes, and it has been replaced.

![Metrics by house](../outputs/figures/phase3/06_metrics_by_house.png)

**Interpretation.**
- The model has the lowest **RMSE** and **MAE on active minutes** in every house. RMSE is 23–59% below the
  better baseline and active-minute MAE 32–60% below, and the model is the only method that detects anything.
- **MAE** favours always-off in Houses 2 and 9, because the model pays for false "on" estimates on the ~95% of
  off minutes. This is why MAE alone would be a misleading headline for a sparse appliance.
- The **causal** (real-time) variant is worse on RMSE and F1 in every house. Seeing what the aggregate does in the
  hour *after* a minute helps confirm a wash.
- **House 2** has very low cycle F1 (0.17) despite reasonable minute metrics. Its programmes are long (median
  ~98 min, Phase 2), and the predicted cycle usually covers only the heater block, so IoU stays below 0.5
  (44 of its 64 cycles are overlapped but unmatched, §7.2).
- **House 15** is the easiest home: high minute precision (0.88) and the lowest errors. Its aggregate is low and quiet
  (Phase 2: about 6 kWh/day), so the heater step stands out. Its energy is under-estimated by 34% because the
  long low-power tail is missed.

---

## 4. Actual vs predicted

![Actual vs predicted, 1-minute](../outputs/figures/phase3/03_actual_vs_predicted_minutes.png)

Most minutes sit at (0, 0). True heater minutes (~2.0–2.3 kW) form a vertical band. The model usually
recognises them but often predicts less than the true value. True low-power minutes (20–500 W) are mostly
predicted near 0. A smaller cloud at "actual 0, predicted > 0" shows the false detections.

![Representative test days](../outputs/figures/phase3/05_typical_test_days.png)

The days shown are chosen **by rule**, not hand-picked: for each house, the test day (≥ 95% usable, at least one
active minute) whose daily MAE is closest to that house's median day
([`15_typical_days.csv`](../outputs/tables/phase3/15_typical_days.csv)). Typical behaviour:
- The heater block is found but often under-sized (Houses 2 and 9).
- In quieter homes or periods it is tracked closely (Houses 7 and 15).
- The long low-power tail is largely missed.
- Some kettle-sized spikes in the aggregate cause brief false estimates.

---

## 5. Per-cycle energy: why total energy agreement is partly a coincidence

Source: [`09_cycle_energy_by_house.csv`](../outputs/tables/phase3/09_cycle_energy_by_house.csv)

| House | True test cycles | Actual energy in cycles (kWh) | Predicted energy in the same windows (kWh) | Median per-cycle abs. error | Predicted energy in false detections (kWh) | Total predicted (kWh) |
|---|---|---|---|---|---|---|
| 2 | 64 | 41.980 | 20.149 | 52% | 19.699 | 46.076 |
| 7 | 202 | 114.939 | 76.247 | 34% | 13.659 | 97.555 |
| 8 | 73 | 74.656 | 56.066 | 27% | 17.148 | 83.102 |
| 9 | 50 | 36.527 | 21.551 | 45% | 12.135 | 39.775 |
| 15 | 45 | 40.860 | 26.209 | 39% | 0.719 | 27.206 |

![Per-cycle energy](../outputs/figures/phase3/04_cycle_energy_actual_vs_predicted.png)

**Interpretation.** In every house the model assigns **less** energy to the true cycle windows than they actually
used: 48% of the real energy in House 2, 75% in House 8. The tail is the missing part. Totals look acceptable
(e.g. House 2: +9%) only because energy from false detections (House 2: 19.699 kWh) fills the gap. A utility using
the totals would be counting dishwasher and kettle energy as washing. **Totals should therefore not be read as
evidence of accurate per-cycle estimates.** House 15 has almost no false-detection energy and shows the
under-estimate plainly (−34%).

---

## 6. Unseen households: leave-one-house-out

Source: [`07_metrics_leave_one_house_out.csv`](../outputs/tables/phase3/07_metrics_leave_one_house_out.csv),
[`08_event_metrics.csv`](../outputs/tables/phase3/08_event_metrics.csv),
[`05b_loho_threshold_selection.csv`](../outputs/tables/phase3/05b_loho_threshold_selection.csv). Same test windows
as §3.

**Protocol per fold:**
- The model is fitted on the other four houses' training periods and early-stopped on *their* validation periods.
- The fold's "on" threshold τ is chosen by maximising minute F1 on *those four houses' validation periods only*.
- That τ is applied to the held-out house's test predictions, for both minute and cycle metrics.

The held-out house contributes nothing to fitting, early stopping or τ.

**Fold thresholds:** τ = 50 W in all five folds (held-out House 2, 7, 8, 9, 15). This is the same value as the
within-house model, so the LOHO minute metrics are identical to those of an earlier run that wrongly took τ from
validation data including the held-out house. That earlier protocol was a leak in principle, even though it did
not change the numbers here.

| Held-out house | Fold τ (W) | MAE (W) | RMSE (W) | RMSE, always off | Energy error | Minute F1 | Cycle recall | Cycle precision | Cycle F1 | Cycle F1, within-house |
|---|---|---|---|---|---|---|---|---|---|---|
| 2 | 50 | 29.1 | 168.9 | 179.8 | −5.0% | 0.308 | 0.172 | 0.074 | 0.103 | 0.170 |
| 7 | 50 | 41.1 | 188.1 | 275.0 | −12.7% | 0.587 | 0.822 | 0.456 | 0.587 | 0.720 |
| 8 | 50 | 58.5 | 246.5 | 260.4 | **+45.7%** | 0.364 | 0.726 | 0.182 | 0.290 | 0.478 |
| 9 | 50 | 27.4 | 151.9 | 175.5 | **+48.0%** | 0.411 | 0.820 | 0.190 | 0.308 | 0.430 |
| 15 | 50 | 10.2 | 94.0 | 177.3 | **−50.2%** | 0.529 | 0.622 | 0.667 | 0.644 | 0.773 |
| Pooled | | | | | +4.7% | | 0.689 | 0.281 | 0.399 | 0.533 |

Cycle columns use one-to-one matching at IoU ≥ 0.5. With the looser one-to-one "any overlap" criterion, pooled
LOHO recall is 0.919, precision 0.375 and F1 0.533.

**Interpretation.** This is the only test of a never-seen home, and it covers just five houses from one UK region.
- **What holds up:** the model still beats always-off on RMSE in every held-out house (only just in House 8) and
  still matches most cycles in Houses 7, 8 and 9.
- **What gets worse:**
  - Cycle F1 falls in every house compared with the within-house model.
  - Precision falls sharply in Houses 2, 8 and 9, where most predicted cycles are not washes.
  - Total energy becomes unreliable (+46% to +48% in Houses 8 and 9, −50% in House 15). The pooled +4.7% hides
    these opposite-signed errors.
- **Why:** the new home's other appliances, and its washing machine's exact signature, were never in training.

**Cross-household generalisation is therefore weak to moderate. It should not be assumed without local
validation.**

---

## 7. Error analysis

### 7.1 Where the errors come from

Source: [`13_error_by_state_and_overlap.csv`](../outputs/tables/phase3/13_error_by_state_and_overlap.csv)
(all test minutes pooled)

| What the WM was actually doing | Share of test minutes | LightGBM MAE (bias) | Hour profile MAE | Always off MAE |
|---|---|---|---|---|
| Off (≤ 20 W) | 95.0% | 8.9 W (+8.6) | 20.6 W | 0.1 W |
| Low-power tail (20–1000 W) | 3.9% | 114.3 W (−37.5) | 91.4 W | 121.4 W |
| Heating (> 1000 W) | 1.1% | 805.6 W (−758.1) | 2025.1 W | 2084.8 W |
| Active, other load < 500 W | 2.3% | 226.4 W (−208.7) | 545.1 W | 578.6 W |
| Active, other load 500–1500 W | 1.7% | 221.9 W (−205.8) | 405.9 W | 444.6 W |
| Active, other load ≥ 1500 W | 1.0% | 415.1 W (−140.4) | 606.2 W | 645.3 W |

![Error by state](../outputs/figures/phase3/08_error_by_state_and_overlap.png)

- **Heater minutes** have the largest error per minute, and the model **under-sizes** them (bias −758 W), but it
  still cuts the error by about 60% compared with the baselines.
- **Low-power minutes** are barely better than always-off and *worse* than the hour profile. A few hundred watts of
  drum or spin cannot be separated from fridges, lighting and electronics in the aggregate.
- **Overlapping loads:** MAE during active minutes almost doubles when ≥ 1.5 kW of other load runs at the same time.
- **Off minutes:** the model adds a small positive error (+8.6 W bias). These are its false "on" estimates.

### 7.2 Unmatched true cycles

Source: [`10_test_true_cycles.csv`](../outputs/tables/phase3/10_test_true_cycles.csv) (`status` column),
[`12_missed_cycles.csv`](../outputs/tables/phase3/12_missed_cycles.csv),
[`12b_missed_vs_detected.csv`](../outputs/tables/phase3/12b_missed_vs_detected.csv)

| Outcome (IoU ≥ 0.5 matching) | Cycles | Median energy | Heated (> 1 kW) | Median duration | Median concurrent other load | Median best IoU with any prediction |
|---|---|---|---|---|---|---|
| Matched | 337 | 0.625 kWh | 100.0% | 68 min | 919 W | 0.871 |
| Overlapped, IoU below 0.5 | 84 | 0.611 kWh | 97.6% | 88.5 min | 862 W | 0.316 |
| No overlap at all | 13 | 0.202 kWh | 61.5% | 47 min | 479 W | 0 |

| House | Matched | Overlapped, IoU < 0.5 | No overlap |
|---|---|---|---|
| 2 | 19 | 44 | 1 |
| 7 | 181 | 17 | 4 |
| 8 | 60 | 12 | 1 |
| 9 | 43 | 6 | 1 |
| 15 | 34 | 5 | 6 |

Unmatched true cycles fall into two groups:
- **Found but mis-timed (84):** ordinary heated cycles, typically overlapped by exactly one predicted cycle. That
  prediction covers the heater block but stops early because the low-power tail is missed, so the IoU is about
  0.3. They are longer than matched cycles, and House 2 (long programmes) accounts for 44. Some of these "true
  cycles" are probably two back-to-back loads that the Phase 2 rule joins into one (see failure case 2), so part
  of the error lies in the ground-truth definition.
- **Not found at all (13; 6 in House 15):** small, short and often unheated. These are cold or quick programmes
  whose only signature is the weak tail, or heater blocks buried under several kW of other load (failure case 1).

### 7.3 False detections

Source: [`11_false_detections_attributed.csv`](../outputs/tables/phase3/11_false_detections_attributed.csv).
Of the 494 unmatched predicted cycles (within-house):
- **97** overlap some real WM activity. These are mis-timed or fragmented estimates of a real wash.
- **397** overlap no WM activity at all. These *pure false detections* are analysed here.

Each pure false detection is attributed to the other **sub-metered** appliance with the most energy in that window,
but only if that appliance used at least half of the falsely predicted energy. Otherwise the cause is "unmetered or
unclear".

| Attributed cause | False detections | Falsely predicted WM energy (kWh) |
|---|---|---|
| Dishwasher | 132 | 16.5 |
| Unmetered or unclear | 111 | 23.6 |
| Kettle | 55 | 5.4 |
| Tumble dryer | 55 | 10.0 |
| Washer-dryer (separate machine, Houses 8 and 9) | 19 | 5.7 |
| Microwave | 16 | 1.4 |
| Other (TV site, fridge-freezer, freezer, toaster) | 9 | 0.8 |

Per house:
- **Houses 2 and 9:** dishwashers dominate (45 and 56).
- **House 7:** its tumble dryer accounts for 51, then the dishwasher for 31.
- **House 8:** most false detections (68) have no metered cause. Its aggregate carries large unmetered loads (DATA.md §6).

All the main culprits have **~2 kW heating elements**, like the washing machine's heater, so a heater step alone
does not identify the appliance.

### 7.4 Failure cases (chosen by rule)

[`16_failure_cases.csv`](../outputs/tables/phase3/16_failure_cases.csv)

![Failure cases](../outputs/figures/phase3/07_failure_cases.png)

1. **Missed cycle** (the largest-energy true cycle with no overlapping prediction; House 7, 15 Apr 2015). The 2 kW
   heater block sits inside a stretch of repeated 1–6 kW loads, so the step is not distinguishable.
2. **Found but badly aligned** (the largest-energy true cycle overlapped below IoU 0.5; House 2, 23 Feb 2015, best
   IoU 0.40). The model predicts both heater blocks, but the Phase 2 rule joins about 3.5 hours with two heater
   blocks (probably two loads) into one true cycle. No single predicted cycle covers half of it, so one-to-one
   matching counts it as unmatched.
3. **False detection** (the largest predicted cycle that overlaps no WM activity; House 2, 28 Mar 2015). A steady
   ~2.3 kW load runs for hours overnight on no sub-meter (possibly immersion or storage heating) and is read as
   washing.
4. **Overlapping loads** (the true cycle with the highest concurrent other load; House 8, 24 Feb 2015). The heater
   is tracked, but energy leaks into the surrounding 4–6 kW period.

### 7.5 Where the model is likely to struggle

- **Homes with other ~2 kW heating appliances** (dishwasher, tumble dryer, washer-dryer, kettle, immersion
  heater), especially when those run at times when washing is also common.
- **Cold or short washes** with no heater block: they are missed or badly under-estimated.
- **Long programmes and back-to-back loads:** the predicted cycle ends with the heater block, so start and end
  times (and IoU) are poor even when the wash is found.
- **Busy periods** (cooking, showers) where the heater step overlaps other large loads.
- **New homes** (§6): precision and energy accuracy drop without local training data.

---

## 8. Model behaviour

[`14_feature_importance.csv`](../outputs/tables/phase3/14_feature_importance.csv). The most-used features
(share of total gain):

| Feature | Gain share |
|---|---|
| Load above the local 2-hour minimum (`excess_over_base`) | 11.3% |
| Minimum aggregate over the past 5 min | 11.0% |
| Minimum aggregate over the centred ±5 min | 5.1% |
| Hour of day (sin) | 4.5% |
| Number of > 1.5 kW step minutes in ±60 min | 4.5% |

These features capture a **sustained** step above the baseline. A short kettle spike raises the maximum but not
the 5-minute minimum, while a 10–20-minute heater block raises both. Hour of day contributes too, which ties the
model partly to each house's routine (Phase 2) and is one reason transfer to new homes is weaker.

Validation results (pooled): MAE 24.7 W, RMSE 141.5 W, F1 0.48, energy error +6.4%. These are in line with the
test results, so there is no sign of over-fitting to the validation period.

---

## 9. Research ideas adopted

| Source | Idea used here |
|---|---|
| Murray, D., Stankovic, L., Stankovic, V. (2017). *An electrical load measurements dataset of United Kingdom households from a two-year longitudinal study.* Scientific Data 4, 160122 | Dataset description, channel meanings and known collection issues |
| Zhang, C., Zhong, M., Wang, Z., Goddard, N., Sutton, C. (2018). *Sequence-to-point learning with neural networks for non-intrusive load monitoring.* AAAI | Predict the appliance's value at one point from a **window of aggregate centred on it**. Here that is a ±60-min window summarised into features for a tree model rather than a CNN |
| Kelly, J., Knottenbelt, W. (2015). *Neural NILM: Deep neural networks applied to energy disaggregation.* ACM BuildSys | **On-power thresholds** with precision/recall/F1 alongside energy errors, and testing on **unseen houses** as the real generalisation check |
| Batra, N. et al. (2014). *NILMTK: an open source toolkit for non-intrusive load monitoring.* ACM e-Energy | Reporting several complementary metrics (error in W, energy error, on/off F1) rather than one score |

All modelling and evaluation code here is my own. No pre-trained models or NILM toolkits were used.

---

## 10. Limitations

> **Limitation:** ground truth is the WM sub-meter. Sub-meter and aggregate are not synchronised to the second,
> so a minute or two of timing error at cycle edges is unavoidable.

> **Limitation:** five houses, one region, 2013–2015 appliances. The leave-one-house-out results are the only
> cross-household evidence and rest on five folds.

> **Limitation:** test periods are late winter to summer 2015 only. Performance in other seasons, when heating
> loads overlap more, may differ.

> **Limitation:** τ (50 W) and the Phase 2 cycle rule shape the event metrics. Different thresholds would shift
> the precision/recall balance.

> **Limitation:** false-detection attribution only sees sub-metered appliances. "Unmetered or unclear" cases
> cannot be explained further from this data.

**What I would try next (not done):**
1. Add a second-stage cycle classifier on detected events (duration, heater timing, tail shape) to reject
   dishwasher and tumble-dryer look-alikes.
2. Model the low-power tail explicitly, for example by predicting cycle energy per detected event instead of
   per minute.
3. Fine-tune with a few labelled cycles per new home, since leave-one-house-out shows home-specific signatures
   matter.
