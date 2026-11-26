import argparse
from collections.abc import Iterable, Iterator
from contextlib import closing
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

import databento as db

from saf.data.book import OrderBook
from saf.data.events import Event, classify_event
from saf.data.ingest import iter_mbo


F_LAST = 128


@dataclass
class ReplayStep:
    """One completed raw group, its classifications, and before/after quotes.

    Example: records=[T, F, C], events=[Event("bid_execution", F, C)].
    Here T/F/C stand for raw MBOMsg objects, not action strings.
    quote_before/quote_after contain bid_price, bid_size, ask_price,
    ask_size, or are None when a two-sided quote is unavailable.
    Both replacement legs share the group's quote_before.
    """

    records: list[db.MBOMsg]
    events: list[Event]
    quote_before: dict[str, int] | None
    quote_after: dict[str, int] | None


def replay_records(records: Iterable[db.MBOMsg]) -> Iterator[ReplayStep]:
    """Yield one ReplayStep per F_LAST-completed group, in input order.

    Input: raw records, for example iter_mbo("local.dbn.zst").
    Output: ReplayStep objects; one can contain zero, one, or two Events.
    Books and pending buffers are keyed by (publisher, instrument, channel).
    Clearing a completed pending buffer does not clear resting orders.
    """
    books: dict[tuple[int, int, int], OrderBook] = {}
    pending: dict[tuple[int, int, int], list[db.MBOMsg]] = {}
    quotes: dict[tuple[int, int, int], dict[str, int] | None] = {}

    for record in records:
        key = (
            int(record.publisher_id),
            int(record.instrument_id),
            int(record.channel_id),
        )

        if key not in books:
            books[key] = OrderBook()

        batch = pending.setdefault(key, [])
        batch.append(record)

        # Every record reaches the book, including records without F_LAST.
        quote_after = books[key].apply(record)

        if not (int(record.flags) & F_LAST):
            continue

        events = classify_event(batch)
        quote_before = quotes.get(key)

        # Keep internal quote history independent of returned dictionaries.
        quotes[key] = None if quote_after is None else quote_after.copy()
        del pending[key]

        yield ReplayStep(
            records=batch,
            events=events,
            quote_before=quote_before,
            quote_after=quote_after,
        )

    if pending:
        raise ValueError(
            f"Input ended before F_LAST for these sources: {list(pending)}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Preview book quotes and classified events from a DBN file."
    )
    parser.add_argument("path", type=Path, help="Local .dbn or .dbn.zst file")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    if args.limit <= 0:
        parser.error("--limit must be greater than zero")

    with closing(iter_mbo(args.path)) as records:
        for step in islice(replay_records(records), args.limit):
            last = step.records[-1]
            print(
                f"instrument={last.instrument_id} "
                f"sequence={last.sequence}"
            )
            print("events:", [event.stream for event in step.events])
            print("quote_before:", step.quote_before)
            print("quote_after:", step.quote_after)


if __name__ == "__main__":
    main()
