"""Count arrivals by time bin and pre-event best-queue shares.

See docs/code_examples.md for complete inputs, outputs, and a runnable example.
"""

from collections.abc import Iterable
from datetime import datetime

from saf.data.binning import _to_ns, bin_event_stream
from saf.data.events import Event
from saf.data.replay import ReplayStep


QueueState = tuple[int, int]  # (best bid shares, best ask shares)


def bin_events_by_state(
    steps: Iterable[ReplayStep],
    *,
    start: datetime,
    end: datetime,
    bin_minutes: int = 15,
    timestamp_field: str = "ts_recv",
) -> dict[str, dict[QueueState, dict[str, int]]]:
    """Count events by time bin and pre-event queue state for one source.

    Input: ReplayStep objects, with state from each step.quote_before.
    Output: {time_bin: {(bid_size, ask_size): six_stream_counts}}.
    Example lookup: result["09:45:00-10:00:00"][(30, 50)]["bid_cancel"].
    Missing pre-event quotes are skipped. Empty bins are {}; states without
    arrivals are absent. This says nothing about their exposure seconds.
    Events from all classified price levels remain eligible. This function
    buffers events in memory and does not calculate state dwell times/rates.
    """
    # Reuse binning.py to validate parameters and create the time bins.
    empty_bins = bin_event_stream(
        [],
        start=start,
        end=end,
        bin_minutes=bin_minutes,
        timestamp_field=timestamp_field,
    )

    start_ns = _to_ns(start)
    end_ns = _to_ns(end)

    events_by_state: dict[QueueState, list[Event]] = {}
    source_key: tuple[int, int, int] | None = None

    # Group events using the queue sizes BEFORE their raw event group.
    for step in steps:
        quote = step.quote_before

        # A two-sided quote is required to define the state.
        if quote is None:
            continue

        state = (quote["bid_size"], quote["ask_size"])

        for event in step.events:
            timestamp = int(getattr(event.record, timestamp_field))

            if not start_ns <= timestamp < end_ns:
                continue

            if state[0] <= 0 or state[1] <= 0:
                raise ValueError("Pre-event queue sizes must be positive.")

            # Check the source across all states.
            key = (
                int(event.record.publisher_id),
                int(event.record.instrument_id),
                int(event.record.channel_id),
            )

            if source_key is None:
                source_key = key
            elif key != source_key:
                raise ValueError("Pass steps from one instrument/source only.")

            events_by_state.setdefault(state, []).append(event)

    # Preserve time bins even when they contain no counted events.
    result: dict[str, dict[QueueState, dict[str, int]]] = {
        name: {} for name in empty_bins
    }

    # Reuse the existing time-binning function for each state.
    for state, events in events_by_state.items():
        state_bins = bin_event_stream(
            events,
            start=start,
            end=end,
            bin_minutes=bin_minutes,
            timestamp_field=timestamp_field,
        )

        for name, counts in state_bins.items():
            # Only include states with arrivals in this time bin.
            if any(counts.values()):
                result[name][state] = counts

    return result
