from collections.abc import Sequence
from math import isfinite

from sklearn.linear_model import LogisticRegression

from saf.labels import Label


def _imbalance(feature: dict[str, int | float]) -> float:
    value = float(feature["imbalance"])
    if not isfinite(value) or not -1 <= value <= 1:
        raise ValueError("Imbalance must be finite and between -1 and 1.")
    return value


def fit_imbalance(
    features: Sequence[dict[str, int | float] | None],
    labels: Sequence[Label],
    *,
    C: float = 1.0,
) -> LogisticRegression:
    """Fit a logistic benchmark using training rows only."""
    if len(features) != len(labels):
        raise ValueError("Features and labels must have the same length.")

    X: list[list[float]] = []
    y: list[int] = []

    for feature, label in zip(features, labels, strict=True):
        if feature is None or label.y_up is None:
            continue

        if label.y_up not in {0, 1}:
            raise ValueError("A training label must be 0 or 1.")

        X.append([_imbalance(feature)])
        y.append(label.y_up)

    if set(y) != {0, 1}:
        raise ValueError("Training data must contain both upward and downward moves.")

    model = LogisticRegression(C=C, max_iter=1000)
    model.fit(X, y)
    return model


def predict_up(
    model: LogisticRegression,
    features: Sequence[dict[str, int | float] | None],
) -> list[float | None]:
    """Return upward probabilities, preserving missing-row positions."""
    probabilities: list[float | None] = [None] * len(features)
    indices: list[int] = []
    X: list[list[float]] = []

    for i, feature in enumerate(features):
        if feature is not None:
            indices.append(i)
            X.append([_imbalance(feature)])

    if not X:
        return probabilities

    up_column = list(model.classes_).index(1)
    predictions = model.predict_proba(X)[:, up_column]

    for i, probability in zip(indices, predictions, strict=True):
        probabilities[i] = float(probability)

    return probabilities


if __name__ == "__main__":
    from saf.features import build_features
    from saf.labels import build_labels

    # Synthetic training rows: bid price, bid size, ask size.
    rows = [
        (100_000_000_000, 100, 50),
        (100_010_000_000, 50, 100),
        (100_000_000_000, 200, 50),
        (100_020_000_000, 50, 200),
        (100_000_000_000, 100, 50),
    ]
    quotes = [
        {
            "bid_price": bid,
            "ask_price": bid + 20_000_000,
            "bid_size": bid_size,
            "ask_size": ask_size,
        }
        for bid, bid_size, ask_size in rows
    ]

    train_features = [build_features(quote) for quote in quotes]
    train_labels = build_labels(quotes)
    model = fit_imbalance(train_features, train_labels)

    current_features = [
        {"imbalance": -0.5},
        {"imbalance": 0.0},
        {"imbalance": 0.5},
    ]
    print("p_up:", predict_up(model, current_features))
