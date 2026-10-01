"""`arena tailscale-key`: mint the auth key remote workers join the tailnet with.

Uses a Tailscale OAuth client (auth_keys scope) whose id and secret live in files on the
engine's machine, never in the repo or in workers. Workers get only the minted key: reusable,
ephemeral (nodes vanish when they stop), pre-approved, tagged, with an expiry; it can add
worker nodes and nothing else.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.tailscale.com/api/v2"


def _token(cid: str, secret: str) -> str:
    body = urllib.parse.urlencode({"client_id": cid, "client_secret": secret,
                                   "grant_type": "client_credentials"}).encode()
    with urllib.request.urlopen(urllib.request.Request(f"{API}/oauth/token", data=body), timeout=30) as r:
        return json.load(r)["access_token"]


def mint(id_file: Path, secret_file: Path, tag: str, days: int, description: str) -> dict:
    tok = _token(id_file.read_text().strip(), secret_file.read_text().strip())
    req = {"capabilities": {"devices": {"create": {"reusable": True, "ephemeral": True,
                                                    "preauthorized": True, "tags": [tag]}}},
           "expirySeconds": days * 86400, "description": description[:50]}
    r = urllib.request.Request(f"{API}/tailnet/-/keys", data=json.dumps(req).encode(), method="POST",
                               headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.load(resp)


def main(args) -> int:
    conf = Path(args.dir).expanduser()
    out = Path(args.out).expanduser() if args.out else conf / "worker-ts-authkey"
    k = mint(conf / "tailscale-oauth-id", conf / "tailscale-oauth", args.tag, args.days,
             "tokenz-arena workers")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.touch(mode=0o600)
    out.chmod(0o600)
    out.write_text(k["key"] + "\n")
    print(f"minted key {k['id']} (tag {args.tag}, {args.days} days, reusable, ephemeral, "
          f"pre-approved) -> {out} (mode 600); the key itself isn't printed")
    return 0
