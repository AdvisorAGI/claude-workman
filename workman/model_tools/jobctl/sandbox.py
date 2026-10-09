"""OS-level filesystem sandbox for codex image jobs (bubblewrap, Linux).

The prompt is not the boundary: codex's own read-only sandbox still lets the model
read any file by absolute path. So the codex child runs inside bwrap where nothing
personal exists:

    /                    read-only (system files only)
    $HOME                an empty tmpfs: ~/ENV, ~/.ssh, ~/.config/gh, ~/.claude*,
                         ~/.codex, ~/.local, every repo and note are gone
    /tmp, /var/tmp       empty tmpfs
    /run/user/<uid>      empty tmpfs (no session bus, no keyring socket)
    pid namespace        its own (--unshare-pid): killing bwrap ends every process inside
    /tmp/wm-sbx/codex    the exact @openai/codex package dir, read-only
    /tmp/wm-sbx/bin      the node binary file (and codex, when it is not a package)
    /tmp/wm-sbx/codex-home   the child's CODEX_HOME, built from an allow-list:
                         auth.json   the real login file, bound alone, read-write so a
                                     token refresh lands in the real file
                         config.toml a minimal private config (read-only sandbox, no
                                     MCP servers, outward features off), read-only
                         generated_images   <job>/generated
    <job dir>            read-write, bound by its real path after checking it is the
                         directory launch created (no symlinks)

Nothing under $HOME is ever bound as a directory except the job dir; no parent of
home content is bound at all. Symlinks are resolved and refused when they point
anywhere unexpected. systemd --user units cannot do this here (ProtectHome= and
InaccessiblePaths= are ignored in user units, probed 2026-10-08), so this is bwrap;
a machine without a working bwrap (the Macs) refuses image jobs.

The network stays shared: codex has to reach OpenAI. Shell commands the model runs
are network-blocked by codex's own read-only sandbox, and the codex features that
reach outside the job are off in the private config.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

from .common import _write, home, jobs_root  # jobctl:local

BOX = "/tmp/wm-sbx"
BOX_BIN = BOX + "/bin"
BOX_CODEX_HOME = BOX + "/codex-home"
# The private config of an image run: no MCP servers, read-only sandbox, and the
# features that reach outside the job off (ChatGPT apps/connectors, browser use with
# CDP access to the logged-in Chrome, computer use). image_generation stays on.
MIN_CODEX_CONFIG = """approval_policy = "never"
sandbox_mode = "read-only"

[features]
image_generation = true
apps = false
browser_use = false
browser_use_external = false
browser_use_full_cdp_access = false
computer_use = false
"""


class SandboxRefused(Exception):
    """The sandbox cannot be built safely for this job; nothing was started."""


def bwrap_path() -> str:
    return os.environ.get("WM_JOBCTL_BWRAP") or shutil.which("bwrap", path="/usr/bin:/bin:/usr/local/bin") or ""


def codex_home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.join(home(), ".codex")


def auth_file() -> str:
    """The real auth.json: a regular file directly inside the codex home, symlinks
    resolved. Anything else is refused."""
    ch = os.path.realpath(codex_home())
    real = os.path.realpath(os.path.join(codex_home(), "auth.json"))
    if not os.path.isfile(real) or os.path.dirname(real) != ch:
        raise SandboxRefused("auth.json is missing or points outside %s" % ch)
    return real


def sandbox_problem() -> str:
    """'' when bwrap can build the image sandbox on this machine, else the reason."""
    b = bwrap_path()
    if not b or not os.access(b, os.X_OK):
        return "no usable bwrap on this machine; codex image jobs need the Linux bwrap sandbox"
    try:
        auth_file()
        r = subprocess.run([b, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", home(),
                            "--", "/bin/true"], capture_output=True, text=True, timeout=20)
    except SandboxRefused as exc:
        return str(exc)
    except (OSError, subprocess.SubprocessError) as exc:
        return "bwrap cannot start here: %s" % exc
    return "" if r.returncode == 0 else "bwrap cannot start here: %s" % (r.stderr or "").strip()[-200:]


def owned_dir(path: str, parent: str) -> str:
    """The real path of a directory this job created directly under `parent`: not a
    symlink, a directory, and its real parent is the real `parent`."""
    if os.path.islink(path) or not os.path.isdir(path):
        raise SandboxRefused("%s is not a plain directory" % path)
    real = os.path.realpath(path)
    if os.path.dirname(real) != os.path.realpath(parent):
        raise SandboxRefused("%s resolves outside %s" % (path, parent))
    return real


def codex_package(exe: str) -> str:
    """The @openai/codex package dir holding `exe` (by its package.json name), or ''."""
    d = os.path.dirname(os.path.realpath(exe))
    for _ in range(4):
        try:
            with open(os.path.join(d, "package.json"), encoding="utf-8") as fh:
                if json.load(fh).get("name") == "@openai/codex":
                    return d
        except (OSError, ValueError, AttributeError):
            pass
        d = os.path.dirname(d)
    return ""


def _no_home_parent(path: str) -> None:
    h, p = os.path.realpath(home()), os.path.realpath(path)
    if p in ("/", h) or h.startswith(p.rstrip("/") + "/"):
        raise SandboxRefused("refusing to bind %s: it holds the home dir" % p)


def mount_exe(exe: str, name: str):
    """Bind arguments for an executable and its path inside the box: the exact codex
    package dir, or the single executable file. Never a parent of home content."""
    real = os.path.realpath(exe)
    if not os.path.isfile(real):
        raise SandboxRefused("%s is not a file" % exe)
    pkg = codex_package(real) if name == "codex" else ""
    if pkg:
        _no_home_parent(pkg)
        inner = BOX + "/codex"
        return ["--ro-bind", pkg, inner], os.path.join(inner, os.path.relpath(real, pkg))
    inner = BOX_BIN + "/" + name
    return ["--ro-bind", real, inner], inner


def image_sandbox(d: str, work: str, codex: str, node: str = ""):
    """(bwrap argv prefix, codex path inside the box) for a codex image run of job `d`."""
    d = owned_dir(d, jobs_root())
    work = owned_dir(work, d)
    auth = auth_file()
    cfg = os.path.join(d, "codex-config.toml")
    _write(cfg, MIN_CODEX_CONFIG)
    gen = os.path.join(d, "generated")
    if os.path.lexists(gen):
        gen = owned_dir(gen, d)
    else:
        os.makedirs(gen)
    args = [bwrap_path(), "--die-with-parent", "--unshare-pid", "--ro-bind", "/", "/", "--dev", "/dev",
            "--proc", "/proc", "--tmpfs", "/tmp", "--tmpfs", "/var/tmp"]
    run = "/run/user/%d" % os.getuid()
    if os.path.isdir(run):
        args += ["--tmpfs", run]
    args += ["--tmpfs", home()]
    real_ch = os.path.realpath(codex_home())
    if not real_ch.startswith(os.path.realpath(home()).rstrip("/") + "/"):
        args += ["--tmpfs", real_ch]      # a codex home outside $HOME is hidden too
    codex_args, codex_in = mount_exe(codex, "codex")
    args += codex_args
    if node:
        args += mount_exe(node, "node")[0]
    args += ["--dir", BOX_CODEX_HOME, "--bind", auth, BOX_CODEX_HOME + "/auth.json",
             "--ro-bind", cfg, BOX_CODEX_HOME + "/config.toml",
             "--bind", gen, BOX_CODEX_HOME + "/generated_images",
             "--bind", d, d, "--chdir", work, "--"]
    return args, codex_in
