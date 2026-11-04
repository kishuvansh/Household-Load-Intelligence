# FlowEnergy — Agent Rules

These rules govern how the agent handles data, analysis, and notebooks in this project.
They apply to every task unless explicitly overridden by the user for a specific step.

---

## 1 · Data Integrity

- **Never modify the original raw dataset.**
  Always create a separate processed version (e.g. `data/processed/` or a clearly named copy)
  before applying any transformations, filtering, or cleaning.

## 2 · Cleaning Transparency

- **Every cleaning decision must have a documented reason.**
  Add an inline comment or a Markdown cell explaining *why* each transformation is applied
  (e.g. "Dropping rows where flow_rate < 0 — physically impossible, likely sensor error").

## 3 · Visualisation Standards

- **Every graph must include:**
  - A descriptive **title**
  - Labelled **axes** with **units** (e.g. `Flow Rate (L/min)`, `Time (s)`)
  - A **legend** when more than one series is plotted

## 4 · Reproducibility of Results

- **Every important numerical result must be reproducible through code.**
  Do not hard-code or copy-paste computed values into prose; derive them programmatically
  so re-running the notebook regenerates the same numbers.

## 5 · Notebook Execution Order

- **Keep notebook cells in strict top-to-bottom execution order.**
  A fresh `Restart & Run All` must succeed without errors, so anyone can reproduce
  results without manual intervention.

## 6 · Saving Outputs

- **Save important outputs explicitly** (figures, summary tables, model artefacts)
  to disk (e.g. `outputs/figures/`, `outputs/tables/`) rather than relying solely
  on notebook display, which is lost when the kernel restarts.

## 7 · Assumptions & Limitations

- **Record assumptions and limitations as they are discovered, not at the very end.**
  Add a Markdown cell at the point in the notebook where the assumption first arises,
  labelled clearly (e.g. `> **Assumption:** ...` or `> **Limitation:** ...`).

