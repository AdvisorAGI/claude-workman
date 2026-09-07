"""Which account is this machine signed in as, on the Claude lane and the Grok
lane, without knowing any account in advance.

Workman is installed as a plugin. A plugin cannot be built around one email
address: whoever is logged in on the machine it lands on is the account it must
work for. The DGX alone carries seven Claude config directories with five
distinct signed-in identities, and a child process launched from a hook there
resolves the DEFAULT directory rather than the one that owns the session that
fired the hook. That is the bug this module exists to close: give a caller the
config dir and identity of the ACTIVE lane so it can pass it on explicitly.

Where an account lives, verified on the fleet 2026-09-05:
  ~/.claude.json                 the mini keeps its only account here
  <config_dir>/.claude.json      the DGX keeps one inside most of its dirs
  both at once                   also the DGX: a root file AND per-dir files
So a config dir's account file is its own .claude.json when that exists, and
~/.claude.json only for the DEFAULT dir (~/.claude). Anything else would report
the root account for every alternate dir on the box.

Resolution order for the active lane, most explicit first:
  an explicit config_dir argument, then $CLAUDE_CONFIG_DIR, then the config dir
  implied by a transcript path (<config_dir>/projects/<slug>/<sid>.jsonl, so
  parents[2]), then ~/.claude. The answer carries `source` so a caller can see
  which of those it got rather than guessing.

WHAT THIS MODULE WILL NOT RETURN. Only the identity fields in IDENTITY_FIELDS
are read out of `oauthAccount`, by allowlist, never by exclusion. The rest of
.claude.json is not read: it holds `mcpServers`, whose entries carry bearer
tokens in their env blocks. On the Grok side nothing is parsed at all. The seat
check is the same presence test ~/.claude/hooks/xai-seat-check.sh makes: does
the auth file contain the marker `"https://auth.x.ai::`. No key file is read
into a value, XAI_API_KEY is never read (only tested for emptiness), and
api.x.ai is never called. A PreToolUse guard denies those and it is right to.

Nothing here raises. Every entry point returns a dict, with an `error` key when
something went wrong, matching workman/session.py.

Env:
  CLAUDE_CONFIG_DIR    the config dir the current session is running under
  XAI_API_KEY          tested for presence only, never read
  GROK_API_KEY         tested for presence only, never read
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# The only keys copied out of oauthAccount. An allowlist, so a field added
# upstream (an access token, a refresh token, a session cookie) cannot leak by
# default: it is simply not on this list.
IDENTITY_FIELDS = (
    "emailAddress",
    "displayName",
    "organizationName",
    "organizationUuid",
    "accountUuid",
    "seatTier",
    "billingType",
)

ACCOUNT_FILE = ".claude.json"
DEFAULT_CONFIG_DIRNAME = ".claude"
CONFIG_DIR_ENV = "CLAUDE_CONFIG_DIR"

# The marker a logged-in Grok seat writes as a KEY NAME in ~/.grok/auth.json.
# Matched against the raw text, so no value is ever parsed out of that file.
GROK_SEAT_MARKER = '"https://auth.x.ai::'
# The seat CLI, in the order the fleet installs it. Deliberately NOT a PATH
# search: /opt/homebrew/bin/grok on the mini is a different product entirely
# (@vibe-kit/grok-cli), and reporting it as the seat would be a false positive.
GROK_CLI_CANDIDATES = ("~/.grok/bin/grok", "/usr/local/bin/grok",
                       "~/.local/bin/grok")
GROK_KEY_ENV = ("XAI_API_KEY", "GROK_API_KEY")


def _home() -> Path:
    """Home as the shell sees it, so a test can move it with $HOME."""
    return Path(os.path.expanduser("~"))


def default_config_dir() -> Path:
    return _home() / DEFAULT_CONFIG_DIRNAME


def _expand(path: str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(path)))


def config_dir_from_transcript(transcript_path: str) -> str:
    """The config dir a transcript sits under.

    Claude Code writes <config_dir>/projects/<cwd slug>/<session_id>.jsonl, so
    the config dir is exactly three levels up. Derived, never guessed from a
    slug: a cwd containing a space is slugged with dashes too, so rebuilding
    the slug by hand points at a directory nothing reads.
    """
    if not transcript_path:
        return ""
    try:
        parents = _expand(transcript_path).parents
        candidate = parents[2]
    except (IndexError, OSError, ValueError):
        return ""
    # Only trust it when the shape actually matches, otherwise a stray path
    # would name some unrelated directory as a config dir.
    return str(candidate) if parents[1].name == "projects" else ""


def config_dirs() -> list[Path]:
    """Every Claude config dir on this machine, active one first.

    $CLAUDE_CONFIG_DIR (which may list several, separated by a colon or a
    comma), then ~/.claude, then every ~/.claude-* sibling. Deduplicated,
    existing directories only.
    """
    found: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        try:
            if not path.is_dir():
                return
        except OSError:
            return
        key = str(path)
        if key not in seen:
            seen.add(key)
            found.append(path)

    raw = os.environ.get(CONFIG_DIR_ENV) or ""
    for chunk in raw.replace(",", ":").split(":"):
        if chunk.strip():
            add(_expand(chunk.strip()))
    add(default_config_dir())
    try:
        siblings = sorted(_home().glob(".claude-*"))
    except OSError:
        siblings = []
    for sibling in siblings:
        add(sibling)
    return found


def account_file_for(config_dir: str | Path) -> str:
    """The .claude.json that belongs to one config dir, or '' if it has none.

    Its own file wins. The home-root file is used only for the DEFAULT dir,
    because on a machine with several config dirs the root file belongs to the
    default one and reporting it for the others would name the wrong account.
    """
    try:
        directory = _expand(str(config_dir))
        own = directory / ACCOUNT_FILE
        if own.is_file():
            return str(own)
        if directory == default_config_dir():
            root = _home() / ACCOUNT_FILE
            if root.is_file():
                return str(root)
    except OSError:
        pass
    return ""


def _identity_from(path: str) -> dict[str, Any]:
    """The allowlisted identity fields in one account file.

    Reads `oauthAccount` and copies across only IDENTITY_FIELDS, and only when
    the value is a string, number or bool. Nothing else in the file is touched:
    `mcpServers` sits in the same document and carries bearer tokens.
    """
    identity: dict[str, Any] = {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return identity
    oauth = data.get("oauthAccount") if isinstance(data, dict) else None
    if not isinstance(oauth, dict):
        return identity
    for field in IDENTITY_FIELDS:
        value = oauth.get(field)
        if isinstance(value, (str, int, float, bool)) and not isinstance(value, bytes):
            identity[field] = value
    return identity


def describe(config_dir: str | Path) -> dict[str, Any]:
    """One config dir: where its account file is, and who is signed in there."""
    directory = str(_expand(str(config_dir)))
    path = account_file_for(directory)
    identity = _identity_from(path) if path else {}
    return {
        "config_dir": directory,
        "account_file": path,
        "logged_in": bool(identity.get("emailAddress")),
        "account": identity,
    }


# ---- what the tools call ---------------------------------------------------
def discover(config_dir: str = "", transcript_path: str = "") -> dict[str, Any]:
    """The account lane the CALLING session is on, and how it was decided.

    `source` is env, transcript, argument or default. When $CLAUDE_CONFIG_DIR
    and the transcript disagree, the env wins and the transcript's answer is
    reported as `transcript_config_dir` so the disagreement is visible rather
    than silent.
    """
    try:
        from_transcript = config_dir_from_transcript(transcript_path)
        env = (os.environ.get(CONFIG_DIR_ENV) or "").strip()
        env = env.replace(",", ":").split(":")[0].strip()
        if config_dir:
            chosen, source = str(_expand(config_dir)), "argument"
        elif env:
            chosen, source = str(_expand(env)), "env"
        elif from_transcript:
            chosen, source = from_transcript, "transcript"
        else:
            chosen, source = str(default_config_dir()), "default"
        out = {"ok": True, "source": source, **describe(chosen)}
        if from_transcript and from_transcript != out["config_dir"]:
            out["transcript_config_dir"] = from_transcript
        return out
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def enumerate_accounts(config_dir: str = "",
                       transcript_path: str = "") -> dict[str, Any]:
    """Every Claude config dir on this machine, with the active one marked."""
    try:
        active = discover(config_dir=config_dir, transcript_path=transcript_path)
        active_dir = active.get("config_dir") or ""
        accounts = []
        directories = [str(d) for d in config_dirs()]
        if active_dir and active_dir not in directories:
            directories.insert(0, active_dir)
        for directory in directories:
            entry = describe(directory)
            entry["active"] = entry["config_dir"] == active_dir
            accounts.append(entry)
        return {
            "ok": True,
            "count": len(accounts),
            "signed_in": sum(1 for a in accounts if a["logged_in"]),
            "active_config_dir": active_dir,
            "source": active.get("source", "default"),
            "accounts": accounts,
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def grok_seat() -> dict[str, Any]:
    """The Grok lane, by presence only.

    Owner rule: Grok runs on the logged-in CLI seat, never on a metered key.
    So this reports whether the CLI exists, whether the seat marker is present
    in the auth file, and whether a key variable is exported. It never reads a
    key, never prints a suffix, and never calls api.x.ai.
    """
    try:
        grok_home = _home() / ".grok"
        cli = ""
        for candidate in GROK_CLI_CANDIDATES:
            path = _expand(candidate)
            if path.is_file() and os.access(path, os.X_OK):
                cli = str(path)
                break
        auth = grok_home / "auth.json"
        logged_in = False
        try:
            # Text scan for the key NAME marker, the same test
            # ~/.claude/hooks/xai-seat-check.sh makes with grep. Deliberately
            # not json.load: parsing would pull token values into memory.
            logged_in = GROK_SEAT_MARKER in auth.read_text(
                encoding="utf-8", errors="replace")
        except Exception:
            logged_in = False
        auth_method = ""
        try:
            cache = json.loads((grok_home / "models_cache.json")
                               .read_text(encoding="utf-8"))
            value = cache.get("auth_method") if isinstance(cache, dict) else None
            auth_method = str(value) if isinstance(value, str) else ""
        except Exception:
            auth_method = ""
        return {
            "ok": True,
            "home": str(grok_home) if grok_home.is_dir() else "",
            "cli": cli,
            "installed": bool(cli),
            "logged_in": logged_in,
            "auth_method": auth_method,
            # Presence of the variable only. Its value is never read.
            "api_key_env_set": any(bool(os.environ.get(name))
                                   for name in GROK_KEY_ENV),
            "note": "seat presence only: no key is read and api.x.ai is never called",
        }
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def lanes(config_dir: str = "", transcript_path: str = "") -> dict[str, Any]:
    """Both lanes at once: every Claude account here, and the Grok seat.

    The stable shape a caller picks a lane from without knowing any account in
    advance: `claude` carries the enumeration with `active` marked, `grok`
    carries the seat.
    """
    try:
        claude = enumerate_accounts(config_dir=config_dir,
                                    transcript_path=transcript_path)
        grok = grok_seat()
        out = {
            "ok": bool(claude.get("ok")) and bool(grok.get("ok")),
            "host": os.uname().nodename if hasattr(os, "uname") else "",
            "claude": claude,
            "grok": grok,
        }
        if not out["ok"]:
            # A caller checking only the top-level ok must not be told the
            # answer is good when half of it failed.
            out["error"] = claude.get("error") or grok.get("error") or "lane lookup failed"
        return out
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def active(config_dir: str = "", transcript_path: str = "") -> dict[str, Any]:
    """The one lane this session should run its children on."""
    try:
        out = discover(config_dir=config_dir, transcript_path=transcript_path)
        out["grok"] = grok_seat()
        return out
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _line(data: dict[str, Any]) -> str:
    """One human line, the same sentence bin/workman-account prints."""
    account = data.get("account") or {}
    email = account.get("emailAddress") or "not-logged-in"
    org = account.get("organizationName") or "-"
    tier = account.get("seatTier") or "-"
    grok = data.get("grok") or {}
    seat = ("seat" if grok.get("logged_in")
            else ("installed" if grok.get("installed") else "absent"))
    key = "key-exported" if grok.get("api_key_env_set") else "no-key"
    return (f"claude: {email} org={org} tier={tier} "
            f"config_dir={data.get('config_dir') or '-'} "
            f"source={data.get('source') or '-'} | grok: {seat} "
            f"auth={grok.get('auth_method') or '-'} {key}")


def main(argv: list[str] | None = None) -> int:
    """CLI: python -m workman.account [--json] [--all] [--transcript PATH]

    Shared by bin/workman-account so the CLI, the hook and the MCP tools all
    come out of one code path.
    """
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    every = "--all" in args
    transcript = ""
    if "--transcript" in args:
        index = args.index("--transcript")
        if index + 1 < len(args):
            transcript = args[index + 1]
    if every:
        data = lanes(transcript_path=transcript)
        if as_json:
            print(json.dumps(data, indent=2, sort_keys=True))
            return 0
        for entry in (data.get("claude") or {}).get("accounts") or []:
            mark = "*" if entry.get("active") else " "
            account = entry.get("account") or {}
            print(f"{mark} {entry.get('config_dir')}  "
                  f"{account.get('emailAddress') or 'not-logged-in'}  "
                  f"{account.get('organizationName') or '-'}")
        print(_line({**active(transcript_path=transcript)}))
        return 0
    data = active(transcript_path=transcript)
    print(json.dumps(data, indent=2, sort_keys=True) if as_json else _line(data))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
