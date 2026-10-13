"""Framework-free native HA actions using the canonical household calculator."""

from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import stat
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..electricity_market.prices import API_URL, MARKET_INTERVAL, TALLINN, UTC
from ..electricity_pricing.data import market_prices
from ..electricity_pricing.engine import (
    EnergyInterval, PricingError, PricingProfile, PricingResult, QuantityBasis,
    ScenarioResult, decimal_value, make_interval, make_load, period_bounds,
    price_month_to_date, price_period, price_scenario, quote_interval,
    scenario_bounds, validate_intervals,
)
from ..household_consumption.client import API_BASE, ConsumptionError, Credentials, DateWindow, EstfeedClient
from .pricing import profile_path


PERIODS = ("custom", "month_to_date", "previous_month", "last_12_months")
ACTIONS = (
    "quote_electricity_interval",
    "calculate_electricity_period",
    "estimate_electricity_scenario",
)
_ARGUMENTS = {
    ACTIONS[0]: {
        "start", "end", "consumption_kwh", "export_kwh", "quantity_basis",
        "price_basis", "price_intervals",
    },
    ACTIONS[1]: {
        "period", "start_date", "end_date", "days", "intervals", "quantity_basis",
        "mode", "include_daily", "price_basis", "price_intervals",
    },
    ACTIONS[2]: {
        "mode", "start", "duration_minutes", "power_kw", "energy_kwh",
        "baseline_intervals", "scenario_intervals", "quantity_basis",
        "price_basis", "price_intervals",
    },
}


def load_credentials_file(config_dir: Path, name: str) -> Credentials:
    path = profile_path(config_dir, name)

    def unique_fields(fields: List[Tuple[str, object]]) -> dict:
        result = dict(fields)
        if len(result) != len(fields):
            raise ConsumptionError("The private Elering credentials file contains duplicate fields.")
        return result

    try:
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise ConsumptionError("The private Elering credentials file must not be accessible to group or other users.")
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_fields)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConsumptionError("The private Elering credentials JSON file is unavailable or invalid.") from None
    if (
        not isinstance(value, dict) or set(value) != {"client_id", "client_secret"}
        or any(not isinstance(item, str) or not item.strip() for item in value.values())
    ):
        raise ConsumptionError("The private Elering file must contain only nonempty client_id and client_secret strings.")
    return Credentials(value["client_id"], value["client_secret"])


def resolve_period(
    now: datetime, period: str = "custom", start_date: Optional[str] = None,
    end_date: Optional[str] = None, days: Optional[int] = None,
) -> Tuple[date, date, datetime, datetime]:
    if now.utcoffset() is None:
        raise PricingError("The calculation clock must include a UTC offset.")
    if period not in PERIODS:
        raise PricingError("Choose custom, month_to_date, previous_month or last_12_months.")
    today = now.astimezone(TALLINN).date()
    if period != "custom":
        if any(value is not None for value in (start_date, end_date, days)):
            raise PricingError("Date presets cannot be combined with custom dates or a length.")
        month = today.replace(day=1)
        if period == "month_to_date":
            first, last = month, today - timedelta(days=1)
            if last < first:
                raise PricingError("No completed delivery day exists in the current month yet.")
        else:
            last = month - timedelta(days=1)
            first = last.replace(day=1) if period == "previous_month" else month.replace(year=month.year - 1)
        return period_bounds(first.isoformat(), last.isoformat())
    if days is not None:
        if isinstance(days, bool) or not isinstance(days, int) or days <= 0:
            raise PricingError("days must be a positive whole number.")
        if end_date is not None:
            raise PricingError("Supply either end_date or days, not both.")
        try:
            if start_date is None:
                first, last = today - timedelta(days=days), today - timedelta(days=1)
            else:
                first = period_bounds(start_date)[0]
                last = first + timedelta(days=days - 1)
        except (OverflowError, ValueError):
            raise PricingError("The requested length exceeds supported date bounds.") from None
        return period_bounds(first.isoformat(), last.isoformat())
    if start_date is None:
        raise PricingError("Custom periods need start_date, or days for the latest completed days.")
    return period_bounds(start_date, end_date)


def _energy_rows(values: Sequence[Mapping[str, object]]) -> List[EnergyInterval]:
    if not isinstance(values, (list, tuple)):
        raise PricingError("Energy intervals must be a list of quarter-hour objects.")
    rows = []
    for value in values:
        if (
            not isinstance(value, dict) or not {"start", "end"}.issubset(value)
            or set(value) - {"start", "end", "consumption_kwh", "export_kwh"}
            or not isinstance(value["start"], str) or not isinstance(value["end"], str)
        ):
            raise PricingError("Each energy interval needs start/end timestamps and only consumption_kwh/export_kwh quantities.")
        rows.append(make_interval(
            value["start"], value["end"], value.get("consumption_kwh"), value.get("export_kwh"),
        ))
    return rows


class ElectricityCalculator:
    def __init__(
        self, profile_provider: Callable[[], PricingProfile],
        client: Optional[EstfeedClient] = None,
        price_provider: Callable[[date, date], Dict[datetime, Decimal]] = market_prices,
        now_provider: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.profile_provider = profile_provider
        self.client = client
        self.price_provider = price_provider
        self.now_provider = now_provider

    def invoke(self, action: str, arguments: Mapping[str, object]) -> dict:
        if action not in ACTIONS or set(arguments) - _ARGUMENTS[action]:
            raise PricingError("Unknown calculator action or unsupported input fields.")
        methods = {
            ACTIONS[0]: self.quote,
            ACTIONS[1]: self.period,
            ACTIONS[2]: self.scenario,
        }
        return methods[action](**arguments)

    def _prices(
        self, first: datetime, last: datetime, basis: str,
        intervals: Optional[Sequence[Mapping[str, object]]],
    ) -> Dict[datetime, Decimal]:
        if basis == "published":
            if intervals is not None:
                raise PricingError("Published-price calculations retrieve verified prices; supplied prices require price_basis: estimate.")
            return self.price_provider(
                first.astimezone(TALLINN).date(),
                (last - timedelta(microseconds=1)).astimezone(TALLINN).date(),
            )
        if basis != "estimate" or intervals is None:
            raise PricingError("Estimates require price_basis: estimate and explicitly supplied price_intervals; there is no forecast fallback.")
        if not isinstance(intervals, (list, tuple)):
            raise PricingError("Estimated price intervals must be a list of quarter-hour objects.")
        prices = {}
        for value in intervals:
            if (
                not isinstance(value, dict) or set(value) != {"start", "end", "price_eur_mwh"}
                or not isinstance(value["start"], str) or not isinstance(value["end"], str)
            ):
                raise PricingError("Estimated price intervals need start, end and price_eur_mwh only.")
            row = make_interval(value["start"], value["end"], "0", "0")
            if row.start.timestamp() % 900 or row.end - row.start != MARKET_INTERVAL:
                raise PricingError("Estimated prices must use aligned, offset-aware 15-minute intervals.")
            if row.start in prices or row.start < first or row.end > last:
                raise PricingError("Estimated price intervals must be unique and inside the accounting window.")
            prices[row.start] = decimal_value(value["price_eur_mwh"], "Estimated EUR/MWh price")
        if len(prices) != int((last - first) / MARKET_INTERVAL):
            raise PricingError("Incomplete estimated price coverage; every accounting quarter needs an explicit price.")
        return prices

    def _response(
        self, result: PricingResult | ScenarioResult, price_basis: str, kind: str,
        **metadata: object,
    ) -> dict:
        warnings = list(result.warnings)
        if price_basis == "estimate":
            warnings.append("Caller-supplied estimated prices are not published exchange prices. This estimate cannot finalize an actual bill.")
        result = replace(
            result,
            price_source=API_URL if price_basis == "published" else "caller_supplied_estimated_prices",
            prices_retrieved_at=self.now_provider().astimezone(TALLINN).isoformat() if price_basis == "published" else None,
            warnings=warnings,
        )
        if isinstance(result, ScenarioResult):
            result = replace(
                result,
                scenario=replace(result.scenario, price_source=result.price_source, prices_retrieved_at=result.prices_retrieved_at),
                baseline=replace(result.baseline, price_source=result.price_source, prices_retrieved_at=result.prices_retrieved_at)
                if result.baseline is not None else None,
            )
        return {
            **asdict(result), "calculation_kind": kind,
            "price_basis": price_basis, "as_of": self.now_provider().astimezone(TALLINN).isoformat(),
            **metadata,
        }

    def quote(
        self, start: str, end: str, consumption_kwh: object = None, export_kwh: object = None,
        quantity_basis: QuantityBasis = "raw", price_basis: str = "published",
        price_intervals: Optional[Sequence[Mapping[str, object]]] = None,
    ) -> dict:
        profile = self.profile_provider()
        row = make_interval(start, end, consumption_kwh, export_kwh)
        if row.start.timestamp() % 900 or row.end - row.start != MARKET_INTERVAL:
            raise PricingError("Quote exactly one aligned 15-minute delivery interval.")
        validate_intervals(profile, [row], quantity_basis, row.start, row.end, True)
        prices = self._prices(row.start, row.end, price_basis, price_intervals)
        return self._response(
            quote_interval(profile, row, prices, quantity_basis), price_basis,
            "estimate" if price_basis == "estimate" else "interval_quote",
        )

    def period(
        self, period: str = "custom", start_date: Optional[str] = None,
        end_date: Optional[str] = None, days: Optional[int] = None,
        intervals: Optional[Sequence[Mapping[str, object]]] = None,
        quantity_basis: QuantityBasis = "raw", mode: str = "auto",
        include_daily: bool = False, price_basis: str = "published",
        price_intervals: Optional[Sequence[Mapping[str, object]]] = None,
    ) -> dict:
        now = self.now_provider()
        first, last, start, requested_end = resolve_period(now, period, start_date, end_date, days)
        if price_basis not in ("published", "estimate"):
            raise PricingError("price_basis must be published or estimate.")
        if price_basis == "published" and price_intervals is not None:
            raise PricingError("Supplied prices require price_basis: estimate.")
        if mode not in ("auto", "report", "monthly"):
            raise PricingError("mode must be auto, report or monthly.")
        selected_mode = ("monthly" if period in ("previous_month", "last_12_months") else "report") if mode == "auto" else mode
        if period != "custom" and (price_basis != "published" or price_intervals is not None):
            raise PricingError("Actual-bill presets use published prices only; use a custom, explicitly estimated period for planning.")
        if price_basis == "estimate" and (intervals is None or selected_mode == "monthly"):
            raise PricingError("Estimated periods require caller-supplied energy and report mode, never automatic metering or finalized monthly billing.")
        if period == "month_to_date" and mode != "auto":
            raise PricingError("The month_to_date preset uses its own provisional full-fixed-fee calculation; omit mode.")
        if selected_mode == "monthly" and (first.day != 1 or (last + timedelta(days=1)).day != 1):
            raise PricingError("Monthly billing requires complete calendar months; use report for custom partial months.")
        profile = self.profile_provider()
        profile.check_coverage(start, requested_end)
        readings_retrieved_at = None
        warnings = []
        if intervals is None:
            if quantity_basis != "raw":
                raise PricingError("Automatic Elering measurements are raw, not billable quantities.")
            if requested_end > now.astimezone(UTC):
                raise PricingError("Automatic billing requires completed past delivery days; future estimates need explicitly supplied intervals.")
            if self.client is None:
                raise ConsumptionError("Configure private Elering credentials for automatic household measurements.")
            report = self.client.get_consumption(DateWindow(first, last, start, requested_end), "15_minutes")
            rows = [
                EnergyInterval(row.start, row.start + MARKET_INTERVAL, row.consumption_kwh, row.export_kwh)
                for row in report.readings
            ]
            source = API_BASE + "/api/public/v1/metering-data"
            readings_retrieved_at = report.retrieved_at.astimezone(TALLINN).isoformat()
            warnings.append("Elering readings may be revised; complete coverage does not establish settlement finality.")
            if period == "month_to_date":
                known_ends = [row.end for row in rows if row.consumption_kwh is not None and row.export_kwh is not None]
                if not known_ends:
                    raise PricingError("No complete measured delivery day is available for this month yet.")
                latest = max(known_ends).astimezone(TALLINN)
                last = min(last, latest.date() - timedelta(days=1))
                if last < first:
                    raise PricingError("No complete measured delivery day is available for this month yet.")
                _, _, _, end = period_bounds(first.isoformat(), last.isoformat())
                rows = [row for row in rows if row.end <= end]
        else:
            rows = _energy_rows(intervals)
            source = "caller_supplied"
            warnings.append("Supplied quantities are caller inputs, not independently verified household measurements.")
        _, _, _, end = period_bounds(first.isoformat(), last.isoformat())
        validate_intervals(profile, rows, quantity_basis, start, end, False)
        prices = self._prices(start, end, price_basis, price_intervals)
        if period == "month_to_date":
            result = price_month_to_date(profile, rows, prices, first.isoformat(), last.isoformat(), quantity_basis, include_daily)
            warnings.append("Today's unfinished delivery day is excluded. Later completed days without full metering remain pending, not zero.")
            kind = "provisional_bill"
        else:
            result = price_period(profile, rows, prices, first.isoformat(), last.isoformat(), quantity_basis, selected_mode, include_daily)
            kind = "estimate" if price_basis == "estimate" else "calculated_bill" if selected_mode == "monthly" else "period_report"
        result = replace(
            result, input_source=source, readings_retrieved_at=readings_retrieved_at,
            warnings=result.warnings + warnings,
        )
        return self._response(
            result, price_basis, kind, preset=period,
            coverage={
                "requested_start": start.astimezone(TALLINN).isoformat(),
                "requested_end": requested_end.astimezone(TALLINN).isoformat(),
                "covered_start": result.start, "covered_end": result.end,
                "pending_completed_days": (requested_end.astimezone(TALLINN).date() - end.astimezone(TALLINN).date()).days,
                "current_day_included": period != "month_to_date" and last == now.astimezone(TALLINN).date(),
            },
        )

    def scenario(
        self, mode: str = "load", start: Optional[str] = None,
        duration_minutes: object = None, power_kw: object = None, energy_kwh: object = None,
        baseline_intervals: Optional[Sequence[Mapping[str, object]]] = None,
        scenario_intervals: Optional[Sequence[Mapping[str, object]]] = None,
        quantity_basis: Optional[QuantityBasis] = None, price_basis: str = "published",
        price_intervals: Optional[Sequence[Mapping[str, object]]] = None,
    ) -> dict:
        profile = self.profile_provider()
        before = None
        load = None
        if mode == "load":
            if start is None or duration_minutes is None:
                raise PricingError("Load mode needs start and duration_minutes.")
            if baseline_intervals is not None or scenario_intervals is not None or quantity_basis is not None:
                raise PricingError("Load mode takes a hypothetical load, not grid profiles or a quantity_basis.")
            load = make_load(profile, start, duration_minutes, power_kw, energy_kwh)
            rows = list(load.intervals)
            basis: QuantityBasis = "billable"
        elif mode == "comparison":
            if any(value is not None for value in (start, duration_minutes, power_kw, energy_kwh)):
                raise PricingError("Comparison mode takes before/after grid profiles, not load parameters.")
            if baseline_intervals is None or scenario_intervals is None:
                raise PricingError("Comparison mode requires baseline_intervals and scenario_intervals.")
            before, rows = _energy_rows(baseline_intervals), _energy_rows(scenario_intervals)
            basis = "raw" if quantity_basis is None else quantity_basis
        else:
            raise PricingError("Scenario mode must be load or comparison.")
        first, last = scenario_bounds(profile, rows, basis)
        if before is not None and scenario_bounds(profile, before, basis) != (first, last):
            raise PricingError("Baseline and scenario must cover the same accounting window.")
        prices = self._prices(first, last, price_basis, price_intervals)
        result = price_scenario(profile, rows, prices, basis, before)
        if load is not None:
            result = replace(
                result, start=load.start.astimezone(TALLINN).isoformat(),
                end=load.end.astimezone(TALLINN).isoformat(), energy_allocation=list(load.allocation),
            )
        return self._response(result, price_basis, "estimate")
