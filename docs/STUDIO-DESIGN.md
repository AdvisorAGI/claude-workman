# Workman Studio (design brief, owner order 2026-09-25)

Name: **Workman Studio**, python module `workman.studio`, MCP tools prefixed `studio_`.
One built-in feature that packages the whole ad-video method proven on 2026-09-25 in
~/advisorppc-ads-20260925 (grok clips, breaking a video open, synced voice, one sound bed,
Codex stills, assembly, QC, publish gate) so any session runs it as a set of role agents
instead of re-inventing it. Owner: "make it in Atmosmotion's way": a library plus named
crew, not one script. Atmosmotion stays the motion engine underneath.

## Source of truth (read these before writing a line)
- ~/advisorppc-ads-20260925/claude-copy/build_v3.py, build_partner.py, build_series.py and their
  test_*.py: ElevenLabs VO with-timestamps onsets, words on the horizon, one bed, chime on a
  detected frame, loudnorm, frame/onset/loudness verification, strips.
- ~/bin/video-inspect: frames.jpg, cuts.txt, loudness.txt, audio.png, transcript.txt.
- ~/.claude/skills/grok-imagine-browser/SKILL.md (controls, limits, "Prompting the video
  model", "grok audio"), ~/grok-web-mcp/ui-map-20260925/UI-MAP-v2.md, ~/grok-web-mcp/server.py
  (CDP lane), and the pane lane used today (built-in browser: navigate, find refs, type,
  Post actions > Download, GTK Save dialog renamed via wm.py/xdotool).
- ~/.claude/projects/-home-monzurul/memory/ref_codex_cli_image_generation.md (Codex still).
- ~/.claude/skills/ad-video-craft/SKILL.md sections 1, 9, 10 (rules, trailer voice, inspect).
- ~/advisorppc-ads-20260925/claude-copy/CREATIVE-v3.md, CREATIVE-partner.md, CREATIVE-series.md
  (the brief format the crew consumes), ../refs/FACTS.md (claim tracing).
- ~/advisorppc-ads-20260925/ATMOSMOTION-COMBO.md and ~/advisorppc-ads-20260925/ident/
  (Atmosmotion engine-native route for motion graphics).

## The crew (role agents, each a `.claude/agents/studio-<role>.md`, Opus 5.5 unless noted)
| agent | owns | deliverable |
|---|---|---|
| studio-video-director | the whole job: reads the brief, dispatches the crew in order, holds the creative bar, stops at owner gates | JOB.md status + final review |
| studio-strategist | stance, audience, one true claim, facts | BRIEF.md with fact IDs |
| studio-copywriter | script, VO lines, on-screen words, trailer cadence (hook, turn, reveal) | SCRIPT.md with timings |
| studio-cinematographer | stills and clips: Codex still, grok prompts (locks first, one change, 6 s/15 s), first/last frame, downloads and names | clips/ + CLIPS.md |
| studio-motion-director | logo idents and type motion in Atmosmotion engine-native, LED signal, easing | ident/ mp4s |
| studio-sound | VO takes (Brian, timestamps), one continuous bed, SFX on detected frames, loudness | audio/ + SOUND.md |
| studio-editor | assembly: cuts, horizons, words on onsets, end card, render (nice 19, 4 threads, heat gate) | out/*.mp4 |
| studio-qc (Sonnet) | breaks the render open (video-inspect), rules: no women, no em dash, no model names, claims trace, loudness, onsets | QC.md pass/fail |
| studio-producer | quota, heat, budget, schedule, owner gates (spend, publish), handoff | STATE.md |
| studio-traffic | formats, naming, upload unlisted on owner go (youtube-channel-publish) | links |

## Library (`workman/studio/`)
- `inspect.py`: video-inspect as a function + `studio_inspect` MCP tool (returns paths + transcript).
- `voice.py`: ElevenLabs VO with-timestamps, onset lookup by word, bed and SFX generation,
  loudness measure. ElevenLabs is the only API; key from ~/ENV/elevenlabs.local.env, never printed.
- `grok.py`: prompt builders (locked-shot, narration/accent block, brand voice), the pane/CDP
  download flow (Post actions > Download, rename in the GTK dialog), clip probe/rename.
- `still.py`: Codex CLI still generation (stdin prompt, -i reference), aspect variants.
- `assemble.py`: spec-driven build (shots, words on onsets, bed, chime-on-frame, card), same
  checks as build_v3/partner/series, contact strips.
- `qc.py`: rule checks (women/person detection via contact sheet review prompt, banned strings,
  fact IDs, loudness, onsets, frame grid) producing QC.md.
- `job.py`: JOB folder layout (BRIEF, SCRIPT, CLIPS, SOUND, QC, STATE), heat gate, ffmpeg
  serialisation (one render at a time), PID-only kills.
- `server.py` registration: `studio_new_job`, `studio_inspect`, `studio_voice`, `studio_assemble`,
  `studio_qc`, `studio_status`. Tests under tests/studio/ with small fixtures (no network in tests).

## Hard rules baked in
No women, photoreal men only; no music melody, one continuous bed; male brand voice (Brian,
Helios), native American delivery; no em dashes, no model names, no stats without a fact ID;
brand look (matte black, off-white, one emerald accent); ffmpeg nice 19 <= 4 threads, heat 86/80
gate, one gen model at a time; kill by PID only; nothing published or spent without the owner;
grok downloads only through grok's own Download button (never cookie reads).
