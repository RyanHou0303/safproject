from collections.abc import Sequence
from dataclasses import dataclass

import databento as db


F_LAST = 128
F_SNAPSHOT = 32


@dataclass(frozen=True)
class Event:
    """A classified event with its original source records."""

    stream: str
    record: db.MBOMsg
    linked_record: db.MBOMsg | None = None
    is_replace: bool = False


def _make_event(
    kind: str,
    record: db.MBOMsg,
    *,
    linked_record: db.MBOMsg | None = None,
    is_replace: bool = False,
) -> Event:
    side = str(record.side)

    if side not in {"B", "A"}:
        raise ValueError("A classified event must have a resting side.")

    if int(record.size) <= 0 or int(record.price) == db.UNDEF_PRICE:
        raise ValueError("Invalid event size or price.")

    side_name = "bid" if side == "B" else "ask"

    return Event(
        stream=f"{side_name}_{kind}",
        record=record,
        linked_record=linked_record,
        is_replace=is_replace,
    )


def classify_event(records: Sequence[db.MBOMsg]) -> list[Event]:
    """Classify one complete XNAS.ITCH event in original record order."""
    if not records:
        return []

    # A complete event ends with F_LAST.
    if not (int(records[-1].flags) & F_LAST):
        raise ValueError("The event is incomplete: missing F_LAST.")

    if any(int(record.flags) & F_LAST for record in records[:-1]):
        raise ValueError("Pass one complete event at a time.")

    # Snapshot orders describe existing state, not new arrivals.
    snapshots = [
        bool(int(record.flags) & F_SNAPSHOT)
        for record in records
    ]

    if all(snapshots):
        return []

    if any(snapshots):
        raise ValueError(
            "Mixed snapshot and incremental records are not supported yet."
        )

    # Normalized records from one Nasdaq source message share a sequence.
    keys = {
        (
            int(record.publisher_id),
            int(record.instrument_id),
            int(record.channel_id),
            int(record.sequence),
        )
        for record in records
    }

    if len(keys) != 1:
        raise ValueError(
            "Records must belong to one XNAS.ITCH source message."
        )

    actions = "".join(str(record.action) for record in records)

    if actions == "A":
        return [_make_event("add", records[0])]

    if actions == "C":
        return [_make_event("cancel", records[0])]

    if actions in {"TFC", "FC"}:
        fill, cancel = records[-2:]

        fill_key = (
            int(fill.order_id),
            str(fill.side),
            int(fill.size),
        )
        cancel_key = (
            int(cancel.order_id),
            str(cancel.side),
            int(cancel.size),
        )

        if fill_key != cancel_key:
            raise ValueError("Fill and cancellation records do not match.")

        # Use the resting side from F, not the aggressor side from T.
        return [
            _make_event(
                "execution",
                fill,
                linked_record=cancel,
            )
        ]

    if actions == "CA":
        # Preserve both replacement legs and mark their shared origin.
        return [
            _make_event(
                "cancel",
                records[0],
                linked_record=records[1],
                is_replace=True,
            ),
            _make_event(
                "add",
                records[1],
                linked_record=records[0],
                is_replace=True,
            ),
        ]

    if actions in {"T", "R", "N"}:
        # Standalone trades do not identify a displayed resting-order fill.
        # Resets and no-action records are not arrival events.
        return []

    raise ValueError(
        f"Unsupported XNAS.ITCH action sequence: {actions}"
    )
