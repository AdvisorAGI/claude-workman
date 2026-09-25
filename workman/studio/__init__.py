"""Workman Studio: the ad-video method of 2026-09-25 as a library.

One module per craft, each a set of small pure functions plus a thin io layer and
a CLI (`python -m workman.studio.<module>`):

    job       job folder layout, heat gate, one render at a time, PID-only kills
    inspect   break a video open: probe, frame sheet, cuts, loudness, audio, transcript
    voice     ElevenLabs VO with timestamps, onsets, bed and SFX, loudness
    grok      grok Imagine prompt builders and the Download > Save-dialog > intake flow
    still     Codex CLI stills (plan login, stdin prompt, -i reference)
    assemble  spec-driven build: shots, words on onsets, bed, chime on frame, card, verify
    qc        rule checks that write QC.md

Source of truth: ~/advisorppc-ads-20260925/claude-copy/build_{v3,partner,series}.py and
~/bin/video-inspect. Library reference: docs/STUDIO.md.

Importing this package imports nothing heavy; modules are imported by name.
"""
