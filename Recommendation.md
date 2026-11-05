# Recommendation: Washing-Machine Insight from Smart-Meter Data

*For utility decision-makers. Based on five UK households (REFIT data, 2013–2015). Supporting analysis:
[phase4/RESULTS.md](phase4/RESULTS.md).*

## The opportunity: move washing out of the evening peak

A washing machine is a flexible ~2 kW load: its water heater runs for 10–20 minutes at the start of a wash.
Encouraging customers to run washes outside **weekdays 16:00–19:00** (the window used in this study) can lower
evening demand.

- **This is peak reduction, not energy saving.** A moved wash still uses the same kWh.
- **The effect per household is small.** On average the machine adds **22 W** to a home's 16:00–19:00 demand
  (3% of the home's 0.71 kW). 14.3% of weekday washes start in that window, only slightly more than its share of
  weekday time.
- **Homes differ a lot.** One home started 33% of its weekday washes in the window; another almost none.
- **Best value:** a targeted message to homes that really wash in the peak, or part of a wider flexible-load
  offer (dishwasher, tumble dryer, EV charging), rather than a stand-alone washing-machine campaign.
- **Secondary idea:** cooler washes. About 81% of washing-machine energy goes on heating water, so this is the
  real *energy-saving* message. No savings figure is claimed.

## How to prove it worked

- Run a **randomised trial**: intervention homes vs control homes over the same weeks, with balanced groups and
  baseline data for both.
- **Main outcome:** average whole-house kW, weekdays 16:00–19:00.
- **Secondary outcomes:** maximum 30-minute demand in that window; whole-house kWh in the window; whole-day kWh
  (to confirm load was moved, not added).
- Measure everything from **meter readings, not model estimates**.
- Analyse everyone as randomised, track drop-outs, and adjust for each home's baseline and for season.
- **Size the trial properly.** An illustrative calculation from five homes suggests thousands of households per
  arm for a washing-machine-only effect. Targeting and baseline adjustment reduce this.

## What the disaggregation model is good for, and not

| Suitable | Not suitable |
|---|---|
| Rough, after-the-fact view of washing patterns in homes it was trained on | Picking out individual peak-time washes: it matched about half of the real ones, and only 19% of its predicted peak-time washes matched a real wash |
| Screening ideas before a pilot, confirmed with customers or meter data | Measuring a programme's impact, or bill-grade washing-machine kWh |
| Finding most wash cycles in 1-minute data (78% matched one-to-one) | New homes without local checks (cycle F1 0.40 on unseen homes; energy errors up to ±50%) |

The model confuses washing with other ~2 kW heaters (dishwashers, kettles, tumble dryers) and misses the
low-power part of each wash.

## Key limits

- **Missing and frozen data:** logger outages affected all homes; frozen loggers showed false "0 W" readings.
- **Small sample:** five homes in one English region. Results describe them, not a population.
- **Overlapping signatures and household differences:** see the model section above and "Homes differ a lot".
- **Clock labels:** the official data use UK local time instead of UTC in parts of 2014–15. This was found and
  corrected before analysis; it raised the peak-window share of washes from 12.8% to 14.3%.

## If only 15- or 30-minute smart-meter data were available

Averaging over 15 or 30 minutes spreads the 10–20-minute heater spike across the interval. Its median visibility
above normal background swings drops from 7.4 at 1 minute to 1.9 at 30 minutes, so individual washes, their start
times and their energy can no longer be identified reliably, and confusion with other loads grows. Peak-reduction
*outcomes* (average kW in the window) can still be measured at 30 minutes; *targeting* homes from disaggregation
cannot. *(An illustration of information loss, not a tested 15- or 30-minute model.)*

## UK to India: do not transfer directly

**What the UK data show:** these machines heat their own water, and that heater spike is what the model relies on.

**Untested assumptions about India:** many machines may be semi-automatic or top-loading, or wash cold, which
would remove that spike. Water heaters, air conditioning, pumps, power cuts and inverters may also mask it.

**Needed first:** sub-metered data from Indian homes to retrain the model and validate it on unseen Indian
households.
