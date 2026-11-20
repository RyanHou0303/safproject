#!/usr/bin/env python3
"""Plan, quote, and cache historical MBO requests for the frozen date manifest."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import date, datetime, time, timezone
from decimal import Decimal
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import threading
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc


def utc_string(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_dates(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as source:
        rows = list(csv.DictReader(source))
    if not rows or any(set(row) != {"trading_date", "split"} for row in rows):
        raise ValueError("Date manifest needs trading_date and split columns.")
    dates = [date.fromisoformat(row["trading_date"]) for row in rows]
    if dates != sorted(set(dates)):
        raise ValueError("Date manifest must be chronological, without duplicates.")
    today = datetime.now(ZoneInfo("America/New_York")).date()
    if any(d.weekday() >= 5 or d >= today for d in dates):
        raise ValueError("Use completed weekday sessions only.")
    if any(row["split"] not in {"train", "validation", "test"} for row in rows):
        raise ValueError("Unknown split in date manifest.")
    return rows


def build_requests(config: dict, rows: list[dict], symbols: list[str]) -> list[dict]:
    if not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("Specify a nonempty, unique symbol list.")
    if any(not re.fullmatch(r"[A-Z][A-Z0-9.]{0,14}", s) or s == "ALL_SYMBOLS" for s in symbols):
        raise ValueError("Use explicit stock/ETF symbols; all-symbol requests are disabled.")
    if not set(symbols) <= set(config["symbols"]):
        raise ValueError("Add any newly selected instruments to configs/databento.json first.")
    if config["dataset"] != "XNAS.ITCH" or config["schema"] != "mbo":
        raise ValueError("This downloader is configured for XNAS.ITCH / mbo only.")
    zone = ZoneInfo(config["timezone"])
    start_time = time.fromisoformat(config["analysis_start"])
    end_time = time.fromisoformat(config["analysis_end"])
    download_end = time.fromisoformat(config["download_end"])
    if not start_time < end_time <= download_end or download_end.minute % 10:
        raise ValueError("Download end must cover analysis and align to a 10-minute boundary.")
    requests = []
    for row in rows:
        day = date.fromisoformat(row["trading_date"])
        for symbol in symbols:
            requests.append({
                "trading_date": day.isoformat(), "split": row["split"], "symbol": symbol,
                "dataset": config["dataset"], "schema": config["schema"],
                "start": utc_string(datetime.combine(day, time(0), UTC)),
                "end": utc_string(datetime.combine(day, download_end, zone)),
                "analysis_start": utc_string(datetime.combine(day, start_time, zone)),
                "analysis_end": utc_string(datetime.combine(day, end_time, zone)),
            })
    if not requests:
        raise ValueError("No selected requests.")
    return requests


def api_parameters(request: dict) -> dict:
    return {key: request[key] for key in ("dataset", "schema", "start", "end")} | {
        "symbols": [request["symbol"]], "stype_in": "raw_symbol",
    }


def raw_path(data_dir: Path, request: dict) -> Path:
    return (data_dir / "raw" / request["dataset"] / request["schema"] /
            request["symbol"] / (request["trading_date"] + ".dbn.zst"))


def cached_receipt(data_dir: Path, request: dict) -> dict | None:
    path = raw_path(data_dir, request)
    receipt_path = path.with_name(path.name + ".json")
    partial = path.with_name(path.name + ".partial")
    if partial.exists():
        raise ValueError(f"Incomplete download exists: {partial}. Resolve it before retrying; "
                         "streaming retries may incur another charge.")
    if not path.exists() and not receipt_path.exists():
        return None
    if not path.is_file() or not receipt_path.is_file():
        raise ValueError(f"File/receipt pair is incomplete: {path}. No automatic redownload.")
    receipt = json.loads(receipt_path.read_text())
    if (receipt.get("request") != api_parameters(request)
            or receipt.get("file_bytes") != path.stat().st_size
            or receipt.get("sha256") != checksum(path)):
        raise ValueError(f"Cache verification failed: {path}. No automatic redownload.")
    return receipt


def check_coverage(client, requests: list[dict], output_dir: Path) -> None:
    dataset = requests[0]["dataset"]
    available = client.metadata.get_dataset_range(dataset=dataset)
    dates = sorted({r["trading_date"] for r in requests})
    conditions = client.metadata.get_dataset_condition(
        dataset=dataset, start_date=dates[0], end_date=dates[-1],
    )
    schema_range = available.get("schema", {}).get(requests[0]["schema"])
    status_by_date = {row["date"]: row["condition"] for row in conditions}
    failures = []
    if not schema_range:
        failures.append("No MBO availability range was returned for this account.")
    else:
        start = datetime.fromisoformat(schema_range["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(schema_range["end"].replace("Z", "+00:00"))
        for request in requests:
            if (datetime.fromisoformat(request["start"].replace("Z", "+00:00")) < start
                    or datetime.fromisoformat(request["end"].replace("Z", "+00:00")) > end):
                failures.append(f'{request["trading_date"]}: outside MBO availability range')
    for day in dates:
        status = status_by_date.get(day, "not returned")
        if status != "available":
            failures.append(f"{day}: dataset condition is {status}")
    write_json(output_dir / "coverage.json", {
        "checked_at": utc_string(datetime.now(UTC)), "dataset": dataset,
        "available_range": available, "daily_conditions": conditions,
        "selected_dates": dates, "failures": sorted(set(failures)),
    })
    if failures:
        raise ValueError("Coverage check failed; calendar unchanged. " + "; ".join(sorted(set(failures))))


def quote_requests(client, requests: list[dict], data_dir: Path, *, client_factory=None, workers=8) -> dict:
    quotes = {}
    total_cost = Decimal("0")
    total_bytes = 0
    pending = []
    for index, request in enumerate(requests, 1):
        receipt = cached_receipt(data_dir, request)
        if receipt:
            quotes[index] = {"request": request, "cached": True, "cost_usd": "0", "billable_bytes": 0}
        else:
            pending.append((index, request))

    thread_state = threading.local()

    def quote_one(request):
        if client_factory is not None:
            if not hasattr(thread_state, "client"):
                thread_state.client = client_factory()
            local_client = thread_state.client
        else:
            local_client = client
        parameters = api_parameters(request)
        cost = Decimal(str(local_client.metadata.get_cost(**parameters)))
        size = int(local_client.metadata.get_billable_size(**parameters))
        records = int(local_client.metadata.get_record_count(**parameters))
        if not cost.is_finite() or cost < 0 or size <= 0 or records <= 0:
            raise ValueError(f'Invalid or empty quote for {request["symbol"]} {request["trading_date"]}.')
        return {"request": request, "cached": False, "cost_usd": str(cost),
                "billable_bytes": size, "estimated_records": records}

    # Each worker owns a separate SDK client/session. Paid downloads stay sequential.
    with ThreadPoolExecutor(max_workers=workers if client_factory else 1) as pool:
        futures = {pool.submit(quote_one, request): index for index, request in pending}
        for future in as_completed(futures):
            item = future.result()
            quotes[futures[future]] = item
            cost = Decimal(item["cost_usd"])
            size = item["billable_bytes"]
            total_cost += cost
            total_bytes += size
            request = item["request"]
            print(f'Quote {len(quotes)}/{len(requests)}: {request["symbol"]} {request["trading_date"]}: '
                  f'${cost:.4f}, {size / 1e9:.3f} GB uncompressed', flush=True)
    return {"quoted_at": utc_string(datetime.now(UTC)),
            "requests": [quotes[index] for index in sorted(quotes)],
            "remaining_cost_usd": str(total_cost), "remaining_billable_bytes": total_bytes}


@contextmanager
def download_lock(data_dir: Path):
    lock_path = data_dir / "raw" / ".fetch.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("Another downloader is already using this data directory.") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def download_quoted(client, quote: dict, data_dir: Path, max_cost: Decimal) -> None:
    if not max_cost.is_finite() or max_cost < 0:
        raise ValueError("Budget must be a finite, nonnegative USD amount.")
    if Decimal(quote["remaining_cost_usd"]) > max_cost:
        raise ValueError(f'Quote ${quote["remaining_cost_usd"]} exceeds budget ${max_cost}. '
                         "No download started.")
    for index, item in enumerate(quote["requests"], 1):
        request = item["request"]
        if cached_receipt(data_dir, request):
            print(f'Skip verified cache: {request["symbol"]} {request["trading_date"]}', flush=True)
            continue
        if item["cached"]:
            raise ValueError("Previously cached data disappeared. Obtain a fresh quote before downloading.")
        path = raw_path(data_dir, request)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".partial")
        print(f'Download {index}/{len(quote["requests"])}: {request["symbol"]} '
              f'{request["trading_date"]}', flush=True)
        store = client.timeseries.get_range(**api_parameters(request), path=partial)
        if not partial.is_file() or partial.stat().st_size == 0 or next(iter(store), None) is None:
            raise ValueError(f"Downloaded file is empty: {partial}")
        receipt = {
            "request": api_parameters(request), "trading_date": request["trading_date"],
            "split": request["split"], "analysis_start": request["analysis_start"],
            "analysis_end": request["analysis_end"], "file_bytes": partial.stat().st_size,
            "sha256": checksum(partial), "quoted_cost_usd": item["cost_usd"],
            "estimated_records": item["estimated_records"],
            "downloaded_at": utc_string(datetime.now(UTC)),
        }
        partial.replace(path)
        write_json(path.with_name(path.name + ".json"), receipt)
        print(f'Completed: {path} ({receipt["file_bytes"] / 1e6:.1f} MB compressed)', flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "quote", "download"])
    parser.add_argument("--config", type=Path, default=ROOT / "configs/databento.json")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--symbols", nargs="+")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--pilot", action="store_true", help="First training day, SPY by default")
    selection.add_argument("--dates", nargs="+", help="Explicit dates from the frozen manifest")
    parser.add_argument("--split", choices=["train", "validation", "test", "all"], default="all")
    parser.add_argument("--max-cost-usd", type=Decimal, help="Required for paid download; checked against a fresh quote")
    args = parser.parse_args(argv)
    if args.action == "download" and args.max_cost_usd is None:
        parser.error("download requires --max-cost-usd; run quote first")
    try:
        from dotenv import load_dotenv
    except ImportError:
        if args.action != "plan":
            raise ValueError("Install requirements-data.txt in the project virtual environment first.")
    else:
        load_dotenv(ROOT / ".env", override=False)
    config = json.loads(args.config.read_text())
    rows = read_dates(ROOT / config["date_manifest"])
    if args.pilot:
        rows = [next(row for row in rows if row["split"] == "train")]
    elif args.dates:
        if not set(args.dates) <= {row["trading_date"] for row in rows}:
            raise ValueError("Requested dates must belong to the frozen manifest.")
        rows = [row for row in rows if row["trading_date"] in args.dates]
    if args.split != "all":
        rows = [row for row in rows if row["split"] == args.split]
    symbols = args.symbols if args.symbols is not None else (["SPY"] if args.pilot else config["symbols"])
    requests = build_requests(config, rows, symbols)
    data_dir = args.data_dir or Path(os.environ.get("SAF_DATA_DIR", "data"))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    output_dir = data_dir / "metadata" / "databento"
    write_json(output_dir / "request_plan.json", {
        "created_at": utc_string(datetime.now(UTC)), "manifest_sha256": checksum(ROOT / config["date_manifest"]),
        "requests": requests,
    })
    print(f'{len(requests)} requests; symbols: {", ".join(symbols)}; '
          f'{rows[0]["trading_date"]} through {rows[-1]["trading_date"]}.', flush=True)
    if args.action == "plan":
        print(f"Offline plan: {output_dir / 'request_plan.json'}; no API calls made.")
        return 0
    if not os.environ.get("DATABENTO_API_KEY", "").strip():
        raise ValueError("DATABENTO_API_KEY is missing. Set it in the project .env or environment; "
                         "do not paste the key into chat or commit it to Git.")
    import databento as db
    client = db.Historical(os.environ["DATABENTO_API_KEY"])

    def preflight() -> dict:
        check_coverage(client, requests, output_dir)
        quote = quote_requests(client, requests, data_dir,
                               client_factory=lambda: db.Historical(os.environ["DATABENTO_API_KEY"]))
        write_json(output_dir / "quote.json", quote)
        print(f'Remaining quote: ${Decimal(quote["remaining_cost_usd"]):.4f}; '
              f'{quote["remaining_billable_bytes"] / 1e9:.3f} GB uncompressed. '
              'Account credits are not subtracted.', flush=True)
        return quote

    if args.action == "quote":
        preflight()
    else:
        with download_lock(data_dir):
            quote = preflight()
            download_quoted(client, quote, data_dir, args.max_cost_usd)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted. Any partial file remains unverified; no automatic retry.", file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        message = str(error)
        key = os.environ.get("DATABENTO_API_KEY", "")
        if key:
            message = message.replace(key, "<redacted>")
        print(f"Error: {message}", file=sys.stderr)
        sys.exit(1)
