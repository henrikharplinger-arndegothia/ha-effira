"""Effira OPTi integration for Home Assistant."""
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
import homeassistant.helpers.config_validation as cv
import homeassistant.util.dt as dt_util
import voluptuous as vol

from .const import DOMAIN, ACTION_BOOST, ACTION_STOP, ACTION_NORMAL
from .coordinator import EffiraCoordinator

PLATFORMS = ["sensor"]

ACTION_SCHEMA = vol.Schema({
    vol.Optional("start"): cv.datetime,
    vol.Optional("end"): cv.datetime,
    vol.Optional("replace", default=False): cv.boolean,
})


def _utc(value):
    """Service datetimes without a zone are HA local time."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return dt_util.as_utc(value)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    coordinator = EffiraCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN][entry.entry_id] = coordinator

    def make_handler(action):
        async def handler(call: ServiceCall):
            try:
                await coordinator.async_action(
                    action, _utc(call.data.get("start")), _utc(call.data.get("end")),
                    call.data.get("replace", False),
                )
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err
        return handler

    async def handle_clear_plan(call: ServiceCall):
        await coordinator.async_clear_plan()

    for action in (ACTION_BOOST, ACTION_STOP, ACTION_NORMAL):
        hass.services.async_register(DOMAIN, action, make_handler(action), schema=ACTION_SCHEMA)
    hass.services.async_register(DOMAIN, "clear_plan", handle_clear_plan)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
