"""Read integer-encoded CSV or local DBN without making network requests."""

import csv
from dataclasses import dataclass
from pathlib import Path

RAW_TYPES = {
    "ts_recv": "uint64", "ts_event": "uint64", "rtype": "uint8",
    "publisher_id": "uint16", "instrument_id": "uint32", "action": "string",
    "side": "string", "price": "int64", "size": "uint32", "channel_id": "uint8",
    "order_id": "uint64", "flags": "uint8", "ts_in_delta": "int32", "sequence": "uint32",
}
UNDEF_PRICE = 2**63 - 1
PRICE_SCALE = 10**9
F_LAST = 128


@dataclass(frozen=True, slots=True)
class MBORecord:
    source_index: int
    ts_recv: int
    ts_event: int
    rtype: int
    publisher_id: int
    instrument_id: int
    action: str
    side: str
    price: int
    size: int
    channel_id: int
    order_id: int
    flags: int
    ts_in_delta: int
    sequence: int


def record_from_values(values, source_index):
    converted = {}
    for name, dtype in RAW_TYPES.items():
        value = values[name]
        if dtype == "string":
            converted[name] = chr(value) if isinstance(value, int) else str(value)
        else:
            if isinstance(value, float):
                raise ValueError(f"{name}: floating-point input can lose precision; use integers.")
            value = int(value)
            bits = int(dtype.removeprefix("uint").removeprefix("int"))
            low = 0 if dtype.startswith("uint") else -(2 ** (bits - 1))
            high = 2**bits - 1 if dtype.startswith("uint") else 2 ** (bits - 1) - 1
            if not low <= value <= high:
                raise ValueError(f"{name} is outside {dtype}.")
            converted[name] = value
    if converted["rtype"] != 160 or converted["action"] not in set("AMCRTFN") or converted["side"] not in set("BAN"):
        raise ValueError("Expected a normalized MBO record with known action and side.")
    return MBORecord(source_index=source_index, **converted)


def read_mbo(path):
    path = Path(path)
    if path.suffix == ".csv":
        with path.open(newline="") as source:
            reader = csv.DictReader(source)
            if set(reader.fieldnames or []) != set(RAW_TYPES):
                raise ValueError("Use the documented integer CSV fields, or read DBN directly.")
            for index, values in enumerate(reader):
                yield record_from_values(values, index)
    elif path.name.endswith((".dbn", ".dbn.zst")):
        try:
            import databento as db
            import databento_dbn as dbn
        except ImportError as error:
            raise ValueError("Local DBN reading needs requirements-data.txt; CSV needs only Python.") from error
        store = db.DBNStore.from_file(path)
        decoder = dbn.DBNDecoder(upgrade_policy=dbn.VersionUpgradePolicy.UPGRADE_TO_V3)
        index = 0
        with store.reader as reader:
            while block := reader.read(65536):
                for record in decoder.write_and_decode(block):
                    if isinstance(record, dbn.Metadata):
                        continue
                    if int(record.rtype) == 160:
                        yield record_from_values({name: getattr(record, name) for name in RAW_TYPES}, index)
                    index += 1
            decoder.decode()
            if len(decoder.buffer()):
                raise ValueError("Truncated DBN record at EOF.")
    else:
        raise ValueError("Expected .csv, .dbn, or .dbn.zst; no network data source is accepted.")
