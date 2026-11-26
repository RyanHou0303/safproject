# Code guide: inputs, outputs, and key classes

Read this alongside the [project plan and progress](../README.md). All values below are synthetic. The final section contains one runnable example; it does not download data.

## 1. The data passed between files

```text
Local DBN file
  -> ingest.iter_mbo(): raw MBOMsg records, one at a time
  -> replay.replay_records(): completed ReplayStep objects, one at a time
       -> book.OrderBook: current resting orders and price-level totals
       -> events.classify_event(): zero, one, or two classified Event objects

ReplayStep.quote_after -> features.build_features(): one feature dictionary
List of quote_after   -> labels.build_labels(): one Label per quote
Features + labels     -> imbalance.fit_imbalance(): fitted logistic model
Model + features      -> imbalance.predict_up(): probabilities

ReplayStep.events     -> binning.bin_event_stream(): time bin -> six counts
ReplayStep objects    -> state_events.bin_events_by_state():
                          time bin -> pre-event queue state -> six counts
Time-bin counts + valid exposure seconds -> poisson.fit_poisson(): PoissonBin objects
State-conditioned counts + state exposure -> queue-reactive rates: NOT IMPLEMENTED
```

| Function | Input | Output |
| --- | --- | --- |
| `iter_mbo(path)` | A local `.dbn` or `.dbn.zst` MBO file path | An iterator of original `db.MBOMsg` records; no download or classification |
| `OrderBook.apply(record)` | One raw record for this book | A best-quote dictionary at `F_LAST` when both sides exist; otherwise `None`. The book is updated even without `F_LAST`. |
| `classify_event(records)` | A list containing one complete raw event group | A `list[Event]`; `T/F/C` becomes one execution, replacement `C/A` becomes two marked events, reset becomes an empty list |
| `replay_records(records)` | An iterable of raw records in original order | An iterator of `ReplayStep` objects; separate books per source key |
| `build_features(quote)` | One quote dictionary or `None` | One feature dictionary or `None` |
| `build_labels(quotes)` | A sequence of quotes for one instrument/source/session | A same-length `list[Label]` |
| `fit_imbalance(features, labels)` | Aligned training features and labels containing both directions | A fitted scikit-learn `LogisticRegression` |
| `predict_up(model, features)` | A fitted logistic model and feature rows | A same-length list of upward probabilities; missing feature rows stay `None` |
| `bin_event_stream(events, ...)` | Classified `Event` objects and an explicit observation window | `dict[time_bin, dict[stream, count]]` |
| `bin_events_by_state(steps, ...)` | `ReplayStep` objects and an explicit observation window | `dict[time_bin, dict[QueueState, dict[stream, count]]]` |
| `fit_poisson(counts, exposure)` | Training counts and valid observed seconds by time bin | `dict[time_bin, PoissonBin]` |

An iterator produces its next item when a caller loops over it. It is consumed once. Use a small list when learning or when reusing the same synthetic sample for several functions. Do not convert a large market-data file to a list just for convenience.

## 2. Raw `MBOMsg`: one feed record

The Python class is `databento.MBOMsg`. A record has fields such as:

```python
# Selected fields; this is a readable sketch, not a constructor call.
record.order_id       # 1001
record.action         # "C": remove shares from a resting order
record.side           # "B": bid
record.price          # 100_000_000_000: $100.00
record.size           # 5 shares removed, not the remaining order size
record.ts_recv        # 1_735_829_100_000_000_000
record.sequence       # 4
record.flags          # 128: F_LAST
```

Prices use integer units of `1e-9` dollars. Timestamps use Unix nanoseconds; the timestamp above is January 2, 2025 at 14:45 UTC / 09:45 New York. `flags` is a bit field: check `int(record.flags) & 128`, because other flags can also be set.

The source key is `(publisher_id, instrument_id, channel_id)`, for example `(2, 12345, 0)`. It separates books and streams. It is neither an order ID nor a unique event ID. `instrument_id` needs provider symbol mapping before calling it a particular ticker.

`F_LAST` closes a raw event group for an instrument. It does not mean an order has finished its lifetime. Replay preserves input order; it does not sort by sequence or timestamp.

## 3. `Order` and the three dictionaries in `OrderBook`

An `Order` stores one currently resting order's side, integer price, and remaining shares:

```python
Order(side="B", price=100_000_000_000, size=10)
```

For two bid orders at $100.00 and one ask order at $100.02, the book contains:

```python
book.orders = {
    1001: Order(side="B", price=100_000_000_000, size=10),
    1002: Order(side="B", price=100_000_000_000, size=20),
    2001: Order(side="A", price=100_020_000_000, size=50),
}

book.bids = {100_000_000_000: 30}  # 10 + 20 shares at this price
book.asks = {100_020_000_000: 50}
```

`orders` maps order ID to an `Order`. `bids` and `asks` map price to total displayed shares at that price, across all currently resting orders there. They contain all retained price levels, not just the best level, and do not store order counts or FIFO positions.

If a cancellation removes 5 shares from order 1001:

```python
book.orders[1001]  # Order(side="B", price=100_000_000_000, size=5)
book.bids         # {100_000_000_000: 25}
book.asks         # {100_020_000_000: 50}

# The returned quote at the completed event boundary:
quote = {
    "bid_price": 100_000_000_000,
    "bid_size": 25,
    "ask_price": 100_020_000_000,
    "ask_size": 50,
}
```

An order is removed when its remaining size reaches zero, or when a reset clears the book. Finishing a raw event group only clears replay's temporary `pending` list; resting orders remain in the book. These dictionaries are current state, not a day's history.

## 4. `Event` and `ReplayStep`: a record, a classification, and a group

Suppose a displayed bid order is executed for 5 shares. Its raw group is `[trade, fill, cancel]`, where the actions are `T`, `F`, `C`. Only the final `C` has `F_LAST` in this example.

```python
Event(
    stream="bid_execution",
    record=fill,           # The original F record identifies the resting side/order.
    linked_record=cancel,  # The original C record performs the book reduction.
    is_replace=False,
)
```

| `Event` field | Meaning |
| --- | --- |
| `stream` | One of `bid_add`, `ask_add`, `bid_execution`, `ask_execution`, `bid_cancel`, `ask_cancel` |
| `record` | One representative raw record: `A` for an addition, `C` for a cancellation, or `F` for an execution |
| `linked_record` | One companion record, not a list. Execution links `F` to `C`; replacement legs link to each other. Ordinary adds/cancels use `None`. |
| `is_replace` | `True` for both classified legs of a replacement `C/A` group |

A replacement produces two `Event` objects, even though it came from one raw group. An execution produces one `Event`, even though its group can have three raw records. Partial executions and cancellations also count as one classified event each; `size` retains their quantity.

Replay yields the whole group and its before/after quotes together:

```python
ReplayStep(
    records=[trade, fill, cancel],
    events=[
        Event(
            stream="bid_execution",
            record=fill,
            linked_record=cancel,
            is_replace=False,
        ),
    ],
    quote_before={
        "bid_price": 100_000_000_000, "bid_size": 25,
        "ask_price": 100_020_000_000, "ask_size": 60,
    },
    quote_after={
        "bid_price": 100_000_000_000, "bid_size": 20,
        "ask_price": 100_020_000_000, "ask_size": 60,
    },
)
```

`ReplayStep.records` retains every raw record in that group, including `T`. `ReplayStep.events` contains its research classifications. All records reach the book; `T` and `F` do not deduct volume, and `C` deducts it once.

The quotes are the completed states immediately before and after the raw group for this source. A quote is `None` if a two-sided book is unavailable. Both replacement legs use the group's same `quote_before`; there is no intermediate per-leg state in this interface. Snapshot-only groups update the book but have no arrival events. Mixed snapshot/incremental groups and unsupported action patterns currently raise errors.

## 5. Feature dictionaries and `Label`

For this input:

```python
quote = {
    "bid_price": 100_000_000_000, "bid_size": 100,
    "ask_price": 100_020_000_000, "ask_size": 50,
}
```

`build_features(quote)` returns:

```python
{
    "bid_size": 100,
    "ask_size": 50,
    "spread_dollars": 0.02,
    "mid_price_dollars": 100.01,
    "imbalance": 0.3333333333333333,
}
```

`build_features(None)` returns `None`. Supply valid two-sided quotes; the current feature helper does not validate all invalid-price/size cases.

`build_labels()` takes a list of quotes, not a list of `Event` objects. For quotes whose mid-prices are `[100.01, 100.01, 100.02, 100.00]`, it returns:

```python
[
    Label(y_up=1, next_move_index=2),
    Label(y_up=1, next_move_index=2),
    Label(y_up=0, next_move_index=3),
    Label(y_up=None, next_move_index=None),
]
```

`next_move_index` is a zero-based position in the supplied quote list. It is not a raw record's `sequence` or a timestamp. The label asks about the first later *different* mid-price. `None` means no usable next move was observed; it must not be treated as a downward move. A missing quote breaks the interval.

Keep feature and label rows aligned. The logistic helper trains only on `imbalance`, skips rows with missing features or labels, and needs both upward and downward labels. For example, `predict_up(model, [{"imbalance": -0.5}, None, {"imbalance": 0.5}])` returns `[p_low, None, p_high]`, where the two probabilities are floats determined by the fitted model. All these calls should use one instrument/source/session at a time; date splits are the caller's responsibility.

## 6. Time-bin counts from `bin_event_stream()`

Input: classified events, timezone-aware `start` and `end`, and optional `bin_minutes` / `timestamp_field`. The default width is 15 minutes and the default clock is `ts_recv`. `ts_event` can be selected explicitly.

For a 09:45-10:15 window, the output shape is:

```python
{
    "09:45:00-10:00:00": {
        "bid_add": 0,
        "ask_add": 1,
        "bid_execution": 0,
        "ask_execution": 0,
        "bid_cancel": 1,
        "ask_cancel": 0,
    },
    "10:00:00-10:15:00": {
        "bid_add": 1,
        "ask_add": 0,
        "bid_execution": 1,
        "ask_execution": 0,
        "bid_cancel": 1,
        "ask_cancel": 0,
    },
}
```

These are the counts from the runnable sample below. One 5-share cancellation adds **1**, not 5. A `T/F/C` group adds one execution, not three arrivals.

Bins are `[left, right)`: an event exactly at 10:00 belongs to the second bin; an event exactly at `end` is excluded. Empty bins still contain all six streams with zero counts. A final partial bin ends at `end`. Labels use `start`'s timezone. Process one local day and one source at a time. This function returns counts only; it does not measure valid observed seconds.

## 7. `QueueState` and state-conditioned counts

`QueueState` is a type alias for a tuple, not a new class with attributes:

```python
state: QueueState = (30, 50)  # 30 best-bid shares, 50 best-ask shares
```

It uses the shares at the best bid and best ask from `step.quote_before`. It does not count orders. `(30, 50)` and `(25, 50)` are different exact states; no small/medium/large grouping is implemented.

`bin_events_by_state()` takes replay steps because `Event` alone does not carry the pre-event quote. It groups their events by state and reuses `bin_event_stream()` for the time buckets. One entry from its output is:

```python
{
    "09:45:00-10:00:00": {
        (30, 50): {
            "bid_add": 0,
            "ask_add": 0,
            "bid_execution": 0,
            "ask_execution": 0,
            "bid_cancel": 1,
            "ask_cancel": 0,
        },
        # Other observed states have their own six-count dictionaries.
    },
    # Other time bins have the same structure.
}
```

Interpretation: one bid cancellation arrived while the pre-event best queues contained 30 bid shares and 50 ask shares. Its post-event bid size of 25 is not used to classify this cancellation's state. If the same state recurs later in the same bin, its counts accumulate in the same dictionary.

Missing pre-event quotes are skipped. Only states with counted arrivals appear in a bin; a completely empty time bin is `{}`. An absent state means zero counted arrivals, not zero time spent there. Both counters currently retain classified events from all price levels: conditioning on best-queue sizes does not make the events best-level-only.

This prototype buffers the window's events by state in memory. Start with small samples. It returns counts, not rates. Queue-reactive fitting still needs valid state dwell times:

```text
lambda(stream, time bin, state)
    = arrivals of that stream in that state and time bin
      / valid seconds spent in that state and time bin
```

For example, 4 cancellations during 20 seconds in a state give `4 / 20 = 0.2 events/second`. The denominator is 20 seconds, not the whole 900-second time bin. States with exposure but no arrivals must also be represented during fitting.

## 8. `PoissonBin`: counts, exposure, and rates together

`fit_poisson()` takes counts shaped like section 6 plus a separate dictionary of valid exposure seconds with matching time-bin names. For example, if a fully observed synthetic 15-minute bin contains one ask addition and one bid cancellation:

```python
PoissonBin(
    counts={
        "bid_add": 0, "ask_add": 1,
        "bid_execution": 0, "ask_execution": 0,
        "bid_cancel": 1, "ask_cancel": 0,
    },
    exposure_seconds=900.0,
    rates_per_second={
        "bid_add": 0.0, "ask_add": 1 / 900,
        "bid_execution": 0.0, "ask_execution": 0.0,
        "bid_cancel": 1 / 900, "ask_cancel": 0.0,
    },
)
```

`1 / 900` is about `0.001111` events/second. These are time-of-day Poisson rates; do not pass the nested state-count dictionary directly to `fit_poisson()`.

A bin with positive exposure and no arrivals has zero rates. A bin with zero exposure and zero counts has `rates_per_second=None`; positive counts with zero exposure raise an error. At least one bin must have positive exposure. Pool training days by summing counts and valid seconds before fitting. An empty count bin alone is not evidence that all its seconds were observed.

## 9. Runnable synthetic workflow

Run `uv sync --locked` first. From the repository root, paste the following whole command into a terminal. It constructs eight complete raw event groups, replays them, and checks the example outputs. This is a walkthrough of current interfaces, not a real-data validation or model evaluation.

```bash
PYTHONPATH=src uv run python - <<'PY'
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import databento as db

from saf.data.binning import _to_ns, bin_event_stream
from saf.data.replay import replay_records
from saf.data.state_events import bin_events_by_state
from saf.features import build_features
from saf.labels import Label, build_labels
from saf.models.imbalance import fit_imbalance, predict_up
from saf.models.poisson import fit_poisson

ny = ZoneInfo("America/New_York")
start = datetime(2025, 1, 2, 9, 45, tzinfo=ny)
end = start + timedelta(minutes=30)
base = _to_ns(start)
second = 1_000_000_000


def record(action, side, order_id, price, size, sequence, timestamp, *, last=True):
    return db.MBOMsg(
        publisher_id=2,
        instrument_id=12345,
        channel_id=0,
        ts_event=timestamp,
        ts_recv=timestamp,
        ts_in_delta=0,
        sequence=sequence,
        action=db.Action(action),
        side=db.Side(side),
        order_id=order_id,
        price=price,
        size=size,
        flags=128 if last else 0,
    )


bid = 100_000_000_000
ask = 100_020_000_000
improved_bid = 100_010_000_000
records = [
    # Initialize the book BEFORE the analysis window: bid 30, ask 50.
    record("A", "B", 1001, bid, 10, 1, base - 3),
    record("A", "B", 1002, bid, 20, 2, base - 2),
    record("A", "A", 2001, ask, 50, 3, base - 1),
    # First bin: bid cancellation, then ask addition.
    record("C", "B", 1001, bid, 5, 4, base),
    record("A", "A", 2002, ask, 10, 5, base + second),
    # Second bin: one execution represented by three raw records.
    record("T", "A", 0, bid, 5, 6, base + 900 * second, last=False),
    record("F", "B", 1002, bid, 5, 6, base + 900 * second, last=False),
    record("C", "B", 1002, bid, 5, 6, base + 900 * second),
    # Improve the bid, then remove that order: mid-price moves up, then down.
    record("A", "B", 1003, improved_bid, 40, 7, base + 901 * second),
    record("C", "B", 1003, improved_bid, 40, 8, base + 902 * second),
]

# Lists make this tiny example reusable; large files should be streamed.
steps = list(replay_records(records))
window_steps = [step for step in steps if step.records[-1].ts_recv >= base]
events = [event for step in steps for event in step.events]
assert len(steps) == 8
assert len(window_steps) == 5

counts = bin_event_stream(events, start=start, end=end)
state_counts = bin_events_by_state(steps, start=start, end=end)
first = "09:45:00-10:00:00"
last = "10:00:00-10:15:00"
assert sum(counts[first].values()) == 2
assert sum(counts[last].values()) == 3
assert state_counts[first][(30, 50)]["bid_cancel"] == 1
assert state_counts[first][(25, 50)]["ask_add"] == 1
assert state_counts[last][(25, 60)]["bid_execution"] == 1
assert state_counts[last][(20, 60)]["bid_add"] == 1
assert state_counts[last][(40, 60)]["bid_cancel"] == 1
for name, six_counts in counts.items():
    for stream, count in six_counts.items():
        assert sum(row[stream] for row in state_counts[name].values()) == count

execution = window_steps[2]
assert [str(raw.action) for raw in execution.records] == ["T", "F", "C"]
assert len(execution.events) == 1
assert execution.events[0].linked_record is execution.records[-1]

quotes = [step.quote_after for step in window_steps]
features = [build_features(quote) for quote in quotes]
labels = build_labels(quotes)
assert labels == [Label(1, 3), Label(1, 3), Label(1, 3), Label(0, 4), Label(None, None)]

# This invented timeline is assumed fully valid through end, even after
# its last arrival. Real data needs measured coverage and invalid-gap masks.
exposure = {name: 900.0 for name in counts}
poisson = fit_poisson(counts, exposure)
assert poisson[first].rates_per_second["bid_cancel"] == 1 / 900

# Toy fitting only: do not interpret in-sample probabilities as performance.
logistic = fit_imbalance(features, labels)
probabilities = predict_up(logistic, [features[0], None, features[3]])
assert probabilities[1] is None
assert all(0 <= p <= 1 for p in probabilities if p is not None)

print("Time-bin counts:", counts)
print("State-conditioned counts:", state_counts)
print("Execution ReplayStep:", execution)
print("First feature row:", features[0])
print("Labels:", labels)
print("First PoissonBin:", poisson[first])
print("Toy upward probabilities:", probabilities)
print("All synthetic walkthrough checks passed.")
PY
```

With a local file, the raw input starts as follows. This is an API usage example; replace the path and choose the intended instrument/source before binning:

```python
from contextlib import closing
from saf.data.ingest import iter_mbo

with closing(iter_mbo("path/to/file.dbn.zst")) as records:
    for step in replay_records(records):
        print(step.quote_after, step.events)
```

Replay enough pre-window history to initialize the book, then restrict analysis outputs to the chosen window. Do not discard initialization messages before replay. For state-rate fitting, retain a timeline through empty-event steps too: counting only classified arrivals cannot measure how long the book stayed in each state.
