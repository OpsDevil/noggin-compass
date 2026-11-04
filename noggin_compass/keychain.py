"""Read secrets from the macOS Keychain. On other systems it simply finds nothing."""

from __future__ import annotations

import subprocess


def read(service: str) -> str | None:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:  # no `security` command: not macOS
        return None
    if result.returncode != 0:
        return None
    return result.stdout.rstrip("\n") or None
