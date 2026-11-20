import csv
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from saf.data.ingest import read_mbo, record_from_values, RAW_TYPES
from saf.data.reference import replay, model_inputs, STREAM_ORDER
from saf.data.schema import validate_tables, TABLE_TYPES

spec = importlib.util.spec_from_file_location("run_minimal_example", ROOT / "scripts/run_minimal_example.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class MinimalExampleTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / "examples/minimal"
        self.metadata = json.loads((self.directory / "metadata.json").read_text())
        self.records = list(read_mbo(self.directory / "mbo.csv"))
        self.tables = replay(self.records, self.metadata)

    def test_hand_calculated_stream_counts_and_exposure(self):
        self.assertEqual(len(self.records), 22)
        self.assertEqual(len(self.tables["quotes"]), 18)
        self.assertEqual(len(self.tables["events"]), 11)
        inputs = model_inputs(self.tables, self.metadata)
        self.assertEqual(inputs["counts"], [3, 2, 1, 1, 2, 2])
        self.assertEqual(inputs["valid_exposure_s"], 12.0)
        self.assertEqual(inputs["event_times_s"], {
            "limit_bid": [0.1, 8.0, 10.0], "limit_ask": [1.0, 9.0],
            "execution_bid": [4.0], "execution_ask": [5.0],
            "cancel_bid": [2.0, 7.0], "cancel_ask": [3.0, 6.0],
        })

    def test_trade_fill_cancel_is_one_execution_and_one_book_reduction(self):
        executions = [r for r in self.tables["events"] if r["event_type"].startswith("execution")]
        self.assertEqual([(r["event_type"], r["size"]) for r in executions],
                         [("execution_bid", 6), ("execution_ask", 10)])
        self.assertEqual([(r["source_record_index"], r["book_update_record_index"]) for r in executions],
                         [(10, 11), (13, 14)])
        states = {r["state_id"].split("/")[-1]: r for r in self.tables["quotes"]}
        self.assertEqual((states["s0011"]["bid_size"], states["s0011"]["ask_size"]), (5, 18))
        self.assertEqual((states["s0014"]["bid_size"], states["s0014"]["ask_size"]), (5, 8))
        self.assertEqual(sum(r["size"] for r in self.tables["events"] if r["event_type"].startswith("cancel")), 19)

    def test_hand_calculated_features_labels_and_censoring(self):
        features = self.tables["features"]
        self.assertEqual((features[0]["bid_size"], features[0]["ask_size"]), (15, 12))
        self.assertAlmostEqual(features[0]["imbalance"], 1/9)
        self.assertEqual(features[0]["mid_price_x2"], 200020000000)
        labels = self.tables["forecasts"]
        self.assertEqual([r["y_up"] for r in labels], [1, 1, 1, 1, 1, 1, 0, 1, None, None, None, None])
        self.assertTrue(all(r["censor_reason"] == "analysis_end" for r in labels[-4:]))
        self.assertTrue(all(r["next_move_ts_ns"] is None for r in labels[-4:]))

    def test_signed_placement_distances_use_pre_event_quote(self):
        limits = [r for r in self.tables["events"] if r["event_type"].startswith("limit")]
        self.assertEqual([r["placement_distance_ticks"] for r in limits], [0, 0, -1, 1, 2])
        self.assertEqual(model_inputs(self.tables, self.metadata)["positive_placement_distances_ticks"], [1, 2])

    def test_nonprintable_fill_without_a_trade_still_counts_once(self):
        records = [record for record in self.records if record.source_index != 9]
        records = [replace(record, source_index=index) for index, record in enumerate(records)]
        result = replay(records, self.metadata)
        self.assertEqual(model_inputs(result, self.metadata)["counts"], [3, 2, 1, 1, 2, 2])

    def test_different_clock_or_price_units_require_an_explicit_adapter(self):
        for changed in [dict(self.metadata, price_scale=100), dict(self.metadata, event_clock='ts_recv')]:
            with self.assertRaisesRegex(ValueError, 'price scale'):
                replay(self.records, changed)

    def test_changing_future_data_changes_labels_but_not_earlier_features(self):
        changed = self.records.copy()
        changed[15] = replace(changed[15], size=7)
        result = replay(changed, self.metadata)
        self.assertEqual(self.tables["features"][:6], result["features"][:6])
        self.assertEqual(self.tables["forecasts"][0]["y_up"], 1)
        self.assertEqual(result["forecasts"][0]["y_up"], 0)

    def test_cannot_label_or_compute_exposure_past_truncated_input(self):
        with self.assertRaisesRegex(ValueError, "observation end"):
            replay(self.records[:-1], self.metadata)

    def test_incomplete_group_and_mismatched_execution_fail(self):
        broken = self.records.copy()
        broken[-1] = replace(broken[-1], flags=0)
        with self.assertRaisesRegex(ValueError, "F_LAST"):
            replay(broken, self.metadata)
        broken = self.records.copy()
        broken[11] = replace(broken[11], size=5)
        with self.assertRaisesRegex(ValueError, "Fill/cancel mismatch"):
            replay(broken, self.metadata)

    def test_atomic_replacement_does_not_expose_a_transient_empty_bid(self):
        original = self.records[17]
        cancel = replace(original, action="C", order_id=102, price=99990000000, size=20, flags=0)
        replacement = [*self.records[:17], cancel, original, *self.records[18:]]
        replacement = [replace(r, source_index=i) for i, r in enumerate(replacement)]
        result = replay(replacement, self.metadata)
        self.assertEqual(len(result["quotes"]), 18)
        self.assertEqual(result["forecasts"][7]["y_up"], 1)
        self.assertFalse(result["forecasts"][7]["censored"])

    def test_duration_is_configurable_without_sixty_day_assumptions(self):
        shorter = dict(self.metadata, analysis_end_ns=self.metadata["analysis_start_ns"] + 10*10**9)
        result = replay(self.records, shorter)
        self.assertEqual(len(result["forecasts"]), 10)
        self.assertEqual(sum(r["dt_s"] for r in result["exposure"]), 10.0)

    def test_integer_precision_and_float_rejection(self):
        self.assertEqual(self.records[0].ts_event, self.metadata["analysis_start_ns"] - 10**9)
        values = {name: getattr(self.records[1], name) for name in RAW_TYPES}
        values["order_id"] = 2**63 + 123
        self.assertEqual(record_from_values(values, 1).order_id, 2**63 + 123)
        values["ts_event"] = float(values["ts_event"])
        with self.assertRaisesRegex(ValueError, "floating-point"):
            record_from_values(values, 1)

    def test_reference_outputs_match_the_checked_in_contract(self):
        validate_tables(self.tables)
        with TemporaryDirectory() as directory:
            out = Path(directory)
            inputs = model_inputs(self.tables, self.metadata)
            runner.write_outputs(out, self.tables, inputs, self.metadata)
            for name in TABLE_TYPES:
                self.assertEqual((out / (name + ".csv")).read_text(),
                                 (self.directory / "expected" / (name + ".csv")).read_text())

    @unittest.skipUnless(importlib.util.find_spec("databento") and importlib.util.find_spec("databento_dbn"),
                         "Optional SDK is not installed")
    def test_real_dbn_encoding_round_trip_uses_no_network(self):
        import databento_dbn as dbn
        import zstandard
        action = {name: getattr(dbn.Action, label) for name, label in
                  {"A": "ADD", "C": "CANCEL", "R": "CLEAR", "T": "TRADE", "F": "FILL", "N": "NONE"}.items()}
        side = {"A": dbn.Side.ASK, "B": dbn.Side.BID, "N": dbn.Side.NONE}
        metadata = dbn.Metadata(dataset="XNAS.ITCH", start=self.records[0].ts_event,
                                stype_in=dbn.SType.RAW_SYMBOL, stype_out=dbn.SType.INSTRUMENT_ID,
                                schema=dbn.Schema.MBO, symbols=["DEMO"])
        binary = bytes(metadata)
        for record in self.records:
            values = {name: getattr(record, name) for name in RAW_TYPES if name != "rtype"}
            values.update(action=action[record.action], side=side[record.side])
            binary += bytes(dbn.MBOMsg(**values))
        with TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.dbn.zst"
            path.write_bytes(zstandard.ZstdCompressor().compress(binary))
            decoded = list(read_mbo(path))
        self.assertEqual(decoded, self.records)
        self.assertEqual(replay(decoded, self.metadata), self.tables)

    @unittest.skipUnless(importlib.util.find_spec("pyarrow"), "Optional pyarrow is not installed")
    def test_parquet_preserves_nanoseconds_nullable_integers_and_booleans(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        with TemporaryDirectory() as directory:
            out = Path(directory)
            runner.write_outputs(out, self.tables, model_inputs(self.tables, self.metadata), self.metadata, True)
            labels = pq.read_table(out / "forecasts.parquet")
            quotes = pq.read_table(out / "quotes.parquet")
            self.assertEqual(labels.schema.field("y_up").type, pa.uint8())
            self.assertEqual(labels["y_up"].null_count, 4)
            self.assertEqual(quotes.schema.field("ts_event_ns").type, pa.uint64())
            self.assertEqual(quotes.to_pylist(), self.tables["quotes"])


if __name__ == "__main__":
    unittest.main()
