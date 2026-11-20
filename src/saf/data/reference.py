"""Readable reference for one complete XNAS instrument-session, not a production feed handler."""

from bisect import bisect_right
from collections import defaultdict
from .ingest import F_LAST, PRICE_SCALE, UNDEF_PRICE

STREAM_ORDER = ["limit_bid", "limit_ask", "execution_bid", "execution_ask", "cancel_bid", "cancel_ask"]


def replay(records, metadata):
    if metadata.get("price_scale") != PRICE_SCALE or metadata.get("event_clock") != "ts_event":
        raise ValueError("Contract 1.0 uses price scale 1e9 and the documented ts_event clock.")
    start, end = metadata["analysis_start_ns"], metadata["analysis_end_ns"]
    if start >= end or metadata["tick_size_int"] <= 0:
        raise ValueError("Invalid observation window or tick size.")
    identity = (metadata["publisher_id"], metadata["instrument_id"])
    session = metadata["session_id"]
    orders, quotes, queues, events = {}, [], [], []
    epoch, initialized, previous_index = 0, False, -1
    group = []

    def snapshot():
        levels = {"B": defaultdict(lambda: [0, 0]), "A": defaultdict(lambda: [0, 0])}
        for side, price, size in orders.values():
            levels[side][price][0] += size
            levels[side][price][1] += 1
        bid = max(levels["B"], default=None)
        ask = min(levels["A"], default=None)
        if bid is not None and ask is not None and bid > ask:
            raise ValueError("Crossed book at a completed event boundary.")
        valid = initialized and bid is not None and ask is not None and bid < ask
        return levels, {
            "bid_price_int": bid, "ask_price_int": ask,
            "bid_size": levels["B"][bid][0] if bid is not None else 0,
            "ask_size": levels["A"][ask][0] if ask is not None else 0,
            "mid_price_x2": bid + ask if valid else None, "book_valid": valid,
        }

    for record in records:
        if record.source_index <= previous_index:
            raise ValueError("Source ordering must be preserved.")
        previous_index = record.source_index
        if (record.publisher_id, record.instrument_id) != identity:
            raise ValueError("Partition by publisher/instrument/session before reference replay.")
        if record.flags & (4 | 16 | 64):
            raise ValueError("Bad-book, MBP, and top-of-book records need a separate recovery/adapter policy.")
        group.append(record)
        if not record.flags & F_LAST:
            continue
        if len({r.sequence for r in group}) != 1 or len({r.channel_id for r in group}) != 1:
            raise ValueError("The minimal XNAS adapter expects one source sequence/channel per atomic group.")
        ts = group[-1].ts_event
        if len({r.ts_event for r in group}) != 1 or (quotes and ts < quotes[-1]["ts_event_ns"]):
            raise ValueError("Reference replay requires nondecreasing completed-event exchange timestamps.")
        state_id = f"{session}/s{record.source_index:04d}"
        before_id = quotes[-1]["state_id"] if quotes else ""
        _, before = snapshot()
        eligible = (start <= ts < end and before["book_valid"]
                    and not any(r.action == "R" or r.flags & 32 for r in group))
        fills = {}
        for r in group:
            if r.action == "F":
                key = (r.sequence, r.order_id, r.side)
                if key in fills:
                    raise ValueError("Multiple fills for one order/sequence need an explicit aggregation policy.")
                fills[key] = r

        def emit(kind, r, price, size, source, distance=None):
            if eligible:
                event_type = kind + ("_bid" if r.side == "B" else "_ask")
                events.append({
                    "event_id": f"{session}/e{r.source_index:04d}",
                    "state_before_id": before_id, "state_after_id": state_id,
                    "ts_event_ns": ts, "t_rel_s": (ts - start) / 10**9,
                    "event_type": event_type, "event_type_id": STREAM_ORDER.index(event_type),
                    "side": r.side, "price_int": price, "size": size, "order_id": r.order_id,
                    "placement_distance_ticks": distance, "source_record_index": source,
                    "book_update_record_index": r.source_index, "source_sequence": r.sequence,
                })

        for r in group:
            if r.action == "R":
                orders.clear()
                initialized = True
                epoch += 1
            elif r.action == "A":
                if not initialized or r.side not in {"B", "A"} or r.order_id in orders:
                    raise ValueError("Add before reset, duplicate order, or invalid resting side.")
                if r.size <= 0 or r.price <= 0 or r.price == UNDEF_PRICE or r.price % metadata["tick_size_int"]:
                    raise ValueError("Invalid addition size/price/tick grid.")
                best = before["bid_price_int"] if r.side == "B" else before["ask_price_int"]
                distance = None if best is None else ((best-r.price) if r.side == "B" else (r.price-best)) // metadata["tick_size_int"]
                orders[r.order_id] = [r.side, r.price, r.size]
                emit("limit", r, r.price, r.size, r.source_index, distance)
            elif r.action == "C":
                if r.order_id not in orders:
                    raise ValueError("Unknown order cancellation; initialization or data recovery is incomplete.")
                side, price, size = orders[r.order_id]
                if side != r.side or price != r.price or not 0 < r.size <= size:
                    raise ValueError("Cancellation does not match the resting order.")
                fill = fills.pop((r.sequence, r.order_id, r.side), None)
                if fill and (fill.size != r.size or fill.price != price or fill.source_index >= r.source_index):
                    raise ValueError("Fill/cancel mismatch; execution linkage cannot be inferred safely.")
                emit("execution" if fill else "cancel", r, price, r.size,
                     fill.source_index if fill else r.source_index)
                if size == r.size:
                    del orders[r.order_id]
                else:
                    orders[r.order_id][2] -= r.size
            elif r.action == "M":
                raise ValueError("Generic M is outside this XNAS example; XNAS replacements use C then A.")
            elif r.action not in {"T", "F", "N"}:
                raise ValueError("Unsupported action.")
        if fills:
            raise ValueError("Unlinked fill: its cancellation must occur in the same completed source group.")
        levels, state = snapshot()
        quote = {"state_id": state_id, "ts_event_ns": ts, "ts_recv_ns": record.ts_recv,
                 "epoch": epoch, **state}
        quotes.append(quote)
        for side in ("B", "A"):
            for level, price in enumerate(sorted(levels[side], reverse=side == "B"), 1):
                size, count = levels[side][price]
                queues.append({"state_id": state_id, "side": side, "level": level,
                               "price_int": price, "size": size, "order_count": count})
        group = []
    if group:
        raise ValueError("Incomplete atomic group at EOF; F_LAST is missing.")
    if not quotes:
        raise ValueError("No completed book states.")
    if quotes[-1]["ts_event_ns"] < end:
        raise ValueError("Input ends before the declared observation end; do not assume missing data is inactivity.")

    exposure = []
    for index, state in enumerate(quotes):
        left = max(start, state["ts_event_ns"])
        right = min(end, quotes[index+1]["ts_event_ns"] if index+1 < len(quotes) else end)
        if state["book_valid"] and left < right:
            exposure.append({"state_id": state["state_id"], "start_ns": left, "end_ns": right,
                             "dt_s": (right-left)/10**9, "bid_size": state["bid_size"],
                             "ask_size": state["ask_size"]})

    features, forecasts = [], []
    times = [q["ts_event_ns"] for q in quotes]
    step, offset = metadata["forecast_step_ns"], metadata["forecast_offset_ns"]
    if step <= 0 or not 0 <= offset < step:
        raise ValueError("Invalid forecast grid.")
    for index, ts in enumerate(range(start+offset, end, step)):
        asof = bisect_right(times, ts) - 1
        if asof < 0:
            raise ValueError("Forecast grid starts before a completed initial book state.")
        state = quotes[asof]
        forecast_id = f"{session}/f{index:04d}"
        valid = state["book_valid"]
        features.append({
            "forecast_id": forecast_id, "state_id": state["state_id"], "forecast_ts_ns": ts,
            "t_rel_s": (ts-start)/10**9, "bid_price_int": state["bid_price_int"],
            "ask_price_int": state["ask_price_int"], "bid_size": state["bid_size"],
            "ask_size": state["ask_size"], "mid_price_x2": state["mid_price_x2"],
            "spread_ticks": (state["ask_price_int"]-state["bid_price_int"]) // metadata["tick_size_int"] if valid else None,
            "imbalance": (state["bid_size"]-state["ask_size"]) / (state["bid_size"]+state["ask_size"]) if valid else None,
            "feature_valid": valid,
        })
        label = {"forecast_id": forecast_id, "y_up": None, "next_move_ts_ns": None,
                 "censored": True, "censor_reason": "analysis_end" if valid else "invalid_at_forecast"}
        if valid:
            for future in quotes[asof+1:]:
                if future["ts_event_ns"] >= end:
                    break
                if future["epoch"] != state["epoch"] or not future["book_valid"]:
                    label["censor_reason"] = "invalid_interval"
                    break
                if future["mid_price_x2"] != state["mid_price_x2"]:
                    label.update(y_up=int(future["mid_price_x2"] > state["mid_price_x2"]),
                                 next_move_ts_ns=future["ts_event_ns"], censored=False, censor_reason="")
                    break
        forecasts.append(label)
    return {"events": events, "quotes": quotes, "queues": queues,
            "exposure": exposure, "features": features, "forecasts": forecasts}


def model_inputs(tables, metadata):
    streams = {name: [r["t_rel_s"] for r in tables["events"] if r["event_type"] == name] for name in STREAM_ORDER}
    exposure = sum(r["dt_s"] for r in tables["exposure"])
    counts = [len(streams[name]) for name in STREAM_ORDER]
    return {
        "contract_version": metadata["contract_version"], "synthetic": metadata["synthetic"],
        "stream_order": STREAM_ORDER, "observation_end_s": (metadata["analysis_end_ns"]-metadata["analysis_start_ns"])/10**9,
        "event_times_s": streams, "counts": counts, "valid_exposure_s": exposure,
        "illustrative_poisson_mle_per_s": [count/exposure for count in counts] if exposure else None,
        "positive_placement_distances_ticks": [r["placement_distance_ticks"] for r in tables["events"]
                                              if r["event_type"].startswith("limit_") and (r["placement_distance_ticks"] or 0) > 0],
        "logistic_features": ["imbalance"], "queue_reactive_state": ["bid_size", "ask_size"],
        "labeled_forecasts": sum(not r["censored"] for r in tables["forecasts"]),
    }
