"""A deliberately selected read-only API, not an implicit HA LLM platform."""

from __future__ import annotations

import probatio

from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.helpers.llm import LLMContext, ToolAnnotations, ToolInput, ToolResult

from .const import DOMAIN
from .coordinator import HouseholdPriceCoordinator


class HouseholdPricesTool(llm.Tool):
    name = "GetHouseholdElectricityPrices"
    title = "Read household electricity prices"
    description = (
        "Read published quarter-hour spot prices, all-in marginal import costs, "
        "and net export earnings for today or tomorrow in Europe/Tallinn. "
        "Rates are EUR/kWh, fixed fees are excluded, and incomplete coverage "
        "and tariff provenance remain explicit. This tool does not control devices."
    )
    integration = DOMAIN
    annotations = ToolAnnotations(
        read_only=True, destructive=False, idempotent=True, open_world=True
    )
    parameters = probatio.Schema({
        probatio.Optional("period", default="today"): probatio.In(("today", "tomorrow")),
    })

    def __init__(self, coordinator: HouseholdPriceCoordinator) -> None:
        self.coordinator = coordinator

    async def async_call(
        self, hass: HomeAssistant, tool_input: ToolInput, llm_context: LLMContext
    ) -> ToolResult:
        return ToolResult(
            data=await self.coordinator.async_price_response(tool_input.tool_args.get("period", "today"))
        )


class HouseholdPriceAPI(llm.API):
    def __init__(self, hass: HomeAssistant, coordinator: HouseholdPriceCoordinator) -> None:
        super().__init__(hass=hass, id=DOMAIN, name="Household electricity prices (read only)")
        self.coordinator = coordinator

    async def async_get_api_instance(self, llm_context: LLMContext) -> llm.APIInstance:
        return llm.APIInstance(
            api=self,
            api_prompt=(
                "Use the household price tool for published, tariff-aware quarter prices. "
                "These are marginal rates, not an invoice. Preserve negative export "
                "earnings, missing-data reasons, and invoice-derived qualifications. "
                "Do not call incomplete next-day coverage a complete forecast."
            ),
            llm_context=llm_context,
            tools=[HouseholdPricesTool(self.coordinator)],
        )
