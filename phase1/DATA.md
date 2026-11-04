# Phase 1 Data Notes: REFIT House 1 (raw files)

What the raw files contain, how each column is interpreted, and every cleaning decision with its reason. Results,
validation status and limitations are in [RESULTS.md](RESULTS.md). Table numbers refer to `outputs/tables/phase1/`.

## 1. Inputs

| File | Rows | First / last `Time` (UK clock) | First / last canonical UTC |
|---|---|---|---|
| `Data/REFIT_RAW_081116/RAW_House1_Part1.csv` | 4,377,877 | 2013-10-09 14:06:17 / 2014-10-01 01:27:07 | 2013-10-09 13:06:17 / 2014-10-01 00:27:07 |
| `Data/REFIT_RAW_081116/RAW_House1_Part2.csv` | 4,140,886 | 2014-06-27 21:57:50 / 2015-07-10 11:56:32 | 2014-06-27 20:57:49 / 2015-07-10 10:56:32 |

Source: table 01. The raw files are read only and never modified. House 1 is a 4-bed detached house with
2 occupants (REFIT readme, `docs/CLEAN_READ_ME_081116.txt`).

**Channels** (labels from the REFIT readme for House 1):

| Column | Label | Column | Label |
|---|---|---|---|
| `Aggregate` | whole-house mains | `Appliance5` | Washing Machine |
| `Appliance1` | Fridge | `Appliance6` | Dishwasher |
| `Appliance2` | Freezer (1) | `Appliance7` | Computer |
| `Appliance3` | Freezer (2) | `Appliance8` | Television Site |
| `Appliance4` | Washer Dryer | `Appliance9` | Electric Heater |

All values are active power in watts.

**How the two files record data** (REFIT readme, checked in table 11):

- **Part 1** writes every channel on every row. A sensor that was not available is written as **0 W**.
- **Part 2** writes `NaN` for a channel that did not report in that packet. Part 2 has 5,284,083 `NaN` cells
  (table 02), but each channel still reports about every 7 s (median).

## 2. Columns `Time` and `Unix`

- `Time` is the **UK wall clock** (Europe/London): BST (UTC+1) in summer, GMT (UTC+0) in winter.
- `Unix` equals `Time` read as if it were UTC: `Unix − naive(Time)` is 0 or −1 s on every row of both files.
- `Unix` is therefore **not** UTC during BST.

Evidence and the official-file comparison are in RESULTS.md section 1 and tables 07–09 and 15.

## 3. Cleaning decisions, in pipeline order

Code: [run_cleaning.py](run_cleaning.py).

| # | Step | Decision | Reason | Source |
|---|---|---|---|---|
| 1 | Canonical UTC | `utc = Unix − 3600` in BST, `Unix − 0` in GMT, using Europe/London rules | `Time` is the UK wall clock | Derived from the data |
| 1a | Repeated hour, 27 Oct 2013 (Part 1) | rows before the backward clock step are BST, rows after are GMT | the file shows the clock going back once, in order | Derived from the data |
| 1b | Repeated hour, 26 Oct 2014 (Part 2) | 986 rows removed | both copies of the hour are interleaved and cannot be told apart | Project choice (no invented correction) |
| 1c | Non-existent local times (spring) | none found; the pipeline would stop if any appeared | — | Check |
| 2 | Physical limits | appliance > 4,000 W, aggregate > 25,000 W or < 0 W → missing | above an IAM plug's rating / above a 100 A single-phase supply | 4 kW matches REFIT; **setting to missing is a project choice** (REFIT sets IAM spikes to 0 W) |
| 3 | Handoff | Part 1 for UTC ≤ 2014-09-27 13:51:12, Part 2 after (= 14:51:12 UK clock) | keeps the original pipeline's instant; both files carry the same signals there (table 12) | Project choice |
| 4 | Same-second rows | per channel, keep the last non-missing reading in each UTC second | keeps complementary Part 2 packets; no reading is lost (table 11) | Project choice |
| 5 | Hold limit | each reading holds until the channel's next reading, for at most 60 s (Aggregate, Appliance4, 5, 6) or 120 s (Appliance1, 2, 3, 7, 8, 9) | bridges normal reporting gaps (about 7 s median) without inventing load during logger silences | **Project choice** (REFIT forward-fills with no limit) |
| 6 | 1-minute resampling | exact time-weighted mean: held intervals split at minute boundaries; minute kept only if at least 30 s covered | conserves the energy of the held signal exactly | Project choice |
| 7 | Outages | never filled; left missing | missing ≠ 0 W | Project choice |

## 4. Output

`Data/processed/phase1/house1_cleaned_1min.parquet` and `.csv.gz`:

- one row per UTC minute, 920,033 rows, from 2013-10-09 13:06 to 2015-07-10 10:58;
- columns `Time` (UTC, `YYYY-MM-DD HH:MM:SS`), `Unix` (UTC seconds) and the 10 channels in W;
- `NaN` means no valid data in that minute.

| Table | Content |
|---|---|
| 01 | Raw file inventory |
| 02 | Anomaly counts |
| 03 | Physical-limit flags |
| 04 | Availability per channel |
| 05 | Arithmetic vs time-weighted means |
| 06 | Energy validation |
| 07 | Clock changes |
| 08 | Alignment against the official file and Part 1 vs Part 2 |
| 09 | Daily Part 1 vs Part 2 clock offset |
| 10 | Breakdown of the old energy result |
| 11 | Cleaning-decision checks |
| 12 | Handoff agreement |
| 13 | Aggregate outages over 1 h |
| 14 | Assertions |
| 15 | Clock-change signatures in the official files |

Phases 2–4 do not use this output; they use the official REFIT cleaned files.

## 5. Assumptions and limitations

See RESULTS.md section 6. The most important are:

- the logger clocks drift apart by up to 7 minutes, and no correction is possible;
- two UTC hours on 26 Oct 2014 are removed;
- a Part 1 zero may mean "sensor unavailable";
- implausible readings below 4 kW are kept;
- the hold and coverage limits are this project's own conservative choices.
