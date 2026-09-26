# Raw Caveman: Workman test only

Owner correction: raw Caveman is for testing Workman, not doctrine. Default is
off. It does not alter Moon/Mars/Cosmos, normal coding, other sessions or model
settings. One style provider may be selected per explicit Workman test ID.

GitHub checked 2026-09-08 UTC: JuliusBrussee/caveman main at
`15581d14007fd01fb3f132016741962f34936ca2` (commit 2026-09-07 08:19:43Z),
upstream package version 2.6.0. The exact 7,022-byte response skill is preserved
under `integrations/caveman/upstream/SKILL.md`; SHA-256:
`c4d7354b4b063d54601fcdd5097a5b1713d1a1a2e386ac39efa438aa1ffef8ce`.
The upstream license explicitly identifies skills as MIT. Provenance, license
and package metadata are stored beside it. No installer, hook, proxy, engine,
telemetry, transcript collector or model-API client was installed.

Fresh `git ls-remote` at 02:03 UTC on September 8 returned the same HEAD. The
reviewed pin and local skill digest still match exactly, so the packaged test
provider needed no source update.

Sources:
- https://github.com/JuliusBrussee/caveman/commit/15581d14007fd01fb3f132016741962f34936ca2
- https://github.com/JuliusBrussee/caveman/blob/15581d14007fd01fb3f132016741962f34936ca2/skills/caveman/SKILL.md
- https://github.com/JuliusBrussee/caveman/blob/15581d14007fd01fb3f132016741962f34936ca2/LICENSING.md

## Workman interface

`fleet_test_mode` actions: list, register, select, get, payload, off. Select
`provider:caveman-raw`, `level:full` for a named test. Other upstream intensities
are available explicitly. `payload` returns the unmodified pinned skill and
its provenance, for that isolated test prompt. The tool does not apply it to
the current conversation. `off` clears the test selection.

The declarative registry also accepts reviewed skill/plugin Markdown inside
the package or a resource reference for an already configured MCP server.
It checks scope, defaults, size, path containment and SHA-256. Registering an
MCP reference does not connect it or grant permission; the host must verify the
returned resource against the pin. No arbitrary executable or remote installer
is accepted. See `test-providers/README.md` for the schema.

The installed MCP resource `workman://tests/{test_id}/raw-style` serves the
same exact source only while that explicit test has a local provider selected.
At 00:19:43 UTC on September 8, the installed seven-tool plugin returned all
7,022 bytes with the expected SHA-256, left an independent test OFF, and refused
the resource read after OFF. Evidence: `evidence/caveman-installed-check.json`.
The test selection was cleared afterward. This checks the connector interface,
not model behavior or token savings.

## Comparison contract and current evidence

Compare baseline Workman and raw Caveman on matched harmless Air fixtures, with
the same Astra/xhigh/model-Fast-OFF configuration, tools, task data and shared
mandatory permission, STOP, focus, exact-text and evidence controls. Randomize
or counterbalance order. Keep Workman Fast input preset a separate arm, so
clipboard/batching gains are not attributed to prose style. Include failure,
retry, interruption and unattempted denominators, not only successful rows.

Report task success, exact text/digest, click error, elapsed time, STOP response,
CPU/memory/idle, and actual total input/output/reasoning tokens and cost only
when a provider supplies them. A human baseline needs a person doing the same
trials. Do not infer token savings from shorter text, word counts, published
upstream percentages or the separate mini Fast Mode timing measurements.

Verified here: byte-exact source pin, test-ID isolation, default off, off/reset,
rejection of unsafe/modified manifests, and non-executing MCP references.
A matched live Astra raw-Caveman comparison has not been run. Existing constraints
prohibit metered model API calls and creating/delegating another task from this
session. No costs or token savings are claimed. Air control testing is separately
coordinated with the owner's credential workflow. The doctrine experiment may
reuse this interface/evidence but does not adopt Caveman into doctrine.
