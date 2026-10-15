"""Constants for the household pricing integration."""

DOMAIN = "household_electricity_pricing"
NAME = "Household electricity pricing"
CONF_NORDPOOL_ENTRY = "nordpool_config_entry"
CONF_PROFILE_PATH = "profile_path"
DEFAULT_PROFILE_PATH = "private/pricing/profile.json"
SERVICE_GET_PRICES = "get_prices"
CONF_SOLAR_ENTRY = "solar_config_entry"
CONF_DAIKIN_ENTRY = "daikin_config_entry"
CONF_ELERING_PATH = "elering_credentials_path"
CONTROL_API_ID = "home_energy_devices"

CONF_DHW_MORNING_READY = "dhw_morning_ready"
CONF_DHW_EVENING_READY = "dhw_evening_ready"
CONF_DHW_SOFT_TARGET = "dhw_soft_target"
CONF_DHW_FLOOR_TARGET = "dhw_floor_target"
CONF_DHW_NORMAL_TARGET = "dhw_normal_target"
CONF_DHW_PREHEAT_TARGET = "dhw_preheat_target"
CONF_DHW_MAX_LEASE_HOURS = "dhw_max_lease_hours"
CONF_DHW_PRICE_DELTA = "dhw_price_delta_eur_kwh"
CONF_DHW_PRICE_PERCENT = "dhw_price_delta_percent"
CONF_DHW_INITIAL_HEAT_MINUTES = "dhw_initial_heat_minutes"
CONF_DHW_COAST_HOURS = "dhw_coast_horizon_hours"

DEFAULT_DHW_MORNING_READY = "07:00"
DEFAULT_DHW_EVENING_READY = "21:00"
DEFAULT_DHW_SOFT_TARGET = 30
DEFAULT_DHW_FLOOR_TARGET = 45
DEFAULT_DHW_NORMAL_TARGET = 50
DEFAULT_DHW_PREHEAT_TARGET = 55
DEFAULT_DHW_MAX_LEASE_HOURS = 4
DEFAULT_DHW_PRICE_DELTA = 0.05
DEFAULT_DHW_PRICE_PERCENT = 20
DEFAULT_DHW_INITIAL_HEAT_MINUTES = 90
DEFAULT_DHW_COAST_HOURS = 8

DHW_MODE_PAUSED = "paused"
DHW_MODE_SHADOW = "shadow"
DHW_MODE_SAFE = "safe_target_shift"
DHW_MODE_SOFT = "soft_standby"
DHW_MODES = (DHW_MODE_PAUSED, DHW_MODE_SHADOW, DHW_MODE_SAFE, DHW_MODE_SOFT)

EVENT_DHW_DECISION = DOMAIN + "_dhw_decision"
