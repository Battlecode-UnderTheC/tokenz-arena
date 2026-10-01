"""Who's calling. The arena is tailnet-only and has no auth, but it tells teammates apart.

1. Behind `tailscale serve`: the `Tailscale-User-Login` / `Tailscale-User-Name` headers.
   Serve sets these itself; they're trusted only when the request came through serve
   (from 127.0.0.1), never from a direct connection.
2. Direct tailnet connections: `tailscale whois --json <ip>` (the local Tailscale API),
   cached per address for a few minutes.
3. Tagged devices (no user login): the device's name, unless the request passes `owner`.

The optional agent label comes from the `agent` field, or the bearer value the toolkit sends
(`unswbc auth set <label>`). It's shown, not trusted.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass


@dataclass
class Caller:
    owner: str               # "alice@github", or a device name for tagged devices
    name: str | None = None  # display name
    agent: str | None = None
    tagged: bool = False


def whois(ip: str) -> dict | None:
    try:
        out = subprocess.run(["tailscale", "whois", "--json", ip], capture_output=True, text=True,
                             timeout=5, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return json.loads(out)


_CACHE: dict[str, tuple[float, Caller | None]] = {}
# True when the arena is reachable only through `tailscale serve` (the container setup: it
# listens on 127.0.0.1 in the Tailscale sidecar's namespace). Serve then forwards with the
# caller's tailnet IP as the source, so the headers are trusted from any address.
# Set from [api] trust_tailscale_headers by create_app.
TRUST_HEADERS = False
_LOCAL = {"127.0.0.1", "::1", "localhost", "testclient"}


def _from_whois(ip: str) -> Caller | None:
    hit = _CACHE.get(ip)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    w = whois(ip)
    caller = None
    if w:
        node = w.get("Node") or {}
        user = w.get("UserProfile") or {}
        login = user.get("LoginName") or ""
        device = (node.get("ComputedName") or node.get("Name") or ip).split(".")[0]
        if node.get("Tags") or not login or login == "tagged-devices":
            caller = Caller(owner=device, name=device, tagged=True)
        else:
            caller = Caller(owner=login, name=user.get("DisplayName") or login)
    _CACHE[ip] = (time.time(), caller)
    return caller


def identify(headers, client_ip: str | None, agent: str | None = None,
             owner: str | None = None) -> Caller:
    """The caller of an HTTP request. `headers` is a case-insensitive mapping."""
    bearer = (headers.get("authorization") or "").removeprefix("Bearer ").strip() or None
    agent = (agent or bearer or "").strip()[:64] or None
    ip = client_ip or ""
    login = headers.get("tailscale-user-login")
    if login and (ip in _LOCAL or TRUST_HEADERS):   # set by `tailscale serve`
        c = Caller(owner=login, name=headers.get("tailscale-user-name") or login)
    elif ip in _LOCAL:
        c = Caller(owner="local", name="local", tagged=True)
    else:
        c = _from_whois(ip) or Caller(owner=f"ip:{ip}", name=ip, tagged=True)
    if owner and c.tagged:
        c = Caller(owner=owner.strip()[:64], name=owner.strip()[:64], tagged=True)
    c.agent = agent
    return c
