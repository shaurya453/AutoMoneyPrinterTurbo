"""One-off setup: register an Openverse API client and print/store credentials.

Anonymous Openverse requests are Cloudflare-blocked from server IPs, so the
image provider in app/services/media/images.py needs a registered OAuth2
client (openverse_client_id/_secret in config.toml). Openverse's register
endpoint creates that client programmatically instead of via their web form.

Docs: https://api.openverse.org/v1/#tag/auth/operation/register

Usage:
    python scripts/register_openverse.py --name "My Project" \\
        --email you@example.com \\
        --description "Fetches openly-licensed images for an automated video pipeline" \\
        [--write-config]

Without --write-config the credentials are only printed — paste them into
config.toml's openverse_client_id/_secret yourself. With --write-config this
script edits config.toml in place.

Openverse rate-limits unverified clients more strictly; check the inbox for
the given email and click the verification link after registering.
"""
import argparse
import os
import re
import sys

import requests

_REGISTER_URL = "https://api.openverse.org/v1/auth_tokens/register/"
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def register(name: str, email: str, description: str) -> dict:
    resp = requests.post(
        _REGISTER_URL,
        json={"name": name, "email": email, "description": description},
        timeout=(15, 30),
    )
    if not resp.ok:
        print(
            f"Openverse registration failed: HTTP {resp.status_code} — {resp.text}",
            file=sys.stderr,
        )
        sys.exit(1)
    return resp.json()


def write_config(config_path: str, client_id: str, client_secret: str) -> None:
    with open(config_path, "r", encoding="utf-8") as f:
        text = f.read()
    text, n_id = re.subn(
        r'(?m)^openverse_client_id\s*=\s*".*"$',
        f'openverse_client_id = "{client_id}"',
        text,
    )
    text, n_secret = re.subn(
        r'(?m)^openverse_client_secret\s*=\s*".*"$',
        f'openverse_client_secret = "{client_secret}"',
        text,
    )
    if n_id == 0 or n_secret == 0:
        print(
            f"Could not find openverse_client_id/_secret lines in {config_path} — "
            "leaving the file untouched. Paste the credentials above in manually.",
            file=sys.stderr,
        )
        sys.exit(1)
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="Application name shown to Openverse")
    parser.add_argument("--email", required=True, help="Contact email (verify it after registering)")
    parser.add_argument("--description", required=True, help="What this client is used for")
    parser.add_argument(
        "--write-config", action="store_true",
        help="Write the returned credentials into config.toml instead of just printing them",
    )
    parser.add_argument(
        "--config-path", default=os.path.join(_REPO_ROOT, "config.toml"),
        help="Path to config.toml (default: repo root config.toml)",
    )
    args = parser.parse_args()

    payload = register(args.name, args.email, args.description)
    client_id = payload.get("client_id", "")
    client_secret = payload.get("client_secret", "")
    if not client_id or not client_secret:
        print(f"Unexpected Openverse response, no client_id/secret in it: {payload}", file=sys.stderr)
        sys.exit(1)

    print(f"client_id:     {client_id}")
    print(f"client_secret: {client_secret}")
    print(f"Check {args.email} for a verification link — unverified clients get a lower rate limit.")

    if args.write_config:
        write_config(args.config_path, client_id, client_secret)
        print(f"Wrote credentials into {args.config_path}")


if __name__ == "__main__":
    main()
