# Recovery and rollback

No rollback has been executed. No existing environment/folder was relocated or
deleted. Do not reset a dirty repository, remove a virtual environment, replace a
changed file blindly, or roll macOS privacy databases backward.

## Verified recovery material

`ROLLBACK_MANIFEST.json` lists ten original helper/reporter backups, original
SHA-256 values and the expected currently installed SHA-256. A fresh check
verified both backup and current-file hashes for every entry. Restore only an
entry whose current hash still matches; otherwise inspect newer work first.
Use existing authenticated SSH and copy the selected backup to a temporary file
beside its destination, preserve mode, then atomically replace and re-hash it.

For helper changes, restart only the existing `ai.atmosphere.workman` LaunchAgent
in the relevant logged-in user's GUI session after that device is released.
Recheck live grants and capture/input. Never modify TCC. The prior runtime stays
at the same absolute interpreter path. Added Pillow is retained; do not remove
it if another working installation now uses it.

The mini taskboard's complete original source, binary and unchanged LaunchAgent
plist are at:
`/Users/tariqul/.claude/board/backups/workman-v1.1-20260907T233101Z/`.
Its `manifest.json` records hashes. Original source SHA-256 begins `20178d6807`,
original binary `180132f116`. Restore only after coordinating mini screen use,
then restart `com.tariqul.taskboard`. Do not recreate the duplicate manual
instance. Do not modify or delete `.claude/board/sessions/*.json`; all three task
files had identical hashes across the consolidation.

`docs/fleet-v1.1/backups/legacy-source-before-v1.1.tar.gz` preserves the scoped
legacy source baseline; exact per-file remote backups are preferable. The six
original v1.0 checkpoint files remain at the supplied release path and match
all six initial hashes. The adjacent old WORKMAN_SETUP.md is not current advice.

## Disable the new entry point first

Stop new fleet input with the local Workman Input Controls launcher or
`fleet_control(node,"input",{mouse:false,keyboard:false})`. Do not automatically
turn OFF switches back on. Release the current task's lease. Coordinate legacy
direct clients manually because they do not participate in fleet leases.

Disable Workman Fleet in Codex's plugin controls before removing an installed
cache. Existing direct workman/workman-mini/workman-air MCP entries and the old
skill symlink were retained. The maintained package in
`/home/monzurul/workman/codex/workman-fleet` and personal marketplace link can
reinstall the plugin using the supported Codex installer; no venv moves are
needed. This installation does not add a daemon or public listening port.

To undo the four scoped desktop-source edits, reverse only the reviewed v1.1
patches in `workman/x11.py`, `workman/gtkops.py` and their two test files after
checking for newer changes. Avoid whole-tree reset. Keep unrelated dirty helper
hooks, staged repository changes, browser companion and caches intact.

New fleet support files/launchers can remain inactive during recovery. Do not
remove journals or graph history as cleanup. Corrections use `correct` to
retract verification without deleting evidence. The SQLite graph is a projection
of the per-device journals; preserve both before any rebuild. No scheduler was
added, and no existing knowledge-sync schedule was changed.
