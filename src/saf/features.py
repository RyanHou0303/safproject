PRICE_SCALE = 1_000_000_000

"""
quote = {
    "bid_price": 100_000_000_000,  # bid1：$100.00
    "bid_size": 100,             # bid1#：100
    "ask_price": 100_020_000_000,  # ask1：$100.02
    "ask_size": 50,              # ask1：50
}
"""
def build_features(
    quote: dict[str, int] | None,
):
    if quote is None:
        return None

    bid_price = quote["bid_price"]
    ask_price = quote["ask_price"]
    bid_size = quote["bid_size"]
    ask_size = quote["ask_size"]

    return {
        "bid_size": bid_size,
        "ask_size": ask_size,
        "spread_dollars": (ask_price - bid_price) / PRICE_SCALE,
        "mid_price_dollars": (bid_price + ask_price) / (2 * PRICE_SCALE),
        "imbalance": (bid_size - ask_size) / (bid_size + ask_size),
    }
if __name__ == "__main__":
    # Synthetic quote: $100.00 bid, $100.02 ask.
    quote = {
        "bid_price": 100_000_000_000,
        "bid_size": 100,
        "ask_price": 100_020_000_000,
        "ask_size": 50,
    }
    print(build_features(quote))
