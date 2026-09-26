# Owner pause listener

Linux (DGX): `systemctl --user enable --now workman-esc-pause.service`
(unit in this directory; installed copy at ~/.config/systemd/user/).
Status: `python -m workman.esc_pause --status`. Resume: `--resume`.
Release everything agents hold: `--release`.

macOS: copy `ai.atmosphere.workman-esc-pause.plist` to
`~/Library/LaunchAgents/`, grant Accessibility to the interpreter it runs,
then `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.atmosphere.workman-esc-pause.plist`.
