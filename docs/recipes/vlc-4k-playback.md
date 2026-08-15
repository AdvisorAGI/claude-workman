# Recipe: driving a video player on a 4K TV, headlessly

*Operator recipe, not a `claude-workman` tool.* It belongs beside the desktop
tools because it is the same loop applied to a media player: grab the screen,
read what is actually there, act, then **verify from the screen again** — and it
is a good worked example of why that last step is not optional.

Verified on Ubuntu 24.04 / GNOME / X11 `:1`, VLC 3.0.20, a 42" 4K panel on
HDMI-0 at 3840x2160 @ 59.94.

## Measure before you tune

An agent asked to "make playback smooth" will reach for codecs and hardware
acceleration. Check whether the machine is even working first:

```bash
top -bn1 -p $(pgrep -x vlc|head -1) | tail -1
nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader
```

A 1080p file here runs ~5% CPU across 20 cores and ~8% GPU. When those numbers
are low, **the defect is the output path or the display, not the decoder.**

## `xrandr` is not a witness that a display mode works

The most useful thing in this document.

Film is 23.976 fps; the panel runs 59.94 Hz. 59.94 / 23.976 = exactly 2.5, so
each frame is held for 3 refreshes then 2 — 3:2 pulldown, and every slow pan
judders. The obvious fix is to run the panel at 23.98 Hz for a 1:1 cadence.
`xrandr` lists that mode and **accepts the switch, reporting success.**

The panel then dropped the HDMI signal entirely and fell back to its own TV
input. Picture gone, mid-film.

```bash
# recover with the known-good mode
DISPLAY=:1 xrandr --output HDMI-0 --mode 3840x2160 --rate 59.94
```

`xrandr` reports what X *asked for*, not what the sink did with it. **Any
unattended refresh-rate change is a way to lose the display with a success
message in your log.** Do it by hand, watching the screen, or not at all.

## Player settings belong in a launcher, not the config file

VLC rewrites `~/.config/vlc/vlcrc` on exit, so settings written there are
silently clobbered by the next plain launch. Command-line options always win.
The same reasoning applies to any app that persists its own config on quit.

One option is load-bearing enough to name: **`--no-spdif`**. This TV refuses
HDMI digital pass-through, and VLC does not fall back — audio dies completely
rather than degrading. A failed pass-through probe still appears in the log at
startup; that is noise, provided the stream is actually live.

## Verify from the screen, not from exit codes

VLC 3 cycles tracks forward only (`v` subtitles, `b` audio) with no reverse key,
so "go back one" means cycling through all of them — this file had 12 subtitle
tracks. Read the on-screen display instead of counting keypresses. It renders at
the **top-right of the video area** and fades after ~2s:

```bash
DISPLAY=:1 xdotool windowactivate "$WIN"; sleep 0.4; xdotool key v; sleep 0.4
ffmpeg -y -loglevel error -f x11grab -video_size 3840x2160 -i :1 -frames:v 1 \
  -vf "crop=3840:200:0:20,scale=1600:83" /tmp/osd.png
```

Know what you are cycling toward before you start:

```bash
ffprobe -v error -select_streams s -show_entries stream=index:stream_tags=language \
  -of csv=p=0 FILE
```

### One screenshot cannot prove a subtitle renders

Quiet stretches have no dialogue. Sample several frames and score the bottom
band for bright pixels, keeping the best candidate:

```bash
ffmpeg ... -vf "crop=3840:600:0:1560,scale=1250:195" p.png
python3 -c "from PIL import Image; im=Image.open('p.png').convert('L'); \
print(sum(im.histogram()[190:]))"
```

## Resuming at the exact frame across a restart

Changing output settings needs a restart, and VLC writes its position **only on
clean exit**. Quit properly, read the position, relaunch into it:

```bash
DISPLAY=:1 xdotool windowactivate "$WIN"; xdotool key ctrl+q; sleep 3
t=$(grep '^times=' ~/.config/vlc/vlc-qt-interface.conf | sed 's/times=//' | cut -d, -f1)
vlc --start-time=$((t/1000)) FILE
```

`times=-1` means nothing was saved — it is still running, or you read too early.

## Small things that waste time

- **What is playing:** `ls -l /proc/$(pgrep -x vlc|head -1)/fd | grep -Ei 'mkv|mp4'`
- **Audio:** `pactl` may not exist; on PipeWire use `wpctl status` / `pw-dump`.
  Sound may be going to a Bluetooth speaker, not the TV — check before
  diagnosing silence.
- **Black bars:** `c` cycles crop; 16:9 fills a 16:9 panel. Say the cost out
  loud — a 2.39:1 film is being *cut* top and bottom, not revealed.
- **Upscaling:** `--swscale-mode=2` (bicubic) is the real quality win for
  1080p→2160p. It cannot add detail the source lacks; do not claim otherwise.
