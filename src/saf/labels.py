from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Label:
    """Direction and list position of the first later different mid-price.

    Label(1, 3): the next move is up, at quotes[3] in the input list.
    Label(0, 4): the next move is down, at quotes[4].
    Label(None, None): no usable next move was observed.
    next_move_index is not a feed sequence number or timestamp.
    """

    y_up: int | None
    next_move_index: int | None


def _mid_price_x2(quote: dict[str, int] | None) -> int | None:
    """Return twice the mid-price in the original integer price units."""
    if quote is None:
        return None

    bid = quote["bid_price"]
    ask = quote["ask_price"]

    if bid <= 0 or ask <= 0 or bid >= ask:
        raise ValueError("Expected positive prices with bid below ask.")

    if quote["bid_size"] <= 0 or quote["ask_size"] <= 0:
        raise ValueError("Expected positive displayed sizes.")

    return bid + ask


def build_labels(
    quotes: Sequence[dict[str, int] | None],
) -> list[Label]:
    """Label one instrument/source/session in replay order.

    None quotes split the input into separate valid intervals.
    The caller must keep different instruments and trading days separate.
    """
    mids = [_mid_price_x2(quote) for quote in quotes]
    labels = [Label(None, None) for _ in mids]

    # Work backward so unchanged prices can reuse the next row's answer.
    for i in range(len(mids) - 2, -1, -1):
        current = mids[i]
        following = mids[i + 1]

        if current is None or following is None:
            continue

        if current == following:
            labels[i] = labels[i + 1]
        else:
            labels[i] = Label(
                y_up=1 if following > current else 0,
                next_move_index=i + 1,
            )

    return labels


if __name__ == "__main__":
    base = {
        "bid_price": 100_000_000_000,
        "ask_price": 100_020_000_000,
        "bid_size": 100,
        "ask_size": 50,
    }

    quotes = [
        base,
        base.copy(),
        {
            **base,
            "bid_price": 100_010_000_000,
            "ask_price": 100_030_000_000,
        },
        {
            **base,
            "bid_price": 99_990_000_000,
            "ask_price": 100_010_000_000,
        },
    ]

    for i, label in enumerate(build_labels(quotes)):
        print(i, label)
