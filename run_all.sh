#!/usr/bin/env bash
# Runs the whole project end to end from the REFIT files in Data/ (see README "Setup").
#   ./run_all.sh               run the four phase scripts (writes outputs/ and Data/processed/)
#   ./run_all.sh --notebooks   also re-execute the four notebooks in place
set -euo pipefail
cd "$(dirname "$0")"

for d in Data/REFIT_RAW_081116 Data/CLEAN_REFIT_081116; do
  if [ ! -d "$d" ]; then
    echo "Missing $d - download and extract the REFIT archives first (README, Setup)." >&2
    exit 1
  fi
done

echo "== Phase 1: House 1 raw cleaning =="
python3 phase1/run_cleaning.py
echo "== Phase 2: washing-machine EDA =="
python3 phase2/wm_eda.py
echo "== Phase 3: disaggregation model =="
python3 phase3/wm_nilm.py
echo "== Phase 4: practical implications =="
python3 phase4/phase4_analysis.py
echo "== Audit: clock used by the official cleaned files (read only) =="
python3 audit/official_timestamp_check.py

if [ "${1:-}" = "--notebooks" ]; then
  for nb in phase1/01_raw_data_inspection_and_cleaning.ipynb phase2/02_washing_machine_eda.ipynb \
            phase3/03_wm_disaggregation.ipynb phase4/04_practical_implications.ipynb; do
    echo "== Executing $nb =="
    (cd "$(dirname "$nb")" && python3 -m jupyter nbconvert --to notebook --execute --inplace \
        --ExecutePreprocessor.timeout=3600 "$(basename "$nb")")
  done
fi
echo "Done."
