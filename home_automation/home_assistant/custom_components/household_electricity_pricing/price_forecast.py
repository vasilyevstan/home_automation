"""Read-only seven-day household price forecasts and retained predictions."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from ._vendor.home_automation.electricity_market.forecast import (
    ACCURACY_URL,
    FORECAST_URL,
    AccuracyReport,
    ForecastError,
    ForecastReport,
    PredictedHour,
    PublishedHour,
    fetch_accuracy,
    fetch_forecast,
)
from ._vendor.home_automation.electricity_market.prices import (
    MARKET_INTERVAL,
    TALLINN,
    UTC,
    market_bounds,
    next_delivery_date,
)
from ._vendor.home_automation.electricity_pricing.engine import (
    PricingError,
    PricingProfile,
    load_profile,
)
from ._vendor.home_automation.home_assistant.pricing import (
    forecast_import_eur_kwh,
    profile_path,
)
from .const import DOMAIN
from .coordinator import HouseholdPriceCoordinator


LOGGER = logging.getLogger(__name__)
FORECAST_DAYS = 7
FORECAST_STALE_SECONDS = 3 * 60 * 60
FORECAST_UPDATE_INTERVAL = timedelta(hours=1)
ARCHIVE_RETENTION_DAYS = 90
STORE_VERSION = 1
STORE_KEY_SUFFIX = "price_forecast"
HOUR = timedelta(hours=1)


@dataclass(frozen=True)
class HouseholdPriceForecast:
    response: dict
    next_prediction: Decimal | None
    attributes: dict


def _instant(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ForecastError(label + " must be an ISO timestamp")
    try:
        result = datetime.fromisoformat(value)
    except ValueError as error:
        raise ForecastError(label + " must be an ISO timestamp") from error
    if result.utcoffset() is None:
        raise ForecastError(label + " must include a UTC offset")
    return result.astimezone(UTC)


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ForecastError(label + " must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ForecastError(label + " must be numeric") from error
    if not result.is_finite():
        raise ForecastError(label + " must be finite")
    return result


def _window_bounds(now: datetime) -> tuple[datetime, datetime]:
    first_day = next_delivery_date(now)
    start, _ = market_bounds(first_day)
    _, end = market_bounds(first_day + timedelta(days=FORECAST_DAYS - 1))
    return start, end


def _average(values: list[Decimal]) -> str | None:
    if not values:
        return None
    with localcontext() as context:
        context.prec = 80
        return format(sum(values, Decimal(0)) / len(values), "f")


def _point_value(point: dict) -> Decimal | None:
    value = point.get("published_import_eur_kwh")
    if value is None and isinstance(point.get("prediction"), dict):
        value = point["prediction"].get("p50_import_eur_kwh")
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _basis(published: int, predicted: int, unavailable: int) -> str:
    kinds = sum(value > 0 for value in (published, predicted, unavailable))
    if kinds > 1:
        return "mixed"
    if published:
        return "published"
    if predicted:
        return "forecast"
    return "unavailable"


def _daily_summaries(points: list[dict], first_day: date) -> list[dict]:
    summaries = []
    for index in range(FORECAST_DAYS):
        delivery_day = first_day + timedelta(days=index)
        start, end = market_bounds(delivery_day)
        rows = [
            point
            for point in points
            if start <= _instant(point["start"], "forecast point") < end
        ]
        published = sum(point["basis"] == "published" for point in rows)
        predicted = sum(point["basis"] == "forecast" for point in rows)
        unavailable = sum(point["basis"] == "unavailable" for point in rows)
        values = [
            value for value in (_point_value(point) for point in rows)
            if value is not None
        ]
        valued_rows = [
            (point, value)
            for point in rows
            if (value := _point_value(point)) is not None
        ]
        minimum = min(valued_rows, key=lambda item: item[1]) if valued_rows else None
        maximum = max(valued_rows, key=lambda item: item[1]) if valued_rows else None
        expected = int((end - start) / MARKET_INTERVAL)
        summaries.append({
            "delivery_date": delivery_day.isoformat(),
            "expected_intervals": expected,
            "published_intervals": published,
            "forecast_intervals": predicted,
            "unavailable_intervals": unavailable,
            "complete": len(rows) == expected and unavailable == 0,
            "basis": _basis(published, predicted, unavailable),
            "average_import_eur_kwh": _average(values),
            "minimum": (
                {
                    "start": minimum[0]["start"],
                    "import_eur_kwh": format(minimum[1], "f"),
                }
                if minimum else None
            ),
            "maximum": (
                {
                    "start": maximum[0]["start"],
                    "import_eur_kwh": format(maximum[1], "f"),
                }
                if maximum else None
            ),
        })
    return summaries


def _accuracy_response(
    report: AccuracyReport | None, error: str | None
) -> dict:
    horizons = []
    if report is not None:
        for item in report.horizons:
            live = item.live
            backtest = item.backtest
            horizons.append({
                "horizon_days": item.horizon_days,
                "live": (
                    {
                        "mae_eur_per_mwh": format(live.mae, "f"),
                        "mae_cents_per_kwh_excluding_vat": format(
                            live.mae / Decimal(10), "f"
                        ),
                        "interval_coverage_percent": format(
                            live.coverage_percent, "f"
                        ),
                        "sample_count": live.sample_count,
                    }
                    if live is not None else None
                ),
                "backtest": (
                    {
                        "mae_eur_per_mwh": format(backtest.mae, "f"),
                        "mae_cents_per_kwh_excluding_vat": format(
                            backtest.mae / Decimal(10), "f"
                        ),
                        "naive_week_mae_eur_per_mwh": format(
                            backtest.naive_week_mae, "f"
                        ),
                        "naive_lastknown_mae_eur_per_mwh": format(
                            backtest.naive_lastknown_mae, "f"
                        ),
                        "sample_count": backtest.sample_count,
                        "from_date": backtest.from_date.isoformat(),
                    }
                    if backtest is not None else None
                ),
            })
    return {
        "source": ACCURACY_URL,
        "status": "available" if report is not None else "unavailable",
        "unavailable_reason": None if report is not None else error,
        "evidence": "Provider-reported raw wholesale-model accuracy",
        "horizon_definition": (
            "Days after the last settled local day, not elapsed 24-hour lead times"
        ),
        "nominal_interval_coverage_percent": 80,
        "retrieved_at": (
            report.retrieved_at.astimezone(TALLINN).isoformat()
            if report is not None else None
        ),
        "model_trained_at": (
            report.model_trained_at.astimezone(TALLINN).isoformat()
            if report is not None else None
        ),
        "live_window_days": report.live_days if report is not None else None,
        "band_method": report.band_method if report is not None else None,
        "horizons": horizons,
    }


def _prediction(
    profile: PricingProfile, slot: PredictedHour, start: datetime
) -> dict:
    return {
        "p10_spot_eur_mwh": format(slot.p10, "f"),
        "p50_spot_eur_mwh": format(slot.p50, "f"),
        "p90_spot_eur_mwh": format(slot.p90, "f"),
        "p10_import_eur_kwh": forecast_import_eur_kwh(profile, slot.p10, start),
        "p50_import_eur_kwh": forecast_import_eur_kwh(profile, slot.p50, start),
        "p90_import_eur_kwh": forecast_import_eur_kwh(profile, slot.p90, start),
        "horizon_days": slot.horizon_days,
    }


def _next_prediction(points: list[dict]) -> Decimal | None:
    for point in points:
        prediction = point.get("prediction")
        if point.get("basis") == "forecast" and isinstance(prediction, dict):
            return Decimal(prediction["p50_import_eur_kwh"])
    return None


def _sensor_attributes(response: dict) -> dict:
    return {
        key: response.get(key)
        for key in (
            "source",
            "attribution",
            "advisory_only",
            "price_basis",
            "forecast_basis",
            "fixed_fees",
            "as_of",
            "window_start",
            "window_end",
            "issued_at",
            "retrieved_at",
            "model_trained_at",
            "age_seconds",
            "stale_after_seconds",
            "stale",
            "complete",
            "expected_intervals",
            "published_intervals",
            "forecast_intervals",
            "unavailable_intervals",
            "profile_version",
            "profile_sha256",
            "daily_summaries",
            "accuracy",
            "archive",
            "warnings",
        )
    }


class HouseholdPriceForecastCoordinator(
    DataUpdateCoordinator[HouseholdPriceForecast]
):
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        price_coordinator: HouseholdPriceCoordinator,
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN + " price forecast",
            config_entry=entry,
            update_interval=FORECAST_UPDATE_INTERVAL,
        )
        self.entry = entry
        self.price_coordinator = price_coordinator
        self.profile_name = entry.data["profile_path"]
        self.store = Store(
            hass,
            STORE_VERSION,
            f"{DOMAIN}.{entry.entry_id}.{STORE_KEY_SUFFIX}",
            private=True,
        )
        self.archive: dict[str, dict] = {}
        self.refresh_error: str | None = None

    async def async_load(self) -> None:
        stored = await self.store.async_load()
        if stored is None:
            return
        rows = stored.get("records") if isinstance(stored, dict) else None
        if not isinstance(rows, list):
            LOGGER.warning("Stored household price forecasts are invalid; archive reset.")
            return
        try:
            self.archive = {
                self._archive_key(row): self._validated_archive_record(row)
                for row in rows
            }
        except ForecastError as error:
            LOGGER.warning("Stored household price forecasts are invalid: %s", error)
            self.archive = {}

    def _profile(self) -> PricingProfile:
        path = profile_path(Path(self.hass.config.config_dir), self.profile_name)
        return load_profile(path)

    def _fetch_inputs(
        self,
    ) -> tuple[PricingProfile, ForecastReport, AccuracyReport | None, str | None]:
        profile = self._profile()
        report = fetch_forecast()
        try:
            accuracy = fetch_accuracy()
        except ForecastError as error:
            accuracy = None
            accuracy_error = str(error)
        else:
            accuracy_error = None
        return profile, report, accuracy, accuracy_error

    def _published_prices(self, now: datetime) -> dict[datetime, str]:
        data = self.price_coordinator.data
        if data is None or not data.contains(now):
            return {}
        result = {}
        for window in (data.today, data.tomorrow):
            for point in window.prices:
                result[_instant(point.start, "published price")] = (
                    point.import_eur_kwh
                )
        return result

    def _build_current_response(
        self,
        profile: PricingProfile,
        report: ForecastReport,
        accuracy: AccuracyReport | None,
        accuracy_error: str | None,
        now: datetime,
        published_prices: dict[datetime, str],
    ) -> HouseholdPriceForecast:
        start, end = _window_bounds(now)
        slots = {
            slot.start.astimezone(UTC): slot
            for slot in report.hours
            if start <= slot.start.astimezone(UTC) < end
        }
        if not any(isinstance(slot, PredictedHour) for slot in slots.values()):
            raise ForecastError(
                "No usable forecast hours exist in the requested seven-day window"
            )
        points = []
        unavailable_reasons: set[str] = set()
        current = start
        while current < end:
            published = published_prices.get(current)
            hour = current.replace(minute=0, second=0, microsecond=0)
            slot = slots.get(hour)
            if published is not None:
                point = {
                    "start": current.astimezone(TALLINN).isoformat(),
                    "end": (current + MARKET_INTERVAL).astimezone(TALLINN).isoformat(),
                    "basis": "published",
                    "published_import_eur_kwh": published,
                    "prediction": None,
                    "unavailable_reason": None,
                }
            elif isinstance(slot, PredictedHour):
                try:
                    prediction = _prediction(profile, slot, current)
                except PricingError as error:
                    reason = str(error)
                    unavailable_reasons.add(reason)
                    point = {
                        "start": current.astimezone(TALLINN).isoformat(),
                        "end": (
                            current + MARKET_INTERVAL
                        ).astimezone(TALLINN).isoformat(),
                        "basis": "unavailable",
                        "published_import_eur_kwh": None,
                        "prediction": None,
                        "unavailable_reason": reason,
                    }
                else:
                    point = {
                        "start": current.astimezone(TALLINN).isoformat(),
                        "end": (
                            current + MARKET_INTERVAL
                        ).astimezone(TALLINN).isoformat(),
                        "basis": "forecast",
                        "published_import_eur_kwh": None,
                        "prediction": prediction,
                        "unavailable_reason": None,
                    }
            else:
                reason = (
                    "The canonical Nord Pool quarter is unavailable."
                    if isinstance(slot, PublishedHour)
                    else "The forecast provider returned no value for this hour."
                )
                unavailable_reasons.add(reason)
                point = {
                    "start": current.astimezone(TALLINN).isoformat(),
                    "end": (current + MARKET_INTERVAL).astimezone(TALLINN).isoformat(),
                    "basis": "unavailable",
                    "published_import_eur_kwh": None,
                    "prediction": None,
                    "unavailable_reason": reason,
                }
            points.append(point)
            current += MARKET_INTERVAL
        published_count = sum(point["basis"] == "published" for point in points)
        forecast_count = sum(point["basis"] == "forecast" for point in points)
        unavailable_count = len(points) - published_count - forecast_count
        age = (now.astimezone(UTC) - report.issued_at.astimezone(UTC)).total_seconds()
        stale = age > FORECAST_STALE_SECONDS
        accuracy_response = _accuracy_response(accuracy, accuracy_error)
        warnings = [
            (
                "Experimental predictions are not Nord Pool auction results. "
                "Use published prices for exact scheduling once available."
            ),
            (
                "The p10-p90 range has nominal 80% provider coverage and is not "
                "a guaranteed household price bound."
            ),
        ]
        if stale:
            warnings.append(
                "Forecast is stale: issued {:.1f} hours ago.".format(age / 3600)
            )
        if unavailable_count:
            warnings.append(
                "{} of {} quarter-hours are unavailable; missing values are not "
                "zero-filled.".format(unavailable_count, len(points))
            )
        warnings.extend(sorted(unavailable_reasons))
        if accuracy is None:
            warnings.append(
                "Provider accuracy is unavailable: "
                + str(accuracy_response["unavailable_reason"])
            )
        else:
            undercovered = [
                str(item.horizon_days)
                for item in accuracy.horizons
                if item.live is not None and item.live.coverage_percent < 80
            ]
            if undercovered:
                warnings.append(
                    "Provider live interval coverage is below 80% at horizons: "
                    + ", ".join(undercovered)
                )
        first_day = start.astimezone(TALLINN).date()
        response = {
            "schema_version": 1,
            "view": "current",
            "area": "EE",
            "timezone": "Europe/Tallinn",
            "currency": "EUR",
            "interval_minutes": 15,
            "source": FORECAST_URL,
            "published_source": "Home Assistant Nord Pool EE/EUR",
            "attribution": report.attribution,
            "model_mode": "raw",
            "advisory_only": True,
            "price_basis": "household_all_in_import_including_applicable_vat",
            "forecast_basis": "raw_wholesale_p10_p50_p90_plus_household_tariff",
            "fixed_fees": "excluded",
            "as_of": now.astimezone(TALLINN).isoformat(),
            "window_start": start.astimezone(TALLINN).isoformat(),
            "window_end": end.astimezone(TALLINN).isoformat(),
            "issued_at": report.issued_at.astimezone(TALLINN).isoformat(),
            "retrieved_at": report.retrieved_at.astimezone(TALLINN).isoformat(),
            "model_trained_at": report.model_trained_at.astimezone(TALLINN).isoformat(),
            "age_seconds": age,
            "stale_after_seconds": FORECAST_STALE_SECONDS,
            "stale": stale,
            "refresh_status": "ok",
            "refresh_error": None,
            "complete": unavailable_count == 0,
            "expected_intervals": len(points),
            "published_intervals": published_count,
            "forecast_intervals": forecast_count,
            "unavailable_intervals": unavailable_count,
            "profile_id": profile.id,
            "profile_version": profile.version,
            "profile_sha256": profile.sha256,
            "intervals": points,
            "daily_summaries": _daily_summaries(points, first_day),
            "accuracy": accuracy_response,
            "archive": {
                "retention_days": ARCHIVE_RETENTION_DAYS,
                "record_count": len(self.archive),
                "sampling": (
                    "Latest provider issuance retained per delivery hour and "
                    "provider horizon day"
                ),
            },
            "warnings": warnings,
        }
        return HouseholdPriceForecast(
            response=response,
            next_prediction=_next_prediction(points),
            attributes=_sensor_attributes(response),
        )

    @staticmethod
    def _archive_key(record: dict) -> str:
        return "{}|{}".format(record.get("start"), record.get("horizon_days"))

    @staticmethod
    def _validated_archive_record(record: object) -> dict:
        if not isinstance(record, dict):
            raise ForecastError("Stored forecast record must be an object")
        start = _instant(record.get("start"), "Stored forecast start")
        issued_at = _instant(record.get("issued_at"), "Stored forecast issuance")
        trained_at = _instant(
            record.get("model_trained_at"), "Stored forecast model timestamp"
        )
        horizon = record.get("horizon_days")
        if isinstance(horizon, bool) or not isinstance(horizon, int) or not 1 <= horizon <= 7:
            raise ForecastError("Stored forecast horizon must be 1 through 7")
        p10 = _decimal(record.get("p10_spot_eur_mwh"), "Stored forecast p10")
        p50 = _decimal(record.get("p50_spot_eur_mwh"), "Stored forecast p50")
        p90 = _decimal(record.get("p90_spot_eur_mwh"), "Stored forecast p90")
        if not p10 <= p50 <= p90:
            raise ForecastError("Stored forecast must have p10 <= p50 <= p90")
        return {
            "start": start.isoformat(),
            "issued_at": issued_at.isoformat(),
            "model_trained_at": trained_at.isoformat(),
            "horizon_days": horizon,
            "p10_spot_eur_mwh": format(p10, "f"),
            "p50_spot_eur_mwh": format(p50, "f"),
            "p90_spot_eur_mwh": format(p90, "f"),
        }

    def _record_report(self, report: ForecastReport, now: datetime) -> bool:
        changed = False
        start, end = _window_bounds(now)
        for slot in report.hours:
            slot_start = slot.start.astimezone(UTC)
            if not isinstance(slot, PredictedHour) or not start <= slot_start < end:
                continue
            record = {
                "start": slot_start.isoformat(),
                "issued_at": report.issued_at.astimezone(UTC).isoformat(),
                "model_trained_at": report.model_trained_at.astimezone(UTC).isoformat(),
                "horizon_days": slot.horizon_days,
                "p10_spot_eur_mwh": format(slot.p10, "f"),
                "p50_spot_eur_mwh": format(slot.p50, "f"),
                "p90_spot_eur_mwh": format(slot.p90, "f"),
            }
            key = self._archive_key(record)
            previous = self.archive.get(key)
            if previous is not None:
                previous_issued = _instant(
                    previous["issued_at"], "Stored forecast issuance"
                )
                if previous_issued > report.issued_at.astimezone(UTC):
                    continue
                if previous_issued == report.issued_at.astimezone(UTC):
                    if previous != record:
                        raise ForecastError(
                            "The provider changed a forecast without a new issuance."
                        )
                    continue
            self.archive[key] = record
            changed = True
        cutoff = now.astimezone(UTC) - timedelta(days=ARCHIVE_RETENTION_DAYS)
        expired = [
            key
            for key, record in self.archive.items()
            if _instant(record["start"], "Stored forecast start") < cutoff
        ]
        for key in expired:
            del self.archive[key]
            changed = True
        return changed

    async def _save_archive(self) -> None:
        await self.store.async_save({
            "records": sorted(
                self.archive.values(),
                key=lambda record: (
                    record["start"],
                    record["horizon_days"],
                ),
            )
        })

    async def _async_update_data(self) -> HouseholdPriceForecast:
        now = dt_util.utcnow().astimezone(UTC)
        try:
            profile, report, accuracy, accuracy_error = (
                await self.hass.async_add_executor_job(self._fetch_inputs)
            )
            if self._record_report(report, now):
                await self._save_archive()
            published = self._published_prices(now)
            result = await self.hass.async_add_executor_job(
                self._build_current_response,
                profile,
                report,
                accuracy,
                accuracy_error,
                now,
                published,
            )
            result.response["archive"]["record_count"] = len(self.archive)
            result.attributes["archive"] = result.response["archive"]
            self.refresh_error = None
            return result
        except (ForecastError, PricingError) as error:
            self.refresh_error = str(error)
            raise UpdateFailed(str(error)) from error

    def _archive_response(
        self,
        profile: PricingProfile,
        now: datetime,
        history_days: int,
        horizon_days: int,
    ) -> dict:
        start_bound = now.astimezone(UTC) - timedelta(days=history_days)
        points = []
        stored_hours = 0
        for record in sorted(self.archive.values(), key=lambda item: item["start"]):
            start = _instant(record["start"], "Stored forecast start")
            if (
                record["horizon_days"] != horizon_days
                or not start_bound <= start < now.astimezone(UTC)
            ):
                continue
            stored_hours += 1
            raw = {
                key: _decimal(record[key], key)
                for key in (
                    "p10_spot_eur_mwh",
                    "p50_spot_eur_mwh",
                    "p90_spot_eur_mwh",
                )
            }
            for index in range(4):
                quarter = start + index * MARKET_INTERVAL
                points.append({
                    "start": quarter.astimezone(TALLINN).isoformat(),
                    "end": (
                        quarter + MARKET_INTERVAL
                    ).astimezone(TALLINN).isoformat(),
                    "issued_at": _instant(
                        record["issued_at"], "Stored forecast issuance"
                    ).astimezone(TALLINN).isoformat(),
                    "model_trained_at": _instant(
                        record["model_trained_at"],
                        "Stored forecast model timestamp",
                    ).astimezone(TALLINN).isoformat(),
                    "horizon_days": horizon_days,
                    "prediction": {
                        "p10_spot_eur_mwh": format(
                            raw["p10_spot_eur_mwh"], "f"
                        ),
                        "p50_spot_eur_mwh": format(
                            raw["p50_spot_eur_mwh"], "f"
                        ),
                        "p90_spot_eur_mwh": format(
                            raw["p90_spot_eur_mwh"], "f"
                        ),
                        "p10_import_eur_kwh": forecast_import_eur_kwh(
                            profile, raw["p10_spot_eur_mwh"], quarter
                        ),
                        "p50_import_eur_kwh": forecast_import_eur_kwh(
                            profile, raw["p50_spot_eur_mwh"], quarter
                        ),
                        "p90_import_eur_kwh": forecast_import_eur_kwh(
                            profile, raw["p90_spot_eur_mwh"], quarter
                        ),
                    },
                })
        return {
            "schema_version": 1,
            "view": "archive",
            "source": FORECAST_URL,
            "advisory_only": True,
            "timezone": "Europe/Tallinn",
            "currency": "EUR",
            "interval_minutes": 15,
            "price_basis": "household_all_in_import_including_applicable_vat",
            "forecast_basis": "raw_wholesale_p10_p50_p90_plus_household_tariff",
            "fixed_fees": "excluded",
            "history_days": history_days,
            "horizon_days": horizon_days,
            "as_of": now.astimezone(TALLINN).isoformat(),
            "stored_hours": stored_hours,
            "intervals": points,
            "profile_id": profile.id,
            "profile_version": profile.version,
            "profile_sha256": profile.sha256,
            "archive": {
                "retention_days": ARCHIVE_RETENTION_DAYS,
                "record_count": len(self.archive),
                "sampling": (
                    "Latest provider issuance retained per delivery hour and "
                    "provider horizon day"
                ),
            },
        }

    async def async_response(
        self,
        view: str,
        history_days: int,
        horizon_days: int,
    ) -> dict:
        if view == "current":
            if self.data is None:
                await self.async_request_refresh()
            if self.data is None:
                raise ServiceValidationError(
                    "The seven-day household price forecast is unavailable"
                    + (
                        ": " + self.refresh_error
                        if self.refresh_error is not None
                        else "."
                    )
                )
            response = copy.deepcopy(self.data.response)
            now = dt_util.utcnow().astimezone(UTC)
            issued_at = _instant(response["issued_at"], "Forecast issuance")
            age = (now - issued_at).total_seconds()
            response["as_of"] = now.astimezone(TALLINN).isoformat()
            response["age_seconds"] = age
            response["stale"] = age > FORECAST_STALE_SECONDS
            response["refresh_status"] = (
                "failed" if self.refresh_error is not None else "ok"
            )
            response["refresh_error"] = self.refresh_error
            if response["stale"] and not any(
                "forecast is stale" in warning.lower()
                for warning in response["warnings"]
            ):
                response["warnings"].append(
                    "Forecast is stale: issued {:.1f} hours ago.".format(
                        age / 3600
                    )
                )
            if self.refresh_error is not None:
                response["warnings"].append(
                    "Latest forecast refresh failed; the retained issuance is "
                    "shown explicitly: " + self.refresh_error
                )
            response["archive"]["record_count"] = len(self.archive)
            return response
        if view != "archive":
            raise ServiceValidationError("Choose current or archive forecast view.")
        try:
            profile = await self.hass.async_add_executor_job(self._profile)
            return await self.hass.async_add_executor_job(
                self._archive_response,
                profile,
                dt_util.utcnow().astimezone(UTC),
                history_days,
                horizon_days,
            )
        except PricingError as error:
            raise ServiceValidationError(str(error)) from error
