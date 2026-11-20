"""Versioned model-facing table types; no provider account or calendar dependencies."""

from .ingest import RAW_TYPES

TABLE_TYPES = {
    "events": {
        "event_id": "string", "state_before_id": "string", "state_after_id": "string",
        "ts_event_ns": "uint64", "t_rel_s": "float64", "event_type": "string",
        "event_type_id": "uint8", "side": "string", "price_int": "int64", "size": "uint32",
        "order_id": "uint64", "placement_distance_ticks": "int32", "source_record_index": "uint64",
        "book_update_record_index": "uint64", "source_sequence": "uint32",
    },
    "quotes": {
        "state_id": "string", "ts_event_ns": "uint64", "ts_recv_ns": "uint64", "epoch": "uint32",
        "bid_price_int": "int64", "ask_price_int": "int64", "bid_size": "int64", "ask_size": "int64",
        "mid_price_x2": "int64", "book_valid": "bool",
    },
    "queues": {
        "state_id": "string", "side": "string", "level": "uint32", "price_int": "int64",
        "size": "int64", "order_count": "uint32",
    },
    "exposure": {
        "state_id": "string", "start_ns": "uint64", "end_ns": "uint64", "dt_s": "float64",
        "bid_size": "int64", "ask_size": "int64",
    },
    "features": {
        "forecast_id": "string", "state_id": "string", "forecast_ts_ns": "uint64", "t_rel_s": "float64",
        "bid_price_int": "int64", "ask_price_int": "int64", "bid_size": "int64", "ask_size": "int64",
        "mid_price_x2": "int64", "spread_ticks": "int32", "imbalance": "float64", "feature_valid": "bool",
    },
    "forecasts": {
        "forecast_id": "string", "y_up": "uint8", "next_move_ts_ns": "uint64",
        "censored": "bool", "censor_reason": "string",
    },
}
NULLABLE = {
    "events": {"placement_distance_ticks"},
    "quotes": {"bid_price_int", "ask_price_int", "mid_price_x2"},
    "features": {"bid_price_int", "ask_price_int", "mid_price_x2", "spread_ticks", "imbalance"},
    "forecasts": {"y_up", "next_move_ts_ns"},
}


def catalog():
    return {
        "contract_version": "1.0", "raw_integer_csv": RAW_TYPES,
        "tables": {name: {field: {"type": dtype, "nullable": field in NULLABLE.get(name, set())}
                          for field, dtype in fields.items()} for name, fields in TABLE_TYPES.items()},
        "prediction_columns": {"forecast_id": "string", "model": "string", "run_id": "string", "p_up": "float64"},
    }


def validate_tables(tables):
    for name, fields in TABLE_TYPES.items():
        for row in tables[name]:
            if set(row) != set(fields):
                raise ValueError(f"{name}: unexpected columns.")
            for field, dtype in fields.items():
                value = row[field]
                if value is None:
                    if field not in NULLABLE.get(name, set()):
                        raise ValueError(f"{name}.{field}: unexpected null.")
                elif dtype == "bool" and type(value) is not bool:
                    raise ValueError(f"{name}.{field}: expected bool.")
                elif dtype == "string" and not isinstance(value, str):
                    raise ValueError(f"{name}.{field}: expected string.")
                elif dtype.startswith(("uint", "int")):
                    if type(value) is not int:
                        raise ValueError(f"{name}.{field}: expected integer.")
                    bits = int(dtype.removeprefix("uint").removeprefix("int"))
                    lower = 0 if dtype.startswith("uint") else -(2**(bits-1))
                    upper = 2**bits-1 if dtype.startswith("uint") else 2**(bits-1)-1
                    if not lower <= value <= upper:
                        raise ValueError(f"{name}.{field}: outside {dtype}.")
                elif dtype == "float64":
                    import math
                    if not isinstance(value, (int, float)) or not math.isfinite(value):
                        raise ValueError(f"{name}.{field}: expected finite number.")
