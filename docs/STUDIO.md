# Workman Studio library reference

`workman.studio` packages the ad-video method proven on 2026-09-25 in
`~/advisorppc-ads-20260925` (build_v3, build_partner, build_series, `~/bin/video-inspect`,
the grok and Codex lanes) as small pure functions, a CLI per module and six MCP tools. The
crew that calls it is described in [STUDIO-CREW.md](STUDIO-CREW.md); the brief is
[STUDIO-DESIGN.md](STUDIO-DESIGN.md).

## Rules the library enforces
| rule | where |
|---|---|
| ffmpeg nice 19, 4 threads, no stdin | `job.ffmpeg_cmd`, every call goes through `job.ff` |
| heat: start under 86 C, else wait until under 80 C, fail closed after 45 min | `job.cool_down` before every ffmpeg pass, Atmosmotion render and encode |
| one render at a time | `job.render_lock` (flock) + `job.wait_turn` (no ffmpeg writing into the job's out/) |
| kill by PID only, and only a PID the job started | `job.track_pid` ledger in `<job>/.studio/pids.json`, `job.kill_job_pid` checks /proc argv |
| a render is never overwritten | `assemble.render` refuses an existing `out/<name>.mp4`; bump `v<n>` |
| ElevenLabs only, key never printed | `voice.eleven_key` reads env or `~/ENV/elevenlabs.local.env`; errors carry endpoint + status only; 3 tries, 5 s / 10 s backoff |
| no em dashes, no model names | `voice.copy_problem` (the adbuild `BANNED` pattern) in `validate`, `tts_body`, `qc` |
| every claim traced | `qc.fact_rows`: cited IDs exist in FACTS.md, Conflicts IDs flagged, VO lines carry `facts` or `opinion`, no number without an ID |
| one type system | `typesystem.TYPE` mirrors TYPE-SYSTEM.md; a spec that sets a font, size or colour is invalid |
| men only, male voices | `grok.check_prompt`, `grok.narration` refuses female voices; `qc` keeps the women check PENDING until a reviewer passes it |
| grok files only through grok's Download button | `grok.download_steps` + `grok.save_dialog`; no cookie or CDP download code |
| Codex stills on the plan login | `still.child_env` strips `OPENAI_API_KEY`/`ORG`/`PROJECT` |

## Job folder (`job.py`)
`~/studio-jobs/<YYYYMMDD>-<slug>/` (root: `WORKMAN_STUDIO_ROOT`) holds `JOB.md STATE.md
BRIEF.md SCRIPT.md CLIPS.md SOUND.md QC.md` and `clips/ stills/ audio/ ident/ out/ review/`.
Templates carry a `<!-- studio:template -->` marker so `status` can tell written from untouched.
A job is named by slug (newest dated folder), by folder name, or by absolute path.

```
new_job(slug, root=None, title="", day=None) -> {ok, job, created, kept, dirs}
resolve_job(job, root=None) -> Path
status(job) -> {docs, clips, renders, qc, render, processes, heat_c, heat_gate}
board_temp() -> float;  cool_down(max_wait=2700) -> seconds waited
wait_turn(job_dir, max_wait=5400);  render_lock(path=None)
track_pid(job_dir, pid, what, expect, log="");  kill_job_pid(job_dir, pid)
ff(args, timeout=1800) -> stderr;  probe(path) -> dict;  duration(path) -> float
```
CLI: `python -m workman.studio.job new <slug> | status <job> | heat | kill <job> <pid>`.

## Inspect (`inspect.py`)
`inspect(path, outdir=None, transcribe=True, stt=None)` writes `probe.json frames.jpg cuts.txt
loudness.txt audio.png transcript.txt` into `<file>.inspect/` exactly as `~/bin/video-inspect`
(4 fps sheet 8 wide, scene > 0.3, momentary loudness every 0.5 s, waveform over spectrogram,
Scribe word timings). Files over 20 minutes are refused. `stt` injects a transcript for tests.
CLI: `python -m workman.studio.inspect <mp4> [outdir] [--no-transcript]`.

## Voice (`voice.py`)
Pure: `spoken masked word_onset phrase_at phrase_onset speech_span placement overruns pauses
wps articulation_wps vo_texts take_fit choose audible_end attack_time loudness_errors
parse_ebur128 master_chain solve_gain copy_problem multipart tts_body`.
IO: `eleven(path, body)`, `tts(text, mp3, json, voice=BRIAN, previous_text, next_text)`,
`sound(path, prompt, seconds)` (bed and SFX, an existing file is kept), `stt(audio)`,
`ebur128`, `max_volume`, `pcm`, `master_gain(premix, end)`.
Settings are the worked jobs': Brian `nPczCjzI2devNBz1zQrb`, `eleven_multilingual_v2`,
stability 0.55, similarity 0.75, style 0.15; takes in preference order `vo_texts` (0.6 s
breath at each full stop, 0.3 s, none); a take is placed so its first letter lands on `at`
and trimmed at its last window above -60 dBFS plus 0.1 s. On-screen words appear at
`text_onset` = max(alignment onset, `audible_onset`: the first 10 ms window at -35 dBFS RMS or
louder within 0.5 s after it, at its first sample that loud) + the spec's `onset_lead` (default 0).
Smoke test 2026-09-25: alignment put the words 0.10 and 0.12 s before Scribe heard them; with the
audible onset they land 0.008 and 0.055 s before Scribe's word start, inside the clock's 0.075 s
allowance for Scribe's own word-start jitter (`inspect.SCRIBE_EARLY`).
CLI: `python -m workman.studio.voice tts "<text>" out.mp3 | sound "<prompt>" out.mp3 --seconds 20 | loudness <file> | onset <take.json> "<phrase>"`.

## Type system (`typesystem.py`)
`TYPE` (read-only) mirrors `~/advisorppc-ads-20260925/TYPE-SYSTEM.md`: Fraunces 118 px words
(132 px for the brand word, two lines 132 px apart past 1000 px), Geist 54/40/44, white at
92 percent, the dark card `#0F1112` with `#F5F4EF` copy and `#A8A6A0` URL, one emerald
`#10B981`, baseline 44 px above the horizon, CTA 160 px under the mark, words 8 frame
easeOutCubic in with a 6 px rise and 8 frames out, card copy 10 frames with 6 px. Px values
are on a 1080 short side, so 9:16, 1:1 and 16:9 all draw 118 px words. `parse_md`/`drift`
read the same values from the markdown; `tests/studio/test_studio_typesystem.py` fails when
the file changes and the mirror does not. Both fonts are variable and used through their axes:
Fraunces wght 600, opsz = pixel size clamped 9..144, SOFT 50, WONK 0; Geist wght 500 (CTA) and
400 (URL, labels). `headline_font(px)` and `sans_font(role)` return the owner's pinned instances
from `~/bin/font-instance` (`tools/fonts/instances/`, axes in the name, e.g.
`Fraunces-wght600-opsz118-SOFT50-WONK0.ttf`), making a missing one on first use; never the
default instance and never a cut of our own. Atmosmotion gets the same instances until its text
layer takes `fontVariation` (TODO in `plate_comp`).
CLI: `python -m workman.studio.typesystem [TYPE-SYSTEM.md]` prints the drift.

## grok (`grok.py`)
Prompt builders: `locked_shot(subject, change, seconds=6)` (locks first, one change, lock
again; refuses camera and mood words), `narration(line, at, seconds, voice="Helios")`
(quoted line, start time, voice named, native American delivery, at most 2.5 words per
second, male voices only), `sound(bed=False, sync=())`, `man(age, build)`, `pick_seconds`,
`check_prompt`. Download flow: `download_steps(post_id, name)` for the pane agent,
`save_dialog(name)` (finds the `blob:https://grok.com` dialog with wmctrl, ctrl+a, types the
name, Return, then waits for `~/Downloads/<name>` to appear and stop growing; fails closed
otherwise), `intake(src, job, name)` (move without overwrite into clips/, probe, 10-frame
strip in clips/sheets/), `clip_errors` (1088x1920, 24 fps), `last_frame` (chain shots),
`clip_name(what, post_id, seconds)`.
CLI: `python -m workman.studio.grok locked | narrate | save | intake`.

## Still (`still.py`)
`make(dir, name, subject, aspect="9:16", refs=[])` pipes the ticket on stdin to
`codex exec --skip-git-repo-check -C <dir> -i <ref>...`, with OpenAI key variables removed
from the environment, then checks the saved file's magic bytes. No retry (each try spends
plan quota); 15 min timeout; never overwrites. `variants(dir, src, aspects)` recomposes one
still per aspect with the source as reference.
CLI: `python -m workman.studio.still make <dir> <name.png> "<subject>" [-i ref.png] | variants <dir> <src.png>`.

## Assemble (`assemble.py`)
`<job>/spec.json` (see the module docstring for the full shape): `name`, `fps` (24/25/30),
`size`, `end`, `renderer` (`drawtext` | `atmosmotion`), `onset_lead` (0..0.3 s, default 0),
optional `formats` (`1:1`, `16:9`, `9:16`), `shots` (clip with `from` and `horizon`, `logo`, `card` with `mark`, `cta`, `url`,
`facts`) each ending on an integer `end_frame`, `vo` lines (`shot`, `at`, `text`, `words`,
`facts` or `opinion`), `bed`, `chime`. The card's mark must be the family of the logo shot
before it (`typesystem.LOGO_FAMILIES`: the green mark after a status-hold-green clip, the plain
Ai after a dissolve clip, the plain Ai when there is no logo shot); anything else is invalid.

Stages (`python -m workman.studio.assemble <job> plan|assets|render|verify|all [--format F]`):
- `assets`: VO takes (stops at the first that fits and reads 1.5 s), bed, chime. The only
  network stage.
- `render`: reads saved takes only; clips cropped (`cover`: grok 1088x1920 becomes
  `crop=1080:1920:4:0` exactly as the builders; other aspects scale to cover and the
  horizon is remapped); logo scaled to fit on black and its green frame found by colour;
  chime attack on that frame; words on their spoken onsets; dark card; audio premixed
  (bed -26 LUFS with a swell on the green frame or mid-spot, chime -12 dBFS peak, VO peaks
  -6 dBFS) and mastered with one solved static gain plus a 4x oversampled limiter at
  -2.5 dB to -14 LUFS. Writes `out/<name>.mp4 .cmd .timing.json`, `audio/<name>_premix.wav`.
- `verify`: probe (h264, size, fps, aac, length), 7-frame strip, onset band test (text band
  clean 0.1 s before the onset, drawn 0.4 s after, against the source frame of the shot
  that frame shows), the Scribe transcript as an independent clock when
  `out/<name>.inspect/transcript.txt` exists (every phrase at most 0.1 s after its word is
  heard, never more than 0.075 s before Scribe's word start), every planned cut seen on the render's own frames (`planned_cuts`: the luma change
  across the cut is at least 1.8x the shot's own frame-to-frame change (the larger median of the
  frames before and after) and at least 1.0;
  ffmpeg scene scores of 0.14 to 0.28 missed every cut of the dark brand footage), green frame, loudness (-14 +-0.5, TP <= -1.0), VO inside shots, spoken
  text equals the script; deviations (read time under 1.5 s, card under 3 s) are listed but
  do not fail. Writes `review/<name>_verify.json`, `review/<name>_strip.jpg`,
  `review/<base>_contact.jpg`.

Renderers: `drawtext` draws the words and the card copy in ffmpeg with the type system's
alpha and rise expressions. `atmosmotion` builds `plate_comp` (white-on-black word plate) and
`card_comp` (the whole dark card) and renders them with engine-native through
`atmos_render.mjs` (`Composition.parse`, `nativeBackend.supports` must be `yes`,
`composeAnimatedFrame` piped to our ffmpeg); the plate is alphamerged over the clips. The
engine path is `WORKMAN_ATMOS_ENGINE` (default the canonical 09-23 worktree). TODO in code:
switch to the engine's own alpha (ProRes 4444) once it lands.

## QC (`qc.py`)
`qc(job, name="", spec="spec.json", eye=None)` writes `QC.md` (first line `RESULT: PASS |
FAIL | PENDING REVIEW`, then `| # | check | PASS/FAIL | evidence | owner of the fix |`, ending
"Owner still hears the final cut.") and `review/<name>_qc.json`. Rows: copy rules, fact IDs,
conflicts (WARN), untraced numbers, probe, loudness, VO in shots, VO says the script, onset
band, green frame, readability (WARN), planned cuts from verify's frame check (FAIL when one is
not on the frames) and no unplanned scene cut inside a shot (`cuts.txt`, FAIL), spoken words equal the script and phrases land at most 0.1 s after (and no more than 0.075 s before) their spoken
word (from `transcript.txt`), and four eye checks that stay PENDING until `eye` passes them.
`copy_check(job)` runs the copy and claim rows without a render and writes nothing.
CLI: `python -m workman.studio.qc <job> [--name N] [--copy-only] [--eye JSON]`.

## MCP tools (`workman/server.py`)
| tool | signature | what it does |
|---|---|---|
| `studio_new_job` | `(slug, title="")` | `job.new_job` |
| `studio_status` | `(job)` | `job.status` |
| `studio_inspect` | `(path, outdir="", transcribe=True)` | `inspect.inspect`, transcript capped at 4000 chars inline |
| `studio_voice` | `(job, action="assets", spec="spec.json", text="", name="", phrase="", prompt="", seconds=0.0)` | `assets` for the spec, or one `tts`, `sound`, `onset`, `loudness` under `audio/` |
| `studio_assemble` | `(job, spec="spec.json", stage="all", format="")` | `plan` inline; `render`/`verify`/`all` detached with the PID in the ledger |
| `studio_qc` | `(job, name="", spec="spec.json", eye=None, copy_only=False, transcribe=True)` | inspects the render if needed, then `qc.qc` (or `copy_check`) |

Every tool returns a dict and never raises (`{"ok": false, "error": ...}`).

## Where the library differs from the source scripts
- Master: build_series ended on two-pass `loudnorm`; the library uses build_partner's solved
  static gain plus limiter, because two-pass loudnorm landed a 3 s programme at -13.4 LUFS
  (dynamic fallback inside its 3 s window) and the partner method converges on any length.
- Words: the builders put the glyph box bottom 44 px above the horizon; TYPE-SYSTEM.md puts
  the baseline there, 92 percent white, easeOutCubic. The library follows TYPE-SYSTEM.md.
- End card: the builders used a white card (v3, series) and a black wordmark card (partner);
  TYPE-SYSTEM.md allows only the dark card with the Ai mark, so the white card is gone.
- Loudness gate: verify and qc keep build_series' true peak gate of -1.0 dBTP; studio-qc.md
  asks for -1.5 dBTP. The master limits at -2.5 dB, so renders measure well under both.
- The onset band test compares a pre-onset frame with the clip that frame actually shows
  (build_v3's `source_frame`); build_series always used the word's own clip.
- Not reproduced: build_partner's "Claude Partner Network member" label line and its bed cap
  (added 2026-09-25 while this library was written), the Atmosmotion ident kit
  (`ident/build-assets.py`, `render-ident.mjs`), and the grok CDP lane (it downloads with
  browser cookies, which the studio does not read).
