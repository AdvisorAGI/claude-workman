# Workman status interface, version 1

The existing taskboard reads `~/.claude/board/sessions/*.json` without writing it.
Workman's journal append path is the sole writer of its own
`~/.grok/workman-learn/fleet-status.json`. No second reporter or scheduler owns it.

Fields: `schema_version:1`, `device`, stable `task`, UTC `updated`,
`current_action`, allowlisted `outcome`, `latest_verified`,
`verification_event`, `next_action`, `permissions`,
`permissions_observed_at`, `source_event`, and task-only `scope`.
The file is replaced atomically with mode 0600 after local event recording.

Display consumers must reject unsupported versions and missing identity fields.
A claimed verification needs its matching local journal event, successful action,
later same-device capture hash and no correction. Text alone is not proof.
Unknown/missing evidence is displayed as needing rechecking. Permissions older
than five minutes are stale and never authorize control. Graph queries also
reject conflicting or future-dated permission values. Consumers must not add
unverified success labels, poll personal activity, or modify source task records.

The mini taskboard refreshes its display every two seconds using the existing
UI timer. User scroll-away or Pause stops following; Resume live re-enables it.
Completed/repeated rows are collapsed only in the display. A-/A+ controls adjust
text; WM in the menu bar hides/reopens the same panel. No endless animation.
