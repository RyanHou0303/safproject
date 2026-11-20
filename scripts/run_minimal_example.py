#!/usr/bin/env python3
"""Run the synthetic reference data flow offline; no API key or network is used."""

import argparse
import csv
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from saf.data.ingest import read_mbo
from saf.data.reference import replay, model_inputs
from saf.data.schema import TABLE_TYPES, NULLABLE, catalog, validate_tables


def write_outputs(out, tables, inputs, metadata, parquet=False):
    out.mkdir(parents=True, exist_ok=True)
    for name, fields in TABLE_TYPES.items():
        with (out / (name + ".csv")).open("w", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=list(fields), lineterminator="\n")
            writer.writeheader()
            for row in tables[name]:
                writer.writerow({k: str(v).lower() if type(v) is bool else v for k, v in row.items()})
        if parquet:
            import pyarrow as pa
            import pyarrow.parquet as pq
            schema = pa.schema([pa.field(field, pa.type_for_alias(dtype), nullable=field in NULLABLE.get(name, set()))
                                for field, dtype in fields.items()])
            table = pa.Table.from_pylist(tables[name], schema=schema)
            pq.write_table(table, out / (name + ".parquet"))
    for name, value in {"model_inputs": inputs, "schema": catalog(), "metadata": metadata}.items():
        (out / (name + ".json")).write_text(json.dumps(value, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "examples/minimal/mbo.csv")
    parser.add_argument("--metadata", type=Path, default=ROOT / "examples/minimal/metadata.json")
    parser.add_argument("--out", type=Path, default=ROOT / "artifacts/minimal_example")
    parser.add_argument("--parquet", action="store_true", help="Also write explicitly typed Parquet (requires pyarrow)")
    parser.add_argument("--check-expected", action="store_true", help="Compare against the checked-in synthetic reference outputs")
    args = parser.parse_args()
    metadata = json.loads(args.metadata.read_text())
    if metadata["contract_version"] != "1.0":
        raise ValueError("Unsupported contract version.")
    records = list(read_mbo(args.input))
    tables = replay(records, metadata)
    validate_tables(tables)
    inputs = model_inputs(tables, metadata)
    write_outputs(args.out, tables, inputs, metadata, args.parquet)
    if args.check_expected:
        expected = ROOT / "examples/minimal/expected"
        for name in TABLE_TYPES:
            if (args.out / (name + ".csv")).read_text() != (expected / (name + ".csv")).read_text():
                raise ValueError(f"{name} differs from the checked-in expected output.")
        if inputs != json.loads((expected / "model_inputs.json").read_text()):
            raise ValueError("Model inputs differ from the checked-in reference.")
        print("Expected outputs match.")
    print("SYNTHETIC development data; no empirical model conclusions can be drawn from this fixture.")
    print(f'{len(records)} MBO records -> {len(tables["quotes"])} completed book states -> {len(tables["events"])} model events.')
    print(f'Counts in {inputs["stream_order"]}: {inputs["counts"]}; valid exposure: {inputs["valid_exposure_s"]} seconds.')
    print(f'{len(tables["forecasts"])} forecast rows: {inputs["labeled_forecasts"]} labeled, '
          f'{len(tables["forecasts"])-inputs["labeled_forecasts"]} censored.')
    print(f"Output: {args.out}")


if __name__ == "__main__":
    main()
