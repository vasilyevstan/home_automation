"""SDK-free access to published quarter-hour electricity market prices."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Callable, Dict, Optional, Sequence

from ..electricity_market.prices import PriceReport, UTC, fetch_prices_range
from .engine import PricingError


def market_prices(
    first: date, last: date,
    fetcher: Optional[Callable[[date, date], Sequence[PriceReport]]] = None,
) -> Dict[datetime, Decimal]:
    fetch = fetch_prices_range if fetcher is None else fetcher
    prices: Dict[datetime, Decimal] = {}
    current = first
    while current <= last:
        stop = min(current + timedelta(days=30), last)
        for report in fetch(current, stop):
            for interval in report.intervals:
                timestamp = interval.start.astimezone(UTC)
                if timestamp in prices:
                    raise PricingError("Duplicate market price across range batches.")
                prices[timestamp] = interval.price_eur_mwh
        current = stop + timedelta(days=1)
    return prices
