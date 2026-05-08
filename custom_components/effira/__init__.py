"""Effira OPTi integration for Home Assistant."""
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN, ACTION_BOOST, ACTION_STOP, ACTION_NORMAL
from .coordinator import EffiraCoordinator

PLATFORMS = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    coordinator = EffiraCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN][entry.entry_id] = coordinator

    async def handle_boost(call: ServiceCall):
        await coordinator.async_action(ACTION_BOOST)

    async def handle_stop(call: ServiceCall):
        await coordinator.async_action(ACTION_STOP)

    async def handle_normal(call: ServiceCall):
        await coordinator.async_action(ACTION_NORMAL)

    async def handle_clear_plan(call: ServiceCall):
        await coordinator.async_clear_plan()

    hass.services.async_register(DOMAIN, "boost", handle_boost)
    hass.services.async_register(DOMAIN, "stop", handle_stop)
    hass.services.async_register(DOMAIN, "normal", handle_normal)
    hass.services.async_register(DOMAIN, "clear_plan", handle_clear_plan)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
