from __future__ import annotations

import requests


class BackendError(RuntimeError):
    pass


def request_livekit_token(
    *,
    backend_url: str,
    client_id: str,
    display_name: str,
    system_address: str,
    system_name: str | None = None,
    game_mode: str | None = None,
    group: str | None = None,
    room_name: str | None = None,
    timeout: float = 10.0,
) -> dict:
    url = backend_url.rstrip("/") + "/api/token"

    payload = {
        "client_id": client_id,
        "display_name": display_name,
        "system_address": str(system_address),
        "system_name": system_name,
        "game_mode": game_mode,
        "group": group,
        "room_name": room_name,
    }

    try:
        response = requests.post(url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise BackendError(f"Could not reach backend: {exc}") from exc

    if response.status_code != 200:
        raise BackendError(f"Backend returned {response.status_code}: {response.text}")

    data = response.json()

    required = {"url", "room", "token"}
    missing = required - set(data)
    if missing:
        raise BackendError(f"Backend response missing fields: {', '.join(sorted(missing))}")

    return data