from dataclasses import dataclass
import databento as db
"""
MboMsg(
    instrument_id=12345,
    order_id=1001,
    action='A',
    side='B',
    price=100000000000,
    size=10,
    ts_event=1735829100000000000,
    flags=LAST (128),
    sequence=1
)
"""
@dataclass
class Order:
    """One resting order's side, integer price, and remaining shares.

    Example: Order(side="B", price=100_000_000_000, size=10).
    The order ID is the key in OrderBook.orders, not a field here.
    """

    side:str
    price:int
    size:int

F_LAST = 128
class OrderBook:
    """Current state for one source, not a history of completed orders.

    orders: {1001: Order("B", 100_000_000_000, 10),
             1002: Order("B", 100_000_000_000, 20),
             2001: Order("A", 100_020_000_000, 50)}
    bids:   {100_000_000_000: 30}  # 10 + 20 shares.
    asks:   {100_020_000_000: 50}
    See docs/code_examples.md for a consistent multi-order example.
    """

    def __init__(self)->None:
        self.orders:dict[int,Order]={}#{orderid:Order}
        self.bids:dict[int,int]={}#{price:size}
        self.asks:dict[int,int]={}
        self._book_key:tuple[int,int]|None=None

    def _update_level(self,side:str,price:int,change:int)->None:
        levels = self.bids if side =="B" else self.asks
        new_size = levels.get(price,0)+change
        if new_size == 0:
            levels.pop(price, None)
        else:
            levels[price] = new_size

    def apply(self,record:db.MBOMsg):
        """Apply one raw record; return a quote only at a complete boundary.

        Output example: {"bid_price": 100_000_000_000, "bid_size": 30,
                         "ask_price": 100_020_000_000, "ask_size": 50}.
        Returns None without F_LAST or without both book sides.
        Raw prices stay in integer units; sizes are shares.
        """

        book_key = (int(record.publisher_id),int(record.instrument_id))
        if self._book_key is None:
            self._book_key= book_key
        action = str(record.action)
        order_id = int(record.order_id)
        side = str(record.side)
        price = int(record.price)
        size = int(record.size)
        F_last = int(record.flags)

        if action =="R":
            self.orders.clear()
            self.bids.clear()
            self.asks.clear()
        elif action =="A":
            self.orders[order_id] = Order(side, price, size)
            self._update_level(side, price, size)
        elif action =="C":
            order = self.orders.get(order_id)
            self._update_level(order.side, order.price, -size)
            order.size -= size
            if order.size == 0:
                    del self.orders[order_id]
        elif action == "M":
            order = self.orders.get(order_id)
            if order is None:
                    raise ValueError(f"Unknown order: {order_id}")
            self._update_level(order.side, order.price, -order.size)
            self.orders[order_id] = Order(side, price, size)
            self._update_level(side, price, size)
        elif action in {"T", "F", "N"}:
            pass

        if not int(record.flags) & F_LAST:
            return None

        if not self.bids or not self.asks:
            return None

        best_bid = max(self.bids)
        best_ask = min(self.asks)




        return {
                "bid_price": best_bid,
                "bid_size": self.bids[best_bid],
                "ask_price": best_ask,
                "ask_size": self.asks[best_ask],
            }
