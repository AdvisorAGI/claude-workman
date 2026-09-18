"""Discover logged-in CLI seats and build argv. Never interpolates a shell."""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# House rule: Grok runs on the logged-in CLI seat, not a metered API key.
_STRIP_ENV = ("XAI_API_KEY", "XAI_API_KEYS")


@dataclass(frozen=True)
class Vendor:
    id: str
    label: str
    binaries: tuple[str, ...]
    fallback_models: tuple[str, ...]
    default_model: str
    list_models: tuple[str, ...] | None = None  # extra argv after the binary


def which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    home = Path.home()
    names = [name]
    if os.name == "nt" and not name.lower().endswith(".exe"):
        names.append(name + ".exe")
    roots = [
        home / ".grok" / "bin",
        home / ".local" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path("/usr/bin"),
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "Programs" / "cursor")
    for root in roots:
        for candidate in names:
            path = root / candidate
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
    return None


VENDORS: tuple[Vendor, ...] = (
    Vendor("grok", "Grok", ("grok",), ("grok-4.6", "grok-4.5"), "grok-4.6",
           list_models=("models",)),
    Vendor("claude", "Claude", ("claude",),
           ("sonnet", "opus", "haiku", "fable"), "sonnet"),
    Vendor("codex", "Codex", ("codex",),
           ("gpt-5.2-codex", "o3", "gpt-4.1"), "gpt-5.2-codex"),
    Vendor("cursor", "Cursor", ("cursor-agent", "agent"),
           ("auto", "composer-2.5", "gpt-5.2"), "auto",
           list_models=("--list-models",)),
)


def parse_grok_models(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(("*", "-")):
            token = line.lstrip("*- ").split()[0]
            if token:
                out.append(token)
    return out


def parse_cursor_models(text: str) -> list[str]:
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.lower().startswith("available"):
            continue
        token = line.split()[0]
        if token.endswith("-"):
            continue
        if token.replace(".", "").replace("-", "").replace("_", "").isalnum():
            out.append(token)
    return out


def _run_list(bin_path: str, extra: tuple[str, ...]) -> str:
    try:
        proc = subprocess.run(
            [bin_path, *extra], capture_output=True, text=True, timeout=8,
            env=_seat_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (proc.stdout or "") + (proc.stderr or "")


def _seat_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in _STRIP_ENV:
        env.pop(key, None)
    return env


def models_for(vendor: Vendor, bin_path: str) -> list[str]:
    if vendor.list_models:
        text = _run_list(bin_path, vendor.list_models)
        parsed = parse_grok_models(text) if vendor.id == "grok" else parse_cursor_models(text)
        if parsed:
            return parsed
    return list(vendor.fallback_models)


@dataclass
class Seat:
    vendor: Vendor
    binary: str
    models: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.vendor.id,
            "label": self.vendor.label,
            "binary": self.binary,
            "models": self.models,
            "default_model": (
                self.vendor.default_model if self.vendor.default_model in self.models
                else (self.models[0] if self.models else self.vendor.default_model)
            ),
        }


_catalog_cache: tuple[float, list[Seat]] | None = None
_CATALOG_TTL = 30.0


def catalog(*, refresh: bool = False) -> list[Seat]:
    import time
    global _catalog_cache
    now = time.monotonic()
    if not refresh and _catalog_cache and now - _catalog_cache[0] < _CATALOG_TTL:
        return _catalog_cache[1]
    seats: list[Seat] = []
    for vendor in VENDORS:
        binary = None
        for name in vendor.binaries:
            binary = which(name)
            if binary:
                break
        if not binary:
            continue
        seats.append(Seat(vendor, binary, models_for(vendor, binary)))
    _catalog_cache = (now, seats)
    return seats


def build_argv(
    vendor_id: str,
    binary: str,
    model: str,
    prompt: str,
    *,
    prompt_file: str | None = None,
    auto_approve: bool = False,
) -> list[str]:
    """Argv only. Callers pass this to subprocess — never to a shell."""
    vid = (vendor_id or "").strip().lower()
    model = (model or "").strip()
    prompt = prompt or ""
    if vid == "grok":
        cmd = [binary, "--no-auto-update", "--no-alt-screen"]
        if model:
            cmd.extend(["-m", model])
        if auto_approve:
            cmd.append("--always-approve")
        if prompt_file:
            cmd.extend(["--prompt-file", prompt_file])
        else:
            cmd.extend(["-p", prompt])
        return cmd
    if vid == "claude":
        cmd = [binary, "--output-format", "text"]
        if model:
            cmd.extend(["--model", model])
        cmd.extend(["-p", prompt])
        return cmd
    if vid == "codex":
        cmd = [binary, "exec"]
        if model:
            cmd.extend(["-m", model])
        if auto_approve:
            cmd.append("--full-auto")
        cmd.append(prompt)
        return cmd
    if vid == "cursor":
        cmd = [binary, "-p", "--output-format", "text"]
        if model:
            cmd.extend(["--model", model])
        if auto_approve:
            cmd.append("--force")
        cmd.append(prompt)
        return cmd
    raise ValueError(f"unknown vendor {vendor_id!r}")


def launch_env() -> dict[str, str]:
    return _seat_env()
