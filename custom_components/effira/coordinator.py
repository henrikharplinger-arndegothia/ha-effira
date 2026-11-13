"""DataUpdateCoordinator for Effira OPTi.

Polls currentStatus, referencetemperature, plan/manual, the indoor temperature and the OPTi timeline (the 24 h
plan OPTi makes and adapts during the day; same endpoint as the Effira app) every 5 minutes.
Provides async_action() and async_clear_plan() for HA services to call.
"""
import logging
from datetime import datetime, timezone, timedelta

import requests
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, EFFIRA_BASE

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=5)


class EffiraCoordinator(DataUpdateCoordinator):

    def __init__(self, hass, config_entry):
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self.config_entry = config_entry
        self._timeline_warned = False
        self._indoor_warned = False

    # ── Auth ──────────────────────────────────────────────────────────────────

    def _effira_token(self):
        key_id = self.config_entry.data["key_id"]
        key_secret = self.config_entry.data["key_secret"]
        r = requests.post(
            f"{EFFIRA_BASE}/api/v1/auth/token",
            auth=(key_id, key_secret),
            timeout=10,
        )
        r.raise_for_status()
        return r.json()["access_token"]

    # ── Time helpers ──────────────────────────────────────────────────────────

    def _quantize_up(self, dt):
        rem = dt.minute % 15
        if rem == 0 and dt.second == 0 and dt.microsecond == 0:
            return dt
        return (dt + timedelta(minutes=15 - rem)).replace(second=0, microsecond=0)

    def _fmt(self, dt):
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # ── API calls ─────────────────────────────────────────────────────────────

    def _get(self, token, path):
        r = requests.get(
            f"{EFFIRA_BASE}/api/v1{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        r.raise_for_status()
        return r.json() if r.content else None

    def _fetch_all(self, asset_id):
        token = self._effira_token()
        status = self._get(token, f"/assets/{asset_id}/currentStatus")
        ref_temp = self._get(token, f"/assets/{asset_id}/referencetemperature")
        manual_plan = self._get(token, f"/assets/{asset_id}/plan/manual")
        timeline = self._fetch_timeline(token, asset_id)
        indoor = self._fetch_indoor(token, asset_id)
        return status, ref_temp, manual_plan, timeline, indoor

    def _fetch_indoor(self, token, asset_id):
        """Latest reading of the asset's primary indoor temperature sensor (tempsensor/latest).

        Optional: returns None on any error or when the API answers 204 (no reading in 30 days).
        """
        try:
            r = requests.get(
                f"{EFFIRA_BASE}/api/v1/assets/{asset_id}/tempsensor/latest",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            r.raise_for_status()
            if r.status_code == 204 or not r.content:
                return None
            data = r.json() or {}
        except Exception as err:  # noqa: BLE001 — optional data
            if not self._indoor_warned:
                _LOGGER.warning("Effira indoor temperature not available: %s", err)
                self._indoor_warned = True
            return None
        self._indoor_warned = False
        if not isinstance(data.get("temperature"), (int, float)):
            return None
        return {
            "temperature": round(float(data["temperature"]), 2),
            "measured_at": data.get("timeStamp"),
            "sensor_id": data.get("sensorId"),
        }

    def _fetch_timeline(self, token, asset_id):
        """OPTi plan: past 12 h + next 36 h. Optional — failure never breaks the other sensors.

        Returns a compact list of boost/stop periods [{start, end, mode, reason, sg_ready}] with
        adjacent equal periods merged, or None if the endpoint is unavailable.
        """
        now = datetime.now(timezone.utc)
        start = (now - timedelta(hours=12)).replace(minute=0, second=0, microsecond=0)
        stop = start + timedelta(hours=48)
        try:
            r = requests.get(
                f"{EFFIRA_BASE}/api/v1/assets/{asset_id}/timeline",
                params={"start": self._fmt(start), "stop": self._fmt(stop)},
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json() or {}
        except Exception as err:  # noqa: BLE001 — optional data
            if not self._timeline_warned:
                _LOGGER.warning("Effira timeline not available: %s", err)
                self._timeline_warned = True
            return None
        self._timeline_warned = False
        periods = []
        # item.state is the action (boost / stop); control.mode is the SG Ready signal sent to
        # the heat pump (e.g. SG-READY-1 = blocked, SG-READY-2 = normal). Verified 2026-11-13.
        for item in sorted(data.get("timeline") or [], key=lambda i: i.get("start") or ""):
            action = item.get("state")
            if action not in ("boost", "stop"):
                continue
            control = item.get("control") or {}
            reason = item.get("reason") or (item.get("priority") or {}).get("name")
            p = {"start": item.get("start"), "end": item.get("end"), "mode": action,
                 "reason": reason, "sg_ready": control.get("mode")}
            last = periods[-1] if periods else None
            if last and last["end"] == p["start"] and last["mode"] == action and last["reason"] == reason:
                last["end"] = p["end"]
            else:
                periods.append(p)
        return periods

    def _submit_action(self, asset_id, action):
        token = self._effira_token()
        now = datetime.now(timezone.utc)
        start = self._quantize_up(now)
        end = start + timedelta(minutes=15)
        r = requests.post(
            f"{EFFIRA_BASE}/api/v1/assets/{asset_id}/plan/manual",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"periods": [{"start": self._fmt(start), "end": self._fmt(end), "action": action}]},
            timeout=15,
        )
        r.raise_for_status()

    def _clear_plan(self, asset_id):
        token = self._effira_token()
        r = requests.post(
            f"{EFFIRA_BASE}/api/v1/assets/{asset_id}/plan/manual",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"periods": []},
            timeout=15,
        )
        r.raise_for_status()

    # ── Service entrypoints ───────────────────────────────────────────────────

    async def async_action(self, action):
        asset_id = self.config_entry.data["asset_id"]
        await self.hass.async_add_executor_job(self._submit_action, asset_id, action)
        self.async_set_updated_data({
            **(self.data or {}),
            "manual_action": action,
            "last_action_at": datetime.now(timezone.utc).isoformat(),
        })

    async def async_clear_plan(self):
        asset_id = self.config_entry.data["asset_id"]
        await self.hass.async_add_executor_job(self._clear_plan, asset_id)
        self.async_set_updated_data({
            **(self.data or {}),
            "manual_action": None,
            "last_action_at": datetime.now(timezone.utc).isoformat(),
        })

    # ── Poll ──────────────────────────────────────────────────────────────────

    async def _async_update_data(self):
        asset_id = self.config_entry.data["asset_id"]
        try:
            status, ref_temp, manual_plan, timeline, indoor = await self.hass.async_add_executor_job(
                self._fetch_all, asset_id
            )

            online = None
            last_action = None
            last_action_source = None
            if status:
                online_obj = status.get("online") or {}
                online = online_obj.get("value")
                action_obj = status.get("lastAction") or {}
                last_action = action_obj.get("state")
                prio = action_obj.get("prio") or {}
                last_action_source = prio.get("type")

            ref_temp_value = None
            if ref_temp:
                ref_temp_value = ref_temp.get("value")

            active_periods = []
            if manual_plan:
                active_periods = manual_plan.get("rawPeriods") or []

            return {
                "connected": True,
                "online": online,
                "last_action": last_action,
                "last_action_source": last_action_source,
                "ref_temp": ref_temp_value,
                "active_periods": active_periods,
                # None = endpoint unavailable this poll → keep the last good timeline
                "timeline": timeline if timeline is not None else (self.data or {}).get("timeline"),
                "timeline_ok": timeline is not None,
                "indoor": indoor,
                "manual_action": (self.data or {}).get("manual_action"),
                "last_action_at": (self.data or {}).get("last_action_at"),
            }

        except Exception as err:
            raise UpdateFailed(f"Effira API error: {err}") from err
