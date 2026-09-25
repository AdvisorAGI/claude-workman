# Workman Studio crew reference

The crew is ten role agents, one file each in `~/.claude/agents/studio-<role>.md`. A session
runs them in a fixed order with the `workman-studio` skill
(`~/.claude/skills/workman-studio/SKILL.md`). The design brief is
[STUDIO-DESIGN.md](STUDIO-DESIGN.md); the library the crew calls is described in
STUDIO.md.

## Roster
| agent | model | deliverable (file) | reads |
|---|---|---|---|
| studio-video-director | opus | JOB.md: plan, step status, gates, `## Review` | ask, all crew files, ad-video-craft 1/9/10 |
| studio-producer | opus | STATE.md: budget line, quota, heat, gates, handoff, links | JOB, SCRIPT, CLIPS, SOUND, QC |
| studio-strategist | opus | BRIEF.md: stance, audience, one true claim, facts with IDs | ask, refs/FACTS.md, CREATIVE-*.md |
| studio-copywriter | opus | SCRIPT.md: shot table, VO + at times, on-screen words, card | BRIEF, CREATIVE-*.md, horizon.json |
| studio-cinematographer | opus | clips/ + CLIPS.md (stills/ too) | SCRIPT, BRIEF, grok-imagine-browser, UI-MAP-v2, Codex still recipe |
| studio-motion-director | opus | ident/ mp4s + comps + review | SCRIPT, ATMOSMOTION-COMBO.md, ident kit |
| studio-sound | opus | audio/ + SOUND.md | SCRIPT, CLIPS, ident/README.md, build_v3/partner/series |
| studio-editor | opus | out/<slug>-v<n>.mp4 + .cmd + .timing.json + strip | SCRIPT, CLIPS, SOUND, ident, builders |
| studio-qc | sonnet | QC.md: RESULT line + one row per check | render, all job files, FACTS.md |
| studio-traffic | opus | Unlisted links in STATE.md and youtube/VIDEOS.md | STATE Gate 2 row, QC, render, youtube-channel-publish |

## Order and gates
```
director (plan) -> producer -> strategist -> copywriter
  -> GATE 1 spend (owner)
  -> [cinematographer | motion-director | sound]   one wave
  -> editor -> qc  (FAIL: owner of the fix -> editor -> qc, at most 3 loops)
  -> director (review)
  -> GATE 2 publish (owner)
  -> traffic (Unlisted)
```
Gate 1 covers grok generations, ElevenLabs characters and Codex stills as one budget
line. Gate 2 covers any upload; Public is always the owner's own click. Gates open only on
the owner's words in chat, asked by the producer with a tappable question and a push.
Waves hold at most 10 agents (fanout guard); renders stay serial (one ffmpeg at a time).

## JOB folder
`JOB.md STATE.md BRIEF.md SCRIPT.md CLIPS.md SOUND.md QC.md` plus
`stills/ clips/ ident/ audio/ out/ review/`. Rejects go to `<dir>/superseded/`; nothing is
deleted; a new cut is a new `v<n>`. Created by `studio_new_job`; the by-hand fallback root
in the skill is `~/studio-jobs/<YYYYMMDD>-<slug>/` until `job.py` fixes the root.

## Who enforces which hard rule
| rule (brief, verbatim) | enforced by |
|---|---|
| No women, photoreal men only | strategist, copywriter, cinematographer, qc (eye review), traffic (last sheet), director |
| no music melody, one continuous bed | sound, editor, qc (spectrogram) |
| male brand voice (Brian, Helios), native American delivery | sound, cinematographer (grok audio), qc (transcript) |
| no em dashes, no model names, no stats without a fact ID | strategist, copywriter, qc, traffic |
| brand look (matte black, off-white, one emerald accent) | cinematographer, motion-director, editor, qc |
| ffmpeg nice 19 <= 4 threads, heat 86/80 gate, one gen model at a time | editor, motion-director, producer, cinematographer |
| kill by PID only | editor, motion-director, producer |
| nothing published or spent without the owner | producer (asks), director (holds), traffic (acts only after) |
| grok downloads only through grok's own Download button (never cookie reads) | cinematographer, producer |

Every agent file carries a `## Never` list covering: publish, spend, delete, read cookies,
name models in product copy, show women.

## Library calls per agent
| agent | studio_new_job | studio_status | studio_inspect | studio_voice | studio_assemble | studio_qc | other tools |
|---|---|---|---|---|---|---|---|
| video-director | yes | yes | yes | | | reads | |
| producer | yes | yes | | | | | heat-gate, adbuild `eleven` (subscription read), grok Usage |
| strategist | | yes | | | | | |
| copywriter | | yes | | | | copy-only if offered | |
| cinematographer | | yes | yes | | | | grok-imagine-browser, grok-web-control, codex exec, wm.py, video-inspect |
| motion-director | | yes | yes | | | | ident kit (build-assets.py, render-ident.mjs, verify-ident.py), heat-gate |
| sound | | yes | yes | yes | | | build_v3/partner/series audio helpers until voice.py lands |
| editor | | yes | yes | | yes | | build_v3/partner/series until assemble.py lands |
| qc | | yes | yes | | | yes | video-inspect, ffmpeg sheet |
| traffic | | yes | yes | | | | youtube-channel-publish, wm.py, scribe SRT |

## Brief item to concrete source
| brief item | concrete source today |
|---|---|
| VO with-timestamps, onsets | `claude-copy/build_v3.py` `tts`, `word_onset`, `speech_span`, `placement`; `build_series.py` `phrase_onset`, `take_fit`, `audible_end` |
| One bed, SFX, chime on a detected frame | `build_partner.py` `sfx`, `bed_shape`, `green_counts`, `first_green`, `chime_lead`, `premix` |
| Loudness, loudnorm | `build_v3.py` `ebur128`, `max_volume`; render filter `loudnorm=I=-14:TP=-1.5:LRA=9,aresample=48000` |
| Words on the horizon | `build_v3.py` `word`, `words_plan`, `alpha_expr`; `build_series.py` `lines_for`; `clips/horizon.json` |
| Frame, onset, loudness verification, strips | `build_v3.py` `verify`, `band_diff`, `grab`; `build_series.py` `verify`, `contact` |
| Heat gate, one render at a time | `build_v3.py` `cool_down`; `build_series.py` `other_ffmpeg`, `wait_turn`; `~/bin/heat-gate` |
| Banned copy (em dash, model names) | `tools/adbuild.py` `BANNED` |
| ElevenLabs call with timeout and bounded retry | `tools/adbuild.py` `eleven`, `eleven_key` |
| Break a video open | `~/bin/video-inspect` |
| grok download and intake | pane: Post actions > Download; `claude-copy/tools/save-dialog.sh` (Save), `claude-copy/tools/intake.sh` (move, probe, sheet); CDP: `~/grok-web-mcp/server.py` `imagine_download` |
| grok prompts | `grok-imagine-browser` SKILL "Prompting the video model", "grok audio"; `clips/PLAN.md` |
| Codex still | memory `ref_codex_cli_image_generation.md` (recipe) |
| Atmosmotion ident | `ident/build-assets.py`, `render-ident.mjs`, `verify-ident.py`, `comps/*.cm.json` |
| Upload Unlisted | `youtube-channel-publish` SKILL, `youtube/VIDEOS.md` |

## Gaps: brief items with no concrete source script yet
- **Fact-ID tracing.** No script checks that a cited `F<nn>` exists in FACTS.md or avoids
  its conflicts list. `build_series.copy_errors` only compares the ad to its brief and runs
  `BANNED`. Strategist and qc do it with grep until `qc.py` does.
- **Women / person detection.** No detector exists; the proven method is a contact sheet
  read by eye (video-inspect frames.jpg, the youtube skill's sheet). `qc.py` is described
  as a "review prompt", so it stays a human-eye check.
- **Renaming in the GTK Save dialog.** `save-dialog.sh` only presses Enter; the rename is
  a documented manual step in the grok skill. No script types the name.
- **Codex still generation.** A recipe in memory, no script; `still.py` is new work.
- **JOB folder and `studio_new_job` / `studio_status`.** No precedent script; the 09-25
  jobs lived flat in `claude-copy/`. Root folder is not fixed yet.
- **Budget line and quota ledger.** No script; grok Usage is read by hand in the pane or by
  `imagine_quota` (CDP lane), ElevenLabs by a subscription read.
- **PID-only kills.** A rule only; no helper tracks the PIDs a job started.
- **Heat thresholds differ.** build_v3 and video-inspect wait at 86 C until under 80 C;
  `~/bin/heat-gate wait` starts under 80 C and resumes at 78 C. `job.py` should pick one.
- **Formats.** No reformat script for 16:9 or 1:1 variants of an ad; only the ident has
  both aspects.
