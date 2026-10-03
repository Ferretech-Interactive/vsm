"""Audit trail: who changed what through the API.

Every route that signs someone in or changes something records one line through `auditor`.
Lines go to the Event log, `logs/manager.log` and, on their own, `logs/audit.log`.
Values of secrets are never written, only that they changed.
"""
from __future__ import annotations

from typing import Callable

from fastapi import Request

MAX_CHANGES = 12
MAX_VALUE = 60


def actor(request: Request) -> str:
    """Who is making this request, as far as the manager can tell."""
    m = request.app.state.manager
    if m.auth.is_local(request):
        return "the desktop window on this PC"
    address = request.client.host if request.client else "an unknown address"
    if m.auth.api_token and request.headers.get("authorization", "").startswith("Bearer "):
        return f"the API token, from {address}"
    return address


def auditor(request: Request) -> Callable[[str], None]:
    """FastAPI dependency: `audit("Deleted backup x.zip")` records the line with who did it."""
    m = request.app.state.manager
    who = actor(request)
    return lambda message: m.audit(message, who)


def _flatten(data: dict, prefix: str = "") -> dict:
    flat = {}
    for key, value in data.items():
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{prefix}{key}."))
        else:
            flat[f"{prefix}{key}"] = value
    return flat


def _show(value) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, str):
        text = value if len(value) <= MAX_VALUE else value[:MAX_VALUE] + "…"
        return f'"{text}"'
    return str(value)


def changes(before: dict, after: dict, secrets: tuple[str, ...] = ()) -> str:
    """Describe what differs between two settings dicts, e.g. `launch.port 7777 → 7780`.

    `secrets` are dotted names whose values must not be written. Returns "" when nothing changed.
    """
    old, new = _flatten(before), _flatten(after)
    parts = []
    for name in new:
        if old.get(name) == new[name]:
            continue
        if name in secrets:
            parts.append(f"{name} changed")
        elif isinstance(new[name], list):
            was = old.get(name) if isinstance(old.get(name), list) else []
            added = [str(v) for v in new[name] if v not in was]
            removed = [str(v) for v in was if v not in new[name]]
            detail = "; ".join(filter(None, ["added " + ", ".join(added) if added else "", "removed " + ", ".join(removed) if removed else ""]))
            parts.append(f"{name}: {detail or 'reordered'}")
        else:
            parts.append(f"{name} {_show(old.get(name))} → {_show(new[name])}")
    if len(parts) > MAX_CHANGES:
        parts = parts[:MAX_CHANGES] + [f"and {len(parts) - MAX_CHANGES} more"]
    return ", ".join(parts)
