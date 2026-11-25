import databento as db
import pytest

from saf.data.replay import replay_records


SCALE = 1_000_000_000


def make_record(
    action: str,
    side: str,
    order_id: int,
    size: int,
    price: int,
    sequence: int,
    *,
    last: bool = True,
    instrument_id: int = 12345,
) -> db.MBOMsg:
    """Create a synthetic MBO record; price is in whole dollars."""
    return db.MBOMsg(
        publisher_id=2,
        instrument_id=instrument_id,
        ts_event=sequence,
        ts_recv=sequence,
        order_id=order_id,
        action=db.Action(action),
        side=db.Side(side),
        price=price * SCALE,
        size=size,
        flags=128 if last else 0,
        channel_id=0,
        sequence=sequence,
        ts_in_delta=0,
    )


def initial_records():
    return [
        make_record("A", "B", 1001, 50, 100, 1),
        make_record("A", "A", 2001, 100, 101, 2),
    ]


def execution_records():
    return [
        make_record("T", "B", 0, 30, 101, 3, last=False),
        make_record("F", "A", 2001, 30, 101, 3, last=False),
        make_record("C", "A", 2001, 30, 101, 3),
    ]


def test_execution_produces_one_step_and_correct_quotes():
    records = initial_records() + execution_records()

    steps = list(replay_records(records))

    # Five raw records produce three completed steps.
    assert len(steps) == 3
    assert [len(step.records) for step in steps] == [1, 1, 3]

    assert steps[0].quote_before is None
    assert steps[0].quote_after is None

    expected_before = {
        "bid_price": 100 * SCALE,
        "bid_size": 50,
        "ask_price": 101 * SCALE,
        "ask_size": 100,
    }
    assert steps[1].quote_after == expected_before
    assert steps[2].quote_before == expected_before
    assert steps[2].quote_after == {
        **expected_before,
        "ask_size": 70,
    }

    assert [event.stream for event in steps[2].events] == [
        "ask_execution"
    ]


def test_instruments_have_separate_books():
    records = [
        make_record("A", "B", 1001, 50, 100, 1),
        make_record("A", "B", 1001, 10, 200, 2, instrument_id=67890),
        make_record("A", "A", 2001, 100, 101, 3),
        make_record("A", "A", 2001, 20, 201, 4, instrument_id=67890),
        make_record("C", "A", 2001, 30, 101, 5),
        make_record("C", "A", 2001, 5, 201, 6, instrument_id=67890),
    ]

    steps = list(replay_records(records))

    # Both instruments reuse the same order IDs.
    assert steps[4].quote_before["ask_size"] == 100
    assert steps[4].quote_after["ask_size"] == 70
    assert steps[4].quote_after["bid_price"] == 100 * SCALE

    assert steps[5].quote_before["ask_size"] == 20
    assert steps[5].quote_after["ask_size"] == 15
    assert steps[5].quote_after["bid_price"] == 200 * SCALE


def test_replacement_produces_one_step_with_two_events():
    records = initial_records() + [
        make_record("C", "B", 1001, 50, 100, 3, last=False),
        make_record("A", "B", 1002, 40, 99, 3),
    ]

    steps = list(replay_records(records))
    replacement = steps[-1]

    assert len(steps) == 3
    assert len(replacement.records) == 2
    assert [event.stream for event in replacement.events] == [
        "bid_cancel",
        "bid_add",
    ]
    assert all(event.is_replace for event in replacement.events)
    assert replacement.quote_before["bid_price"] == 100 * SCALE
    assert replacement.quote_after["bid_price"] == 99 * SCALE
    assert replacement.quote_after["bid_size"] == 40


def test_reset_clears_book_without_arrival_event():
    records = initial_records() + [
        make_record("R", "N", 0, 0, 0, 3)
    ]

    steps = list(replay_records(records))
    reset = steps[-1]

    assert reset.quote_before is not None
    assert reset.quote_after is None
    assert reset.events == []


def test_unfinished_event_at_end_is_rejected():
    records = [
        make_record("A", "B", 1001, 50, 100, 1, last=False)
    ]

    with pytest.raises(ValueError, match="before F_LAST"):
        list(replay_records(records))


def test_yield_reads_only_until_next_completed_event():
    records = initial_records() + execution_records()
    consumed = []

    def source():
        for record in records:
            consumed.append(record)
            yield record

    steps = replay_records(source())
    assert consumed == []

    next(steps)
    assert len(consumed) == 1

    next(steps)
    assert len(consumed) == 2

    last_step = next(steps)
    assert len(consumed) == 5
    assert len(last_step.records) == 3

    with pytest.raises(StopIteration):
        next(steps)
