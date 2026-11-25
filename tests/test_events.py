import databento as db
import pytest

from saf.data.events import classify_event


def make_record(
    action: str,
    side: str,
    order_id: int = 1001,
    size: int = 30,
    *,
    last: bool = True,
) -> db.MBOMsg:
    """Create synthetic records from the same source message."""
    return db.MBOMsg(
        publisher_id=2,
        instrument_id=12345,
        ts_event=0,
        ts_recv=0,
        order_id=order_id,
        action=db.Action(action),
        side=db.Side(side),
        price=100 * 1_000_000_000,
        size=size,
        flags=128 if last else 0,
        channel_id=0,
        sequence=1,
        ts_in_delta=0,
    )


@pytest.mark.parametrize(
    "action, side, expected_stream",
    [
        ("A", "B", "bid_add"),
        ("A", "A", "ask_add"),
        ("C", "B", "bid_cancel"),
        ("C", "A", "ask_cancel"),
    ],
)
def test_add_and_cancel(action, side, expected_stream):
    record = make_record(action, side)

    events = classify_event([record])

    assert len(events) == 1
    assert events[0].stream == expected_stream
    assert events[0].record is record
    assert not events[0].is_replace


@pytest.mark.parametrize(
    "resting_side, expected_stream",
    [("B", "bid_execution"), ("A", "ask_execution")],
)
@pytest.mark.parametrize("include_trade", [True, False])
def test_execution_is_counted_once(
    resting_side, expected_stream, include_trade
):
    fill = make_record("F", resting_side, last=False)
    cancel = make_record("C", resting_side)
    records = [fill, cancel]

    if include_trade:
        aggressor_side = "A" if resting_side == "B" else "B"
        trade = make_record("T", aggressor_side, order_id=0, last=False)
        records.insert(0, trade)

    events = classify_event(records)

    assert len(events) == 1
    assert events[0].stream == expected_stream
    assert events[0].record is fill
    assert events[0].linked_record is cancel
    assert events[0].record.size == 30
    assert not events[0].is_replace


def test_replacement_keeps_both_legs_marked():
    old_order = make_record("C", "B", order_id=1001, last=False)
    new_order = make_record("A", "B", order_id=2001, size=40)

    events = classify_event([old_order, new_order])

    assert [event.stream for event in events] == ["bid_cancel", "bid_add"]
    assert all(event.is_replace for event in events)
    assert events[0].record is old_order
    assert events[1].record is new_order
    assert events[0].linked_record is new_order
    assert events[1].linked_record is old_order


def test_incomplete_event_is_rejected():
    record = make_record("A", "B", last=False)

    with pytest.raises(ValueError, match="missing F_LAST"):
        classify_event([record])


@pytest.mark.parametrize(
    "order_id, side, size",
    [(1002, "B", 30), (1001, "A", 30), (1001, "B", 20)],
)
def test_mismatched_fill_and_cancel_are_rejected(order_id, side, size):
    fill = make_record("F", "B", last=False)
    cancel = make_record("C", side, order_id=order_id, size=size)

    with pytest.raises(ValueError, match="do not match"):
        classify_event([fill, cancel])
