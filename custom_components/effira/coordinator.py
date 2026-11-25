"""DataUpdateCoordinator for Effira OPTi.

Polls currentStatus, referencetemperature, the asset's temperatureSettings, plan/manual, the indoor temperature and the OPTi timeline (the 24 h
plan OPTi makes and adapts during the day; same endpoint as the Effira app) every 5 minutes.
Provides async_action() and async_clear_plan() for HA services to call.
"""
import logging
import threading
from datetime import datetime, timezone, timedelta

import requests
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, EFFIRA_BASE

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=5)
MAX_AHEAD = timedelta(hours=24)  # the API refuses periods ending later than now + 24 h


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
        self._plan_lock = threading.Lock()  # serialises read-merge-post of plan/manual
        self._manual_action = None
        self._last_action_at = None

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
        temp_settings = self._fetch_temp_settings(token, asset_id)
        return status, ref_temp, manual_plan, timeline, indoor, temp_settings

    def _fetch_temp_settings(self, token, asset_id):
        """The asset's temperatureSettings {mode: auto|manual, reference}. Optional: None on any error."""
        try:
            asset = self._get(token, f"/assets/{asset_id}") or {}
        except Exception as err:  # noqa: BLE001 — optional data
            _LOGGER.debug("Effira asset settings not available: %s", err)
            return None
        ts = asset.get("temperatureSettings")
        return ts if isinstance(ts, dict) else None

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
        self._plan_lock = threading.Lock()  # serialises read-merge-post of plan/manual
        self._manual_action = None
        self._last_action_at = None
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

    def _quantize_down(self, dt):
        return dt.replace(minute=dt.minute - dt.minute % 15, second=0, microsecond=0)

    @staticmethod
    def _parse(ts):
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))

    def _submit_action(self, asset_id, action, start=None, end=None, replace=False):
        """Book `action` for [start, end) on 15-min boundaries (default: the next 15 min).

        Unless replace=True, the existing future manual periods are kept: they are read from
        plan/manual, the new window is cut out of them, and the merged list is posted back, so
        each booking adds to the plan instead of overwriting it.
        """
        now = datetime.now(timezone.utc)
        # validate what was asked for before rounding to the 15-min grid
        if start and end and end <= start:
            raise ValueError(f"end ({end}) must be after start ({start})")
        if end and end <= now:
            raise ValueError(f"end ({end}) is already past")
        start = self._quantize_down(start) if start else self._quantize_up(now)
        end = self._quantize_up(end) if end else start + timedelta(minutes=15)
        if end <= now:
            raise ValueError(f"window {start}–{end} is already over")
        if end > now + MAX_AHEAD:
            raise ValueError(f"end {end} is more than 24 h ahead (Effira API limit)")
        with self._plan_lock:
            return self._merge_and_post(asset_id, action, start, end, replace, now)

    def _merge_and_post(self, asset_id, action, start, end, replace, now):
        token = self._effira_token()
        periods = []
        if not replace:
            current = self._get(token, f"/assets/{asset_id}/plan/manual") or {}
            for p in current.get("rawPeriods") or []:
                p_start, p_end = self._parse(p["start"]), self._parse(p["end"])
                if p_end <= now:
                    continue  # finished
                # keep the parts of an existing period that lie outside the new window
                for a, b in ((p_start, min(p_end, start)), (max(p_start, end), p_end)):
                    if a < b:
                        periods.append({"start": self._fmt(a), "end": self._fmt(b), "action": p["action"]})
        periods.append({"start": self._fmt(start), "end": self._fmt(end), "action": action})
        periods.sort(key=lambda p: p["start"])
        r = requests.post(
            f"{EFFIRA_BASE}/api/v1/assets/{asset_id}/plan/manual",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"periods": periods},
            timeout=15,
        )
        r.raise_for_status()
        _LOGGER.info("Effira manual plan posted: %s", periods)
        return periods

    def _clear_plan(self, asset_id):
        with self._plan_lock:
            self._post_empty(asset_id)

    def _post_empty(self, asset_id):
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

    async def async_action(self, action, start=None, end=None, replace=False):
        asset_id = self.config_entry.data["asset_id"]
        await self.hass.async_add_executor_job(self._submit_action, asset_id, action, start, end, replace)
        self._manual_action = action
        self._last_action_at = datetime.now(timezone.utc).isoformat()
        await self.async_refresh()

    async def async_clear_plan(self):
        asset_id = self.config_entry.data["asset_id"]
        await self.hass.async_add_executor_job(self._clear_plan, asset_id)
        self._manual_action = None
        self._last_action_at = datetime.now(timezone.utc).isoformat()
        await self.async_refresh()

    # ── Poll ──────────────────────────────────────────────────────────────────

    async def _async_update_data(self):
        asset_id = self.config_entry.data["asset_id"]
        try:
            status, ref_temp, manual_plan, timeline, indoor, temp_settings = await self.hass.async_add_executor_job(
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

            auto_ref = ref_temp.get("value") if ref_temp else None
            mode = (temp_settings or {}).get("mode")
            manual_ref = (temp_settings or {}).get("reference")
            # The temperature OPTi steers toward: the user's own value in manual mode, else the auto one.
            ref_temp_value = manual_ref if mode == "manual" and isinstance(manual_ref, (int, float)) else auto_ref

            active_periods = []
            if manual_plan:
                active_periods = manual_plan.get("rawPeriods") or []

            return {
                "connected": True,
                "online": online,
                "last_action": last_action,
                "last_action_source": last_action_source,
                "ref_temp": ref_temp_value,
                "ref_mode": mode,
                "ref_auto": auto_ref,
                "active_periods": active_periods,
                # None = endpoint unavailable this poll → keep the last good timeline
                "timeline": timeline if timeline is not None else (self.data or {}).get("timeline"),
                "timeline_ok": timeline is not None,
                "indoor": indoor,
                "manual_action": self._manual_action,
                "last_action_at": self._last_action_at,
            }

        except Exception as err:
            raise UpdateFailed(f"Effira API error: {err}") from err
