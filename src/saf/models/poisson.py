from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from numbers import Integral


STREAMS = (
    "bid_add",
    "ask_add",
    "bid_execution",
    "ask_execution",
    "bid_cancel",
    "ask_cancel",
)


@dataclass(frozen=True)
class PoissonBin:
    counts: dict[str, int]
    exposure_seconds: float
    rates_per_second: dict[str, float] | None


def fit_poisson(
    counts_by_bin: Mapping[str, Mapping[str, int]],
    exposure_seconds_by_bin: Mapping[str, float],
) -> dict[str, PoissonBin]:
    """Fit six Poisson rates per time-of-day bin for one asset.

    Inputs must contain training data only. Counts and exposure must
    cover the same valid intervals and use the same event-selection policy.
    """
    extra_bins = set(counts_by_bin) - set(exposure_seconds_by_bin)
    if extra_bins:
        raise ValueError(f"Missing exposure for bins: {sorted(extra_bins)}")

    fitted: dict[str, PoissonBin] = {}

    for time_bin, raw_seconds in exposure_seconds_by_bin.items():
        seconds = float(raw_seconds)
        if not isfinite(seconds) or seconds < 0:
            raise ValueError("Exposure must be finite and nonnegative.")

        raw_counts = counts_by_bin.get(time_bin, {})
        unknown_streams = set(raw_counts) - set(STREAMS)
        if unknown_streams:
            raise ValueError(f"Unknown event streams: {sorted(unknown_streams)}")

        counts: dict[str, int] = {}
        for stream in STREAMS:
            count = raw_counts.get(stream, 0)
            if (
                isinstance(count, bool)
                or not isinstance(count, Integral)
                or count < 0
            ):
                raise ValueError("Event counts must be nonnegative integers.")
            counts[stream] = int(count)

        if seconds == 0:
            if any(counts.values()):
                raise ValueError("Positive counts require positive exposure.")
            rates = None
        else:
            rates = {
                stream: count / seconds
                for stream, count in counts.items()
            }

        fitted[time_bin] = PoissonBin(
            counts=counts,
            exposure_seconds=seconds,
            rates_per_second=rates,
        )

    if not any(
        result.rates_per_second is not None
        for result in fitted.values()
    ):
        raise ValueError("At least one bin needs positive training exposure.")

    return fitted


if __name__ == "__main__":
    # Synthetic counts pooled across two training days.
    counts_by_bin = {
        "09:45-10:00": {
            "bid_add": 900,
            "ask_add": 720,
            "bid_execution": 180,
            "ask_execution": 240,
            "bid_cancel": 450,
            "ask_cancel": 360,
        },
    }

    # Two fully observed 15-minute intervals: 2 * 15 * 60 seconds.
    exposure_seconds_by_bin = {
        "09:45-10:00": 1800.0,
    }

    model = fit_poisson(counts_by_bin, exposure_seconds_by_bin)

    for time_bin, result in model.items():
        print(time_bin)
        print("counts:", result.counts)
        print("exposure_seconds:", result.exposure_seconds)
        print("rates_per_second:", result.rates_per_second)
