"""OAuth2 application credentials for Effira OPTi.

Defines the Cognito authorization server endpoints.
The client_id is fixed (Effira's public Cognito app client).
No client_secret — uses PKCE flow.

NOTE (Kenny): before this works, the following redirect URI must be
registered in the Cognito app client (4fmn375d1uhammpa9j3rld9kum):
  https://my.home-assistant.io/redirect/oauth
"""
from homeassistant.components.application_credentials import AuthorizationServer
from homeassistant.core import HomeAssistant

from .const import COGNITO_AUTH_URL, COGNITO_TOKEN_URL


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    return AuthorizationServer(
        authorize_url=COGNITO_AUTH_URL,
        token_url=COGNITO_TOKEN_URL,
    )
