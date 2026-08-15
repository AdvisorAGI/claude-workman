# Recipe: register a Linux box as a device in the Claude mobile app

*Operator recipe, not a `claude-workman` tool.* Filed here because it sits
beside desktop control in practice: workman gives an agent the screen on this
machine, Remote Control server mode gives you the machine from your phone.

## The distinction that costs people an hour

Two features share the name "Remote Control":

| | what it publishes | URL you get |
|---|---|---|
| per-session | the session you are already in | `…/code/session_…` |
| **server mode** | **the machine itself** | `…/code?environment=env_…` |

A box can happily publish sessions for weeks and still never appear as a device
in the app's Code tab. **The `environment=env_…` line is the only proof of
registration.** If your log has only `session_…`, server mode is not running.

## Start it

```bash
cd /path/to/a/real/git/project
setsid nohup claude remote-control --name "BOXNAME" --spawn worktree \
  > ~/rc-server.log 2>&1 < /dev/null & disown
```

Each part earns its place:

- **Not `$HOME`.** Workspace trust does not persist for the home directory.
  Use a real project; a git repo with a clean tree is best, because spawned
  worktrees branch from it.
- **`setsid` + `nohup` + `disown`.** It is a long-running foreground server.
  Backgrounding with a plain `&` leaves it attached to the shell's session and
  it dies with the terminal. Running it in the foreground never returns.
- **`--spawn worktree`.** Every mobile session gets an isolated git worktree
  rather than sharing your checkout. Consequence worth knowing: work you start
  from your phone lands in a worktree, not on `main`.

## Verify detachment, don't assume it

```bash
pgrep -af "claude remote-control"
grep -o 'environment=env_[A-Za-z0-9]*' ~/rc-server.log | head -1

pid=$(pgrep -f "claude remote-control" | head -1)
ps -o pid=,ppid=,pgid=,sid=,tty=,etime= -p "$pid"
```

You want its own session id, its own process group, and **`tty` = `?`**. A
process still holding a controlling terminal will be killed with that terminal,
however many `nohup`s were involved.

### Gotcha: `pgrep` matches the shell that ran it

`pgrep -fl "claude remote-control"` happily returns a bare `bash` pid — that is
the shell whose own command line contains the search string. Use `pgrep -af`
and read the full command line before declaring a server alive or dead.

## When it does not connect

The cause is nearly always suppressed outbound traffic rather than the command:

```bash
for v in ANTHROPIC_BASE_URL DISABLE_TELEMETRY DO_NOT_TRACK \
         CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC DISABLE_GROWTHBOOK; do
  echo "$v=${!v:-<unset>}"
done
grep -iE 'TELEMETRY|DO_NOT_TRACK|NONESSENTIAL|GROWTHBOOK|BASE_URL' \
  ~/.claude/settings.json ~/.claude/settings.local.json
```

`ANTHROPIC_BASE_URL` must be `https://api.anthropic.com`; the other four must be
unset. **Check the settings files as well as the shell.** A key in
`settings.json` produces an identical symptom to an exported variable and is
much easier to overlook.

## Handling the URLs

`env_…` and `session_…` values are account-scoped identifiers. Keep them out of
logs you share, issues, and screenshots.

---

Verified 2026-08-15 on an NVIDIA GB10 (aarch64, Ubuntu 24.04, GNOME/X11) with
`claude 2.1.231`. Started from a git repo, registered on first try, no
environment remediation required.
