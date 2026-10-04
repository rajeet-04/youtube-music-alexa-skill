#!/usr/bin/env python3
"""Generate JUKES secrets and write/update them in a private .env.

Run with uv (no project install needed):

    uv run --no-project --with werkzeug --with cryptography scripts/setup-secrets.py

Existing non-empty values are NEVER overwritten (the encryption key must stay
stable or stored sessions become unreadable). The admin password is prompted
and only its hash is stored. Nothing secret is printed.
"""
from __future__ import annotations

import getpass
import os
import re
import secrets
import stat
import sys
from pathlib import Path

from cryptography.fernet import Fernet
from werkzeug.security import generate_password_hash

ENV = Path(sys.argv[1] if len(sys.argv) > 1 else ".env")
TEMPLATE = Path(".env.example")


def read(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def current(lines: list[str], name: str) -> str:
    for line in lines:
        m = re.match(rf"^{re.escape(name)}=(.*)$", line)
        if m:
            return m.group(1).strip()
    return ""


def put(lines: list[str], name: str, value: str) -> None:
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(name)}=", line):
            lines[i] = f"{name}={value}"
            return
    lines.append(f"{name}={value}")


lines = read(ENV) or read(TEMPLATE)
generated = {
    "JUKES_ADMIN_SESSION_KEY": lambda: secrets.token_urlsafe(48),
    "JUKES_CREDENTIAL_ENCRYPTION_KEY": lambda: Fernet.generate_key().decode(),
    "YT_BROWSER_CONTROL_TOKEN": lambda: secrets.token_hex(32),
}
for name, make in generated.items():
    if current(lines, name):
        print(f"keep    {name} (already set)")
    else:
        put(lines, name, make())
        print(f"created {name}")

if current(lines, "JUKES_ADMIN_PASSWORD_HASH"):
    print("keep    JUKES_ADMIN_PASSWORD_HASH (already set)")
else:
    password = getpass.getpass("Admin password (min 12 chars): ")
    if len(password) < 12 or password != getpass.getpass("Repeat: "):
        sys.exit("password too short or mismatched; nothing written")
    # Docker Compose treats $ as interpolation; escape for .env.
    put(lines, "JUKES_ADMIN_PASSWORD_HASH", generate_password_hash(password).replace("$", "$$"))
    print("created JUKES_ADMIN_PASSWORD_HASH")

ENV.write_text("\n".join(lines) + "\n")
os.chmod(ENV, stat.S_IRUSR | stat.S_IWUSR)
print(f"wrote {ENV} (mode 600). Back up the two keys separately from the database.")
