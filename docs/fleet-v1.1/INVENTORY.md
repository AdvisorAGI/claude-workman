# Content inventory and safe organization

Inspected manifests, entry points, source hashes, Git state, runtime files and
skills rather than choosing folders by their names. Detailed initial evidence:
`evidence/local-inventory.json` and `evidence/remote-content-inventory.json`.

| Location | Content and role | Treatment |
|---|---|---|
| `/home/monzurul/workman` | Desktop claude-workman 0.3.0; Python source, X11 backend, tests; existing `.venv` | Fleet development source, branch `codex/workman-fleet-v1.1`; runtime stays in place |
| `/home/monzurul/workman-chrome` | Separate Manifest V3 browser companion 0.1.0; JavaScript extension/tests | Preserved, optional; not required/enabled by fleet desktop control |
| `/home/monzurul/src/claude-atmos-workman` | Distinct app-helper family 0.2.0; dirty hooks and untracked activity/update helpers | Preserved; not confused with desktop source |
| `~/.claude/plugins/cache/.../workman/*` | Installed plugin/helper copies, including the active Air helper | Working installs retained; scoped helper patches have per-file backups |
| `~/.codex/skills/workman` | Existing Workman skill symlink | Retained; new plugin carries its own scoped fleet-control skill |
| `~/plugins/workman-fleet` | Personal plugin development link to `workman/codex/workman-fleet` | One package source; separate generated Codex install cache |
| `~/.codex/plugins/cache/personal/workman-fleet/*` | Generated Codex plugin installations | Updated via Codex plugin installer; source remains outside cache |
| `~/.grok/workman-learn` | Existing Workman journal and learner location | Append only allowlisted task events; added switches/status/lease files |
| `~/autonomy/learnings` on DGX | Existing fleet learning hub | Reused by-device/knowledge layout, added small SQLite graph projection |
| `docs/fleet-v1.1` | Current plan, tests, inventory, rollback and private evidence | Supersedes stale setup notes for this fleet upgrade |
| `workman-releases/v1.0` | Original six-file fleet checkpoint | Preserved; hashes checked against initial inventory |

On mini, `~/claude-atmos-workman` resolves to the working helper source at
`~/src/claude-atmos-workman`; `~/workman` is a separate desktop implementation.
On Air, the active helper is in the inspected Claude cache, while
`~/claude-atmos-workman` is desktop source; neither name alone identifies the
permission-holding runtime. Machome uses its existing source-helper venv.
Exact host/interpreter/source paths are in the package's `fleet.json`.

Organization is logical and documented. No existing source directory, virtual
environment or backup directory was moved or deleted. Existing dirty source,
upstream caches, direct MCP entries and unrelated staged changes were retained.
Only the identified taskboard processes were consolidated; task JSON files were
not rewritten. Fleet setup 1.1 is independent of upstream package versioning.
