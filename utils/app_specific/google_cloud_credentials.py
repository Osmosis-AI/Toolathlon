"""Load Google Cloud credentials without assuming a private-key JSON type.

The hosted runner renders ``configs/gcp-service_account.keys.json`` as
``{"type": "authorized_user_access_token_only", "access_token": ...,
"expires_at": ...}`` — a short-lived impersonated access token, re-rendered
every 15 minutes, because its credential leaser never issues service-account
keys. ``service_account.Credentials`` rejects that shape (no ``token_uri`` or
``private_key``), so callers dispatch here instead of on the strict loader.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as AccessTokenCredentials

ACCESS_TOKEN_TYPE = "authorized_user_access_token_only"


def _naive_utc(expires_at):
    """google-auth compares ``expiry`` against naive UTC datetimes."""
    parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def load_gcp_credentials(path, scopes=None):
    """Return credentials for a service-account key file or the runner's
    access-token stand-in."""
    info = json.loads(Path(path).read_text())
    if info.get("type") != ACCESS_TOKEN_TYPE:
        return service_account.Credentials.from_service_account_info(
            info, scopes=scopes
        )
    credentials = AccessTokenCredentials(
        token=info["access_token"], scopes=scopes
    )
    if info.get("expires_at"):
        credentials.expiry = _naive_utc(info["expires_at"])
    return credentials
