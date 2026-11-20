# Current project decisions

## November 20, 2026: develop against a shared minimal example

- Paid acquisition is paused; `downloads_enabled` is false in `configs/databento.json`.
- No Databento market-data request has been downloaded. Earlier calls queried free metadata and quotations only.
- The selected development input is the hand-built synthetic dataset in `examples/minimal/`, with the version 1.0 contract in `docs/data_dictionary.md`.
- All research components should use that contract. The fixture validates software behavior and interfaces, not empirical market conclusions or statistical power.
- The original six-instrument, 60-day, 40/10/10 design remains the proposal's candidate design. Actual dates, session count, instruments, and chronological split will be decided later according to scope, data quality, and budget.
- The prior date list is retained as `data/metadata/candidate_splits_60d.csv` for reproducibility of the quotation. It is not an acquisition commitment or a hardcoded model setting.
- No fixed assignments by team member. Anyone can pick up a task whose prerequisites are ready and share progress for handoff.

## Contract choices

- Model clock: exchange event time for this offline reference, with source ordering and atomic event boundaries preserved. Capture time remains available for audit.
- Model streams: displayed additions, executions, and cancellations on both resting sides, in a fixed six-stream order, at all displayed levels.
- Units: integer Unix nanoseconds, integer prices in `1e-9`, integer order identifiers/quantities, and relative seconds for intensities.
- Features and future labels are separate. No-move/invalid targets remain censored and nullable.
- Real samples use a configurable `trading_date,split` manifest. Models must not assume a specific number of sessions.
