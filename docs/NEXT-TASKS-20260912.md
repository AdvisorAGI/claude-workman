# Workman next tasks (after the human-mode ship, 2026-09-12)

State at hand-off:
- DGX side verified: Grok 4.6 xhigh verify+fix said "ship". 591 tests pass, 95 tools, and
  `workman-esc-pause.service` is active.
- Full detail is in `docs/HANDOFF-human-mode-20260912.md`.
- Owner rules for this work:
  - Grok 4.6 xhigh builds, then verifies AND fixes what it finds.
  - One Fable 5.1 (high/xhigh) advisor per project, never used as a builder.
  - Opus 5 for design-sensitive code.
  - Never more than 10 agents.

## 1. Push (if it did not go through)
DONE 2026-09-12. GitHub is still unreachable from the home LAN, so pushes go through an ssh SOCKS
tunnel to `hetzner` (`ssh -f -N -D 127.0.0.1:1089 hetzner`, then
`git -c http.proxy=socks5h://127.0.0.1:1089 push ...`). Branch `codex/workman-fleet-v1.1` is on
both `advisoragi` and `atmos` (atmosphere-ai/claude-atmos-workman, as its own branch by owner
order). Never MERGE `atmos` into this checkout.

## 2. L2 live check on the DGX `:0`
Run the typing and hand_back check (handoff L2) when Remmina is NOT the focused window. The new
viewer guard refuses agent input while Remmina has focus, which is correct.
Acceptance:
- `hand_back()["checks"]` are all true.
- The zenity window is gone.
- `agent_held` is empty.

DONE 2026-09-12. The first run FAILED: the text went into the GNOME Activities search while EWMH
still named zenity. c0e0a3d fixed it (shell_guard, real-window focus wait, persisted launches).
Re-run in one process: the text landed in zenity, and hand_back returned `clean: true` with all
five checks true. It closed both zenity windows and did not park the pointer
(`remote_viewer_focused`). What opened the overview is still unknown: it was not the hot corner
(off) and not found in the code.

## 3. Mac daemon parity (project, on the mac-mini)
BUILT, awaiting verify and the owner-window restart. The build clone is `~/wt-mac-daemon-parity`,
branch `feat/mac-daemon-parity`: wave A (Opus) is f3b1c06/5865a69/299e0e0, wave B (Grok) is
271591f, and the plan is `computer-mcp/ADVISOR-PLAN-20260912.md`. The mini suite (guarded
scratch copy) shows 288 passed, 21 skipped. To load it: `launchctl kickstart -k
gui/$(id -u)/ai.atmosphere.workman` in an owner-agreed window, then a live Escape check.

The mini is driven through the Workman.app daemon (`~/src/claude-atmos-workman/computer-mcp`,
`atmos_computer`), which holds the TCC grants. The owner chose no permission toggles. That repo
has ANOTHER session's uncommitted edits in daemon.py, keys.py, motion.py and ops.py: coordinate
before editing, or build in a worktree.
- Escape pause: a listen-only Quartz event tap hosted INSIDE the daemon, so it needs no new grant.
  An agent's own events carry a nonzero source PID and are ignored. It shares
  `~/.grok/workman-learn/fleet-input-switch.json` semantics.
- `hand_back` verb: release tracked keys and buttons, close only windows the daemon launched,
  do not park the pointer over Screen Sharing or a VNC viewer.
- `click_element`: click a Gaussian-jittered point inside the frame, not the exact centre
  (ops.py:778).
- Click hold 60-140 ms (it is 40-90 ms now, ops.py:491).
- Remote-viewer guard: refuse input when Screen Sharing or a VNC or RDP client is frontmost or
  under the target.
- The wm.py `pause`, `resume` and `handoff` verbs should work against the daemon backend.
- Tests with a fake Quartz. A live check needs the owner at the mini.

## 4. Fleet sync
- The Air runs `workman.server` (darwin backend) with the old human.py. Sync the new workman
  package there, check permissions with `wm.py platform`, and run the suite.
- Copy the new `wm.py` and `fleet-wm.sh` to the mini and the Air (R2). The relay streams the DGX
  copy, so this is only for local use there.

## 5. Small leftovers
- DONE: the L1 harness is `scripts/l1_harness.py` (Xvfb :99 only).
- Remote viewer guard: pointer MOVES that cross an unfocused viewer are not checked (accepted, lite).
- A window opened after `launch_app` returns is never closed by hand_back (fails safe).

## 6. Owner items (not agent work)
- Run `/mcp` to reconnect workman in open sessions.
- The debug-port Chrome (pid 1061727, profile google-chrome-teardown, :9222) stays, as the owner
  said. Workman never uses it.
- When done with the Mac mini VNC view: close Remmina, then `pkill -f mini-vnc-forward.py`.
- Open offers that are still unanswered:
  - Update Claude CLI 2.1.263 to 2.1.269 and build a test Function Hooks mod.
  - AdvisorPPC autodeploy is stalled while GitHub is unreachable.
