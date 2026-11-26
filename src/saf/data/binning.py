from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from saf.data.events import Event
from saf.models.poisson import STREAMS


NS_PER_SECOND = 1_000_000_000
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _to_ns(value: datetime) -> int:
    # ts_recv is a Unix timestamp in nanoseconds: when Databento received the record.
    # Example: 1735829100000001000
    # Corresponds to 2025-01-02 14:45:00.000001 UTC.
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Times must include a timezone.")

    delta = value.astimezone(UTC) - EPOCH
    return (
        (delta.days * 86400 + delta.seconds) * NS_PER_SECOND
        + delta.microseconds * 1000
    )


def bin_event_stream(
    events: Iterable[Event],
    *,
    start: datetime,
    end: datetime,
    bin_minutes: int = 15,
    timestamp_field: str = "ts_recv",
) -> dict[str, dict[str, int]]:
    """Count six event types per bin for one instrument/source/day.

    Bins start at start and use half-open intervals [left, right).
    Labels use start's timezone. The last bin ends at end.
    Output: {"09:45:00-10:00:00": {"bid_add": 3, ...six streams...}}.
    Each Event adds one count, regardless of event.record.size.
    Empty bins have six zero counts. Exposure and rates are not returned.
    See docs/code_examples.md for the complete input/output example.
    """
    if (
        isinstance(bin_minutes, bool)
        or not isinstance(bin_minutes, int)
        or bin_minutes <= 0
    ):
        raise ValueError("bin_minutes must be a positive integer.")

    if timestamp_field not in {"ts_recv", "ts_event"}:
        raise ValueError("timestamp_field must be ts_recv or ts_event.")

    start_ns, end_ns = _to_ns(start), _to_ns(end)
    if start_ns >= end_ns:
        raise ValueError("start must be earlier than end.")

    if start.date() != end.astimezone(start.tzinfo).date():
        raise ValueError("Process one local trading day at a time.")

    counts_by_bin: dict[str, dict[str, int]] = {}
    names: list[str] = []

    cursor = start.astimezone(UTC)
    stop = end.astimezone(UTC)
    width = timedelta(minutes=bin_minutes)
    bin_ns = bin_minutes * 60 * NS_PER_SECOND

    # Create all bins first, including bins with no events.
    while cursor < stop:
        boundary = min(cursor + width, stop)
        left = cursor.astimezone(start.tzinfo)
        right = boundary.astimezone(start.tzinfo)
        name = f"{left.time().isoformat()}-{right.time().isoformat()}"

        if name in counts_by_bin:
            raise ValueError("Repeated local-time labels are not supported.")

        names.append(name)
        counts_by_bin[name] = {stream: 0 for stream in STREAMS}
        cursor = boundary

    source_key: tuple[int, int, int] | None = None

    for event in events:
        timestamp = int(getattr(event.record, timestamp_field))

        if not start_ns <= timestamp < end_ns:
            continue

        key = (
            int(event.record.publisher_id),
            int(event.record.instrument_id),
            int(event.record.channel_id),
        )

        if source_key is None:
            source_key = key
        elif key != source_key:
            raise ValueError("Pass events from one instrument/source only.")

        if event.stream not in STREAMS:
            raise ValueError(f"Unknown event stream: {event.stream}")

        index = (timestamp - start_ns) // bin_ns
        counts_by_bin[names[index]][event.stream] += 1

    return counts_by_bin
