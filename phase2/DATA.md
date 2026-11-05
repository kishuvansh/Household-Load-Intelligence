# Phase 2 Data Documentation: Washing-Machine Channels in the Official Cleaned REFIT Data

Scope: data used for the Phase 2 washing-machine EDA, how it was checked and prepared, and what remains
uncertain. Phase 2 uses **only the official cleaned REFIT release**. It does not use Phase 1's House 1 output.

All numbers below are produced by [`wm_eda.py`](wm_eda.py) and saved in
[`outputs/tables/phase2/`](../outputs/tables/phase2/). The file each number comes from is named next to it.

---

## 1. Source files

| Item | Value |
|---|---|
| Release | REFIT Electrical Load Measurements, cleaned version `CLEAN_REFIT_081116` (University of Strathclyde) |
| Location in repo | `Data/CLEAN_REFIT_081116/CLEAN_House{N}.csv` (20 files, excluded from git) |
| Official readme | `CLEAN_READ_ME_081116.txt`, copy saved at [`docs/CLEAN_READ_ME_081116.txt`](../docs/CLEAN_READ_ME_081116.txt) |
| Columns | `Time, Unix, Aggregate, Appliance1 … Appliance9, Issues` |
| Units | Active power in **watts (W)**. `Unix` is seconds since 1970, documented as UTC (see *Timestamps* below) |
| Native sampling | Irregular, roughly every **6–8 s** (median step 7 s in all six houses used). Values are recorded on change and forward-filled by the cleaning pipeline |
| Phase 2 resolution | **1-minute** means |

**What the official cleaning already did** (from the readme):
- corrected UK daylight-saving time
- re-aligned sensor columns when plugs were moved
- **forward-filled NaN values**
- replaced sensor spikes above 4000 W with 0
- added `Issues = 1` where the sub-meters sum to more than the aggregate

**Timestamps and the clock correction.** The `Time` column equals `Unix` exactly in every row, and the readme
documents `Unix` as UTC with daylight saving corrected. A project audit showed this is true only up to the point
where each official file switches from raw Part 1 to raw Part 2, on the evening of 30 Sep 2014. After that, `Unix`
is the UK wall clock, which is one hour later than UTC in summer.

Evidence ([`../audit/official_timestamp_check.py`](../audit/official_timestamp_check.py), tables in
[`outputs/tables/audit/`](../outputs/tables/audit/)):

- The same reading appears in the raw and official files with identical labels after the switch, and 1 h apart
  before it.
- Comparing each house month by month, all 26 summer months built from Part 2 match the raw local clock, while all
  40 summer months built from Part 1 are on UTC.
- Daily routines in Apr–Jun 2015 sit one hour later than in Apr–Jun 2014 as published, and line up once corrected
  (Houses 2, 5, 7 and 9).

`load_house` therefore corrects the timestamps before any other step (`correct_official_clock` in
[`wm_eda.py`](wm_eda.py)):

1. Rows up to each house's switch instant are left unchanged.
2. Later rows are read as Europe/London local time and converted to UTC: −1 h in BST, no change in GMT.
3. Rows labelled 01:00–01:59 on 26 Oct 2014 hold both passes through the repeated hour, interleaved, so they are
   dropped.
4. Shifted rows that land in the hour already covered by Part 1 are dropped.

| House | Rows in file | Rows re-timed (−1 h) | Rows dropped |
|---|---|---|---|
| 2 | 5,733,526 | 875,000 | 1,513 |
| 5 | 7,430,755 | 1,203,834 | 1,233 |
| 7 | 6,756,034 | 1,241,260 | 1,436 |
| 8 | 6,118,469 | 740,548 | 1,426 |
| 9 | 6,169,525 | 1,281,127 | 1,461 |
| 15 | 6,225,696 | 1,311,675 | 1,433 |

Hour-of-day and weekday analysis then converts UTC to **Europe/London** local time, so a 08:00 wash is 08:00 on
the household's clock in both winter and summer. Setting `REFIT_OFFICIAL_CLOCK=as_published` reproduces the
uncorrected run.

> **Limitation:** the dropped repeated hour leaves a 2-hour gap (00:00–02:00 UTC, 26 Oct 2014) in every house.
> That is why each house has one more gap over 60 minutes than in the uncorrected run.

> **Limitation:** the official files' Part 1 conversion also leaves 1-hour artefacts at the 2013–14 clock changes,
> for example an empty 00:00 UTC hour on 30 Mar 2014 (`phase1` table 15). These cannot be separated and are left
> as they are. They are at most an hour or two per house.

---

## 2. Household selection and washing-machine channel mapping

Channel mapping comes from the readme's per-house appliance list
([`01_household_channel_mapping.csv`](../outputs/tables/phase2/01_household_channel_mapping.csv)).

**Rule:** use a house only if its readme entry names **one dedicated "Washing Machine"** with **no change note**
during the study. Sixteen houses pass. Six were chosen to cover different household sizes (1, 2 and 4 occupants)
and to keep the analysis manageable.

| House | WM channel | Readme label | Occupants | Note |
|---|---|---|---|---|
| 2 | `Appliance2` | Washing Machine, LG, F1289TD | 4 | |
| 5 | `Appliance3` | Washing Machine, AEG, L99695HWD | 4 | **See caveat below** |
| 7 | `Appliance5` | Washing Machine, Bosch, Unknown | 4 | |
| 8 | `Appliance4` | Washing Machine (no make/model) | 2 | A separate Washer Dryer is on `Appliance3` (not used) |
| 9 | `Appliance3` | Washing Machine, Bosch, Classixx 6 1200 Express | 2 | A separate Washer Dryer is on `Appliance2` (not used) |
| 15 | `Appliance3` | Washing Machine, Beko, WMB91242LB | 1 | |

**Houses deliberately not used:**

| House | Reason |
|---|---|
| 1 | Has both a Washer Dryer (`Appliance4`) and a Washing Machine (`Appliance5`); which one carries the household's main laundry load is unclear |
| 4 | Has two washing machines (`Appliance4`, `Appliance5`) |
| 13 | Readme notes the washing machine "Changes 25 Mar 2015" |
| 12 | No washing machine listed (several channels are labelled `???`) |

> **Mapping caveat (House 5).** The readme labels this channel "Washing Machine", but the model number
> L99695HWD appears to be an AEG **washer-dryer**. The data agree: between February and May 2015, House 5
> has 15 activity periods lasting 305–572 min (1.34–3.44 kWh each, with ~2.2 kW heater peaks). That is longer
> than any likely wash cycle in the other five houses (maximum 257 min) and fits a wash followed by a dry.
> House 5 is kept because the mapping is official, but its cycle statistics may mix washing and drying.

> **Assumption.** The readme mapping holds for the whole record. The readme itself warns that occupants may
> have moved plugs. The cleaned release says it corrected such moves, but this cannot be checked independently.

---

## 3. Integrity checks on the cleaned files

Source: [`02_integrity_checks.csv`](../outputs/tables/phase2/02_integrity_checks.csv)

| House | Raw rows | NaN values | Out-of-order steps | Duplicate `Unix` | Negative values | WM max (W) | Aggregate max (W) |
|---|---|---|---|---|---|---|---|
| 2 | 5,733,526 | 0 | 0 | 0 | 0 | 3584 | 24,595 |
| 5 | 7,430,755 | 0 | 0 | 0 | 0 | 2437 | 41,738 |
| 7 | 6,756,034 | 0 | 0 | 0 | 0 | 3584 | 32,730 |
| 8 | 6,118,469 | 0 | 0 | 0 | 0 | 3972 | 25,358 |
| 9 | 6,169,525 | 0 | 0 | 0 | 0 | 2484 | 28,836 |
| 15 | 6,225,696 | 0 | 0 | 0 | 0 | 3336 | 8,840 |

The cleaned files are already sorted, free of duplicates and free of NaN values. Missing data therefore
appears in two ways:
- as **missing rows** (time gaps)
- as **forward-filled values** that hide sensor dropouts

The washing-machine maxima are physically plausible: a domestic heater is about 2–2.3 kW, and short inrush
spikes can go higher. Aggregate maxima of 25–42 kW are high for one home. The aggregate is only used for
context in Phase 2, so this is noted for Phase 3 and not acted on here.

---

## 4. Preparation steps and why

| Step | What | Why |
|---|---|---|
| 1 | Read only `Unix`, `Aggregate`, the WM channel and `Issues` | Smaller memory footprint and faster loading. The raw CSVs are never modified |
| 2 | Sort by `Unix`; drop duplicate `Unix` (keep first) | Safety net. Zero rows were affected (table above) |
| 3 | **Mask frozen WM readings:** an identical value above 20 W held for more than 60 min becomes missing | The cleaned data forward-fills dropouts, so a dead sensor can show one wattage for hours. One example: House 5 reads exactly 2129 W for about 13 h, which a washing machine cannot do. Real appliance power always fluctuates. A constant 0–3 W standby value is normal, so values at or below 20 W are never masked |
| 4 | **Mask frozen aggregate readings:** any identical value held for more than 60 min becomes missing | A whole house never draws exactly the same wattage for an hour, because fridges cycle. Only used for the 24-hour trace and the energy-share figure |
| 5 | Resample to **1-minute arithmetic means** over a complete minute index | Readings arrive regularly (every ~6–8 s), so a plain mean of about 8 readings is a good estimate of average power in that minute. Minutes with no valid reading stay missing (`NaN`) |
| 6 | **No gap filling** | A missing minute is unknown. It is **never** treated as 0 W or "machine off". Rates are divided by observed time (Section 6) |

**Frozen-value masking results** (`02_integrity_checks.csv`):

| House | WM minutes masked as frozen | Aggregate samples masked as frozen |
|---|---|---|
| 2 | 0 | 31,699 |
| 5 | **17,228** (≈12 days) | 5,880 |
| 7 | 0 | 8,705 |
| 8 | 0 | 34,361 |
| 9 | 0 | **319,996** |
| 15 | 225 | 27,972 |

House 7 has 92 WM samples masked (one 98-min run at 2178 W), but other readings in the same minutes survive,
so no whole minute is lost. House 9's aggregate is stuck for weeks in spring 2014 while its washing-machine
channel keeps reporting. In total, House 9 has 38,443 minutes (≈26.7 days) with a valid WM reading but no valid
aggregate (`03_availability_quality_summary.csv`). This does not affect the WM-only analysis, but **Phase 3 must
handle it**.

Processed output: `Data/processed/phase2/house{N}_wm_1min.parquet`, with columns `aggregate_w`, `wm_w`,
`issues` and `n_samples` on a UTC minute index. These files are excluded from git because they can be rebuilt
in about 15 s.

---

## 5. Availability and data-quality findings

Source: [`03_availability_quality_summary.csv`](../outputs/tables/phase2/03_availability_quality_summary.csv),
[`04_gaps_over_1h.csv`](../outputs/tables/phase2/04_gaps_over_1h.csv)

"Expected minutes" means every minute from the first to the last reading. "Valid" means a 1-minute WM value exists.

| House | First reading (UTC) | Last reading (UTC) | Span (days) | Expected min | Valid min | Missing min | Availability |
|---|---|---|---|---|---|---|---|
| 2 | 2013-09-17 22:08 | 2015-05-28 07:05 | 617.4 | 889,018 | 670,044 | 218,974 | **75.37%** |
| 5 | 2013-09-26 09:56 | 2015-07-06 16:48 | 648.3 | 933,533 | 826,373 | 107,160 | 88.52% |
| 7 | 2013-11-01 22:01 | 2015-07-08 01:10 | 613.1 | 882,910 | 771,721 | 111,189 | 87.41% |
| 8 | 2013-11-01 22:13 | 2015-05-10 22:36 | 555.0 | 799,224 | 717,101 | 82,123 | **89.72%** |
| 9 | 2013-12-17 17:26 | 2015-07-08 17:52 | 568.0 | 817,947 | 692,064 | 125,883 | 84.61% |
| 15 | 2013-12-17 17:24 | 2015-07-08 01:10 | 567.3 | 816,947 | 705,036 | 111,911 | 86.30% |

| House | Gaps (all) | Gaps ≤ 5 min | Gaps > 60 min | Gaps > 1 day | Longest gap | `Issues` flag (% valid min) | WM exactly 0 W | WM ≤ 20 W (off/standby) | WM > 20 W (active) |
|---|---|---|---|---|---|---|---|---|---|
| 2 | 818 | 355 | 79 | 11 | 62.0 d from 2013-10-25 | 2.01% | 94.39% | 95.31% | 4.69% |
| 5 | 1838 | 963 | 266 | 11 | 8.6 d from 2015-05-10 | 7.54% | 53.39% | 94.47% | 5.53% |
| 7 | 638 | 519 | 25 | 11 | 41.6 d from 2014-01-24 | 4.64% | 74.86% | 91.64% | 8.36% |
| 8 | 638 | 552 | 18 | 8 | 39.1 d from 2014-01-26 | 1.72% | 84.05% | 96.68% | 3.32% |
| 9 | 574 | 495 | 15 | 9 | 41.6 d from 2014-01-24 | 2.39% | 90.63% | 97.32% | 2.68% |
| 15 | 1310 | 616 | 201 | 11 | 8.6 d from 2015-05-10 | 1.61% | 96.17% | 97.10% | 2.90% |

Zero/off/active percentages are shares of **valid** minutes only.

**Main findings**

1. **Study-wide outages.** Several long gaps start or end on the same dates in several houses:
   - Houses 7, 8 and 9 all lose data until **2014-03-06 16:22 UTC**; 7 and 9 from 2014-01-24, 8 from 2014-01-26.
   - Five houses (2, 5, 7, 9, 15) share a gap from **2015-05-10 to 2015-05-19**.
   - Four houses (5, 7, 9, 15) share a gap from **2015-06-10 to 2015-06-18**.

   These are collection-system failures, not household behaviour. The readme notes missing data "notably
   February 2014".
2. **House 2** has the lowest availability. It has two back-to-back outages (62.0 days from 2013-10-25, then
   49.0 days from 2013-12-26), leaving almost no data between late October 2013 and mid-February 2014.
3. **Houses 5 and 15** have many short and medium gaps (266 and 201 gaps over 1 hour) but no outage longer
   than 8.6 days.
4. **`Issues` flag.** 1.6–7.5% of valid minutes are flagged as sub-meters summing to more than the aggregate.
   This describes the relationship between the aggregate and the sub-meters, not the WM channel itself, so these
   minutes are **kept** for the WM EDA. Phase 3, which models the aggregate, should revisit them.
5. **Standby behaviour differs by house.** House 5 sits at about 3 W standby most of the time (only 53% exact
   zeros), and House 7 at about 0.4 W. The others are mostly exactly 0 W. Every house is clearly below the
   20 W activity threshold when idle.

Figure: [`01_wm_daily_availability.png`](../outputs/figures/phase2/01_wm_daily_availability.png). It shows the
daily share of valid minutes, with days outside each house's recording period in grey.

---

## 6. Rules that depend on data quality

- **Rates use observed time.** Cycles per week, cycles per weekday and hour-of-day rates are divided by
  *observed* minutes, not calendar time, so outages don't look like "no washing".
- **Gap-affected cycles.** A detected cycle with any missing minute inside it, or within 15 min of either end, is
  flagged `gap_affected`. Such cycles count towards frequency but are **excluded from duration and energy
  statistics**, because they may be truncated or split.
- **Analysis period.** Each house's full available record (Section 5). No date window was cut, to keep as many
  cycles as possible. Seasonal coverage therefore differs slightly between houses.

---

## 7. Remaining concerns

> **Limitation:** forward-filling in the cleaned release hides short sensor dropouts. Only stuck periods over
> 60 min are caught, so some short frozen stretches remain in the data.

> **Limitation:** the House 5 washer-dryer question above. A frozen-value check cannot settle it, and drying
> energy may be included in House 5's totals.

> **Limitation:** no record of actual wash loads exists. Every "cycle" in Phase 2 is an *electrical activity
> pattern*, not a confirmed load (see RESULTS.md §2).

> **Limitation:** occupancy, construction year and dwelling type come from the readme as single values. Changes
> during the study (for example people moving in or out) are unknown.
