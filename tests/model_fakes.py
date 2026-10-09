"""Shared fakes for the workman.model_tools tests (imported by tests/test_model_*.py).

No test starts a real model, a real systemd unit, a real ssh session or a real
browser. HOME is a temp dir holding a fake ~/bin/run-model (direct and role
mode), a fake model-router, and a fake ~/grok-web-mcp/server.py; PATH starts
with fake ssh, systemd-run, systemctl, codex, grok and cursor-agent. The job
controller really runs (its bundle is piped to `python -I -`) and really detaches
a supervisor, so launch, status, result, cancel, timeout and comeback are covered
end to end."""
from __future__ import annotations

import os
import signal
import stat
import sys
import time
from pathlib import Path

import pytest

from workman import model_tools as mt
from workman.model_tools import catalog as mcat


FAKE_RUN_MODEL = r'''#!/bin/bash
# fake run-model for tests. Roles:
#   cheap        Codex     gpt-6-luna low, read-only digests, summaries
#   review-grok  Cursor    cursor-grok-4.6-xhigh      independent check
# Direct mode, the sanctioned path:
case "${1:-}" in __CLIS__) direct "$@";; esac
log="$HOME/fake-run-model.log"
case "${1:-}" in
  cursor|grok|codex|astra|luna|terra)
    cli=$1; shift
    while [ $# -gt 0 ]; do case "$1" in --effort) e=$2; shift 2;; --cwd) c=$2; shift 2;; --out) o=$2; shift 2;; --) shift; p="$*"; break;; *) shift;; esac; done
    printf 'cli=%s effort=%s cwd=%s out=%s tmo=%s task=%s okey=%s ckey=%s esc=%s nocommit=%s\n' "$cli" "$e" "$c" "$o" \
      "${RUN_MODEL_TIMEOUT:-}" "${WORK_TASK_ID:-}" "${OPENAI_API_KEY:-none}" "${CLAUDE_CODE_OAUTH_TOKEN:-none}" \
      "${SESSION_ESCAPE_DONE:-}" "${RUN_MODEL_NO_COMMIT:-}" >> "$log"
    printf 'scrub lkey=%s admin=%s repl=%s node=%s pystart=%s bashenv=%s envv=%s\n' "${openai_api_key:-none}" \
      "${OPENAI_ADMIN_KEY:-none}" "${REPLICATE_API_TOKEN:-none}" "${NODE_OPTIONS:-none}" "${PYTHONSTARTUP:-none}" \
      "${BASH_ENV:-none}" "${ENV:-none}" >> "$log"
    printf '%s' "$p" > "$HOME/fake-last-prompt.txt"
    echo "run-model: $cli fake-$cli-1 effort=$e cwd=$c log=$o"
    mkdir -p "$HOME/orchestra/runs/active"; echo "cli=$cli" > "$HOME/orchestra/runs/active/$$.job"
    case "$p" in
      *SLEEPLONG*) sleep 30;;
      *FAILNOW*) echo boom > "$o"; rm -f "$HOME/orchestra/runs/active/$$.job"; echo "run-model: exit=3"; exit 3;;
      *TIMEOUTNOW*) echo partial > "$o"; echo "run-model: note: timed out after 1s"; exit 143;;
      *BIGLOG*) for i in $(seq 1 3000); do echo "line $i xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"; done > "$o"
                rm -f "$HOME/orchestra/runs/active/$$.job"; exit 0;;
    esac
    printf 'OpenAI Codex (fake)\nsession id: 01aa0000-0000-0000-0000-000000000001\nuser\n%s\ncodex\nthinking\ntokens used\n1,234\nFINAL ANSWER from %s\n' "$p" "$cli" > "$o"
    printf '{"exit":0,"seconds":0,"report":null}\n' > "$o.manifest.json"
    rm -f "$HOME/orchestra/runs/active/$$.job"
    echo "run-model: exit=0 seconds=0 log=$o"; exit 0;;
esac
role=$1; wd=$2; t=$3; name=$4
d="$HOME/handoffs/model-runs/$(date +%F)"; mkdir -p "$d"
echo "role=$role wd=$wd esc=${SESSION_ESCAPE_DONE:-}" >> "$log"
nohup bash -c "sleep 0.4; cp '$t' '$d/$name.out'; echo luna > '$d/$name.out.lane'; echo ok > '$d/$name.out.route'" > "$d/$name.err" 2>&1 < /dev/null &
echo "role=$role pid=$! wd=$wd ticket=$t started=now" > "$d/$name.meta"
echo "$name pid $! -> $d/$name.out"
'''


FAKE_SSH = r'''#!/bin/bash
host=""
while [ $# -gt 0 ]; do case "$1" in -o) shift 2;; -*) shift;; *) host=$1; shift; break;; esac; done
echo "$host $*" >> "$HOME/fake-ssh.log"
case " ${FAKE_SSH_DOWN:-} " in *" $host "*)
  echo "fleet-connect[$host]: no route" >&2; echo "ssh: connect to host $host port 22: Connection refused" >&2; exit 255;; esac
exec bash -c "$*"
'''


FAKE_SYSTEMD_RUN = r'''#!/bin/bash
echo "$*" >> "$HOME/fake-systemd-run.log"
unit=""; out=""; wd=""; envs=()
while [ $# -gt 0 ]; do
  case "$1" in
    --unit=*) unit=${1#--unit=};;
    --property=StandardOutput=append:*) out=${1#--property=StandardOutput=append:};;
    --setenv=*) envs+=("${1#--setenv=}");;
    --working-directory=*) wd=${1#--working-directory=};;
    --) shift; break;;
    -*) ;;
    *) break;;
  esac; shift
done
[ "${FAKE_SYSTEMD_FAIL:-0}" = 1 ] && { echo "Failed to connect to bus" >&2; exit 1; }
mkdir -p "$HOME/fake-units"
( cd "${wd:-.}"; env "${envs[@]}" nohup "$@" >> "${out:-/dev/null}" 2>&1 < /dev/null & echo $! > "$HOME/fake-units/$unit.pid" )
exit 0
'''


FAKE_SYSTEMCTL = r'''#!/bin/bash
[ "$1" = --user ] && shift
cmd=$1; unit=${2%.service}
f="$HOME/fake-units/$unit.pid"
echo "$cmd $unit" >> "$HOME/fake-systemctl.log"
case "$cmd" in
  is-active) if [ -f "$f" ] && kill -0 "$(cat "$f")" 2>/dev/null; then echo active; else echo inactive; exit 3; fi;;
  stop) [ -f "$f" ] && kill -TERM "$(cat "$f")" 2>/dev/null; exit 0;;
  list-units) for u in "$HOME"/fake-units/*.pid; do [ -e "$u" ] || continue; n=$(basename "$u" .pid)
                if kill -0 "$(cat "$u")" 2>/dev/null; then echo "$n.service loaded active running fake"
                else echo "$n.service loaded inactive dead fake"; fi; done; exit 0;;
  *) exit 0;;
esac
'''


FAKE_CODEX = r'''#!/bin/bash
[ "$1" = exec ] || echo codex "$*" >> "$HOME/fake-cli.log"
case "$1" in
  --version) echo "codex-cli 9.9.9"; exit 0;;
  login) echo "Logged in using ChatGPT"; exit 0;;
  exec) ;;
  *) exit 0;;
esac
# Inside the bwrap sandbox: HOME is the empty work dir, cwd is <job>/work, and only
# <job> is writable. Logs go to <job>; the planted secrets of the test home must be gone.
R=$(cd .. && pwd)
H=$(cd "$PWD/../../../../.." && pwd)
printf '%s\n' "$*" > "$R/fake-codex-argv.txt"
{ [ -e "$H/ENV/secret.txt" ] && echo "env=VISIBLE" || echo "env=hidden"
  [ -e "$H/.ssh/id_test" ] && echo "ssh=VISIBLE" || echo "ssh=hidden"
  echo "history_bytes=$(cat "$CODEX_HOME/history.jsonl" 2>/dev/null | wc -c | tr -d ' ')"
  echo "codex_home=$(ls -A "$CODEX_HOME" | tr '\n' ',' | sed 's/,$//')"
  echo "config_mcp=$(grep -c mcp_servers "$CODEX_HOME/config.toml") config_apps_off=$(grep -c '^apps = false' "$CODEX_HOME/config.toml")"
  [ -s "$CODEX_HOME/auth.json" ] && echo "auth=present" || echo "auth=missing"
  echo "tmp_entries=$(ls -A /tmp | tr '\n' ',' | sed 's/,$//')"
} > "$R/fake-codex-sandbox.txt"
printf 'okey=%s lkey=%s ckey=%s node=%s task=%s pwd=%s\n' "${OPENAI_API_KEY:-none}" "${openai_api_key:-none}" \
  "${CLAUDE_CODE_OAUTH_TOKEN:-none}" "${NODE_OPTIONS:-none}" "${WORK_TASK_ID:-}" "$PWD" > "$R/fake-codex-env.txt"
printf 'home=%s codex_home=%s\n' "$HOME" "$CODEX_HOME" >> "$R/fake-codex-env.txt"
env | cut -d= -f1 | sort | tr '\n' ' ' > "$R/fake-codex-env-names.txt"
o=""; prev=""
for a in "$@"; do [ "$prev" = -o ] && o=$a; prev=$a; done
p=$(cat)
printf '%s' "$p" > "$R/fake-last-prompt.txt"
echo "session id: 01aa0000-0000-0000-0000-000000000001"
n=$(printf '%s\n' "$p" | sed -n 's/^Generate \([0-9]*\) image.*/\1/p' | head -1); n=${n:-1}
case "$p" in *NOIMAGE*) n=0;; esac
g="$CODEX_HOME/generated_images/01aa0000-0000-0000-0000-000000000001"; mkdir -p "$g"
i=1; while [ "$i" -le "$n" ]; do printf '\211PNG\r\n\032\nfake' > "$g/exec-$i.png"; i=$((i+1)); done
[ -n "$o" ] && echo "image 1: a red circle" > "$o"
echo "tokens used"; echo "10"; echo "image 1: a red circle"
'''


FAKE_GROK = '#!/bin/bash\necho grok "$*" >> "$HOME/fake-cli.log"\n' \
            'printf "You are logged in with grok.com.\\n\\nDefault model: grok-4.7\\n\\nAvailable models:\\n' \
            '  * grok-4.7 (default)\\n  - grok-4.6\\n"\n'


FAKE_CURSOR = '#!/bin/bash\necho cursor "$*" >> "$HOME/fake-cli.log"\n' \
              'printf "Available models\\n\\nauto - Auto (default)\\ncomposer-2.5 - Composer 2.5\\n' \
              'grok-4.7-high - Grok 4.7 High\\n"\n'


FAKE_GROK_SERVER = r'''
import json, os
from pathlib import Path
OUT = Path(os.environ["FAKE_GROK_OUT"])
CALLS = Path(os.environ["FAKE_GROK_CALLS"])
DEFAULT_QUOTA = {"status": 200, "json": {"image": {"available": 5, "windowSizeSeconds": 64800},
                                        "video": {"available": 2, "windowSizeSeconds": 64800}}}

def _log(name, kw):
    with open(CALLS, "a") as fh:
        fh.write(json.dumps([name, kw]) + "\n")

def imagine_quota():
    _log("imagine_quota", {})
    q = os.environ.get("FAKE_GROK_QUOTA")
    return json.dumps(json.loads(q) if q else DEFAULT_QUOTA, indent=1)[:1500]

def imagine_image(prompt, wait_seconds=180):
    _log("imagine_image", {"prompt": prompt, "wait_seconds": wait_seconds})
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / "20261008-000000_cat_abcdef12.jpg"
    p.write_bytes(b"\xff\xd8\xff fake")
    return "OK\n" + str(p)

def imagine_video(prompt, aspect_ratio="9:16", duration="15s", resolution="1080p", audio="on",
                  wait_seconds=600, filename=""):
    _log("imagine_video", {"aspect_ratio": aspect_ratio, "duration": duration, "audio": audio})
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / "clip.mp4"
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    return "OK %s (0.0 MB) post_id=11111111-2222-3333-4444-555555555555" % p

def imagine_video_with_refs(prompt, image_paths, voice="", duration="15s", resolution="720p",
                            wait_seconds=600, filename=""):
    _log("imagine_video_with_refs", {"image_paths": image_paths, "duration": duration})
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / "refclip.mp4"
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    return "OK %s (0.0 MB) post_id=66666666-2222-3333-4444-555555555555" % p

def _set_controls(model="", count="", aspect_ratio=""):
    _log("imagine_set_image_controls", {"count": count, "aspect_ratio": aspect_ratio})
    return json.dumps({"set": [count, aspect_ratio]})

class _Tool:
    def __init__(self, fn):
        self.fn = fn

class _Manager:
    def get_tool(self, name):
        return _Tool({"imagine_set_image_controls": _set_controls}[name])

class _Mcp:
    _tool_manager = _Manager()

mcp = _Mcp()
'''


def _exe(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    home = tmp_path / "home"
    fakebin = tmp_path / "fakebin"
    _exe(home / "bin" / "run-model",
         FAKE_RUN_MODEL.replace("__CLIS__", "cursor|grok|codex|astra|luna|terra"))
    _exe(home / "bin" / "model-router", "#!/bin/bash\nexit 0\n")
    _exe(fakebin / "ssh", FAKE_SSH)
    _exe(fakebin / "systemd-run", FAKE_SYSTEMD_RUN)
    _exe(fakebin / "systemctl", FAKE_SYSTEMCTL)
    _exe(fakebin / "codex", FAKE_CODEX)
    _exe(fakebin / "grok", FAKE_GROK)
    _exe(fakebin / "cursor-agent", FAKE_CURSOR)
    grok = tmp_path / "grok-web-mcp"
    grok.mkdir()
    (grok / "server.py").write_text(FAKE_GROK_SERVER)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{fakebin}:{os.environ.get('PATH', '/usr/bin:/bin')}")
    monkeypatch.setenv("WM_JOBCTL_POLL", "0.05")
    monkeypatch.setenv("WM_JOBCTL_NO_SYSTEMD", "1")
    monkeypatch.setenv("WORKMAN_MACHINE", "dgx")
    monkeypatch.setenv("WORKMAN_JOBCTL_PYTHON", sys.executable)
    monkeypatch.setenv("WORKMAN_GROK_WEB_DIR", str(grok))
    monkeypatch.setenv("WORKMAN_GROK_PYTHON", sys.executable)
    monkeypatch.setenv("FAKE_GROK_OUT", str(grok / "out"))
    monkeypatch.setenv("FAKE_GROK_CALLS", str(tmp_path / "grok-calls.jsonl"))
    # Loader variables are listed too: they must be refused even when named here.
    monkeypatch.setenv("WM_JOBCTL_KEEP_ENV", "FAKE_GROK_OUT,FAKE_GROK_CALLS,FAKE_GROK_QUOTA,"
                                             "BASH_ENV,ENV,NODE_OPTIONS,PYTHONSTARTUP")
    # Secrets and loaders a worker must never see.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.setenv("openai_api_key", "lower-test-not-real")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "admin-test-not-real")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8-test-not-real")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "claude-test-not-real")
    monkeypatch.setenv("NODE_OPTIONS", "--require=/nonexistent/evil.js")
    monkeypatch.setenv("PYTHONSTARTUP", "/nonexistent/evil.py")
    # Secrets an image run must not reach (the bwrap sandbox hides HOME).
    (home / "ENV").mkdir()
    (home / "ENV" / "secret.txt").write_text("SECRET-ENV\n")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_test").write_text("SECRET-SSH\n")
    (home / ".codex").mkdir()
    (home / ".codex" / "history.jsonl").write_text('{"text": "SECRET-HISTORY"}\n')
    (home / ".codex" / "auth.json").write_text('{"auth_mode": "chatgpt"}\n')
    (home / ".codex" / "config.toml").write_text('[mcp_servers.local]\nurl = "http://127.0.0.1:1/mcp"\n')
    benv = tmp_path / "bash_env.sh"
    benv.write_text('echo "$0" >> "$HOME/bash-env-sourced.log"\n')
    monkeypatch.setenv("BASH_ENV", str(benv))
    monkeypatch.setenv("ENV", str(benv))
    monkeypatch.delenv("FAKE_SSH_DOWN", raising=False)
    monkeypatch.delenv("FAKE_GROK_QUOTA", raising=False)
    mcat._MODELS_CACHE.clear()
    started: list[str] = []
    yield {"home": home, "tmp": tmp_path, "grok": grok, "started": started}
    # Never leave a detached fake behind.
    for d in (home / "orchestra" / "runs" / "workman").glob("wm-*"):
        for name in ("child_pid", "pid"):
            f = d / name
            if f.is_file() and f.read_text().strip().isdigit():
                try:
                    os.killpg(int(f.read_text().strip()), signal.SIGKILL)
                except OSError:
                    pass


def wait_state(job: str, states=mt.TERMINAL, timeout: float = 20.0) -> dict:
    end = time.time() + timeout
    st = mt.agent_status(job)
    while st.get("state") not in states and time.time() < end:
        time.sleep(0.1)
        st = mt.agent_status(job)
    return st


def fake_log(fleet, name="fake-run-model.log") -> str:
    p = fleet["home"] / name
    return p.read_text() if p.exists() else ""
