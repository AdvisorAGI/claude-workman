# TV remote + Air dual-monitor runbook (Xiaomi Mi TV + MacBook Air), learned 2026-10-02

Read this before touching the owner's Xiaomi TV or the MacBook Air's displays. Every line here was
verified live on 2026-10-02 (session "Mac Air displays + Xiaomi TV fault"). Not to be confused with
the `tv` skill, which is the DGX's own 42" wall.

## 1. The hardware

| thing | facts |
|---|---|
| TV | Xiaomi MiTV-MSSP3 (codename machuca), Android TV 9, build PI.4663, MStar m7322 board hw v74s, CSOT 43" 3840x2160 panel (EDID made Mar 2020) |
| TV network | 192.168.0.191, MAC ec:9c:32:5e:69:8e, Wi-Fi "Advisor Media Group_5G" |
| Air | MacBook Air M4, ssh `air`, user muhammadtariqul. **Built-in screen is broken (black).** Runs headless on two externals |
| LG | 22" FULL HD, 1920x1080 75 Hz, **Main Display**, origin (0,0), id DEE51AB7-4DCC-4F91-8124-C4C8E94329D6 |
| TV as monitor | 1920x1080 HiDPI at 30 Hz (the link only does 4K at 30 Hz; owner keeps it), origin (1920,0) = **right of the LG**, id 69F95991-DA23-42CF-832E-48C29695D9A6 |

The owner has **no keyboard or mouse at the Air**. Never ask him to type, click or read anything on the
Air (least of all the dead built-in screen). Do it remotely; ask him only by tappable question.

## 2. Control channels from the DGX (keys in ~/.config/workman-tv/, chmod 700/600, never print)

1. **ADB over TCP 5555**: pure-python `adb-shell` (the SDK `adb` binary is x86 and fails with
   "Exec format error" on the ARM DGX). Key `adbkey` authorized on the TV with "Always allow"
   (fingerprint EE:4D:16:9A:...). `~/.config/workman-tv/adb.py '<shell cmd>'`.
2. **Android TV Remote v2 (port 6466)**: `androidtvremote2`, paired with code shown on the TV, client
   name `claude-dgx`, cert.pem/key.pem. `tv.py KEYCODE_...` (keys, power, launch URLs).
3. Durable venv: `~/.config/workman-tv/venv` (both libs). Helpers: tv.py, adb.py, nav.py (find/select
   with a danger guard), ui.sh (uiautomator dump), shot.sh, wake.py.
4. Workman tools (branch `feat/tv-control`, commit 869fce7, worktree ~/wt-workman-tv, NOT yet installed
   in running servers): tv_status, tv_screenshot, tv_elements, tv_key, tv_focus_text, tv_select,
   tv_launch, tv_input_hdmi, tv_screen, tv_power_off. Install needs `pip install -e ".[tv]"` + a
   workman restart, which is the owner's call.

## 3. Screen on/off (the owner's "turn the TV on/off")

```bash
~/bin/tv-screen off      # backlight off, TV stays on Wi-Fi and controllable
~/bin/tv-screen on
~/bin/tv-screen status
~/bin/tv-screen poweroff # REAL standby: drops Wi-Fi, only the physical remote wakes it
```
- on/off = hidden `Settings.Global power_picture_off` (**1 = ON, 0 = OFF**; inverted-looking, verified
  in the kernel log `MTGPMX_PANEL_EnableBacklightPower fgEnable:0/1` and by the owner's eyes).
  Found by decompiling TvSettingsPlus (`PowerFragment.onPreferenceTreeClick`) with androguard.
- **Wake-on-LAN does not work** on this set, even after `settings put global network_wol 1`
  (log "setWol after wol v:1"); tested from the DGX and the Air. Never real-power-off unless the
  owner accepts that he must use the remote.
- Idle TV Standby "after 4 hours" stays ON by owner choice. A screen-off TV with no remote input can
  still fall into full standby after 4 h; then it needs the remote once.

## 4. Driving the TV UI

- TV UIs are focus-driven, never tapped. Loop: read focus (`uiautomator dump`) -> one key -> read again.
- **Space keys >= 0.3 s.** Fast bursts get dropped (50 backlight presses moved it 37). Load average ~110.
- `screencap` works for Android UI, **returns empty on the HDMI input** (protected layer). Read menus
  with uiautomator instead; on HDMI the dump is empty by nature.
- Long pulls/greps time out: transport timeout 120 s in adb.py, env `T` in nav.py.
- **Danger items, never select**: Reset, Factory reset, Factory Menu, Remove account, OEM unlocking,
  Revoke USB debugging authorizations, Erase, Format, Wipe. Blind D-pad once landed on the account
  picker next to "Remove account": check focus before every select, photo-guide if the dump is empty.
- Picture activity: `com.android.tv.settings/.partnercustomizer.picture.PictureActivity`.
  HDMI app: `com.mediatek.wwtv.tvcenter`.
- Applied: Picture Mode **Monitor** (picture_mode 11), backlight **50**, Motion Smooth off. The picture
  menu label can show a stale 63; settings + log are the truth. The builder later read backlight 68 in
  Monitor mode: re-check `settings` and reset to 50 if the owner asks.

## 5. The TV's hardware fault (repaired 4-5 times, keeps returning)

Symptoms after long runs: picture fades, many coloured lines, buzzing from the board. Last boot reason
`kernel_panic`. Heat-related. Suspects in order: LED backlight strips (classic Xiaomi cause that keeps
killing power boards), power-board capacitors, T-con board / ribbon, panel COF.
Technician brief given: replace the whole LED strip set (not single strips); new genuine power board or
all caps with 105 °C Japanese parts; reseat/replace T-con ribbons, no reflow-only fix; 4-6 h burn-in at
the shop; old parts back + written warranty; voltage stabilizer at home; stop repairing if the panel is
failing or the cost passes ~40 % of a new TV. Running it at backlight 50 and mostly screen-off reduces heat.

## 6. The Air, headless rules

- FileVault Off, auto-login on (/etc/kcpassword), `sysadminctl -screenLock off`, `pmset sleep 0
  displaysleep 0 SleepDisabled 1`. Never enable FileVault or disable auto-login (brings back a password
  screen nobody can see).
- **"It keeps asking for a password / never comes back"** was the **screensaver**: `com.apple.screensaver
  idleTime 3600` raised the lock shield after 1 h idle (loginwindow `LWScreenLock startScreenLock`,
  ioreg `CGSSessionScreenIsLocked` true). With lock delay INT_MAX one click dismisses it. Fix: idleTime 0
  in BOTH `defaults write com.apple.screensaver` and `defaults -currentHost write ...`. Hot corner br=14 is
  Quick Note, harmless.
- A synthetic key over ssh (`fleet-wm key shift`) does NOT dismiss the shield; a workman **click** does.
- Slowness was not RAM (16 GB, swap ~10 MB): disk 94 % full, an iOS Simulator booted for 7 days, orphaned
  duplicate whatsapp-mcp (`uv ... run main.py` with ppid 1), an animated Aerial wallpaper (~15-20 % CPU),
  VLC playing for 7 days holding audio assertions. No LLM/embedding/container runs on the Air (Docker
  installed but idle; ~/.ollama removed with owner OK).
- Display layout tool: `/opt/homebrew/bin/displayplacer list|"id:... origin:(1920,0)"`.

## 7. The TV desktop dashboard (owner design)

- Wallpaper both screens: solid blackish gray #1a1a1a, `~/Pictures/Wallpaper/blackish-gray.png`
  (generated with ffmpeg on the DGX, set via `osascript ... tell every desktop to set picture`).
- **LG: keep exactly as it is** (calendar left; Dhaka weather, stocks, world clock right). Owner order.
- **TV**: Übersicht (brew cask `ubersicht`, login item) widgets in
  `~/Library/Application Support/Übersicht/widgets/`:
  - `tv-clocks.jsx`, top-right: **Dhaka + Silicon Valley (America/Los_Angeles)**, 12-hour with AM/PM,
    always black, with weather inside each card (Open-Meteo, free, no key; cached 10 min in
    /tmp/tv-weather.json, refreshed in the background so the 1 s clock never blocks).
  - `tv-ai-news.jsx`, top-left: AI headlines from TechCrunch AI, The Verge AI, VentureBeat AI RSS,
    every 15 min, click opens the story.
  - Both `return null` when `window.devicePixelRatio < 2`: the TV is HiDPI, the LG is not, so the
    panels draw on the TV only. (Übersicht's per-screen menu needs a submenu hover, which synthetic
    pointer moves do not open.)
- Apple's City Digital clock has no AM/PM and turns white in daytime; that is why it was replaced.

## 8. Workman on the Air: what worked and what did not

- Region shots: TV region is `region 1920 0 1920 1080`, returns 3840x2160 (2 px per point). LG is 1:1.
  Convert image px to points before clicking: TV x = 1920 + px/2, y = px/2.
- Right-click opens desktop/widget menus; Edit Widgets gallery: a **click** on a gallery widget adds it
  to the display the gallery was opened on.
- Drag: wm.py has no drag verb. `ssh air '/opt/homebrew/bin/cliclick -e 300 m:x,y dd:x,y dm:... du:x,y'`
  moves desktop widgets and swipes away stuck notification banners (drag right off the banner).
- Search fields in widget config sheets need a click in the field first; typing into the list jumps the
  list (type-ahead) instead.
- First launch of a new app throws: Gatekeeper "downloaded from the Internet" (click Open), update
  question (click Don't Check), notification permission banner. Handle them remotely right away; the
  owner sees them on the TV and cannot click.
- `killall NotificationCenter` did not clear the banners; the cliclick swipe did.
- After many ssh connections in a short time the Air's OpenSSH **per-source penalty** refuses fresh
  connections (`Connection closed by UNKNOWN port 65535`, fleet-wm `ConnectionResetError`), while the
  existing ControlMaster still works. Workaround: run wm.py serve through the master:
  `ssh -T air "$(python3 -c 'import workman.remote as r; print(r.launcher_script())')"` with wm.py +
  SENTINEL + one JSON request on stdin (helper pattern: a small wmx.py). It clears on its own later.
- Making a still frame from the Aerial wallpaper: the .mov is in
  `~/Library/Application Support/com.apple.wallpaper/aerials/videos/`, not /System/Library;
  `qlmanage` hangs while the screen is locked and `swift` scripts crashed; ffmpeg on the DGX worked.
- zsh on the Air: never start an echo with `==` (zsh treats `=word` as a path lookup). Pipe scripts
  with `ssh air 'bash -s' < script.sh`.

## 9. Mistakes made here (do not repeat)

1. Placed the TV LEFT of the LG without asking; the owner's photo showed it on the right.
2. Navigated TV menus blind and landed next to "Remove account". Read focus first, always.
3. Burst-pressed TV keys with no spacing; the set dropped a quarter of them.
4. Moved two LG widgets while building the TV layout; the owner said "keep LG as it is". Change only the
   screen the owner named; put things back exactly (I did, verified by screenshot).
5. First Übersicht build drew on BOTH screens and covered the LG widgets. Check every screen after any
   desktop change.
6. Told the owner a screenshot said "locked" when it was the screensaver; he corrected me. Trust what the
   owner sees on the glass.
7. Hammered fresh ssh sessions until the Air penalized the DGX. Reuse one channel; batch remote steps.
8. Recorded "TV placed left" in memory before the correction; fix memory the moment a fact changes.
