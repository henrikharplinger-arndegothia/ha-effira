#!/usr/bin/env python3
"""
Effira OPTi — Home Assistant bridge
====================================
Reads NordPool 15-min prices and current solar export from HA,
computes a 24-hour heat-pump plan, authenticates with the Effira
customer API, and submits the plan.

Run every 15 minutes via HA shell_command (see automations/effira_heat_pump.yaml).
Credentials are read from a .env file alongside this script (see config.env.example).

Action priority per 15-min slot
--------------------------------
1. Peak tariff block (Nov–Mar, weekday 07–19) → "stop"
   Mölndal capacity tariff: avoid adding load during these hours.
2. Solar export ≥ threshold                   → "boost"
   Use free solar surplus to pre-heat house / hot water.
3. Cheap price (≤ threshold)                  → "boost"
   Pre-heat when electricity is inexpensive.
4. Default                                     → omit slot
   Effira's automatic optimisation handles the rest.

Action names from Effira OpenAPI spec (ManualPlanAction enum): "boost", "stop", "normal".
"""

import os
import sys
import json
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Load .env ─────────────────────────────────────────────────────────────────
def _load_env():
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_env()

# ── Config ────────────────────────────────────────────────────────────────────
EFFIRA_KEY_ID     = os.environ["EFFIRA_KEY_ID"]
EFFIRA_KEY_SECRET = os.environ["EFFIRA_KEY_SECRET"]
EFFIRA_ASSET_ID   = os.environ.get("EFFIRA_ASSET_ID", "69fc584c69510b39091a2b02")
EFFIRA_BASE       = "https://unstable-app.enerflex.cloud"

HA_URL   = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN = os.environ["HA_TOKEN"]

# Nordpool sensor entity (same one used by EV + battery automations)
NORDPOOL_ENTITY = os.environ.get(
    "NORDPOOL_ENTITY", "sensor.nordpool_kwh_se3_sek_3_10_025"
)
# GoodWe active power: negative = exporting to grid
GOODWE_ENTITY = os.environ.get("GOODWE_ENTITY", "sensor.goodwe_active_power")

# ── Thresholds (tune to taste) ────────────────────────────────────────────────
CHEAP_PRICE_SEK = float(os.environ.get("CHEAP_PRICE_SEK", "1.0"))  # SEK/kWh incl. VAT
SOLAR_EXPORT_W  = float(os.environ.get("SOLAR_EXPORT_W",  "300"))  # W

# ── Action names (from Effira OpenAPI spec — ManualPlanAction enum) ──────────
ACTION_BOOST  = "boost"   # run heat pump harder
ACTION_STOP   = "stop"    # stop/reduce heat pump (use during peak tariff hours)
ACTION_NORMAL = "normal"  # normal operation (can be used to explicitly override a prior plan)

# ── Mölndal capacity tariff (matches battery_management.yaml) ────────────────
PEAK_MONTHS = {11, 12, 1, 2, 3}
PEAK_HOUR_START = 7
PEAK_HOUR_END   = 19


# ── HA REST helpers ───────────────────────────────────────────────────────────

def ha_state(entity_id: str) -> dict:
    r = requests.get(
        f"{HA_URL}/api/states/{entity_id}",
        headers={"Authorization": f"Bearer {HA_TOKEN}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


# ── Effira auth ───────────────────────────────────────────────────────────────

def effira_token() -> str:
    r = requests.post(
        f"{EFFIRA_BASE}/api/v1/auth/token",
        auth=(EFFIRA_KEY_ID, EFFIRA_KEY_SECRET),
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["access_token"]


# ── Plan computation ──────────────────────────────────────────────────────────

def _quantize_up(dt: datetime) -> datetime:
    """Round up to the next :00/:15/:30/:45 boundary."""
    rem = dt.minute % 15
    if rem == 0 and dt.second == 0 and dt.microsecond == 0:
        return dt
    add = 15 - rem
    return (dt + timedelta(minutes=add)).replace(second=0, microsecond=0)


def _is_peak_block(dt: datetime) -> bool:
    """Mölndal capacity tariff: Nov–Mar, weekday 07–19 (local time)."""
    local = dt.astimezone()
    return (
        local.month in PEAK_MONTHS
        and local.weekday() < 5
        and PEAK_HOUR_START <= local.hour < PEAK_HOUR_END
    )


def _price_for_slot(price_map: dict, t: datetime) -> float | None:
    """
    Find the price for a 15-min slot.
    NordPool raw_today/raw_tomorrow are hourly, so we floor t to the hour
    and look up that bucket. Falls back to scanning backwards.
    """
    # Try floored to hour first (handles hourly NordPool data)
    hour_start = t.replace(minute=0, second=0, microsecond=0)
    if hour_start in price_map:
        return price_map[hour_start]
    # Fallback: nearest earlier entry (handles 15-min data too)
    for candidate in sorted(price_map.keys(), reverse=True):
        if candidate <= t:
            return price_map[candidate]
    return None


def _fmt(dt: datetime) -> str:
    """ISO 8601 UTC string as expected by the Effira API."""
    utc = dt.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_plan(nordpool_slots: list, solar_export_w: float) -> list[dict]:
    """
    Classify each 15-min slot over the next 24h and return a list of
    merged period dicts: {"start": ..., "end": ..., "action": ...}.

    Only slots that override Effira's default behaviour are included.
    Consecutive identical actions are merged into a single period.
    """
    now = datetime.now(timezone.utc)
    plan_start = _quantize_up(now)
    # Cap end at 24h from *now* (not plan_start), rounded down to 15-min boundary
    end_raw = now + timedelta(hours=24)
    rem = end_raw.minute % 15
    plan_end = (end_raw - timedelta(minutes=rem)).replace(second=0, microsecond=0)
    if plan_end <= plan_start:
        plan_end = plan_start + timedelta(minutes=15)

    # Build price lookup keyed by slot-start datetime
    price_map: dict[datetime, float] = {}
    for slot in nordpool_slots:
        raw = slot.get("start", "")
        if not raw:
            continue
        # Handle both Z-suffix and offset formats
        raw = raw.replace("Z", "+00:00")
        try:
            ts = datetime.fromisoformat(raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            price_map[ts.astimezone(timezone.utc)] = float(slot["value"])
        except (ValueError, KeyError):
            continue

    # Walk 15-min slots and classify
    periods: list[dict] = []
    current_action: str | None = None
    period_start:   datetime | None = None

    t = plan_start
    while t < plan_end:
        t_next = t + timedelta(minutes=15)
        price  = _price_for_slot(price_map, t)

        # Determine desired action for this slot
        if _is_peak_block(t):
            action: str | None = ACTION_STOP
        elif solar_export_w >= SOLAR_EXPORT_W:
            action = ACTION_BOOST
        elif price is not None and price <= CHEAP_PRICE_SEK:
            action = ACTION_BOOST
        else:
            action = None  # let Effira decide

        # Merge consecutive same-action slots
        if action != current_action:
            # Close previous period
            if current_action is not None and period_start is not None:
                periods.append({
                    "start":  _fmt(period_start),
                    "end":    _fmt(t),
                    "action": current_action,
                })
            current_action = action
            period_start   = t if action is not None else None

        t = t_next

    # Close final open period
    if current_action is not None and period_start is not None:
        periods.append({
            "start":  _fmt(period_start),
            "end":    _fmt(plan_end),
            "action": current_action,
        })

    return periods


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # 1. NordPool prices
    np_state = ha_state(NORDPOOL_ENTITY)
    attrs    = np_state.get("attributes", {})
    slots    = (attrs.get("raw_today") or []) + (attrs.get("raw_tomorrow") or [])

    if not slots:
        print("ERROR: No NordPool data in sensor attributes.", file=sys.stderr)
        sys.exit(1)

    # 2. Solar export (negative active_power = exporting)
    gw_state      = ha_state(GOODWE_ENTITY)
    active_power  = float(gw_state.get("state") or 0)
    solar_export  = max(0.0, -active_power)

    print(f"Solar export: {solar_export:.0f} W | "
          f"NordPool slots: {len(slots)} | "
          f"Current price: {attrs.get('current_price', '?')} SEK/kWh")

    # 3. Build plan
    plan = build_plan(slots, solar_export)

    if not plan:
        print("No override periods needed — Effira auto logic will handle everything.")
        return

    print(f"Plan: {len(plan)} period(s)")
    for p in plan:
        print(f"  {p['start']}  →  {p['end']}  [{p['action']}]")

    # 4. Authenticate
    token = effira_token()

    # 5. Submit
    r = requests.post(
        f"{EFFIRA_BASE}/api/v1/assets/{EFFIRA_ASSET_ID}/plan/manual",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type":  "application/json",
        },
        json={"periods": plan},
        timeout=15,
    )

    print(f"Effira API: {r.status_code}")
    if not r.ok:
        print(r.text, file=sys.stderr)
        sys.exit(1)

    print("Plan submitted successfully.")


if __name__ == "__main__":
    main()
