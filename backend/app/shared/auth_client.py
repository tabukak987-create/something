from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_API_PREFIX = "/api/v1"


def fetch_access_token(
    base_url: str,
    username: str,
    password: str,
    *,
    api_prefix: str = DEFAULT_API_PREFIX,
    timeout: float = 15.0,
) -> str:
    login_url = f"{base_url.rstrip('/')}{api_prefix}/auth/token"
    form_data = urllib.parse.urlencode(
        {
            "username": username,
            "password": password,
            "grant_type": "password",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        login_url,
        data=form_data,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))

    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise RuntimeError(f"Login succeeded but no access_token was returned by {login_url}")
    return token


def fetch_auth_headers(
    base_url: str,
    username: str,
    password: str,
    *,
    api_prefix: str = DEFAULT_API_PREFIX,
    timeout: float = 15.0,
) -> dict[str, str]:
    token = fetch_access_token(
        base_url,
        username,
        password,
        api_prefix=api_prefix,
        timeout=timeout,
    )
    return {"Authorization": f"Bearer {token}"}


def fetch_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))
