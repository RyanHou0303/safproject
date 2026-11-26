from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import databento as db
import pytest

from saf.data.binning import _to_ns, bin_event_stream
from saf.data.events import Event


NY = ZoneInfo("America/New_York")
START = datetime(2025, 1, 2, 9, 45, tzinfo=NY)
END = START + timedelta(minutes=30)
START_NS = 1_735_829_100_000_000_000
SECOND = 1_000_000_000


def make_event(
    stream: str,
    timestamp: int,
    *,
    instrument_id: int = 12345,
) -> Event:
    """Create an already classified synthetic event."""
    side_name, kind = stream.split("_", 1)
    action = {
        "add": "A",
        "execution": "F",
        "cancel": "C",
    }[kind]

    record = db.MBOMsg(
        publisher_id=2,
        instrument_id=instrument_id,
        ts_event=timestamp,
        ts_recv=timestamp,
        order_id=1001,
        action=db.Action(action),
        side=db.Side("B" if side_name == "bid" else "A"),
        price=100_000_000_000,
        size=30,
        flags=128,
        channel_id=0,
        sequence=1,
        ts_in_delta=0,
    )
    return Event(stream=stream, record=record)


@pytest.mark.parametrize(
    "value",
    [
        datetime(2025, 1, 2, 14, 45, 0, 1, tzinfo=UTC),
        datetime(2025, 1, 2, 9, 45, 0, 1, tzinfo=NY),
    ],
)
def test_to_ns_converts_same_instant_exactly(value):
    assert _to_ns(value) == 1_735_829_100_000_001_000


def test_counts_all_six_event_types():
    streams = [
        "bid_add",
        "bid_add",
        "bid_add",
        "ask_add",
        "bid_execution",
        "ask_execution",
        "bid_cancel",
        "ask_cancel",
    ]
    events = (
        make_event(stream, START_NS + i)
        for i, stream in enumerate(streams)
    )

    result = bin_event_stream(events, start=START, end=END)

    assert result["09:45:00-10:00:00"] == {
        "bid_add": 3,
        "ask_add": 1,
        "bid_execution": 1,
        "ask_execution": 1,
        "bid_cancel": 1,
        "ask_cancel": 1,
    }
    # Each event has size=30, but the total event count is eight.
    assert sum(result["09:45:00-10:00:00"].values()) == 8


def test_bin_boundaries_are_left_closed_right_open():
    boundary = START_NS + 15 * 60 * SECOND
    end_ns = START_NS + 30 * 60 * SECOND
    events = [
        make_event("bid_add", START_NS - 1),
        make_event("bid_add", START_NS),
        make_event("ask_add", boundary - 1),
        make_event("bid_cancel", boundary),
        make_event("ask_cancel", end_ns),
    ]

    result = bin_event_stream(events, start=START, end=END)

    first = result["09:45:00-10:00:00"]
    second = result["10:00:00-10:15:00"]

    assert first["bid_add"] == 1
    assert first["ask_add"] == 1
    assert sum(first.values()) == 2

    assert second["bid_cancel"] == 1
    assert second["ask_cancel"] == 0
    assert sum(second.values()) == 1


def test_empty_stream_keeps_zero_count_bins():
    result = bin_event_stream([], start=START, end=END)

    assert list(result) == [
        "09:45:00-10:00:00",
        "10:00:00-10:15:00",
    ]
    for counts in result.values():
        assert len(counts) == 6
        assert all(count == 0 for count in counts.values())


def test_custom_bin_width_and_partial_last_bin():
    end = START + timedelta(minutes=25)
    events = [
        make_event("ask_add", START_NS + 24 * 60 * SECOND)
    ]

    result = bin_event_stream(
        events,
        start=START,
        end=end,
        bin_minutes=10,
    )

    assert list(result) == [
        "09:45:00-09:55:00",
        "09:55:00-10:05:00",
        "10:05:00-10:10:00",
    ]
    assert result["10:05:00-10:10:00"]["ask_add"] == 1


def test_mixed_instruments_are_rejected():
    events = [
        make_event("bid_add", START_NS, instrument_id=12345),
        make_event("bid_add", START_NS + 1, instrument_id=67890),
    ]

    with pytest.raises(ValueError, match="one instrument/source"):
        bin_event_stream(events, start=START, end=END)