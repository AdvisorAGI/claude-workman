"""The bwrap filesystem sandbox of codex image runs (workman/model_tools/jobctl/sandbox.py).

Every test that runs a process does it through real bwrap with a zero-model stand-in
(/bin/sh or a small script) and is skipped where bwrap is unusable. Secrets are planted
in a temp HOME and must stay unreadable from inside, by ~ and by absolute path.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from workman.model_tools.jobctl import common, sandbox

needs_bwrap = pytest.mark.skipif(not sandbox.bwrap_path() or subprocess.run(
    [sandbox.bwrap_path() or "/nonexistent", "--ro-bind", "/", "/", "--dev", "/dev", "--", "/bin/true"],
    capture_output=True).returncode != 0, reason="no usable bwrap")
JOB = "wm-20261009T000000Z-dgx-image-abcdef"


@pytest.fixture
def box_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for rel, text in (("ENV/secret.txt", "SECRET-ENV"), (".ssh/id_test", "SECRET-SSH"),
                      (".config/gh/hosts.yml", "SECRET-GH"), (".claude/x.json", "SECRET-CLAUDE"),
                      (".local/share/keyrings/k", "SECRET-KEYRING"),
                      (".codex/history.jsonl", "SECRET-HISTORY"), (".codex/auth.json", "AUTH-OK"),
                      (".codex/sessions/2026/x.jsonl", "SECRET-SESSION"),
                      (".codex/config.toml", "[mcp_servers.x]\nsandbox_mode = 'danger-full-access'")):
        f = home / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text + "\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    d = home / "orchestra" / "runs" / "workman" / JOB
    (d / "work").mkdir(parents=True)
    return {"home": home, "d": d, "work": d / "work"}


def run_in(box_args, inner, home, extra_env=None, timeout=60):
    env = {"PATH": ":".join(common.IMAGE_PATH), "HOME": "/nonexistent", "REAL": str(home),
           "CODEX_HOME": sandbox.BOX_CODEX_HOME}
    env.update(extra_env or {})
    return subprocess.run(box_args + inner, capture_output=True, text=True, timeout=timeout, env=env)


@needs_bwrap
def test_home_secrets_unreachable_by_any_path(box_home):
    box, _ = sandbox.image_sandbox(str(box_home["d"]), str(box_home["work"]), "/bin/sh")
    probe = ("cat ~/ENV/secret.txt $REAL/ENV/secret.txt $REAL/.ssh/id_test $REAL/.config/gh/hosts.yml "
             "$REAL/.claude/x.json $REAL/.local/share/keyrings/k $REAL/.codex/history.jsonl "
             "$REAL/.codex/sessions/2026/x.jsonl $REAL/.codex/config.toml 2>/dev/null; echo; "
             "cat $CODEX_HOME/auth.json; ls -A $CODEX_HOME; ls -A /tmp; echo ok > out.txt; touch $REAL/x 2>/dev/null")
    r = run_in(box, ["/bin/sh", "-c", probe], box_home["home"])
    assert "SECRET" not in r.stdout and "danger" not in r.stdout
    assert "AUTH-OK" in r.stdout
    assert "auth.json\nconfig.toml\ngenerated_images\n" in r.stdout
    assert (box_home["work"] / "out.txt").read_text() == "ok\n"
    assert not (box_home["home"] / "x").exists()
    assert "--unshare-pid" in box and "--die-with-parent" in box


@needs_bwrap
def test_symlinked_codex_entries_do_not_leak(box_home):
    """Gap 1: the child's codex home is built from an allow-list; a symlinked config.toml
    or a symlink to a secret inside ~/.codex never reaches the child."""
    codex = box_home["home"] / ".codex"
    evil = box_home["home"] / "evil.toml"
    evil.write_text('sandbox_mode = "danger-full-access"\n[mcp_servers.leak]\nurl = "http://x"\n')
    (codex / "config.toml").unlink()
    (codex / "config.toml").symlink_to(evil)
    (codex / "link-to-env").symlink_to(box_home["home"] / "ENV" / "secret.txt")
    box, _ = sandbox.image_sandbox(str(box_home["d"]), str(box_home["work"]), "/bin/sh")
    r = run_in(box, ["/bin/sh", "-c", "cat $CODEX_HOME/config.toml; echo ---; ls -A $CODEX_HOME; "
                                      "cat $REAL/.codex/link-to-env $REAL/evil.toml 2>/dev/null; true"],
               box_home["home"])
    config, rest = r.stdout.split("---\n", 1)
    assert config == sandbox.MIN_CODEX_CONFIG
    assert rest.split() == ["auth.json", "config.toml", "generated_images"]
    assert "SECRET" not in r.stdout and "danger" not in r.stdout


def test_auth_symlink_pointing_outside_is_refused(box_home):
    auth = box_home["home"] / ".codex" / "auth.json"
    auth.unlink()
    auth.symlink_to(box_home["home"] / "ENV" / "secret.txt")
    with pytest.raises(sandbox.SandboxRefused, match="auth.json"):
        sandbox.image_sandbox(str(box_home["d"]), str(box_home["work"]), "/bin/sh")
    assert "auth.json" in sandbox.sandbox_problem()


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return path


@needs_bwrap
def test_install_under_local_binds_only_the_package(box_home):
    """Gap 2: a codex package and node under ~/.local are bound by themselves to
    /tmp/wm-sbx; nothing else of ~/.local (or ~) becomes visible."""
    pkg = box_home["home"] / ".local" / "lib" / "node_modules" / "@openai" / "codex"
    (pkg).mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({"name": "@openai/codex"}))
    probe = ('[ -e "$REAL/ENV/secret.txt" ] && echo env=VISIBLE || echo env=hidden; '
             '[ -e "$REAL/.local/share/keyrings/k" ] && echo keyring=VISIBLE || echo keyring=hidden; '
             'ls -A "$REAL/.local" 2>/dev/null | wc -l; echo "self=$0"; command -v node')
    codex = _script(pkg / "bin" / "codex.js", probe)
    node = _script(box_home["home"] / ".local" / "bin" / "node", "true")
    box, codex_in = sandbox.image_sandbox(str(box_home["d"]), str(box_home["work"]), str(codex), str(node))
    assert codex_in == sandbox.BOX + "/codex/bin/codex.js"
    binds = [box[i + 1] for i, a in enumerate(box) if a in ("--ro-bind", "--bind") and box[i + 1] != "/"]
    assert str(pkg) in binds and str(node) in binds
    assert not any(b in (str(box_home["home"]), str(box_home["home"] / ".local")) for b in binds)
    r = run_in(box, [codex_in], box_home["home"])
    assert r.stdout.split() == ["env=hidden", "keyring=hidden", "0", "self=/tmp/wm-sbx/codex/bin/codex.js",
                                "/tmp/wm-sbx/bin/node"], r.stdout + r.stderr


def test_a_package_that_holds_home_is_never_bound(box_home, tmp_path):
    (box_home["home"] / "package.json").write_text(json.dumps({"name": "@openai/codex"}))
    codex = _script(box_home["home"] / "bin" / "codex.js", "true")
    with pytest.raises(sandbox.SandboxRefused, match="holds the home dir"):
        sandbox.image_sandbox(str(box_home["d"]), str(box_home["work"]), str(codex))
    (box_home["home"] / "package.json").unlink()
    loose = _script(box_home["home"] / "tools" / "codex", "true")      # not a package: the file alone
    args, inner = sandbox.mount_exe(str(loose), "codex")
    assert args == ["--ro-bind", str(loose), sandbox.BOX_BIN + "/codex"] and inner == sandbox.BOX_BIN + "/codex"


def test_symlinked_job_or_work_dir_is_refused(box_home, tmp_path):
    """Gap 3: the job dir is bound by its real path only when it is the plain directory
    launch created under the jobs root; a symlink (to anything) is refused."""
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "work").mkdir(parents=True)
    root = box_home["d"].parent
    link = root / "wm-20261009T000000Z-dgx-image-000001"
    link.symlink_to(elsewhere)
    with pytest.raises(sandbox.SandboxRefused, match="not a plain directory"):
        sandbox.image_sandbox(str(link), str(link / "work"), "/bin/sh")
    work = box_home["work"]
    os.rmdir(work)
    work.symlink_to(elsewhere / "work")
    with pytest.raises(sandbox.SandboxRefused, match="not a plain directory"):
        sandbox.image_sandbox(str(box_home["d"]), str(work), "/bin/sh")


def test_no_bwrap_means_no_image_jobs(box_home, monkeypatch):
    monkeypatch.setenv("WM_JOBCTL_BWRAP", "/nonexistent/bwrap")
    assert "no usable bwrap" in sandbox.sandbox_problem()
