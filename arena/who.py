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


# TODO(build order 2): identify(request) -> Caller
