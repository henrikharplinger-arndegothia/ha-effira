# ha-effira

Home Assistant integration for [Effira OPTi](https://effiraenergy.com) — submits price- and solar-aware heat pump plans via the Effira customer API.

> **Status:** Early / private beta. Requires access to Effira's test environment.
> The end goal is a proper [HACS](https://hacs.xyz) custom integration.

---

## What it does

Runs every 15 minutes and submits a 24-hour manual plan to your Effira OPTi device based on:

| Priority | Condition | Action |
|---|---|---|
| 1 | Capacity tariff peak hours (configurable) | `stop` |
| 2 | Solar export ≥ threshold | `boost` |
| 3 | NordPool price ≤ threshold | `boost` |
| 4 | Default | *(Effira auto handles it)* |

---

## Prerequisites

- Home Assistant (any recent version)
- [Studio Code Server](https://my.home-assistant.io/redirect/supervisor_addon/?addon=a0d7b954_vscode) add-on (or similar file access to `/config/`)
- NordPool integration — entity with `raw_today` / `raw_tomorrow` attributes
- Effira OPTi device, claimed in the Effira (Preview) app
- Effira API key (see setup below)

---

## Setup

### 1. Get an Effira API key

**a)** Get an authorisation code via [OAuth Debugger](https://oauthdebugger.com):

| Field | Value |
|---|---|
| Authorize URI | `https://easyserv-enduser-unstable.auth.eu-north-1.amazoncognito.com/oauth2/authorize` |
| Client ID | `4fmn375d1uhammpa9j3rld9kum` |
| Redirect URI | `https://oauthdebugger.com/debug` |
| Scope | `enduser/access` |
| Response type | `code` |
| Response mode | `form_post` |

Log in with your Effira account. Copy the `code` from the result page.

**b)** Exchange the code for an access token:

```bash
curl -X POST "https://easyserv-enduser-unstable.auth.eu-north-1.amazoncognito.com/oauth2/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=authorization_code&code=<CODE>&client_id=4fmn375d1uhammpa9j3rld9kum&redirect_uri=https://oauthdebugger.com/debug"
```

Copy `access_token` from the response.

**c)** Create your API key (replace `<ASSET_ID>` with your installation's asset ID):

```bash
curl -X POST "https://unstable-app.enerflex.cloud/api/app/v1/me/api-keys" \
  -H "Authorization: Bearer <ACCESS_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"name": "ha-integration", "assetId": "<ASSET_ID>"}'
```

Save the `keyId` and `secret` from the response — you will need them below.

---

### 2. Deploy to Home Assistant

Open Studio Code Server and create `/config/effira/`:

```
/config/
  effira/
    effira_plan.py       ← copy from this repo
    .env                 ← create from config.env.example
```

Fill in `/config/effira/.env`:

```env
EFFIRA_KEY_ID=<keyId from step 1>
EFFIRA_KEY_SECRET=<secret from step 1>
EFFIRA_ASSET_ID=<your asset ID>
HA_URL=http://homeassistant.local:8123
HA_TOKEN=<HA long-lived access token>
```

Generate a HA long-lived token at: **Profile → Security → Long-lived access tokens**.

---

### 3. Configure Home Assistant

Add to `configuration.yaml`:

```yaml
shell_command:
  effira_update_plan: "python3 /config/effira/effira_plan.py >> /config/effira/effira_plan.log 2>&1"
```

Reload configuration, then import `automations/effira_heat_pump.yaml` into HA.

---

### 4. Test

Run manually from the HA host terminal:

```bash
python3 /config/effira/effira_plan.py
```

Or trigger the automation once from the HA UI and check **Notifications** for any errors.

---

## Configuration

All settings are via environment variables in `.env`:

| Variable | Default | Description |
|---|---|---|
| `EFFIRA_KEY_ID` | required | API key ID from Effira |
| `EFFIRA_KEY_SECRET` | required | API key secret from Effira |
| `EFFIRA_ASSET_ID` | required | Your installation's asset ID |
| `HA_URL` | `http://homeassistant.local:8123` | HA local URL |
| `HA_TOKEN` | required | HA long-lived access token |
| `NORDPOOL_ENTITY` | `sensor.nordpool_kwh_se3_sek_3_10_025` | Your NordPool sensor entity |
| `GOODWE_ENTITY` | `sensor.goodwe_active_power` | Solar inverter active power sensor (negative = export). Remove solar logic if you don't have solar. |
| `CHEAP_PRICE_SEK` | `1.0` | Boost below this price (SEK/kWh incl. VAT) |
| `SOLAR_EXPORT_W` | `300` | Boost when solar export ≥ this (W) |

---

## Roadmap

- [ ] Proper HACS custom integration (config flow UI, no script/shell_command needed)
- [ ] HA sensor entities for current Effira status, last action, savings
- [ ] Capacity tariff configuration via UI (currently hardcoded for Mölndal/SE3)
- [ ] Solar forecast integration (use tomorrow's forecast, not just current export)
- [ ] Support for non-Swedish price areas

---

## License

MIT
