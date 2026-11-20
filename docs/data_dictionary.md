# MBO data contract, version 1.0

All model development should use this contract and the [minimal reference dataset](../examples/minimal/README.md). The example is hand-built synthetic data. Its purpose is to fix units, event definitions, interfaces, and expected answers before obtaining real market data.

The contract is independent of the number of instruments, trading days, and forecast rows. The earlier 60-day calendar is an optional candidate. Choose a real chronological sample later, then keep that sample fixed within each experiment.

## 1. Three data layers

| Layer | Representation | Purpose |
| --- | --- | --- |
| Provider records | Local `.dbn.zst`, or the example's integer-encoded `mbo.csv` | Preserve normalized MBO messages in source order |
| Reconstructed observations | `events`, `quotes`, `queues`, `exposure` | Express economic events and complete book states |
| Model interface | `features`, `forecasts`, six event streams | Train arrival models and compare next-move probabilities |

CSV is used for the tiny fixture so it can be inspected and versioned. The optional Parquet output carries explicit types and nulls. `schema.json`, written by the example runner, is the machine-readable catalogue; its source is [`schema.py`](../src/saf/data/schema.py).

## 2. Provider record fields

The integer CSV uses the fields below, matching the values exposed by the DBN record adapter. Provider CSV exports can use formatted timestamps/prices; this project's integer CSV intentionally keeps their binary units. Read a local DBN directly when the export conventions differ. See the [official MBO schema](https://databento.com/docs/schemas/mbo).

| Field | Type | Meaning |
| --- | --- | --- |
| `ts_recv` | uint64 | Capture timestamp, Unix nanoseconds |
| `ts_event` | uint64 | Exchange timestamp, Unix nanoseconds |
| `rtype` | uint8 | 160 for MBO |
| `publisher_id` | uint16 | Provider venue/feed identifier |
| `instrument_id` | uint32 | Provider instrument identifier |
| `action` | string | One action character |
| `side` | string | One side character |
| `price` | int64 | Fixed-point price integer |
| `size` | uint32 | Quantity for this record |
| `channel_id` | uint8 | Source channel |
| `order_id` | uint64 | Venue order identifier |
| `flags` | uint8 | Bit field |
| `ts_in_delta` | int32 | Timestamp delta |
| `sequence` | uint32 | Source sequence number |

Price units are `1e-9`: `100000000000` represents 100.00. `9223372036854775807` is `UNDEF_PRICE`, not an economic price. The example tick is `10000000`, or 0.01. `B` denotes a bid/resting buy, and `A` an ask/resting sell, except a trade's side describes its aggressor. `N` means unspecified. `F_LAST` is bit 128; test `flags & 128`, not equality. These are [provider conventions](https://databento.com/docs/conventions/common-fields-enums-types).

Keep timestamps, prices, and identifiers as integers. Do not route nanoseconds or uint64 order IDs through floating-point or spreadsheet auto-number conversion. Only relative durations, intensities, and derived ratios use floating-point.

## 3. Reconstruction and event classification

| Action | Book operation | Model treatment |
| --- | --- | --- |
| `R` | Clear resting orders | Initialization/recovery boundary |
| `A` | Insert an order | Limit addition if eligible |
| `C` | Remove `size` shares; delete at zero | Execution-related reduction or true cancellation |
| `T` | No resting-book mutation | Audit print; never another queue reduction |
| `F` | No resting-book mutation | Link execution to its `C` |
| `N` | No mutation | May carry an event-end flag |
| `M` | Generic normalized modification | Reference adapter rejects it for explicit review |

Only expose book state after `F_LAST`. A normalized fill does not mutate the book independently of its cancellation. See [Databento's order-state example](https://databento.com/docs/examples/order-book/order-tracking).

For `XNAS.ITCH`, a displayed execution normally becomes `T`, `F`, then `C`, with the same sequence. Trade order IDs are zero; the fill identifies the resting order. A non-printable execution may omit `T`. A replace becomes `C` for the old ID and `A` for the new ID. A non-displayed/cross print may be a standalone `T` with side `N`. These details come from the [Nasdaq dataset supplement](https://databento.com/docs/datasets/xnas-itch).

Our reference adapter links `F` to `C` within the same completed source group using sequence, order ID, and resting side; publisher and instrument have already been partitioned. It verifies matching price/quantity. The cancellation updates the book once and contributes one `execution_*` event, not a second `cancel_*` event. Unmatched fills, unknown orders, or ambiguous matches raise an error.

Model event scope in version 1.0 is **all eligible displayed price levels**, not just the best level. Price/depth are marks. Count arrivals, not shares; `size` remains available for marked models and simulation. The synthetic initialization is outside the observation window. Snapshot/bootstrap records, resets, and standalone unspecified-side trades are not counted as model arrivals.

## 4. Session metadata and timing

`metadata.json` supplies `contract_version`, `synthetic`, `session_id`, symbol, publisher/instrument IDs, trading date, time zone, price scale, tick size, observation bounds, and forecast grid. Small CSV tables inherit these partition dimensions from this envelope. A larger Parquet store should materialize or partition by the same dimensions.

The reference experiment uses exchange-event time, retaining capture time for audit. Apply all messages in source order; require nondecreasing completed-event timestamps in this reference. It is an offline exchange-clock forecast experiment, not a latency backtest. A different clock requires an explicit contract/experiment decision.

Observation intervals are half-open: `[analysis_start_ns, analysis_end_ns)`. `t_rel_s` is seconds since the observation start, computed by integer subtraction before division. Warm-up messages initialize the book but do not contribute to the example's observation counts or likelihood exposure. The example's final `N` is a synthetic boundary marker proving coverage through the end. Real ingestion should establish coverage from validated source/receipt metadata as well as messages.

Do not label or estimate exposure past a truncated input. A reset changes `epoch`; labels cannot cross a reset or invalid book interval. A valid book has both sides and a strictly positive spread. Market halt/status handling and gap recovery still need a production policy.

## 5. Model-facing tables

All paths below refer to the runner's output directory. Refer to `schema.json` for every column's exact type and nullability.

### `events.csv` / `events.parquet`

One row per eligible canonical arrival. Important columns:

| Columns | Meaning |
| --- | --- |
| `event_id` | Stable event identifier within the session |
| `state_before_id`, `state_after_id` | Complete states around the atomic source event |
| `ts_event_ns`, `t_rel_s` | Absolute integer time and relative model time |
| `event_type`, `event_type_id` | Canonical stream name and fixed index |
| `side`, `price_int`, `size`, `order_id` | Resting side and event marks |
| `placement_distance_ticks` | Signed placement distance; null for non-additions |
| `source_record_index` | Raw evidence row; `F` for an execution |
| `book_update_record_index`, `source_sequence` | Mutating raw row and source linkage |

Zero-based stream order is fixed:

```text
0 limit_bid       1 limit_ask
2 execution_bid   3 execution_ask
4 cancel_bid      5 cancel_ask
```

An execution's side is the depleted resting queue. Thus an aggressing sell (`T`, side `A`) fills the bid and produces `execution_bid`.

For additions, measure distance against the pre-atomic-event same-side best quote:

```text
bid distance = (best_bid_before - order_price) / tick_size
ask distance = (order_price - best_ask_before) / tick_size
```

Zero is at the best, positive is deeper, negative improves the quote. Tail fitting uses positive distances and reports the excluded mass at zero/negative values. The minimum fixture has only two positive distances, so it cannot identify a tail law.

### `quotes.csv` / `quotes.parquet`

One row per completed source group, including initialization/boundary states. Columns identify the state, exchange/capture timestamps, recovery `epoch`, best prices, best sizes, and validity.

`mid_price_x2 = bid_price_int + ask_price_int` stores twice the mid-price in raw price units. Compare these integers when labeling. For display, `mid_price = mid_price_x2 / (2 * price_scale)`. Missing or invalid mid-prices remain null; never replace them with zero.

### `queues.csv` / `queues.parquet`

Rows contain `state_id`, resting `side`, `level` (1 is best), `price_int`, displayed `size`, and `order_count`. Join to `quotes` by state. Order IDs remain in raw/event records; level aggregates alone are not a full L3 snapshot for an order-level simulator.

### `exposure.csv` / `exposure.parquet`

Rows contain `state_id`, `start_ns`, `end_ns`, `dt_s`, `bid_size`, and `ask_size`. Each row is a valid book-state holding interval clipped to the observation window. The event's `state_before_id` identifies the state in which its arrival occurred.

Poisson rates use counts divided by valid time. Queue-reactive rates use counts conditioned on a state divided by time spent in that state. Counting rows in a state is not a substitute for elapsed exposure.

### `features.csv` / `features.parquet`

One row per forecast ID: as-of state, forecast time, relative time, best prices/sizes, exact mid-price representation, spread in ticks, imbalance, and `feature_valid`. Queue imbalance is `(bid_size - ask_size) / (bid_size + ask_size)`. Select the latest complete state at or before the forecast cutoff.

This table contains no target or future-move time. Future-data perturbation is tested to leave earlier features unchanged. Add later features, including past-only event rates or regimes, with explicit definitions and a contract version change when semantics change.

### `forecasts.csv` / `forecasts.parquet`

Columns are `forecast_id`, `y_up`, `next_move_ts_ns`, `censored`, and `censor_reason`. The first future complete-book mid-price change before the observation end determines `y_up`: upward is 1, downward is 0. No observed move or an invalid interval censors the row. Its target stays null.

All models predict on the same forecast IDs; scoring uses the common valid, uncensored rows. Preserve censored rows for coverage reporting. Do not encode them as downward moves or choose different evaluation rows for different models.

## 6. Inputs and outputs for each research component

| Component | Consume |
| --- | --- |
| Placement tails | Addition rows and positive `placement_distance_ticks` |
| Poisson arrivals | Six stream counts/times and valid exposure |
| Queue-reactive arrivals | Event `state_before_id`, state features, and holding-time exposure |
| Hawkes arrivals | Six ordered arrays of relative event times and observation end |
| Queue-imbalance logistic model | Past-only `features` joined to uncensored `forecasts` by ID |
| Shared simulator | Current book/queues, training-estimated marks, and fitted arrival generator |
| Evaluation | Forecast IDs/labels and model probabilities |

`model_inputs.json` shows the exact arrays and illustrative constant Poisson rates for the fixture. No Hawkes, tail, or predictive research result is claimed. A cancel/add replacement can create tied canonical times; choose and document a compound-event or discretization policy before fitting a simple point-process likelihood to real ties. Do not silently add arbitrary timestamp jitter.

Every predictive model should emit `forecast_id`, `model`, `run_id`, and finite `p_up` in `[0, 1]`. Labels are separate. Training, validation, and test partitions are assigned by a chosen real-session manifest, not embedded as 40/10/10 assumptions in a model.

## 7. Scope of the reference implementation

The small implementation is deliberately readable and validated against known answers. It handles one initialized instrument/session, source ordering, atomic groups, displayed additions/cancellations/executions, hidden prints, price changes, exposure, causal features, and censoring. It also reads local DBN and can write typed Parquet.

It is not yet the production ingestion/recovery pipeline. Multi-instrument partitioning, halt/status data, abnormal flags, generic modifications, multiple fill fragments per order/group, and nonmonotone exchange clocks need explicit extensions. Unsupported cases fail rather than inventing a state. Model families and large-sample statistical validation remain future work.

When replacing the fixture with real data, preserve this interface, establish source coverage and tick/symbol metadata, and run the same invariants. Changing the number of days changes the selected observations; it should not require rewriting model code.
