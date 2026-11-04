# Phase 1 Results: House 1 Raw Inspection and Cleaning

**Status: PROVISIONAL.** The timestamp conversion, handoff, cleaning steps and energy validation are resolved and
checked by an executed pipeline with 37 passing assertions. Two timing issues cannot be resolved with the
available data, so they are documented rather than corrected:

- the two logger clocks drift apart by up to 7 minutes;
- one repeated clock-change hour in Part 2 cannot be recovered.

Phases 2–4 do not use these outputs. They use the official REFIT cleaned files.

Data notes and the list of cleaning decisions: [DATA.md](DATA.md). Step-by-step evidence:
[01_raw_data_inspection_and_cleaning.ipynb](01_raw_data_inspection_and_cleaning.ipynb). All tables are in
`outputs/tables/phase1/`.

## Decision summary

| Check | Result | Evidence |
|---|---|---|
| Timestamp correction (UK clock → UTC) | **PASS**, with one unresolved sub-issue (logger clock drift) | tables 07, 08, 09, 14 |
| Energy validation | **PASS** (energy of the observed signal is conserved) | tables 06, 10, 14 |
| Data-cleaning checks | **PASS** (checks run; custom choices documented) | tables 02, 03, 11, 12, 13 |
| Pipeline execution | **PASS** (script and notebook give identical outputs) | table 14 |

## 1. Timestamps

**Finding.** In both raw files `Time` is the UK wall clock (Europe/London). `Unix` is that same clock value read as if
it were UTC: `Unix − naive(Time)` is 0 or −1 s on every row. Neither column is UTC during British Summer Time (BST).

Evidence:

1. **The clock changes appear at the exact UK clock-change instants** (table 07):
   - 27 Oct 2013 (Part 1): the clock goes back from 01:59:59 to 01:00:01.
   - 30 Mar 2014 (Part 1) and 29 Mar 2015 (Part 2): the clock jumps forward from about 01:00 to 02:00.
   - 26 Oct 2014 (Part 2): the 01:00–01:59 hour holds 986 rows, about twice the 534–535 rows in the hours either
     side. Both passes through the repeated hour are merged into one sorted block.
2. **The two files share one clock.** Across the summer-2014 overlap, Part 1 and Part 2 line up best at lag 0 or
   within 2 minutes of it, and not at ±60 minutes (table 08).
3. **The official REFIT file applies the same conversion to Part 1.** On our canonical UTC axis, the official
   `CLEAN_House1.csv` lines up with raw Part 1 at lag 0 in every month, BST and GMT, with correlation ≥ 0.999
   (table 08).

**Treatment.** Canonical UTC = `Unix` − 3600 s in BST and `Unix` − 0 s in GMT.

- The repeated hour in 2013 is assigned by file order: rows before the backward step are BST, rows after are GMT.
- The repeated hour on 26 Oct 2014 is interleaved and cannot be split, so its 986 rows are removed. In UTC this
  leaves a 2-hour gap (00:00–02:00 UTC; see table 13).
- After conversion, the spring "1-hour gaps" turn out to be clock jumps, not outages. The largest real step is 13 s
  in 2014 and 18 s in 2015.

**The 29 Mar 2015 gap of about 3,607 s.** This is a clock change, not a data outage. The UK clock jumped from 00:59:54
to 02:00:01 while readings continued.

**Earlier explanations that were wrong:**

- **"Subtract 3,600 s from Part 1 rows 0–249,600 only"** (the original pipeline). This left the Part 1 BST period from
  30 Mar to 27 Sep 2014, and Part 2's BST periods, one hour late.
- **"Subtract 3,600 s from every Part 1 row"** (a later audit). This would put the Part 1 winter months one hour early.
  The official file does not do this either; the audit's matching of single timestamps was misleading.

**Handoff.** The original handoff instant, 2014-09-27 14:51:12 on the UK clock, is kept. In canonical UTC it is
13:51:12 UTC; in the old pipeline both files happened to use the same shifted clock there, so the instant itself
was consistent. The original *justification* was not supported. It claimed that Part 1's appliance channels failed
after this point while Part 2 kept recording. In fact:

- in the 7 days before the handoff, Part 1 and Part 2 agree within 5 W on 85.7–100% of minutes, depending on the
  channel;
- after the handoff they agree on 90.5–100%, with the same flat channels and spikes in both (table 12).

The handoff is therefore a project choice, not a fix for a failure.

**Unresolved: logger clock drift.** Daily cross-correlation over the overlap covers 87 days (table 09):

- On 46 days the two clocks agree.
- On the other 41 days, Part 1 runs behind Part 2 by 1 to 7 minutes. The largest gap is −7 min, on 2014-08-07.
- The offset builds up gradually and then snaps back to 0, which looks like periodic re-synchronisation.

There is no independent reference clock, so it is not possible to say which logger is right, and no drift correction
is applied. Part 1 timestamps may therefore be off by several minutes, and before Part 2 started (Oct 2013 to
Jun 2014) this cannot be measured at all.

## 2. Official REFIT cleaned files: timestamp observation

In Part 2's BST months, the official `CLEAN_House1.csv` sits exactly +60 min later than canonical UTC (table 08). It
matches the stored local clock instead. So the official file appears to keep UK local time in those periods while
labelling it UTC.

Table 15 checks the official files for the houses used in Phases 2–4 (2, 5, 7, 8, 9, 15) as well as House 1:

| Date | Expected on a true UTC axis | What the official files show |
|---|---|---|
| 29 Mar 2015 | normal hours | the 01:00 UTC hour is empty in all 7 files |
| 26 Oct 2014 | normal hours | the 01:00 UTC hour is doubled in 6 of the 7 files |
| 30 Mar 2014 | normal hours | the 00:00 UTC hour is empty in all 7 files |

These are signs of local-clock time stamps, or of an imperfect conversion, around the clock changes.

Phases 2–4 treat the official `Unix` column as UTC. **If these periods are on local time, hour-of-day results in
BST periods could be one hour off**, for example Phase 4's 16:00–19:00 window. Phases 2–4 were not changed, as
instructed. This is flagged for a decision.

## 3. Energy validation

**Method.**

- Each raw reading is held until the channel's next reading, for at most that channel's hold limit.
- The raw reference is the exact integral of that held signal over a half-open UTC window [start, end).
- The cleaned value is the sum of 1-minute means × 60 s over the minutes that were kept.
- Resampling splits every held interval exactly at minute boundaries.

**Results** (table 06):

| Window (UTC) | Channel | Raw held (kWh) | Cleaned (kWh) | Difference |
|---|---|---|---|---|
| 15–17 Nov 2013, 48 h | Aggregate | 37.3554 | 37.3542 | −0.003% |
| 23 Oct 2013, 05:45–08:00 | Washing machine | 0.5356 | 0.5356 | 0.000% |
| 12 Oct 2013, 00:00–12:00 | Fridge (Appliance1) | 0.1480 | 0.1476 | −0.301% |
| Full record, every channel | all 10 | — | — | at most 0.084% |

- **Like-for-like check.** When each minute's mean is multiplied by its covered seconds instead of 60 s, the cleaned
  energy plus the energy in minutes dropped for low coverage equals the raw held energy exactly. The residual is
  0 W·s in every window, and the full-record check passes for every channel (table 14).
- **Coverage rule.** The small remaining differences come from the 30-second coverage rule: partly covered minutes
  are kept and scaled to 60 s, and minutes with under 30 s of coverage are dropped.

**Why the original washing-machine check showed +8.99%** (table 10, which changes one thing at a time):

| Step | Change | Difference |
|---|---|---|
| A | original calculation | +8.99% |
| B | raw window moved to the same UTC interval as the cleaned window | +0.44% |
| C | each raw reading held forward instead of weighted by the time since the previous row | −0.03% |
| E | revised method | 0.00% |

The original raw window started at 04:45 UTC, an hour before the cleaned window, so it cut off the end of the wash
cycle. About 8.55 percentage points came from that misaligned window and about 0.47 from the backward weighting.
The 60-second hold limit has no effect in this window: removing it changes nothing (step F). **The +8.99% was not
caused by forward filling.**

**Scope of the claim.** The resampling conserves the energy of the *observed* signal. It does not recover energy
used while the logger was silent beyond the hold limit; that time stays missing. Compared with holding every reading
until the next one, the hold limit gives 0.8% less energy for the aggregate and 8.4% less for the fridge in the
example windows. Over the full record it gives 20.1% less for the aggregate, almost all of it because unlimited
holding would fill the long outages (`Hold_Limit_Effect_Pct`, table 06).

## 4. Data-cleaning checks

| Decision | Source | Result |
|---|---|---|
| Rows sharing a UTC second merged per channel (last non-missing reading) | Project choice | every (second, channel) reading kept: Part 1 33,338,258, Part 2 28,960,673 (table 11) |
| Hold limit 60 s (aggregate, washer-dryer, washing machine, dishwasher) / 120 s (others) | **Project choice** (REFIT forward-fills with no limit) | table 11 lists the short gaps the limit leaves partly unfilled |
| Appliance > 4,000 W → missing | Limit matches REFIT; **treatment is a project choice** (REFIT sets these to 0 W) | 3,949 appliance readings flagged (table 03); none remain |
| Aggregate > 25,000 W → missing; negatives → missing | Project choice | 3 aggregate readings flagged; 0 negative readings |
| Long outages kept missing | Project choice | 26 Aggregate gaps over 1 h, 12 of them at least 24 h; the longest is 998.45 h (41.6 days), 2014-01-24 to 2014-03-06 (table 13); no values inside outages of 24 h or more |
| Part 2 per-channel NaN means "not reported in this packet" | REFIT readme | the median gap between a channel's readings is 7 s for every channel (table 11) |
| Minute kept only with at least 30 s coverage | Project choice | 717 partly covered aggregate minutes dropped (table 11) |
| Unresolvable repeated hour (26 Oct 2014) removed | Project choice | 986 rows (tables 02, 07) |

## 5. Output

The cleaned output is in `Data/processed/phase1/house1_cleaned_1min.parquet` (and `.csv.gz`):

- 920,033 one-minute rows, from 2013-10-09 13:06 to 2015-07-10 10:58 UTC;
- columns `Time` (UTC), `Unix`, `Aggregate` and `Appliance1`–`Appliance9`;
- Aggregate availability 86.74% (table 04).

Figures are in `outputs/figures/phase1/`: three raw-versus-cleaned windows (01–03), sampling intervals (04) and
daily availability (05).

## 6. Remaining limitations

1. **Logger clock drift** of up to 7 min in summer 2014, and unknown absolute clock accuracy before June 2014
   (section 1). This is the reason Phase 1 stays provisional.
2. **Two hours removed** on 26 Oct 2014 (00:00–02:00 UTC). The repeated hour cannot be split.
3. **Part 1 zeros are ambiguous.** Part 1 recorded an unavailable sensor as 0 W (REFIT readme), so `Zero_Pct` in
   table 04 counts zero readings and does not distinguish "off" from "no data".
4. **Implausible readings below 4 kW are not removed.** For example, the maximum fridge (Appliance1) minute is
   3,072 W (table 04).
5. **The hold and coverage limits are this project's conservative choices.** They are not the official REFIT
   method, so energy during short unlogged gaps is left missing rather than estimated.
6. **The official REFIT files appear to use local time in BST periods** (section 2). This could affect Phases 2–4,
   which were not changed.

## Reproduce

```bash
python3 phase1/run_cleaning.py      # about 1-2 minutes; also runs final_tz_audit.py (tables 08, 09, 15)
jupyter nbconvert --to notebook --execute phase1/01_raw_data_inspection_and_cleaning.ipynb
```

Nothing is written unless all 37 assertions pass (table 14). Use `--out DIR` to write a separate copy for review.
