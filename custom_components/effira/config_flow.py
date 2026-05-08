"""Config flow for Effira OPTi.

Two authentication paths in one flow:

  1. OAuth (preferred) — "Log in with Effira account"
     Redirects to Cognito, fetches the user's assets, creates a
     long-lived API key automatically. No copy-pasting credentials.

     BLOCKED until Kenny registers this redirect URI in the Cognito
     app client (4fmn375d1uhammpa9j3rld9kum):
       https://my.home-assistant.io/redirect/oauth

  2. Manual API key — "Enter API key"
     User pastes key_id + key_secret + asset_id. Works today.

Cognito details (test environment):
  authorize: https://easyserv-enduser-unstable.auth.eu-north-1.amazoncognito.com/oauth2/authorize
  token:     https://easyserv-enduser-unstable.auth.eu-north-1.amazoncognito.com/oauth2/token
  client_id: 4fmn375d1uhammpa9j3rld9kum
  scope:     enduser/access
"""
import logging

import voluptuous as vol
import requests
from homeassistant import config_entries
from homeassistant.helpers import config_entry_oauth2_flow

from .const import (
    DOMAIN,
    CONF_KEY_ID,
    CONF_KEY_SECRET,
    CONF_ASSET_ID,
    EFFIRA_BASE,
    EFFIRA_APP_BASE,
    COGNITO_SCOPE,
)

_LOGGER = logging.getLogger(__name__)


class EffiraConfigFlow(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler,
    domain=DOMAIN,
):
    """Single config flow supporting both OAuth and manual API key entry."""

    VERSION = 1
    DOMAIN = DOMAIN

    def __init__(self):
        super().__init__()
        self._oauth_token = None
        self._assets = []

    @property
    def logger(self):
        return _LOGGER

    @property
    def extra_authorize_data(self):
        return {"scope": COGNITO_SCOPE}

    # ── Entry point: choose auth method ───────────────────────────────────────

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            if user_input["auth_type"] == "oauth":
                # Hand off to AbstractOAuth2FlowHandler's implementation picker
                return await self.async_step_pick_implementation()
            else:
                return await self.async_step_manual()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("auth_type", default="manual"): vol.In({
                    "oauth": "Log in with Effira account",
                    "manual": "Enter API key manually",
                }),
            }),
        )

    # ── Manual path ───────────────────────────────────────────────────────────

    async def async_step_manual(self, user_input=None):
        errors = {}

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_ASSET_ID])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=f"Effira OPTi ({user_input[CONF_ASSET_ID][:8]}...)",
                data=user_input,
            )

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({
                vol.Required(CONF_KEY_ID): str,
                vol.Required(CONF_KEY_SECRET): str,
                vol.Required(CONF_ASSET_ID): str,
            }),
            errors=errors,
        )

    # ── OAuth path ────────────────────────────────────────────────────────────

    async def async_oauth_create_entry(self, data):
        """Called by AbstractOAuth2FlowHandler after the OAuth dance completes."""
        self._oauth_token = data["token"]["access_token"]

        try:
            assets = await self.hass.async_add_executor_job(
                _fetch_assets, self._oauth_token
            )
        except Exception as err:
            _LOGGER.error("Failed to fetch assets after OAuth: %s", err)
            return self.async_abort(reason="cannot_fetch_assets")

        if not assets:
            return self.async_abort(reason="no_assets")

        if len(assets) == 1:
            return await self._create_entry_for_asset(assets[0])

        self._assets = assets
        return await self.async_step_pick_asset()

    async def async_step_pick_asset(self, user_input=None):
        if user_input is not None:
            asset = next(
                (a for a in self._assets if a["assetId"] == user_input[CONF_ASSET_ID]),
                None,
            )
            if asset:
                return await self._create_entry_for_asset(asset)

        options = {
            a["assetId"]: (
                a.get("address", {}).get("address1") or a["assetId"]
            )
            for a in self._assets
        }
        return self.async_show_form(
            step_id="pick_asset",
            data_schema=vol.Schema({
                vol.Required(CONF_ASSET_ID): vol.In(options),
            }),
        )

    async def _create_entry_for_asset(self, asset):
        asset_id = asset["assetId"]
        try:
            key_id, key_secret = await self.hass.async_add_executor_job(
                _create_api_key, self._oauth_token, asset_id
            )
        except Exception as err:
            _LOGGER.error("Failed to create API key for asset %s: %s", asset_id, err)
            return self.async_abort(reason="cannot_create_api_key")

        await self.async_set_unique_id(asset_id)
        self._abort_if_unique_id_configured()

        address = asset.get("address", {})
        title = address.get("address1") or f"Effira OPTi ({asset_id[:8]}...)"

        return self.async_create_entry(
            title=title,
            data={
                CONF_KEY_ID: key_id,
                CONF_KEY_SECRET: key_secret,
                CONF_ASSET_ID: asset_id,
            },
        )


# ── Blocking helpers (run in executor) ───────────────────────────────────────

def _fetch_assets(access_token):
    r = requests.get(
        f"{EFFIRA_BASE}/api/v1/assets",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def _create_api_key(access_token, asset_id):
    r = requests.post(
        f"{EFFIRA_APP_BASE}/me/api-keys",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={"name": "home-assistant", "assetId": asset_id},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    return data["keyId"], data["secret"]
