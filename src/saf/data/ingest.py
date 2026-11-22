"""
MboMsg(
    instrument_id=12345,
    order_id=1001,
    action='A',
    side='B',
    price=100000000000,
    size=10,
    ts_event=1735829100000000000,
    flags=LAST (128),
    sequence=1
)
"""

import argparse
from collections.abc import Iterator
from contextlib import closing
from itertools import islice
from pathlib import Path

import databento as db


def iter_mbo(path: str | Path) -> Iterator[db.MBOMsg]:
    """Yield original MBO records in file order."""
    file_path = Path(path).expanduser().resolve()


    data = db.DBNStore.from_file(file_path)

    try:
        if data.schema != db.Schema.MBO:
            raise ValueError(
                f"Expected MBO data, received schema: {data.schema}"
            )

        for record in data:
            if isinstance(record, db.MBOMsg):
                yield record
    finally:
        data.reader.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Preview MBO records from a local DBN file."
    )
    parser.add_argument("path", type=Path, help="Local .dbn or .dbn.zst file")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    if args.limit <= 0:
        parser.error("--limit must be greater than zero")

    with closing(iter_mbo(args.path)) as records:
        for record in islice(records, args.limit):
            print(record)


if __name__ == "__main__":
    main()