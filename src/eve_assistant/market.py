"""Market order books at the trade hubs, and what a sale would really fetch.

Order books are fetched per region and item, on demand, through the ESI
client (which respects the 5-minute cache). Selling into buy orders walks
down the book, so a large sale gets the price it would actually get, not
the single best order.
"""

from dataclasses import dataclass

from eve_assistant.esi.client import EsiClient


@dataclass(frozen=True)
class Hub:
    name: str
    station_id: int
    system_id: int
    region_id: int


# The five main NPC trade hubs (IDs checked against the static data).
HUBS = (
    Hub("Jita", 60003760, 30000142, 10000002),
    Hub("Amarr", 60008494, 30002187, 10000043),
    Hub("Dodixie", 60011866, 30002659, 10000032),
    Hub("Rens", 60004588, 30002510, 10000030),
    Hub("Hek", 60005686, 30002053, 10000042),
)


@dataclass(frozen=True)
class Order:
    price: float
    volume: int
    is_buy: bool
    location_id: int
    system_id: int
    range: str


@dataclass(frozen=True)
class Fill:
    quantity: int  # how many units the orders can take
    gross: float  # ISK before fees
    lowest_price: float | None  # the worst price reached while filling

    @property
    def average_price(self) -> float | None:
        return self.gross / self.quantity if self.quantity else None


def order_book(esi: EsiClient, region_id: int, type_id: int) -> list[Order]:
    result = esi.get(f"/markets/{region_id}/orders", params={"type_id": type_id})
    return [
        Order(
            price=o["price"],
            volume=o["volume_remain"],
            is_buy=o["is_buy_order"],
            location_id=o["location_id"],
            system_id=o["system_id"],
            range=o.get("range", "station"),
        )
        for o in result.data
    ]


def _buy_order_reaches(order: Order, station_id: int, system_id: int) -> bool:
    """Whether a buy order accepts items sold at this station. Jump-range orders
    are only counted within the same system: conservative until routes are used."""
    if order.range == "station":
        return order.location_id == station_id
    if order.range == "region":
        return True
    return order.system_id == system_id


def sell_into_buy_orders(book: list[Order], quantity: int, station_id: int, system_id: int) -> Fill:
    """Instant sale: fill the best buy orders that reach this station."""
    bids = sorted(
        (o for o in book if o.is_buy and _buy_order_reaches(o, station_id, system_id)),
        key=lambda o: -o.price,
    )
    left, gross, lowest = quantity, 0.0, None
    for bid in bids:
        if left <= 0:
            break
        take = min(left, bid.volume)
        gross += take * bid.price
        lowest = bid.price
        left -= take
    return Fill(quantity - left, gross, lowest)


def lowest_sell(book: list[Order], station_id: int) -> float | None:
    """The price to beat when listing a sell order at this station."""
    asks = [o.price for o in book if not o.is_buy and o.location_id == station_id]
    return min(asks) if asks else None


def sell_volume_at(book: list[Order], station_id: int) -> int:
    return sum(o.volume for o in book if not o.is_buy and o.location_id == station_id)
