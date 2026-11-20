import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fetch_databento", ROOT / "scripts/fetch_databento.py")
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)
CONFIG = json.loads((ROOT / "configs/databento.json").read_text())


def request(day="2026-08-27"):
    return fetch.build_requests(CONFIG, [{"trading_date": day, "split": "train"}], ["SPY"])[0]


def quoted(item):
    return {"remaining_cost_usd": "2", "requests": [{
        "request": item, "cached": False, "cost_usd": "2", "estimated_records": 1,
    }]}


class DownloadTests(unittest.TestCase):
    def test_concurrent_metadata_quotes_keep_request_order_and_exact_totals(self):
        requests = fetch.build_requests(CONFIG, [{"trading_date": "2026-08-27", "split": "train"}],
                                        ["SPY", "QQQ"])

        def make_client():
            return SimpleNamespace(metadata=SimpleNamespace(
                get_cost=lambda **kwargs: "0.10" if kwargs["symbols"] == ["SPY"] else "0.20",
                get_billable_size=lambda **kwargs: 100,
                get_record_count=lambda **kwargs: 2,
            ))

        with TemporaryDirectory() as directory:
            quote = fetch.quote_requests(None, requests, Path(directory),
                                         client_factory=make_client, workers=2)
        self.assertEqual([item["request"]["symbol"] for item in quote["requests"]], ["SPY", "QQQ"])
        self.assertEqual(Decimal(quote["remaining_cost_usd"]), Decimal("0.30"))
        self.assertEqual(quote["remaining_billable_bytes"], 200)

    def test_windows_keep_initialization_and_handle_daylight_saving(self):
        summer = request("2026-10-30")
        winter = request("2026-11-02")
        self.assertEqual(summer["start"], "2026-10-30T00:00:00Z")
        self.assertEqual(summer["analysis_start"], "2026-10-30T13:45:00Z")
        self.assertEqual(summer["end"], "2026-10-30T19:50:00Z")
        self.assertEqual(winter["analysis_start"], "2026-11-02T14:45:00Z")
        self.assertEqual(winter["analysis_end"], "2026-11-02T20:45:00Z")
        self.assertEqual(winter["end"], "2026-11-02T20:50:00Z")

    def test_accidental_all_symbol_requests_are_rejected(self):
        rows = [{"trading_date": "2026-08-27", "split": "train"}]
        for symbols in [[], ["ALL_SYMBOLS"], ["SPY", "SPY"], ["NVDA"], ["../SPY"]]:
            with self.subTest(symbols=symbols), self.assertRaises(ValueError):
                fetch.build_requests(CONFIG, rows, symbols)

    def test_budget_rejection_occurs_before_paid_endpoint(self):
        client = SimpleNamespace(timeseries=SimpleNamespace(get_range=Mock()))
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "exceeds budget"):
                fetch.download_quoted(client, quoted(request()), Path(directory), Decimal("1"))
        client.timeseries.get_range.assert_not_called()

    def test_completed_cache_is_reused_and_corruption_is_not_redownloaded(self):
        def stream(**parameters):
            parameters["path"].write_bytes(b"synthetic fixture bytes")
            return iter([object()])

        client = SimpleNamespace(timeseries=SimpleNamespace(get_range=Mock(side_effect=stream)))
        with TemporaryDirectory() as directory:
            data_dir = Path(directory)
            item = request()
            fetch.download_quoted(client, quoted(item), data_dir, Decimal("2"))
            fetch.download_quoted(client, quoted(item), data_dir, Decimal("2"))
            self.assertEqual(client.timeseries.get_range.call_count, 1)
            self.assertIsNotNone(fetch.cached_receipt(data_dir, item))
            fetch.raw_path(data_dir, item).write_bytes(b"damaged")
            with self.assertRaisesRegex(ValueError, "Cache verification failed"):
                fetch.download_quoted(client, quoted(item), data_dir, Decimal("2"))
            self.assertEqual(client.timeseries.get_range.call_count, 1)

    def test_failed_stream_is_not_marked_complete_or_automatically_retried(self):
        def stream(**parameters):
            parameters["path"].write_bytes(b"partial")
            raise ConnectionError("Synthetic interrupted stream")

        client = SimpleNamespace(timeseries=SimpleNamespace(get_range=Mock(side_effect=stream)))
        with TemporaryDirectory() as directory:
            data_dir = Path(directory)
            item = request()
            with self.assertRaises(ConnectionError):
                fetch.download_quoted(client, quoted(item), data_dir, Decimal("2"))
            self.assertFalse(fetch.raw_path(data_dir, item).exists())
            with self.assertRaisesRegex(ValueError, "Incomplete download"):
                fetch.download_quoted(client, quoted(item), data_dir, Decimal("2"))
            self.assertEqual(client.timeseries.get_range.call_count, 1)

    def test_degraded_coverage_blocks_before_download_and_saves_evidence(self):
        client = SimpleNamespace(metadata=SimpleNamespace(
            get_dataset_range=Mock(return_value={"schema": {"mbo": {
                "start": "2018-05-01T00:00:00Z", "end": "2026-11-20T00:00:00Z",
            }}}),
            get_dataset_condition=Mock(return_value=[{
                "date": "2026-08-27", "condition": "degraded", "last_modified_date": None,
            }]),
        ))
        with TemporaryDirectory() as directory:
            output = Path(directory)
            with self.assertRaisesRegex(ValueError, "degraded"):
                fetch.check_coverage(client, [request()], output)
            self.assertTrue(json.loads((output / "coverage.json").read_text())["failures"])


if __name__ == "__main__":
    unittest.main()
