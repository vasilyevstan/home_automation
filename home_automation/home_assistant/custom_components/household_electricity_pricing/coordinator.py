"""Refresh quarter prices without blocking HA or installing an MCP runtime."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from ._vendor.home_automation.electricity_market.prices import MARKET_INTERVAL, TALLINN, UTC, PriceError
from ._vendor.home_automation.electricity_pricing.engine import PricingError, load_profile
from ._vendor.home_automation.household_consumption.client import ConsumptionError, Credentials, EstfeedClient
from ._vendor.home_automation.home_assistant.calculator import ElectricityCalculator, load_credentials_file
from ._vendor.home_automation.home_assistant.pricing import (
    NORDPOOL_TIMEZONE,
    PriceSnapshot,
    build_snapshot,
    complete_market_day,
    market_dates,
    market_day_bounds,
    parse_nordpool_prices,
    profile_path,
)
from .const import CONF_ELERING_PATH, CONF_NORDPOOL_ENTRY, CONF_PROFILE_PATH, DOMAIN


LOGGER = logging.getLogger(__name__)
QUARTER_REFRESH_TRANSITION_SECONDS = 60


class HouseholdPriceCoordinator(DataUpdateCoordinator[PriceSnapshot]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, LOGGER, name=DOMAIN, config_entry=entry)
        self.nordpool_entry_id = entry.data[CONF_NORDPOOL_ENTRY]
        self.profile_name = entry.data[CONF_PROFILE_PATH]
        self.market_errors: dict[date, str] = {}
        self._cache: dict[date, dict[datetime, Decimal]] = {}
        self._attempted: dict[date, datetime] = {}
        self._quarter_refresh_in_progress = False
        self._quarter_refresh_started_at: datetime | None = None
        self._quarter_refresh_hold_until: datetime | None = None
        self.credentials_name = entry.data.get(CONF_ELERING_PATH)
        self.calculator = ElectricityCalculator(
            profile_provider=lambda: load_profile(profile_path(Path(hass.config.config_dir), self.profile_name)),
            client=EstfeedClient(credentials_provider=self._credentials),
            now_provider=dt_util.utcnow,
        )

    def _credentials(self) -> Credentials:
        if not self.credentials_name:
            raise ConsumptionError("Configure the optional private Elering credentials path for automatic household measurements.")
        return load_credentials_file(Path(self.hass.config.config_dir), self.credentials_name)

    async def async_calculate_response(self, action: str, arguments: dict) -> dict:
        try:
            return await self.hass.async_add_executor_job(self.calculator.invoke, action, arguments)
        except (PricingError, ConsumptionError, PriceError) as error:
            raise ServiceValidationError(str(error)) from error

    async def async_quarter_changed(self, now: datetime) -> None:
        # Expire the previous quarter before any provider request can block.
        self._quarter_refresh_in_progress = True
        self._quarter_refresh_started_at = now
        self._quarter_refresh_hold_until = now + timedelta(
            seconds=QUARTER_REFRESH_TRANSITION_SECONDS
        )
        self.async_update_listeners()
        try:
            await self.async_request_refresh()
        finally:
            self._quarter_refresh_in_progress = False

    def in_quarter_refresh_transition(self, now: datetime) -> bool:
        """Return whether stale current data is inside the bounded rollover hold."""
        started = self._quarter_refresh_started_at
        hold_until = self._quarter_refresh_hold_until
        if now.utcoffset() is None:
            return False
        if (
            started is not None
            and hold_until is not None
            and started <= now <= hold_until
        ):
            return True
        valid_until = getattr(self.data, "valid_until", None)
        if (
            not isinstance(valid_until, datetime)
            or valid_until.utcoffset() is None
        ):
            return False
        elapsed = (now - valid_until).total_seconds()
        return 0 <= elapsed <= QUARTER_REFRESH_TRANSITION_SECONDS

    async def async_price_response(self, period: str) -> dict:
        if period not in ("today", "tomorrow"):
            raise ServiceValidationError("Choose today or tomorrow in Europe/Tallinn.")
        if not self.last_update_success or self.data is None or not self.data.contains(dt_util.utcnow()):
            await self.async_request_refresh()
        if not self.last_update_success or self.data is None or not self.data.contains(dt_util.utcnow()):
            raise ServiceValidationError("Current household prices are unavailable; no stale price window is returned.")
        window = self.data.today if period == "today" else self.data.tomorrow
        return {
            **window.as_dict(),
            "profile_version": self.data.profile_version,
            "profile_sha256": self.data.profile_sha256,
            "market_errors": list(self.market_errors.values()),
        }

    async def _async_update_data(self) -> PriceSnapshot:
        nordpool = self.hass.config_entries.async_get_entry(self.nordpool_entry_id)
        if nordpool is None or nordpool.domain != "nordpool" or nordpool.state is not ConfigEntryState.LOADED:
            raise UpdateFailed("The selected Nord Pool entry is not loaded.")
        if nordpool.data.get("currency") != "EUR" or "EE" not in nordpool.data.get("areas", []):
            raise UpdateFailed("The selected Nord Pool entry must use EUR and include EE.")
        try:
            path = profile_path(Path(self.hass.config.config_dir), self.profile_name)
            profile = await self.hass.async_add_executor_job(load_profile, path)
            now = dt_util.utcnow()
            days = market_dates(now.astimezone(TALLINN).date())
            self._cache = {day: rows for day, rows in self._cache.items() if day in days}
            self._attempted = {day: stamp for day, stamp in self._attempted.items() if day in days}
            self.market_errors = {day: error for day, error in self.market_errors.items() if day in days}
            current = datetime.fromtimestamp(int(now.timestamp()) // 900 * 900, UTC)
            current_market_day = now.astimezone(NORDPOOL_TIMEZONE).date()
            for day in days:
                cached = self._cache.get(day, {})
                if complete_market_day(cached, day):
                    continue
                attempted = self._attempted.get(day)
                needs_current = day == current_market_day and current not in cached
                if attempted is not None and now - attempted < timedelta(hours=1) and not needs_current:
                    continue
                self._attempted[day] = now
                try:
                    response = await self.hass.services.async_call(
                        "nordpool", "get_prices_for_date",
                        {
                            "config_entry": self.nordpool_entry_id,
                            "date": day.isoformat(), "areas": ["EE"], "currency": "EUR",
                        },
                        blocking=True, return_response=True,
                    )
                    rows = parse_nordpool_prices([response])
                    first, last = market_day_bounds(day)
                    if any(stamp < first or stamp + MARKET_INTERVAL > last for stamp in rows):
                        raise PricingError("Nord Pool returned quarters outside the requested market day.")
                except (HomeAssistantError, PricingError, TimeoutError) as error:
                    self._cache.pop(day, None)
                    message = "{}: Nord Pool retrieval failed ({}): {}".format(
                        day, type(error).__name__, error,
                    )
                    if self.market_errors.get(day) != message:
                        LOGGER.warning("%s", message)
                    self.market_errors[day] = message
                else:
                    self._cache[day] = rows
                    self.market_errors.pop(day, None)
            prices = {}
            for rows in self._cache.values():
                if prices.keys() & rows.keys():
                    raise PricingError("Overlapping Nord Pool market-day responses.")
                prices.update(rows)
            return await self.hass.async_add_executor_job(build_snapshot, profile, prices, dt_util.utcnow())
        except PricingError as error:
            raise UpdateFailed(str(error)) from error
