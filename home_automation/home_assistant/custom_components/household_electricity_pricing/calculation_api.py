"""Shared schemas for native actions and explicitly selected read-only LLM tools."""

from __future__ import annotations

import probatio

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.helpers.llm import LLMContext, ToolAnnotations, ToolInput, ToolResult

from ._vendor.home_automation.home_assistant.calculator import ACTIONS, PERIODS
from .const import DOMAIN
from .coordinator import HouseholdPriceCoordinator


ENERGY_NUMBER = probatio.Any(str, int, float, None)
ENERGY_INTERVAL = {
    probatio.Required("start"): str,
    probatio.Required("end"): str,
    probatio.Optional("consumption_kwh"): ENERGY_NUMBER,
    probatio.Optional("export_kwh"): ENERGY_NUMBER,
}
PRICE_INTERVAL = {
    probatio.Required("start"): str,
    probatio.Required("end"): str,
    probatio.Required("price_eur_mwh"): probatio.Any(str, int, float),
}
PRICE_FIELDS = {
    probatio.Optional("price_basis", default="published"): probatio.In(("published", "estimate")),
    probatio.Optional("price_intervals"): [PRICE_INTERVAL],
}
CALCULATION_FIELDS = {
    ACTIONS[0]: {
        probatio.Required("start"): str,
        probatio.Required("end"): str,
        probatio.Optional("consumption_kwh"): ENERGY_NUMBER,
        probatio.Optional("export_kwh"): ENERGY_NUMBER,
        probatio.Optional("quantity_basis", default="raw"): probatio.In(("raw", "billable")),
        **PRICE_FIELDS,
    },
    ACTIONS[1]: {
        probatio.Optional("period", default="custom"): probatio.In(PERIODS),
        probatio.Optional("start_date"): str,
        probatio.Optional("end_date"): str,
        probatio.Optional("days"): probatio.All(int, probatio.Range(min=1)),
        probatio.Optional("intervals"): [ENERGY_INTERVAL],
        probatio.Optional("quantity_basis", default="raw"): probatio.In(("raw", "billable")),
        probatio.Optional("mode", default="auto"): probatio.In(("auto", "report", "monthly")),
        probatio.Optional("include_daily", default=False): bool,
        **PRICE_FIELDS,
    },
    ACTIONS[2]: {
        probatio.Optional("mode", default="load"): probatio.In(("load", "comparison")),
        probatio.Optional("start"): str,
        probatio.Optional("duration_minutes"): ENERGY_NUMBER,
        probatio.Optional("power_kw"): ENERGY_NUMBER,
        probatio.Optional("energy_kwh"): ENERGY_NUMBER,
        probatio.Optional("baseline_intervals"): [ENERGY_INTERVAL],
        probatio.Optional("scenario_intervals"): [ENERGY_INTERVAL],
        probatio.Optional("quantity_basis"): probatio.In(("raw", "billable")),
        **PRICE_FIELDS,
    },
}
_TOOLS = {
    ACTIONS[0]: (
        "QuoteHouseholdElectricityInterval",
        "Quote a household delivery interval",
        "Price one aligned, offset-aware 15-minute interval with supplied kWh. "
        "Raw and billable flows remain explicit; unchanged monthly fees are excluded.",
    ),
    ACTIONS[1]: (
        "CalculateHouseholdElectricityPeriod",
        "Calculate a household electricity bill or period",
        "Calculate month_to_date, previous_month, last_12_months, or custom dates/days "
        "using complete Elering quarters or supplied readings. Month-to-date includes "
        "full monthly fees and reports its actual metering cutoff. Actual-bill presets "
        "require published prices and known tariff history. Custom future estimates "
        "require supplied energy, price_basis estimate, and explicit price intervals. "
        "Missing data is an error, never zero; calculated bills are not issued invoices.",
    ),
    ACTIONS[2]: (
        "EstimateHouseholdElectricityScenario",
        "Estimate an appliance or grid-profile change",
        "Estimate additional billable grid import from a constant load, or the change "
        "between supplied before/after grid profiles. Include tariff fees and VAT, "
        "exclude unchanged monthly fees, and never infer appliance draw or solar behavior. "
        "Published prices are the default; supplied future prices must be labeled estimate.",
    ),
}


class HouseholdCalculationTool(llm.Tool):
    integration = DOMAIN
    annotations = ToolAnnotations(
        read_only=True, destructive=False, idempotent=True, open_world=True,
    )

    def __init__(self, action: str, coordinator: HouseholdPriceCoordinator) -> None:
        self.action = action
        self.coordinator = coordinator
        self.name, self.title, self.description = _TOOLS[action]
        self.parameters = probatio.Schema(CALCULATION_FIELDS[action])

    async def async_call(
        self, hass: HomeAssistant, tool_input: ToolInput, llm_context: LLMContext,
    ) -> ToolResult:
        return ToolResult(
            data=await self.coordinator.async_calculate_response(self.action, tool_input.tool_args),
        )
