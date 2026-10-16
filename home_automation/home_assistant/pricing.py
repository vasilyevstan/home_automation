"""Normalize published HA Nord Pool quarters and reuse the financial engine."""

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Iterable, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

from ..electricity_market.prices import MARKET_INTERVAL, TALLINN, UTC, market_bounds
from ..electricity_pricing.engine import (
    EnergyInterval,
    PricingError,
    PricingProfile,
    decimal_value,
    parse_instant,
    quote_interval,
)


NORDPOOL_TIMEZONE = ZoneInfo("Europe/Oslo")
ONE = Decimal(1)
ZERO = Decimal(0)


@dataclass(frozen=True)
class UnitPrices:
    start: str
    end: str
    spot_eur_kwh: str
    import_eur_kwh: str
    export_earnings_eur_kwh: str
    evidence_bases: Tuple[str, ...]

    @property
    def invoice_derived(self) -> bool:
        return "invoice_derived" in self.evidence_bases


@dataclass(frozen=True)
class PriceWindow:
    delivery_date: str
    start: str
    end: str
    expected_intervals: int
    missing_market_intervals: int
    complete: bool
    unavailable_reason: Optional[str]
    prices: Tuple[UnitPrices, ...]

    def as_dict(self) -> dict:
        return {
            "currency": "EUR",
            "unit": "EUR/kWh",
            "timezone": "Europe/Tallinn",
            "spot_vat_basis": "excluding_vat",
            "import_vat_basis": "including_applicable_vat",
            "export_basis": "net_earnings_after_export_charges",
            "fixed_fees": "excluded",
            **asdict(self),
        }


@dataclass(frozen=True)
class PriceSnapshot:
    calculated_at: datetime
    valid_from: datetime
    valid_until: datetime
    profile_id: str
    profile_version: str
    profile_sha256: str
    current: UnitPrices
    today: PriceWindow
    tomorrow: PriceWindow

    def contains(self, instant: datetime) -> bool:
        if instant.utcoffset() is None:
            raise PricingError("The observation time must include a UTC offset.")
        return self.valid_from <= instant.astimezone(UTC) < self.valid_until

    def attributes(self) -> dict:
        return {
            "period_start": self.current.start,
            "period_end": self.current.end,
            "calculated_at": self.calculated_at.isoformat(),
            "timezone": "Europe/Tallinn",
            "currency": "EUR",
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "profile_sha256": self.profile_sha256,
            "invoice_derived_rules_used": self.current.invoice_derived,
            "evidence_bases": list(self.current.evidence_bases),
            "fixed_fees": "excluded",
            "today_complete": self.today.complete,
            "today_unavailable_reason": self.today.unavailable_reason,
            "tomorrow_complete": self.tomorrow.complete,
            "tomorrow_unavailable_reason": self.tomorrow.unavailable_reason,
        }


def market_dates(first_local_day: date) -> Tuple[date, ...]:
    """Select the CET market days covering today and tomorrow in Tallinn."""
    start, _ = market_bounds(first_local_day)
    _, end = market_bounds(first_local_day + timedelta(days=1))
    first = start.astimezone(NORDPOOL_TIMEZONE).date()
    last = (end - MARKET_INTERVAL).astimezone(NORDPOOL_TIMEZONE).date()
    return tuple(first + timedelta(days=index) for index in range((last - first).days + 1))


def market_day_bounds(day: date) -> Tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=NORDPOOL_TIMEZONE)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=NORDPOOL_TIMEZONE)
    return start.astimezone(UTC), end.astimezone(UTC)


def complete_market_day(prices: Mapping[datetime, Decimal], day: date) -> bool:
    start, end = market_day_bounds(day)
    expected = int((end - start) / MARKET_INTERVAL)
    return len(prices) == expected and all(
        start + index * MARKET_INTERVAL in prices for index in range(expected)
    )


def profile_path(config_dir: Path, value: str) -> Path:
    if not isinstance(value, str) or not value.strip() or Path(value).is_absolute():
        raise PricingError("Use a private JSON profile path relative to the HA configuration directory.")
    root = config_dir.resolve()
    path = (root / value).resolve()
    if not path.is_relative_to(root) or path.suffix != ".json":
        raise PricingError("The private profile must be a JSON file inside the HA configuration directory.")
    return path


def parse_nordpool_prices(responses: Iterable[object]) -> dict[datetime, Decimal]:
    """Consume get_prices_for_date responses explicitly requested as EE/EUR."""
    prices: dict[datetime, Decimal] = {}
    for response in responses:
        if not isinstance(response, dict) or not isinstance(response.get("EE"), list):
            raise PricingError("Nord Pool must return an EE list in its EUR price response.")
        for row in response["EE"]:
            if not isinstance(row, dict) or set(row) != {"start", "end", "price"}:
                raise PricingError("Each Nord Pool record must contain start, end and price.")
            start = parse_instant(row["start"])
            end = parse_instant(row["end"])
            if start.timestamp() % 900 or end - start != MARKET_INTERVAL:
                raise PricingError(
                    "Published quarter-hour prices are required; hourly prices are not redistributed."
                )
            if start in prices:
                raise PricingError("Duplicate published Nord Pool interval: " + start.isoformat())
            prices[start] = decimal_value(row["price"], "Published Nord Pool EUR/MWh price")
    return prices


def unit_prices(
    profile: PricingProfile, prices: Mapping[datetime, Decimal], start: datetime
) -> UnitPrices:
    end = start + MARKET_INTERVAL
    imported = quote_interval(
        profile, EnergyInterval(start, end, ONE, ZERO), prices, "billable"
    )
    exported = quote_interval(
        profile, EnergyInterval(start, end, ZERO, ONE), prices, "billable"
    )
    bases = tuple(sorted({
        source.basis
        for result in (imported, exported)
        for source in result.profile.sources
    }))
    with localcontext() as context:
        context.prec = 80
        spot = prices[start] / Decimal(1000)
    earnings = Decimal(exported.amounts.including_vat_eur).copy_negate()
    return UnitPrices(
        start.astimezone(TALLINN).isoformat(),
        end.astimezone(TALLINN).isoformat(),
        format(spot, "f"),
        imported.amounts.including_vat_eur,
        format(earnings, "f"),
        bases,
    )


def forecast_import_eur_kwh(
    profile: PricingProfile, price_eur_mwh: Decimal, start: datetime
) -> str:
    """Apply the household tariff to one forecast quarter without inventing export."""
    end = start + MARKET_INTERVAL
    result = quote_interval(
        profile,
        EnergyInterval(start, end, ONE, ZERO),
        {start: price_eur_mwh},
        "billable",
    )
    return result.amounts.including_vat_eur


def price_window(
    profile: PricingProfile, prices: Mapping[datetime, Decimal], delivery_day: date
) -> PriceWindow:
    start, end = market_bounds(delivery_day)
    expected = int((end - start) / MARKET_INTERVAL)
    instants = tuple(start + index * MARKET_INTERVAL for index in range(expected))
    missing = sum(instant not in prices for instant in instants)
    bounds = (
        delivery_day.isoformat(),
        start.astimezone(TALLINN).isoformat(),
        end.astimezone(TALLINN).isoformat(),
        expected,
        missing,
    )
    if not profile.start <= delivery_day < profile.end:
        return PriceWindow(
            *bounds, False, "The tariff profile does not cover this local day.", ()
        )
    points = tuple(
        unit_prices(profile, prices, instant)
        for instant in instants
        if instant in prices
    )
    reason = (
        "Published prices are incomplete: {} of {} quarters are missing.".format(missing, expected)
        if missing else None
    )
    return PriceWindow(*bounds, missing == 0, reason, points)


def build_snapshot(
    profile: PricingProfile, prices: Mapping[datetime, Decimal], now: datetime
) -> PriceSnapshot:
    if now.utcoffset() is None:
        raise PricingError("The observation time must include a UTC offset.")
    observed = now.astimezone(UTC)
    start = datetime.fromtimestamp(int(observed.timestamp()) // 900 * 900, UTC)
    if start not in prices:
        raise PricingError("Published price is unavailable for the current quarter.")
    current = unit_prices(profile, prices, start)
    local_day = observed.astimezone(TALLINN).date()
    return PriceSnapshot(
        observed, start, start + MARKET_INTERVAL,
        profile.id, profile.version, profile.sha256, current,
        price_window(profile, prices, local_day),
        price_window(profile, prices, local_day + timedelta(days=1)),
    )
