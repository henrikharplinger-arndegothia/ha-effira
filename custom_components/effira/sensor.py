"""Sensor entities for Effira OPTi."""
from homeassistant.components.sensor import SensorEntity, SensorDeviceClass
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


async def async_setup_entry(hass, config_entry, async_add_entities):
    coordinator = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities([
        EffiraStatusSensor(coordinator, config_entry),
        EffiraOnlineSensor(coordinator, config_entry),
        EffiraTempSensor(coordinator, config_entry),
        EffiraPlanSensor(coordinator, config_entry),
        EffiraTimelineSensor(coordinator, config_entry),
    ])


class EffiraBaseSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, config_entry):
        super().__init__(coordinator)
        self._config_entry = config_entry

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._config_entry.entry_id)},
            "name": "Effira OPTi",
            "manufacturer": "Effira Energy",
            "model": "OPTi",
        }


class EffiraStatusSensor(EffiraBaseSensor):
    """Last action reported by the heat pump (idle / boost / stop)."""

    @property
    def unique_id(self):
        return f"{self._config_entry.entry_id}_status"

    @property
    def name(self):
        return "Effira Last Action"

    @property
    def state(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("last_action")

    @property
    def extra_state_attributes(self):
        if self.coordinator.data is None:
            return {}
        data = self.coordinator.data
        return {
            "source": data.get("last_action_source"),
            "manual_action": data.get("manual_action"),
            "last_action_at": data.get("last_action_at"),
        }


class EffiraOnlineSensor(EffiraBaseSensor):
    """Whether the heat pump is online according to the Effira API."""

    @property
    def unique_id(self):
        return f"{self._config_entry.entry_id}_online"

    @property
    def name(self):
        return "Effira Online"

    @property
    def state(self):
        if self.coordinator.data is None:
            return None
        online = self.coordinator.data.get("online")
        if online is True:
            return "online"
        if online is False:
            return "offline"
        return None


class EffiraTempSensor(EffiraBaseSensor):
    """Reference temperature as reported by the Effira system."""

    @property
    def unique_id(self):
        return f"{self._config_entry.entry_id}_ref_temp"

    @property
    def name(self):
        return "Effira Reference Temperature"

    @property
    def native_unit_of_measurement(self):
        return "°C"

    @property
    def device_class(self):
        return SensorDeviceClass.TEMPERATURE

    @property
    def state(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("ref_temp")


class EffiraPlanSensor(EffiraBaseSensor):
    """Number of active manual plan periods, with the periods as attributes."""

    @property
    def unique_id(self):
        return f"{self._config_entry.entry_id}_plan"

    @property
    def name(self):
        return "Effira Active Plan Periods"

    @property
    def state(self):
        if self.coordinator.data is None:
            return None
        return len(self.coordinator.data.get("active_periods", []))

    @property
    def extra_state_attributes(self):
        if self.coordinator.data is None:
            return {}
        return {"periods": self.coordinator.data.get("active_periods", [])}


class EffiraTimelineSensor(EffiraBaseSensor):
    """OPTi's own plan (Effira app timeline). State = mode right now: boost / stop / normal.

    Attribute `periods`: non-normal periods for the past 12 h and next 36 h,
    [{start, end, mode, reason, state}] — for plan graphs (e.g. apexcharts-card).
    """

    @property
    def unique_id(self):
        return f"{self._config_entry.entry_id}_timeline"

    @property
    def name(self):
        return "Effira OPTi Plan"

    @property
    def icon(self):
        return "mdi:chart-timeline-variant"

    def _periods(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("timeline")

    @property
    def available(self):
        return super().available and self._periods() is not None

    @property
    def state(self):
        from datetime import datetime, timezone
        periods = self._periods() or []
        now = datetime.now(timezone.utc)
        for p in periods:
            try:
                st = datetime.fromisoformat(p["start"].replace("Z", "+00:00"))
                en = datetime.fromisoformat(p["end"].replace("Z", "+00:00"))
            except (KeyError, AttributeError, ValueError):
                continue
            if st <= now < en:
                return p["mode"]
        return "normal"

    @property
    def extra_state_attributes(self):
        return {
            "periods": self._periods() or [],
            "fresh": bool((self.coordinator.data or {}).get("timeline_ok")),
        }
