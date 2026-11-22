import databento as db

from saf.data.book import OrderBook


SCALE = 1_000_000_000


def make_record(
    action: str,
    order_id: int,
    side: str,
    price: int,
    size: int,
    *,
    last: bool = True,
) -> db.MBOMsg:
    """Create a synthetic MBO record; price is in whole dollars."""
    return db.MBOMsg(
        publisher_id=2,
        instrument_id=12345,
        ts_event=0,
        ts_recv=0,
        order_id=order_id,
        action=db.Action(action),
        side=db.Side(side),
        price=price * SCALE,
        size=size,
        flags=128 if last else 0,
        channel_id=0,
        sequence=1,
        ts_in_delta=0,
    )


def test_one_sided_book_returns_none():
    book = OrderBook()

    result = book.apply(make_record("A", 1001, "B", 100, 10))

    # The bid is stored, but a two-sided quote is not available yet.
    assert book.orders[1001].size == 10
    assert book.bids == {100 * SCALE: 10}
    assert result is None


def test_aggregate_and_cancel_orders():
    book = OrderBook()

    book.apply(make_record("A", 2001, "A", 101, 20))
    book.apply(make_record("A", 1001, "B", 100, 10))
    result = book.apply(make_record("A", 1002, "B", 100, 5))

    # Two bid orders at the same price: 10 + 5 = 15.
    assert result == {
        "bid_price": 100 * SCALE,
        "bid_size": 15,
        "ask_price": 101 * SCALE,
        "ask_size": 20,
    }

    # Partially cancel order 1001: 10 - 4 = 6.
    result = book.apply(make_record("C", 1001, "B", 100, 4))

    assert book.orders[1001].size == 6
    assert result["bid_size"] == 11

    # Cancel its remaining 6 shares; order 1002 remains.
    result = book.apply(make_record("C", 1001, "B", 100, 6))

    assert 1001 not in book.orders
    assert result["bid_size"] == 5

    # Removing the final bid order also removes its price level.
    result = book.apply(make_record("C", 1002, "B", 100, 5))

    assert book.bids == {}
    assert result is None


def test_fill_reduces_quantity_only_once():
    book = OrderBook()

    book.apply(make_record("A", 1001, "B", 100, 50))
    book.apply(make_record("A", 2001, "A", 101, 100))

    # Trade information: no book update and no completed event yet.
    result = book.apply(
        make_record("T", 0, "B", 101, 30, last=False)
    )
    assert result is None
    assert book.orders[2001].size == 100

    # Fill information: still no quantity deduction.
    result = book.apply(
        make_record("F", 2001, "A", 101, 30, last=False)
    )
    assert result is None
    assert book.orders[2001].size == 100

    # The accompanying cancel deducts 30 shares and ends the event.
    result = book.apply(
        make_record("C", 2001, "A", 101, 30)
    )
    assert book.orders[2001].size == 70
    assert result["ask_size"] == 70