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
ARCHIVE_SCHEMA_VERSION = 3
STORE_KEY_SUFFIX = "price_forecast"
HOUR = timedelta(hours=1)
ARCHIVE_ORIGIN_OBSERVED = "observed_vintage"
ARCHIVE_ORIGIN_MIGRATED = "migrated_latest_only"


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
            "evolution_summary",
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
        self.official_schedules: dict[str, dict] = {}
        self.vintage_tracking_started_at: str | None = None
        self.refresh_error: str | None = None

    async def async_load(self) -> None:
        stored = await self.store.async_load()
        if stored is None:
            return
        rows = stored.get("records") if isinstance(stored, dict) else None
        if not isinstance(rows, list):
            LOGGER.warning("Stored household price forecasts are invalid; archive reset.")
            return
        schema_version = stored.get("schema_version", 1)
        if schema_version not in (1, 2, ARCHIVE_SCHEMA_VERSION):
            LOGGER.warning(
                "Stored household price forecast schema %s is unsupported; "
                "archive reset.",
                schema_version,
            )
            return
        try:
            archive = {}
            default_origin = (
                ARCHIVE_ORIGIN_MIGRATED if schema_version == 1 else None
            )
            for row in rows:
                record = self._validated_archive_record(
                    row,
                    default_origin=default_origin,
                )
                key = self._archive_key(record)
                previous = archive.get(key)
                if previous is not None and previous != record:
                    raise ForecastError(
                        "Stored forecast records conflict for one issuance."
                    )
                archive[key] = record
            if schema_version in (2, ARCHIVE_SCHEMA_VERSION):
                stored_tracking = stored.get("vintage_tracking_started_at")
                tracking_started = (
                    _instant(
                        stored_tracking,
                        "Stored vintage tracking timestamp",
                    )
                    if stored_tracking is not None
                    else None
                )
            else:
                tracking_started = dt_util.utcnow().astimezone(UTC)
            schedules = {}
            if schema_version == ARCHIVE_SCHEMA_VERSION:
                stored_schedules = stored.get("official_schedules", [])
                if not isinstance(stored_schedules, list):
                    raise ForecastError(
                        "Stored official price schedules must be an array."
                    )
                for item in stored_schedules:
                    schedule = self._validated_official_schedule(item)
                    delivery_date = schedule["delivery_date"]
                    previous = schedules.get(delivery_date)
                    if previous is not None and previous != schedule:
                        raise ForecastError(
                            "Stored official price schedules conflict for one "
                            "delivery date."
                        )
                    schedules[delivery_date] = schedule
        except ForecastError as error:
            LOGGER.warning("Stored household price forecasts are invalid: %s", error)
            self.archive = {}
            self.official_schedules = {}
            self.vintage_tracking_started_at = None
            return
        self.archive = archive
        self.official_schedules = schedules
        self.vintage_tracking_started_at = (
            tracking_started.isoformat()
            if tracking_started is not None else None
        )
        if schema_version != ARCHIVE_SCHEMA_VERSION:
            await self._save_archive()

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
        evolution = self._evolution_response(
            profile,
            now,
            1,
            1,
            0,
            None,
            published_prices,
        )
        evolution_summary = {
            key: evolution[key]
            for key in (
                "delivery_date",
                "horizons_included",
                "retained_issuance_count",
                "first_retained_issued_at",
                "latest_retained_issued_at",
                "tracking_active_for_full_horizon",
                "contains_migrated_latest_only",
                "expected_intervals",
                "available_intervals",
                "complete_delivery_coverage",
                "partial_delivery_coverage",
                "official_intervals",
                "complete_official_coverage",
                "partial_official_coverage",
                "matched_intervals",
                "complete_matched_coverage",
                "partial_matched_coverage",
                "comparison_intervals",
                "first_retained_mae_eur_kwh",
                "latest_retained_mae_eur_kwh",
                "first_retained_bias_eur_kwh",
                "latest_retained_bias_eur_kwh",
                "mae_change_eur_kwh",
                "movement",
                "improved_intervals",
                "worsened_intervals",
                "unchanged_intervals",
            )
        }
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
            "evolution_summary": evolution_summary,
            "archive": {
                "retention_days": ARCHIVE_RETENTION_DAYS,
                "record_count": len(self.archive),
                "delivery_hour_count": self._delivery_hour_count(),
                "migrated_latest_only_records": self._migrated_record_count(),
                "official_schedule_count": len(self.official_schedules),
                "vintage_tracking_started_at": self.vintage_tracking_started_at,
                "sampling": (
                    "Distinct issuances observed by hourly polling are retained "
                    "per delivery hour and provider horizon day."
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
        return "{}|{}|{}".format(
            record.get("start"),
            record.get("horizon_days"),
            record.get("issued_at"),
        )

    @staticmethod
    def _validated_archive_record(
        record: object,
        *,
        default_origin: str | None = None,
    ) -> dict:
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
        origin = record.get("archive_origin", default_origin)
        if origin not in (ARCHIVE_ORIGIN_OBSERVED, ARCHIVE_ORIGIN_MIGRATED):
            raise ForecastError("Stored forecast archive origin is invalid")
        return {
            "start": start.isoformat(),
            "issued_at": issued_at.isoformat(),
            "model_trained_at": trained_at.isoformat(),
            "horizon_days": horizon,
            "p10_spot_eur_mwh": format(p10, "f"),
            "p50_spot_eur_mwh": format(p50, "f"),
            "p90_spot_eur_mwh": format(p90, "f"),
            "archive_origin": origin,
        }

    @staticmethod
    def _validated_official_schedule(schedule: object) -> dict:
        if not isinstance(schedule, dict):
            raise ForecastError("Stored official price schedule must be an object")
        delivery_text = schedule.get("delivery_date")
        if not isinstance(delivery_text, str):
            raise ForecastError(
                "Stored official price delivery date must be an ISO date"
            )
        try:
            delivery_day = date.fromisoformat(delivery_text)
        except ValueError as error:
            raise ForecastError(
                "Stored official price delivery date must be an ISO date"
            ) from error
        start, end = market_bounds(delivery_day)
        expected = int((end - start) / MARKET_INTERVAL)
        stored_expected = schedule.get("expected_intervals")
        if (
            isinstance(stored_expected, bool)
            or not isinstance(stored_expected, int)
            or stored_expected != expected
        ):
            raise ForecastError(
                "Stored official price interval count does not match the "
                "Tallinn delivery day"
            )
        profile = {}
        for key in ("profile_id", "profile_version", "profile_sha256"):
            value = schedule.get(key)
            if not isinstance(value, str) or not value:
                raise ForecastError(
                    "Stored official price schedule has invalid profile "
                    "provenance"
                )
            profile[key] = value
        rows = schedule.get("intervals")
        if not isinstance(rows, list):
            raise ForecastError(
                "Stored official price schedule intervals must be an array"
            )
        intervals = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ForecastError(
                    "Stored official price interval must be an object"
                )
            quarter = _instant(
                row.get("start"),
                "Stored official price start",
            )
            if (
                not start <= quarter < end
                or (quarter - start) % MARKET_INTERVAL != timedelta(0)
            ):
                raise ForecastError(
                    "Stored official price is outside its Tallinn delivery day"
                )
            value = _decimal(
                row.get("import_eur_kwh"),
                "Stored official household price",
            )
            key = quarter.isoformat()
            if key in intervals:
                raise ForecastError(
                    "Stored official price schedule contains duplicate quarters"
                )
            intervals[key] = {
                "start": key,
                "import_eur_kwh": format(value, "f"),
            }
        available = len(intervals)
        if available > expected:
            raise ForecastError(
                "Stored official price schedule has too many intervals"
            )
        stored_available = schedule.get("available_intervals")
        if (
            isinstance(stored_available, bool)
            or not isinstance(stored_available, int)
            or stored_available != available
        ):
            raise ForecastError(
                "Stored official price availability count is invalid"
            )
        complete = available == expected
        if schedule.get("complete") is not complete:
            raise ForecastError(
                "Stored official price completeness flag is invalid"
            )
        return {
            "delivery_date": delivery_day.isoformat(),
            "expected_intervals": expected,
            "available_intervals": available,
            "complete": complete,
            **profile,
            "intervals": [
                intervals[key] for key in sorted(intervals)
            ],
        }

    def _delivery_hour_count(self) -> int:
        return len({
            (record["start"], record["horizon_days"])
            for record in self.archive.values()
        })

    def _migrated_record_count(self) -> int:
        return sum(
            record["archive_origin"] == ARCHIVE_ORIGIN_MIGRATED
            for record in self.archive.values()
        )

    def _official_schedule_from_window(
        self,
        snapshot: object,
        window: object,
    ) -> dict | None:
        rows = [
            {
                "start": _instant(
                    point.start,
                    "Published price",
                ).isoformat(),
                "import_eur_kwh": format(
                    _decimal(
                        point.import_eur_kwh,
                        "Published household price",
                    ),
                    "f",
                ),
            }
            for point in window.prices
        ]
        if not rows:
            return None
        return self._validated_official_schedule({
            "delivery_date": window.delivery_date,
            "expected_intervals": window.expected_intervals,
            "available_intervals": len(rows),
            "complete": len(rows) == window.expected_intervals,
            "profile_id": snapshot.profile_id,
            "profile_version": snapshot.profile_version,
            "profile_sha256": snapshot.profile_sha256,
            "intervals": rows,
        })

    def _capture_official_schedules(self, now: datetime) -> bool:
        changed = False
        data = self.price_coordinator.data
        if data is not None and data.contains(now):
            for window in (data.today, data.tomorrow):
                current = self._official_schedule_from_window(data, window)
                if current is None:
                    continue
                previous = self.official_schedules.get(
                    current["delivery_date"]
                )
                if (
                    previous is not None
                    and all(
                        previous[key] == current[key]
                        for key in (
                            "profile_id",
                            "profile_version",
                            "profile_sha256",
                        )
                    )
                ):
                    merged = {
                        row["start"]: row
                        for row in previous["intervals"]
                    }
                    merged.update({
                        row["start"]: row
                        for row in current["intervals"]
                    })
                    current = self._validated_official_schedule({
                        **current,
                        "available_intervals": len(merged),
                        "complete": (
                            len(merged) == current["expected_intervals"]
                        ),
                        "intervals": list(merged.values()),
                    })
                if previous != current:
                    self.official_schedules[
                        current["delivery_date"]
                    ] = current
                    changed = True
        earliest_day = (
            now.astimezone(TALLINN).date()
            - timedelta(days=ARCHIVE_RETENTION_DAYS)
        )
        expired = [
            delivery_date
            for delivery_date in self.official_schedules
            if date.fromisoformat(delivery_date) < earliest_day
        ]
        for delivery_date in expired:
            del self.official_schedules[delivery_date]
            changed = True
        return changed

    def _record_report(self, report: ForecastReport, now: datetime) -> bool:
        changed = False
        start, end = _window_bounds(now)
        for slot in report.hours:
            slot_start = slot.start.astimezone(UTC)
            if not isinstance(slot, PredictedHour) or not start <= slot_start < end:
                continue
            if self.vintage_tracking_started_at is None:
                self.vintage_tracking_started_at = now.astimezone(UTC).isoformat()
                changed = True
            record = {
                "start": slot_start.isoformat(),
                "issued_at": report.issued_at.astimezone(UTC).isoformat(),
                "model_trained_at": report.model_trained_at.astimezone(UTC).isoformat(),
                "horizon_days": slot.horizon_days,
                "p10_spot_eur_mwh": format(slot.p10, "f"),
                "p50_spot_eur_mwh": format(slot.p50, "f"),
                "p90_spot_eur_mwh": format(slot.p90, "f"),
                "archive_origin": ARCHIVE_ORIGIN_OBSERVED,
            }
            key = self._archive_key(record)
            previous = self.archive.get(key)
            if previous is not None:
                comparable_keys = (
                    "start",
                    "issued_at",
                    "model_trained_at",
                    "horizon_days",
                    "p10_spot_eur_mwh",
                    "p50_spot_eur_mwh",
                    "p90_spot_eur_mwh",
                )
                if any(previous[key] != record[key] for key in comparable_keys):
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
            "schema_version": ARCHIVE_SCHEMA_VERSION,
            "vintage_tracking_started_at": self.vintage_tracking_started_at,
            "records": sorted(
                self.archive.values(),
                key=lambda record: (
                    record["start"],
                    record["horizon_days"],
                    record["issued_at"],
                ),
            ),
            "official_schedules": [
                self.official_schedules[key]
                for key in sorted(self.official_schedules)
            ],
        })

    async def _async_update_data(self) -> HouseholdPriceForecast:
        now = dt_util.utcnow().astimezone(UTC)
        schedules_changed = self._capture_official_schedules(now)
        try:
            profile, report, accuracy, accuracy_error = (
                await self.hass.async_add_executor_job(self._fetch_inputs)
            )
            archive_changed = self._record_report(report, now)
            if archive_changed or schedules_changed:
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
            result.response["archive"]["official_schedule_count"] = len(
                self.official_schedules
            )
            result.attributes["archive"] = result.response["archive"]
            self.refresh_error = None
            return result
        except (ForecastError, PricingError) as error:
            if schedules_changed:
                await self._save_archive()
            self.refresh_error = str(error)
            raise UpdateFailed(str(error)) from error

    def _archive_response(
        self,
        profile: PricingProfile,
        now: datetime,
        history_days: int,
        horizon_days: int,
        include_future_days: int,
    ) -> dict:
        now_utc = now.astimezone(UTC)
        start_bound = now_utc - timedelta(days=history_days)
        end_bound = now_utc
        if include_future_days:
            _, end_bound = market_bounds(
                now.astimezone(TALLINN).date()
                + timedelta(days=include_future_days)
            )
        latest_records = {}
        for record in self.archive.values():
            start = _instant(record["start"], "Stored forecast start")
            if (
                record["horizon_days"] != horizon_days
                or not start_bound <= start < end_bound
            ):
                continue
            previous = latest_records.get(record["start"])
            if (
                previous is None
                or _instant(
                    previous["issued_at"],
                    "Stored forecast issuance",
                )
                < _instant(record["issued_at"], "Stored forecast issuance")
            ):
                latest_records[record["start"]] = record
        points = []
        stored_hours = 0
        for record in sorted(
            latest_records.values(),
            key=lambda item: item["start"],
        ):
            start = _instant(record["start"], "Stored forecast start")
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
            "include_future_days": include_future_days,
            "as_of": now.astimezone(TALLINN).isoformat(),
            "stored_hours": stored_hours,
            "intervals": points,
            "profile_id": profile.id,
            "profile_version": profile.version,
            "profile_sha256": profile.sha256,
            "archive": {
                "retention_days": ARCHIVE_RETENTION_DAYS,
                "record_count": len(self.archive),
                "delivery_hour_count": self._delivery_hour_count(),
                "migrated_latest_only_records": self._migrated_record_count(),
                "official_schedule_count": len(self.official_schedules),
                "vintage_tracking_started_at": self.vintage_tracking_started_at,
                "sampling": (
                    "The latest retained issuance is selected per delivery hour "
                    "and provider horizon day. Use the evolution view for all "
                    "distinct issuances observed by hourly polling."
                ),
            },
        }

    def _evolution_response(
        self,
        profile: PricingProfile,
        now: datetime,
        history_days: int,
        horizon_days: int,
        include_future_days: int,
        delivery_date: date | None,
        published_prices: dict[datetime, str] | None = None,
        all_horizons: bool = False,
        official_schedules: dict[str, dict] | None = None,
    ) -> dict:
        local_today = now.astimezone(TALLINN).date()
        earliest_day = local_today - timedelta(days=history_days)
        latest_day = local_today + timedelta(days=include_future_days)
        if all_horizons and delivery_date is None:
            raise ForecastError(
                "Choose one delivery date when requesting all forecast "
                "horizons."
            )

        def record_matches_horizon(record: dict) -> bool:
            return (
                all_horizons
                or record["horizon_days"] == horizon_days
            )

        available_days = sorted({
            _instant(record["start"], "Stored forecast start")
            .astimezone(TALLINN)
            .date()
            for record in self.archive.values()
            if record_matches_horizon(record)
            and earliest_day
            <= _instant(record["start"], "Stored forecast start")
            .astimezone(TALLINN)
            .date()
            <= latest_day
        })
        selected_day = delivery_date
        if selected_day is not None and not earliest_day <= selected_day <= latest_day:
            raise ForecastError(
                "The forecast evolution delivery date is outside the requested "
                "history and future bounds."
            )
        if selected_day is None and available_days:
            selected_day = available_days[-1]

        selected_records = []
        if selected_day is not None:
            selected_records = [
                record
                for record in self.archive.values()
                if record_matches_horizon(record)
                and _instant(record["start"], "Stored forecast start")
                .astimezone(TALLINN)
                .date()
                == selected_day
            ]
        horizons_included = sorted({
            record["horizon_days"] for record in selected_records
        })

        expected_intervals = 0
        selected_start = None
        selected_end = None
        if selected_day is not None:
            selected_start, selected_end = market_bounds(selected_day)
            expected_intervals = int(
                (selected_end - selected_start) / MARKET_INTERVAL
            )

        official_schedule = None
        if selected_day is not None and official_schedules is not None:
            stored_schedule = official_schedules.get(
                selected_day.isoformat()
            )
            if stored_schedule is not None:
                official_schedule = self._validated_official_schedule(
                    stored_schedule
                )
        official_prices: dict[datetime, Decimal] = {}
        official_profile_matches: bool | None = None
        if official_schedule is not None:
            official_profile_matches = (
                official_schedule["profile_id"] == profile.id
                and official_schedule["profile_version"] == profile.version
                and official_schedule["profile_sha256"] == profile.sha256
            )
            if official_profile_matches:
                official_prices = {
                    _instant(
                        row["start"],
                        "Stored official price start",
                    ): _decimal(
                        row["import_eur_kwh"],
                        "Stored official household price",
                    )
                    for row in official_schedule["intervals"]
                }
        elif (
            selected_start is not None
            and selected_end is not None
            and published_prices is not None
        ):
            official_prices = {
                instant.astimezone(UTC): _decimal(
                    value,
                    "Published household price",
                )
                for instant, value in published_prices.items()
                if selected_start <= instant.astimezone(UTC) < selected_end
            }
            official_profile_matches = True
            official_schedule = {
                "delivery_date": selected_day.isoformat(),
                "expected_intervals": expected_intervals,
                "available_intervals": len(official_prices),
                "complete": (
                    expected_intervals > 0
                    and len(official_prices) == expected_intervals
                ),
                "profile_id": profile.id,
                "profile_version": profile.version,
                "profile_sha256": profile.sha256,
                "intervals": [
                    {
                        "start": instant.isoformat(),
                        "import_eur_kwh": format(value, "f"),
                    }
                    for instant, value in sorted(official_prices.items())
                ],
            }

        by_start: dict[str, list[dict]] = {}
        for record in selected_records:
            by_start.setdefault(record["start"], []).append(record)

        points = []
        comparisons = []
        for start_text in sorted(by_start):
            start = _instant(start_text, "Stored forecast start")
            records = sorted(
                by_start[start_text],
                key=lambda item: (
                    _instant(
                        item["issued_at"],
                        "Stored forecast issuance",
                    ),
                    -item["horizon_days"],
                ),
            )
            for index in range(4):
                quarter = start + index * MARKET_INTERVAL
                vintages = []
                for record in records:
                    raw = {
                        key: _decimal(record[key], key)
                        for key in (
                            "p10_spot_eur_mwh",
                            "p50_spot_eur_mwh",
                            "p90_spot_eur_mwh",
                        )
                    }
                    vintages.append({
                        "issued_at": _instant(
                            record["issued_at"],
                            "Stored forecast issuance",
                        ).astimezone(TALLINN).isoformat(),
                        "model_trained_at": _instant(
                            record["model_trained_at"],
                            "Stored forecast model timestamp",
                        ).astimezone(TALLINN).isoformat(),
                        "horizon_days": record["horizon_days"],
                        "archive_origin": record["archive_origin"],
                        "prediction": {
                            "p10_spot_eur_mwh": format(
                                raw["p10_spot_eur_mwh"],
                                "f",
                            ),
                            "p50_spot_eur_mwh": format(
                                raw["p50_spot_eur_mwh"],
                                "f",
                            ),
                            "p90_spot_eur_mwh": format(
                                raw["p90_spot_eur_mwh"],
                                "f",
                            ),
                            "p10_import_eur_kwh": forecast_import_eur_kwh(
                                profile,
                                raw["p10_spot_eur_mwh"],
                                quarter,
                            ),
                            "p50_import_eur_kwh": forecast_import_eur_kwh(
                                profile,
                                raw["p50_spot_eur_mwh"],
                                quarter,
                            ),
                            "p90_import_eur_kwh": forecast_import_eur_kwh(
                                profile,
                                raw["p90_spot_eur_mwh"],
                                quarter,
                            ),
                        },
                    })
                points.append({
                    "start": quarter.astimezone(TALLINN).isoformat(),
                    "end": (
                        quarter + MARKET_INTERVAL
                    ).astimezone(TALLINN).isoformat(),
                    "horizon_days": (
                        None if all_horizons else horizon_days
                    ),
                    "horizons_included": sorted({
                        vintage["horizon_days"]
                        for vintage in vintages
                    }),
                    "revision_count": len(vintages),
                    "first_retained": vintages[0],
                    "latest_retained": vintages[-1],
                    "vintages": vintages,
                })
                official = official_prices.get(quarter)
                if official is not None:
                    first_value = _decimal(
                        vintages[0]["prediction"]["p50_import_eur_kwh"],
                        "First retained household forecast",
                    )
                    latest_value = _decimal(
                        vintages[-1]["prediction"]["p50_import_eur_kwh"],
                        "Latest retained household forecast",
                    )
                    first_error = first_value - official
                    latest_error = latest_value - official
                    points[-1].update({
                        "official_import_eur_kwh": format(official, "f"),
                        "first_retained_error_eur_kwh": format(
                            first_error,
                            "f",
                        ),
                        "latest_retained_error_eur_kwh": format(
                            latest_error,
                            "f",
                        ),
                    })
                    comparisons.append((first_error, latest_error))
                else:
                    points[-1].update({
                        "official_import_eur_kwh": None,
                        "first_retained_error_eur_kwh": None,
                        "latest_retained_error_eur_kwh": None,
                    })

        issuance_groups: dict[tuple[datetime, int], dict] = {}
        for record in selected_records:
            issued_utc = _instant(
                record["issued_at"],
                "Stored forecast issuance",
            )
            key = (issued_utc, record["horizon_days"])
            item = issuance_groups.setdefault(
                key,
                {
                    "issued_at": issued_utc.astimezone(
                        TALLINN
                    ).isoformat(),
                    "model_trained_at": _instant(
                        record["model_trained_at"],
                        "Stored forecast model timestamp",
                    ).astimezone(TALLINN).isoformat(),
                    "horizon_days": record["horizon_days"],
                    "records": [],
                    "archive_origins": set(),
                },
            )
            item["records"].append(record)
            item["archive_origins"].add(record["archive_origin"])

        ordered_groups = [
            issuance_groups[key]
            for key in sorted(
                issuance_groups,
                key=lambda item: (item[0], -item[1]),
            )
        ]
        tracking_started = (
            _instant(
                self.vintage_tracking_started_at,
                "Vintage tracking timestamp",
            )
            if self.vintage_tracking_started_at is not None
            else None
        )
        matched_sets = []
        for item in ordered_groups:
            predictions = {}
            for record in item["records"]:
                start = _instant(
                    record["start"],
                    "Stored forecast start",
                )
                raw_p50 = _decimal(
                    record["p50_spot_eur_mwh"],
                    "Stored forecast p50",
                )
                for index in range(4):
                    quarter = start + index * MARKET_INTERVAL
                    value = _decimal(
                        forecast_import_eur_kwh(
                            profile,
                            raw_p50,
                            quarter,
                        ),
                        "Retained household forecast",
                    )
                    previous = predictions.get(quarter)
                    if previous is not None and previous != value:
                        raise ForecastError(
                            "One retained issuance conflicts for a delivery "
                            "quarter."
                        )
                    predictions[quarter] = value
            item["predictions"] = predictions
            matched = set(predictions) & set(official_prices)
            item["matched"] = matched
            matched_sets.append(matched)
            if selected_day is not None and tracking_started is not None:
                required_start, _ = market_bounds(
                    selected_day - timedelta(
                        days=item["horizon_days"]
                    )
                )
                item["tracking_active_for_full_horizon"] = (
                    tracking_started <= required_start
                )
            else:
                item["tracking_active_for_full_horizon"] = False

        common_matched = (
            set.intersection(*matched_sets)
            if matched_sets else set()
        )
        issuance_metrics = []
        ordered_issuances = []
        previous_mae_value = None
        for item in ordered_groups:
            origins = sorted(item["archive_origins"])
            contains_migrated_item = (
                ARCHIVE_ORIGIN_MIGRATED in item["archive_origins"]
            )
            archive_origin = (
                origins[0] if len(origins) == 1 else "mixed"
            )
            errors = [
                item["predictions"][quarter]
                - official_prices[quarter]
                for quarter in sorted(common_matched)
            ]
            mae_value = None
            bias_value = None
            if errors:
                with localcontext() as context:
                    context.prec = 80
                    mae_value = (
                        sum(
                            (abs(error) for error in errors),
                            Decimal(0),
                        )
                        / len(errors)
                    )
                    bias_value = sum(
                        errors,
                        Decimal(0),
                    ) / len(errors)
            movement_vs_previous = "unavailable"
            mae_change_vs_previous = None
            if (
                previous_mae_value is not None
                and mae_value is not None
            ):
                mae_change_vs_previous = (
                    mae_value - previous_mae_value
                )
                if mae_value < previous_mae_value:
                    movement_vs_previous = "improved"
                elif mae_value > previous_mae_value:
                    movement_vs_previous = "worsened"
                else:
                    movement_vs_previous = "unchanged"
            metric = {
                "issued_at": item["issued_at"],
                "model_trained_at": item["model_trained_at"],
                "horizon_days": item["horizon_days"],
                "archive_origin": archive_origin,
                "archive_origins": origins,
                "contains_migrated_latest_only": contains_migrated_item,
                "tracking_active_for_full_horizon": (
                    item["tracking_active_for_full_horizon"]
                ),
                "partial_history": (
                    contains_migrated_item
                    or not item["tracking_active_for_full_horizon"]
                ),
                "available_intervals": len(item["predictions"]),
                "matched_intervals": len(item["matched"]),
                "common_coverage_matched_intervals": len(
                    common_matched
                ),
                "complete_delivery_coverage": (
                    expected_intervals > 0
                    and len(item["predictions"]) == expected_intervals
                ),
                "partial_delivery_coverage": (
                    len(item["predictions"]) > 0
                    and len(item["predictions"]) != expected_intervals
                ),
                "complete_matched_coverage": (
                    expected_intervals > 0
                    and len(item["matched"]) == expected_intervals
                ),
                "partial_matched_coverage": (
                    len(item["matched"]) > 0
                    and len(item["matched"]) != expected_intervals
                ),
                "mae_eur_kwh": (
                    format(mae_value, "f")
                    if mae_value is not None else None
                ),
                "bias_eur_kwh": (
                    format(bias_value, "f")
                    if bias_value is not None else None
                ),
                "mae_change_vs_previous_eur_kwh": (
                    format(mae_change_vs_previous, "f")
                    if mae_change_vs_previous is not None else None
                ),
                "movement_vs_previous": movement_vs_previous,
            }
            issuance_metrics.append(metric)
            ordered_issuances.append({
                "issued_at": item["issued_at"],
                "model_trained_at": item["model_trained_at"],
                "horizon_days": item["horizon_days"],
                "archive_origin": archive_origin,
                "archive_origins": origins,
                "delivery_hours": len(item["records"]),
                "delivery_intervals": len(item["predictions"]),
                "contains_migrated_latest_only": contains_migrated_item,
                "partial_history": metric["partial_history"],
            })
            previous_mae_value = mae_value

        contains_migrated = any(
            record["archive_origin"] == ARCHIVE_ORIGIN_MIGRATED
            for record in selected_records
        )
        tracking_active_for_full_horizon = (
            bool(ordered_groups)
            and all(
                item["tracking_active_for_full_horizon"]
                for item in ordered_groups
            )
        )
        warnings = []
        if selected_day is None or not selected_records:
            warnings.append(
                "No retained forecast vintages match the requested delivery date "
                + (
                    "and horizons."
                    if all_horizons else "and horizon."
                )
            )
        if contains_migrated:
            warnings.append(
                "Migrated latest-only records are partial history; the earliest "
                "retained value is not guaranteed to be the provider's first "
                "issuance."
            )
        if selected_records and not tracking_active_for_full_horizon:
            if all_horizons:
                incomplete_horizons = sorted({
                    item["horizon_days"]
                    for item in ordered_groups
                    if not item["tracking_active_for_full_horizon"]
                })
                warnings.append(
                    "Vintage tracking was not active for every complete "
                    "capture window at horizons {}; first retained values are "
                    "only the earliest available records.".format(
                        ", ".join(
                            "D-{}".format(item)
                            for item in incomplete_horizons
                        )
                    )
                )
            else:
                warnings.append(
                    "Vintage tracking was not active for the complete D-{} "
                    "capture window; the first retained value is only the "
                    "earliest available record.".format(horizon_days)
                )
        if official_profile_matches is False:
            warnings.append(
                "The retained official schedule uses a different pricing "
                "profile, so no household-price comparison is reported."
            )
        if len(ordered_groups) > 1 and not common_matched:
            warnings.append(
                "Retained issuances have no common published-price coverage; "
                "issuance movement is unavailable."
            )

        first_mae = None
        latest_mae = None
        first_bias = None
        latest_bias = None
        mae_change = None
        improved_intervals = 0
        worsened_intervals = 0
        unchanged_intervals = 0
        movement = "unavailable"
        if comparisons:
            with localcontext() as context:
                context.prec = 80
                first_mae_value = sum(
                    abs(first) for first, _ in comparisons
                ) / len(comparisons)
                latest_mae_value = sum(
                    abs(latest) for _, latest in comparisons
                ) / len(comparisons)
                first_bias_value = sum(
                    first for first, _ in comparisons
                ) / len(comparisons)
                latest_bias_value = sum(
                    latest for _, latest in comparisons
                ) / len(comparisons)
                mae_change_value = latest_mae_value - first_mae_value
            first_mae = format(first_mae_value, "f")
            latest_mae = format(latest_mae_value, "f")
            first_bias = format(first_bias_value, "f")
            latest_bias = format(latest_bias_value, "f")
            mae_change = format(mae_change_value, "f")
            for first_error, latest_error in comparisons:
                if abs(latest_error) < abs(first_error):
                    improved_intervals += 1
                elif abs(latest_error) > abs(first_error):
                    worsened_intervals += 1
                else:
                    unchanged_intervals += 1
            if latest_mae_value < first_mae_value:
                movement = "improved"
            elif latest_mae_value > first_mae_value:
                movement = "worsened"
            else:
                movement = "unchanged"

        available_intervals = len(points)
        official_intervals = (
            official_schedule["available_intervals"]
            if official_schedule is not None else 0
        )
        matched_intervals = len(comparisons)
        complete_delivery_coverage = (
            expected_intervals > 0
            and available_intervals == expected_intervals
        )
        complete_official_coverage = (
            expected_intervals > 0
            and official_intervals == expected_intervals
        )
        complete_matched_coverage = (
            expected_intervals > 0
            and matched_intervals == expected_intervals
        )
        return {
            "schema_version": 3,
            "view": "evolution",
            "source": FORECAST_URL,
            "advisory_only": True,
            "timezone": "Europe/Tallinn",
            "currency": "EUR",
            "interval_minutes": 15,
            "price_basis": "household_all_in_import_including_applicable_vat",
            "forecast_basis": "raw_wholesale_p10_p50_p90_plus_household_tariff",
            "fixed_fees": "excluded",
            "history_days": history_days,
            "horizon_days": (
                None if all_horizons else horizon_days
            ),
            "all_horizons": all_horizons,
            "horizons_included": horizons_included,
            "include_future_days": include_future_days,
            "delivery_date": (
                selected_day.isoformat() if selected_day is not None else None
            ),
            "available_delivery_dates": [
                item.isoformat() for item in available_days
            ],
            "as_of": now.astimezone(TALLINN).isoformat(),
            "expected_intervals": expected_intervals,
            "available_intervals": available_intervals,
            "complete_delivery_coverage": complete_delivery_coverage,
            "partial_delivery_coverage": (
                available_intervals > 0
                and not complete_delivery_coverage
            ),
            "official_intervals": official_intervals,
            "complete_official_coverage": complete_official_coverage,
            "partial_official_coverage": (
                official_intervals > 0
                and not complete_official_coverage
            ),
            "matched_intervals": matched_intervals,
            "complete_matched_coverage": complete_matched_coverage,
            "partial_matched_coverage": (
                matched_intervals > 0
                and not complete_matched_coverage
            ),
            "common_coverage_matched_intervals": len(common_matched),
            "retained_issuance_count": len(ordered_issuances),
            "first_retained_issued_at": (
                ordered_issuances[0]["issued_at"]
                if ordered_issuances else None
            ),
            "latest_retained_issued_at": (
                ordered_issuances[-1]["issued_at"]
                if ordered_issuances else None
            ),
            "vintage_tracking_started_at": self.vintage_tracking_started_at,
            "tracking_active_for_full_horizon": (
                tracking_active_for_full_horizon
            ),
            "contains_migrated_latest_only": contains_migrated,
            "comparison_intervals": matched_intervals,
            "first_retained_mae_eur_kwh": first_mae,
            "latest_retained_mae_eur_kwh": latest_mae,
            "first_retained_bias_eur_kwh": first_bias,
            "latest_retained_bias_eur_kwh": latest_bias,
            "mae_change_eur_kwh": mae_change,
            "movement": movement,
            "improved_intervals": improved_intervals,
            "worsened_intervals": worsened_intervals,
            "unchanged_intervals": unchanged_intervals,
            "issuances": ordered_issuances,
            "issuance_metrics": issuance_metrics,
            "intervals": points,
            "profile_id": profile.id,
            "profile_version": profile.version,
            "profile_sha256": profile.sha256,
            "official_schedule": {
                "source": "Home Assistant Nord Pool EE/EUR",
                "delivery_date": (
                    selected_day.isoformat()
                    if selected_day is not None else None
                ),
                "expected_intervals": expected_intervals,
                "available_intervals": official_intervals,
                "complete": complete_official_coverage,
                "profile_id": (
                    official_schedule["profile_id"]
                    if official_schedule is not None else None
                ),
                "profile_version": (
                    official_schedule["profile_version"]
                    if official_schedule is not None else None
                ),
                "profile_sha256": (
                    official_schedule["profile_sha256"]
                    if official_schedule is not None else None
                ),
                "profile_matches_forecast_profile": (
                    official_profile_matches
                ),
            },
            "archive": {
                "retention_days": ARCHIVE_RETENTION_DAYS,
                "record_count": len(self.archive),
                "delivery_hour_count": self._delivery_hour_count(),
                "migrated_latest_only_records": self._migrated_record_count(),
                "official_schedule_count": len(self.official_schedules),
                "sampling": (
                    "Distinct issuances observed by hourly polling are returned "
                    "chronologically per delivery hour and provider horizon day."
                ),
            },
            "warnings": warnings,
        }

    async def async_response(
        self,
        view: str,
        history_days: int,
        horizon_days: int,
        include_future_days: int = 0,
        delivery_date: date | None = None,
        all_horizons: bool = False,
    ) -> dict:
        if all_horizons and view != "evolution":
            raise ServiceValidationError(
                "all_horizons is available only for the evolution view."
            )
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
            response["archive"]["official_schedule_count"] = len(
                self.official_schedules
            )
            return response
        if view not in ("archive", "evolution"):
            raise ServiceValidationError(
                "Choose current, archive, or evolution forecast view."
            )
        try:
            now = dt_util.utcnow().astimezone(UTC)
            if (
                view == "evolution"
                and self._capture_official_schedules(now)
            ):
                await self._save_archive()
            profile = await self.hass.async_add_executor_job(self._profile)
            response_method = (
                self._archive_response
                if view == "archive"
                else self._evolution_response
            )
            arguments = [
                profile,
                now,
                history_days,
                horizon_days,
                include_future_days,
            ]
            if view == "evolution":
                arguments.extend([
                    delivery_date,
                    None,
                    all_horizons,
                    copy.deepcopy(self.official_schedules),
                ])
            return await self.hass.async_add_executor_job(
                response_method,
                *arguments,
            )
        except (ForecastError, PricingError) as error:
            raise ServiceValidationError(str(error)) from error
