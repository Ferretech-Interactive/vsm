"""Line-preserving editor for Unreal Engine ini files.

configparser cannot be used here: UE ini files rely on duplicate keys and
`+Key=` array syntax, and the server is sensitive to key casing. Everything the
manager does not explicitly touch is written back byte-for-byte.
"""
from __future__ import annotations

import os
from pathlib import Path

from .. import VsmError


def _check(key: str, value: str) -> None:
    if not key or any(c in key for c in "=[]\r\n") or "\n" in value or "\r" in value:
        raise VsmError("Invalid ini key or value.")


class IniFile:
    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            text = ""
        self.lines = text.splitlines()

    @staticmethod
    def _header(line: str) -> str | None:
        s = line.strip()
        return s[1:-1] if s.startswith("[") and s.endswith("]") else None

    @staticmethod
    def _split(line: str) -> tuple[str, str] | None:
        s = line.strip()
        if not s or s.startswith(";") or "=" not in s:
            return None
        k, v = s.split("=", 1)
        return k.strip(), v.strip()

    def _is(self, line: str, key: str, array: bool = False) -> bool:
        kv = self._split(line)
        if not kv:
            return False
        k = kv[0].lstrip("+") if array else kv[0]
        return k.lower() == key.lower()

    def _bounds(self, section: str) -> tuple[int, int] | None:
        start = None
        for i, line in enumerate(self.lines):
            h = self._header(line)
            if h is None:
                continue
            if start is not None:
                return start, i
            if h.lower() == section.lower():
                start = i + 1
        return (start, len(self.lines)) if start is not None else None

    def _ensure(self, section: str) -> tuple[int, int]:
        b = self._bounds(section)
        if b:
            return b
        if self.lines and self.lines[-1].strip():
            self.lines.append("")
        self.lines.append(f"[{section}]")
        return len(self.lines), len(self.lines)

    def _tail(self, bounds: tuple[int, int]) -> int:
        """Index just after the last non-blank line of a section."""
        start, end = bounds
        while end > start and not self.lines[end - 1].strip():
            end -= 1
        return end

    def sections(self) -> list[str]:
        seen: dict[str, None] = {}
        for line in self.lines:
            h = self._header(line)
            if h is not None:
                seen.setdefault(h)
        return list(seen)

    def entries(self, section: str) -> list[tuple[str, str]]:
        b = self._bounds(section)
        if not b:
            return []
        return [kv for line in self.lines[b[0]:b[1]] if (kv := self._split(line))]

    def get(self, section: str, key: str, default: str | None = None) -> str | None:
        for k, v in self.entries(section):
            if k.lower() == key.lower():
                return v
        return default

    def get_array(self, section: str, key: str) -> list[str]:
        return [v for k, v in self.entries(section) if k.lstrip("+").lower() == key.lower() and v]

    def set(self, section: str, key: str, value: str) -> None:
        value = str(value)
        _check(key, value)
        b = self._ensure(section)
        for i in range(*b):
            if self._is(self.lines[i], key):
                self.lines[i] = f"{key}={value}"
                return
        self.lines.insert(self._tail(b), f"{key}={value}")

    def remove(self, section: str, key: str) -> None:
        """Remove `key` and any `+key` array continuation lines."""
        b = self._bounds(section)
        if b:
            self.lines[b[0]:b[1]] = [l for l in self.lines[b[0]:b[1]] if not self._is(l, key, array=True)]

    def set_array(self, section: str, key: str, values: list[str]) -> None:
        values = [str(v).strip() for v in values if str(v).strip()]
        for v in values:
            _check(key, v)
        self.remove(section, key)
        if not values:
            return
        at = self._tail(self._ensure(section))
        self.lines[at:at] = [f"{'' if i == 0 else '+'}{key}={v}" for i, v in enumerate(values)]

    def replace_section(self, section: str, entries: list[tuple[str, str]]) -> None:
        for k, v in entries:
            _check(k, v)
        b = self._ensure(section)
        self.lines[b[0]:b[1]] = [f"{k}={v}" for k, v in entries] + [""]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text("\n".join(self.lines).rstrip("\n") + "\n", encoding="utf-8", newline="\n")
        os.replace(tmp, self.path)
