# Minimal synthetic MBO example

Start here before implementing a model. The dataset has **22 raw records, one synthetic instrument (`DEMO`), and a 12-second observation window**. It is hand-built, not downloaded Databento data. The timestamps and IDs are illustrative; they do not identify a real security. Its interfaces and expected answers are the common starting point for development, not empirical evidence.

## Run it offline

From the repository root, with Python 3.10 or newer:

```bash
python3 scripts/run_minimal_example.py --check-expected
```

This uses the standard library, makes no API calls, and needs no key. Outputs go to `artifacts/minimal_example/`. For explicitly typed Parquet, use an environment with PyArrow (also installed by `requirements-data.txt`):

```bash
python scripts/run_minimal_example.py --parquet --check-expected
```

## Files

| File | Role |
| --- | --- |
| `mbo.csv` | 22 normalized-style MBO records, with exact integer timestamps/prices |
| `metadata.json` | Synthetic provenance, instrument mapping, tick size, clock, observation window, forecast grid |
| `expected/events.csv` | 11 canonical model arrivals, with raw-record linkage |
| `expected/quotes.csv` | 18 complete book states; no transient T/F/C states |
| `expected/queues.csv` | Displayed level sizes and order counts for each state |
| `expected/exposure.csv` | Valid state holding intervals; total 12 seconds |
| `expected/features.csv` | 12 past-only feature rows |
| `expected/forecasts.csv` | 8 direction labels and 4 explicitly censored rows |
| `expected/model_inputs.json` | Six event streams, exposure, counts, and model input hints |
| `expected/schema.json` | Field types and nullability |

The full rules are in the [data contract](../../docs/data_dictionary.md). The reference implementation is in [`src/saf/data/`](../../src/saf/data/).

## Hand-check the book

Five initialization records establish bids of 10 shares at 100.00 and 20 at 99.99, plus asks of 12 at 100.02 and 18 at 100.03. They precede the observation window and do not count as observed model arrivals.

Times below are seconds after the observation start, 09:45:00 New York time on the illustrative date. Tick size is 0.01.

| Time | Event | Best bid: price / size | Best ask: price / size | Mid-price |
| --- | --- | --- | --- | --- |
| 0.0 | Initialized state | 100.00 / 10 | 100.02 / 12 | 100.010 |
| 0.1 | Add 5 bid shares at 100.00 | 100.00 / 15 | 100.02 / 12 | 100.010 |
| 1.0 | Add 8 ask shares at 100.02 | 100.00 / 15 | 100.02 / 20 | 100.010 |
| 2.0 | Cancel 4 bid shares | 100.00 / 11 | 100.02 / 20 | 100.010 |
| 3.0 | Cancel 2 ask shares | 100.00 / 11 | 100.02 / 18 | 100.010 |
| 4.0 | Execute 6 bid shares (`T/F/C`) | 100.00 / 5 | 100.02 / 18 | 100.010 |
| 5.0 | Execute 10 ask shares (`T/F/C`) | 100.00 / 5 | 100.02 / 8 | 100.010 |
| 6.0 | Cancel remaining 8 best-ask shares | 100.00 / 5 | 100.03 / 18 | 100.015 |
| 7.0 | Cancel remaining 5 best-bid shares | 99.99 / 20 | 100.03 / 18 | 100.010 |
| 8.0 | Add 7 bid shares, improving to 100.00 | 100.00 / 7 | 100.03 / 18 | 100.015 |
| 9.0 | Add 9 ask shares deeper, at 100.04 | 100.00 / 7 | 100.03 / 18 | 100.015 |
| 10.0 | Add 4 bid shares deeper, at 99.98 | 100.00 / 7 | 100.03 / 18 | 100.015 |
| 11.0 | Unspecified-side trade print; no displayed update | 100.00 / 7 | 100.03 / 18 | 100.015 |
| 12.0 | Synthetic end marker; outside `[0, 12)` | 100.00 / 7 | 100.03 / 18 | 100.015 |

In each displayed execution, `T` reports the aggressor, `F` identifies the resting order, and `C` removes its shares. Removing volume on all three would corrupt the book. The standalone print at second 11 does not become a displayed execution.

The five addition distances are `[0, 0, -1, 1, 2]` ticks. Their positive-distance tail input is `[1, 2]`.

## Hand-check the model inputs

```text
stream order: [limit_bid, limit_ask, execution_bid, execution_ask, cancel_bid, cancel_ask]
event counts: [3, 2, 1, 1, 2, 2]
valid observation time: 12 seconds

limit_bid times:     [0.1, 8, 10]
limit_ask times:     [1, 9]
execution_bid times: [4]
execution_ask times: [5]
cancel_bid times:    [2, 7]
cancel_ask times:    [3, 6]
```

For example, the toy constant Poisson estimate for bid additions is `3 / 12 = 0.25` arrivals per second. Queue-reactive estimation uses each state's holding time from `exposure.csv`, not the number of state rows. Hawkes code receives all six arrays and the observation end, including the silent interval after the final modeled arrival.

Forecast times are 0.5, 1.5, ..., 11.5 seconds. At 0.5 seconds, bid/ask sizes are 15/12, so imbalance is `3/27 = 1/9`. The next mid-price move is upward at second 6. The label vector is:

```text
[1, 1, 1, 1, 1, 1, 0, 1, null, null, null, null]
```

The final four forecasts have no later move before the observation ends. Keep them censored; do not turn them into zeros.

## Extend a model against this example

Implement and test the model against the documented input tables. Emit probabilities keyed by the same forecast IDs. Verify feature causality, event counting, exposure, simulation state changes, and censoring using the known answers. Choose additional synthetic scenarios when a model needs more cases, such as long sequences or regime transitions, and label them as synthetic.

This fixture is too small for reliable six-dimensional Hawkes fitting, placement-tail estimation, calibration analysis, or held-out performance claims. First complete the interfaces and mathematical/unit checks; later choose an appropriately sized real sample and preserve the same contract. There is no requirement to use 60 days.
