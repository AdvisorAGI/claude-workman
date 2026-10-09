"""Front-end compaction: the idle read, the boundary count, the header guard,
the film deferral, the CLI, and the detached launch.

What is checked here is what would actually hurt: typing into a session that is
mid-turn, typing into the wrong session because the header guard let a bad hash
through, running without any header to compare against, waking the owner's
film, and a detached run that fails without a log line. Nothing here touches
the display: every subprocess goes through `front_compact._run`, which the
autouse fixture replaces with a blocker unless a test installs a fake desk.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from workman import front_compact as fc
from workman import server

FIXTURES = Path(__file__).parent / "fixtures" / "front_compact"
LID = "local_b7194fa6-dd65-4e18-9024-61b5a80e7477"
OTHER = "local_beed4b98-2962-4695-9eb8-0e58756533ad"
FRAME = b"header strip pixels"
CALLER = "99999999-0000-4000-8000-000000000000"
GOOD = fc.hash_frame(FRAME)


def _blocked(argv, timeout):
    raise AssertionError(f"test reached a real subprocess: {argv}")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Private HOME, log and frontend lock; no subprocess, no /proc."""
    monkeypatch.setenv("HOME", str(tmp_path))
    # a known caller that is never a target here, whatever the real env holds
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", CALLER)
    monkeypatch.setenv("WORKMAN_FRONT_COMPACT_LOG", str(tmp_path / "compact-log.jsonl"))
    monkeypatch.setattr(fc, "FRONTEND_LOCK", str(tmp_path / "frontend.lock"))
    monkeypatch.setattr(fc, "_run", _blocked)
    monkeypatch.setattr(fc.subprocess, "Popen", _blocked)
    monkeypatch.setattr(fc, "process_names", lambda: [])
    return tmp_path


def fixture(home, name: str, age_s: float = 3600) -> str:
    """A copy of a fixture transcript, last written age_s ago."""
    path = home / name
    shutil.copy(FIXTURES / name, path)
    past = time.time() - age_s
    os.utime(path, (past, past))
    return str(path)


def log_results(home) -> list[str]:
    path = home / "compact-log.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["result"] for line in path.read_text().splitlines()]


def desk_entry(home: Path, local_id: str, cli_session: str) -> None:
    """A desktop-store entry saying local_id's transcript is <cli_session>.jsonl."""
    d = home / ".config" / "Claude" / "claude-code-sessions" / "acct" / "org"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{local_id}.json").write_text(json.dumps({"sessionId": local_id,
                                                   "cliSessionId": cli_session}))


class FakeDesk:
    """Stands in for ffmpeg, xdg-open and xdotool. Clicking send can append a
    compact_boundary line, the way the app does when /compact works."""

    def __init__(self, transcript: str = "", frame: bytes = FRAME, compacts: bool = True,
                 on_open=None, after_compact: str = ""):
        self.transcript, self.frame, self.compacts, self.on_open = transcript, frame, compacts, on_open
        self.after_compact = after_compact   # lines the app writes right after the boundary
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        out = b""
        if argv[0] == "ffmpeg":
            out = self.frame
        elif argv[0] == "xdg-open" and self.on_open:
            self.on_open()
        elif (argv[:2] == ["xdotool", "mousemove"] and tuple(argv[2:4]) == fc.SEND_XY
              and self.compacts and self.typed()[-1:] == ["/compact"]):
            with open(self.transcript, "a") as fh:
                fh.write('{"type":"system","subtype":"compact_boundary"}\n')
                fh.write(self.after_compact)
            os.utime(self.transcript, (0, 0))   # the compacted session has gone quiet
        return subprocess.CompletedProcess(argv, 0, out, b"")

    def typed(self) -> list[str]:
        return [c[-1] for c in self.calls if c[:2] == ["xdotool", "type"]]

    def xdotool(self) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "xdotool"]


def runner(transcript: str, expect: str = GOOD, resume: str = "", procs=lambda: []):
    sleeps: list[float] = []
    return fc.FrontCompact(LID, transcript, expect, resume, sleep=sleeps.append,
                           procs=procs), sleeps


class TestTranscriptIdle:
    def test_ended_turn_and_quiet_is_idle(self, home):
        assert fc.transcript_idle(fixture(home, "idle.jsonl"))

    def test_mid_turn_is_busy(self, home):
        assert not fc.transcript_idle(fixture(home, "busy.jsonl"))

    def test_an_end_turn_followed_by_a_new_prompt_is_busy(self, home):
        assert not fc.transcript_idle(fixture(home, "stale.jsonl"))

    def test_written_within_the_quiet_window_is_busy(self, home):
        path = fixture(home, "idle.jsonl")
        mtime = os.path.getmtime(path)
        assert not fc.transcript_idle(path, now=mtime + 19)
        assert fc.transcript_idle(path, now=mtime + 20)

    def test_missing_file_is_busy(self, home):
        assert not fc.transcript_idle(str(home / "nope.jsonl"))

    def test_no_turn_at_all_is_busy(self, home):
        path = home / "empty.jsonl"
        path.write_text('{"type":"custom-title"}\n')
        os.utime(path, (0, 0))
        assert not fc.transcript_idle(str(path))

    def test_a_local_command_after_the_turn_reads_busy(self, home):
        """Known gap, pinned: a /model typed after the turn ended leaves user
        records last, so the proven rule reads busy and the run times out
        rather than typing. Fail closed until the rule learns local commands."""
        path = fixture(home, "idle.jsonl")
        with open(path, "a") as fh:
            fh.write(json.dumps({"type": "user", "message": {"role": "user", "content":
                     "<local-command-stdout>Set model</local-command-stdout>"}}) + "\n")
        os.utime(path, (0, 0))
        assert not fc.transcript_idle(path)

    def test_reads_only_the_tail_and_skips_the_cut_line(self, home, monkeypatch):
        path = fixture(home, "idle.jsonl")
        size = os.path.getsize(path)
        # Cut inside the end_turn record: what is left of it must not parse into
        # anything, and no earlier record is in reach, so the answer is busy.
        lines = Path(path).read_text().splitlines(keepends=True)
        end_turn_start = sum(len(x) for x in lines[:3])
        monkeypatch.setattr(fc, "TAIL_BYTES", size - end_turn_start - 5)
        assert not fc.transcript_idle(path)
        monkeypatch.setattr(fc, "TAIL_BYTES", size - end_turn_start + 1)
        assert fc.transcript_idle(path)


class TestCountBoundaries:
    def test_counts_lines_like_grep_c(self, home):
        path = home / "t.jsonl"
        path.write_text('{"subtype":"compact_boundary"}\n{"type":"user"}\n'
                        '{"subtype":"compact_boundary","x":"compact_boundary"}\n')
        assert fc.count_boundaries(str(path)) == 2

    def test_none_is_zero(self, home):
        assert fc.count_boundaries(fixture(home, "idle.jsonl")) == 0

    def test_missing_file_is_zero(self, home):
        assert fc.count_boundaries(str(home / "nope.jsonl")) == 0


class TestHeaderHash:
    def test_hash_is_md5sum_cut_to_twelve(self):
        assert fc.hash_frame(b"abc") == "900150983cd2"

    def test_a_failed_grab_hashes_to_nothing(self):
        assert fc.hash_frame(b"") == ""

    def test_equal_well_formed_hashes_match(self):
        assert fc.header_matches(GOOD, GOOD)

    def test_different_hashes_do_not(self):
        assert not fc.header_matches("000000000000", GOOD)

    @pytest.mark.parametrize("expected", ["", "900150983CD2", "900150983cd", "900150983cd2 "])
    def test_a_malformed_expectation_never_matches(self, expected):
        assert not fc.header_matches(expected, expected)

    def test_empty_grab_never_matches(self):
        assert not fc.header_matches("", "")

    def test_ffmpeg_failure_is_empty(self, monkeypatch):
        monkeypatch.setattr(fc, "_run", lambda argv, timeout:
                            subprocess.CompletedProcess(argv, 1, FRAME, b"no display"))
        assert fc.header_hash() == ""


class TestFilmDefer:
    def test_vlc_or_mpv_defers(self):
        assert fc.film_playing(["bash", "vlc"])
        assert fc.film_playing(["mpv"])

    def test_exact_names_only(self):
        assert not fc.film_playing(["vlcx", "mpv-helper", "bash"])
        assert not fc.film_playing([])

    def test_runner_waits_for_the_film_to_end(self, home, monkeypatch):
        path = fixture(home, "idle.jsonl")
        monkeypatch.setattr(fc, "_run", FakeDesk(path))
        states = iter([["vlc"], ["vlc"], []])
        run, sleeps = runner(path, procs=lambda: next(states))
        assert run.run() == fc.OK
        assert sleeps.count(fc.FILM_POLL_S) == 2

    def test_runner_gives_up_after_the_bound_without_touching_the_desk(self, home, monkeypatch):
        desk = FakeDesk()
        monkeypatch.setattr(fc, "_run", desk)
        run, sleeps = runner(fixture(home, "idle.jsonl"), procs=lambda: ["mpv"])
        assert run.run() == fc.FILM_TOO_LONG
        assert sleeps.count(fc.FILM_POLL_S) == fc.FILM_POLLS
        assert desk.calls == []
        assert log_results(home) == ["abort: film player still running after 4 h"]


class TestRunner:
    def test_compacts_then_sends_the_resume_as_one_line(self, home, monkeypatch):
        path = fixture(home, "idle.jsonl")
        desk = FakeDesk(path)
        monkeypatch.setattr(fc, "_run", desk)
        run, _ = runner(path, resume="carry on\nwith  the plan")
        assert run.run() == fc.OK
        assert desk.calls[0] == ["xdg-open", f"claude://claude.ai/epitaxy/{LID}"]
        assert desk.typed() == ["/compact", "carry on with the plan"]
        assert log_results(home) == ["compacted", "resume sent"]

    def test_log_line_shape(self, home, monkeypatch):
        path = fixture(home, "idle.jsonl")
        monkeypatch.setattr(fc, "_run", FakeDesk(path))
        runner(path)[0].run()
        line = json.loads((home / "compact-log.jsonl").read_text().splitlines()[0])
        assert set(line) == {"ts", "target", "result"} and line["target"] == LID

    def test_wrong_session_on_screen_types_nothing(self, home, monkeypatch):
        """The guard the whole tool rests on: a header mismatch aborts before
        a single click or keystroke."""
        path = fixture(home, "idle.jsonl")
        desk = FakeDesk(path, frame=b"some other session")
        monkeypatch.setattr(fc, "_run", desk)
        assert runner(path)[0].run() == fc.HEADER_MISMATCH
        assert desk.xdotool() == []
        assert log_results(home)[0].startswith("abort: header ")

    def test_never_idle_touches_nothing(self, home, monkeypatch):
        desk = FakeDesk()
        monkeypatch.setattr(fc, "_run", desk)
        run, sleeps = runner(fixture(home, "busy.jsonl"))
        assert run.run() == fc.NEVER_IDLE
        assert desk.calls == [] and len(sleeps) == fc.IDLE_POLLS
        assert log_results(home) == ["abort: never idle in 30 min"]

    def test_busy_again_after_fronting_types_nothing(self, home, monkeypatch):
        path = fixture(home, "idle.jsonl")

        def prompt_arrives():
            with open(path, "a") as fh:
                fh.write('{"type":"user","message":{"content":"new prompt"}}\n')
        desk = FakeDesk(path, on_open=prompt_arrives)
        monkeypatch.setattr(fc, "_run", desk)
        assert runner(path)[0].run() == fc.BUSY_AGAIN
        assert desk.xdotool() == []

    def test_no_boundary_is_a_failure_and_sends_no_resume(self, home, monkeypatch):
        path = fixture(home, "idle.jsonl")
        desk = FakeDesk(path, compacts=False)
        monkeypatch.setattr(fc, "_run", desk)
        run, sleeps = runner(path, resume="carry on")
        assert run.run() == fc.NO_BOUNDARY
        assert desk.typed() == ["/compact"]
        assert sleeps.count(fc.BOUNDARY_POLL_S) == fc.BOUNDARY_POLLS
        assert log_results(home) == ["fail: no compact_boundary within 6 min"]

    def test_resume_skipped_when_the_session_changed(self, home, monkeypatch):
        path = fixture(home, "idle.jsonl")
        frames = iter([FRAME, b"someone switched sessions"])
        desk = FakeDesk(path)
        real = desk.__call__

        def switching(argv, timeout):
            if argv[0] == "ffmpeg":
                desk.frame = next(frames)
            return real(argv, timeout)
        monkeypatch.setattr(fc, "_run", switching)
        assert runner(path, resume="carry on")[0].run() == fc.OK
        assert desk.typed() == ["/compact"]
        assert log_results(home)[-1].startswith("resume skipped: header ")

    def test_frontend_lock_held_elsewhere_aborts(self, home, monkeypatch):
        desk = FakeDesk()
        monkeypatch.setattr(fc, "_run", desk)
        held = fc.try_lock(fc.FRONTEND_LOCK, 0)
        try:
            run, sleeps = runner(fixture(home, "idle.jsonl"))
            assert run.run() == fc.LOCK_BUSY
        finally:
            os.close(held)
        assert desk.calls == [] and sleeps.count(1) == fc.LOCK_WAIT_S

    def test_a_second_run_for_the_same_target_refuses(self, home, monkeypatch):
        monkeypatch.setattr(fc, "_run", FakeDesk())
        fc.state_dir().mkdir(parents=True)
        held = fc.try_lock(fc.state_dir() / f"{LID}.run.lock", 0)
        try:
            assert runner(fixture(home, "idle.jsonl"))[0].run() == fc.ALREADY_RUNNING
        finally:
            os.close(held)

    def test_locks_are_released_when_the_run_ends(self, home, monkeypatch):
        path = fixture(home, "idle.jsonl")
        monkeypatch.setattr(fc, "_run", FakeDesk(path))
        assert runner(path)[0].run() == fc.OK
        again = fc.try_lock(fc.FRONTEND_LOCK, 0)
        assert again is not None
        os.close(again)

    def test_an_unexpected_error_is_logged_not_raised(self, home):
        # The autouse blocker raises on the first subprocess.
        assert runner(fixture(home, "idle.jsonl"))[0].run() == fc.ERROR
        assert log_results(home)[0].startswith("abort: AssertionError")


class TestLearn:
    def test_stores_the_hash(self, home, monkeypatch):
        desk = FakeDesk()
        monkeypatch.setattr(fc, "_run", desk)
        out = fc.learn_header(LID, sleep=lambda s: None)
        assert out == {"ok": True, "hash": GOOD, "store": str(fc.header_store())}
        assert fc.stored_header(LID) == GOOD
        assert desk.calls[0] == ["xdg-open", fc.deep_link(LID)]

    def test_refuses_a_hash_another_session_owns(self, home, monkeypatch):
        fc.save_header(OTHER, GOOD)
        monkeypatch.setattr(fc, "_run", FakeDesk())
        out = fc.learn_header(LID, sleep=lambda s: None)
        assert out["ok"] is False and OTHER in out["error"]
        assert fc.stored_header(LID) == ""

    def test_refuses_during_a_film(self, home, monkeypatch):
        desk = FakeDesk()
        monkeypatch.setattr(fc, "_run", desk)
        monkeypatch.setattr(fc, "process_names", lambda: ["vlc"])
        assert fc.learn_header(LID, sleep=lambda s: None)["ok"] is False
        assert desk.calls == []

    def test_a_failed_grab_stores_nothing(self, home, monkeypatch):
        monkeypatch.setattr(fc, "_run", FakeDesk(frame=b""))
        assert fc.learn_header(LID, sleep=lambda s: None)["ok"] is False
        assert fc.load_headers() == {}

    def test_a_corrupt_store_reads_empty(self, home):
        fc.state_dir().mkdir(parents=True)
        fc.header_store().write_text('{"%s": "not-a-hash"}' % LID)
        assert fc.stored_header(LID) == ""
        fc.header_store().write_text("[1, 2")
        assert fc.load_headers() == {}


class TestCli:
    def test_parses_a_run(self, home):
        path = fixture(home, "idle.jsonl")
        args = fc.parse_args([LID, "--transcript", path, "--expect", GOOD, "--resume", "go"])
        assert (args.local_id, args.transcript, args.expect, args.resume, args.learn) == \
            (LID, path, GOOD, "go", False)

    def test_learn_needs_no_transcript(self):
        assert fc.parse_args([LID, "--learn"]).learn is True

    @pytest.mark.parametrize("argv", [
        [LID],                                                   # a run needs a transcript
        ["local_b7194fa6", "--learn"],                           # short id
        ["b7194fa6-dd65-4e18-9024-61b5a80e7477", "--learn"],     # no local_ prefix
        [LID + "; rm -rf ~", "--learn"],                         # goes into a URL
        [LID, "--learn", "--expect", "NOTHEX000000"],
        [LID, "--learn", "--resume", "x" * (fc.MAX_RESUME + 1)],
    ])
    def test_rejects(self, argv):
        with pytest.raises(SystemExit):
            fc.parse_args(argv)

    def test_refuses_to_run_without_a_header(self, home, monkeypatch):
        monkeypatch.setattr(fc, "FrontCompact", _blocked)
        assert fc.main([LID, "--transcript", fixture(home, "idle.jsonl")]) == fc.NO_HEADER
        assert log_results(home) == ["abort: no header hash stored, run --learn first"]

    def test_uses_the_stored_header(self, home, monkeypatch):
        fc.save_header(LID, GOOD)
        desk_entry(home, LID, "idle")
        seen = {}

        class Recorder:
            def __init__(self, local_id, transcript, expect, resume, **kw):
                seen["expect"] = expect

            def run(self):
                return fc.OK
        monkeypatch.setattr(fc, "FrontCompact", Recorder)
        # the fixture has no usage (0 tokens), so only a forced run passes the band
        assert fc.main([LID, "--transcript", fixture(home, "idle.jsonl"), "--force",
                        "--reason", "test fixture"]) == fc.OK
        assert seen["expect"] == GOOD
        assert log_results(home)[-1] == "guards passed, band forced: test fixture"

    def test_outside_the_band_without_force_refuses(self, home, monkeypatch):
        fc.save_header(LID, GOOD)
        desk_entry(home, LID, "idle")
        monkeypatch.setattr(fc, "FrontCompact", _blocked)
        assert fc.main([LID, "--transcript", fixture(home, "idle.jsonl")]) == fc.REFUSED
        assert log_results(home)[-1].startswith("refused: band: ctx 0 outside [300000, 400000]")
        with pytest.raises(SystemExit):   # a force always names its reason
            fc.parse_args([LID, "--transcript", fixture(home, "idle.jsonl"), "--force"])

    def test_a_transcript_that_is_not_this_sessions_refuses(self, home, monkeypatch):
        fc.save_header(LID, GOOD)
        desk_entry(home, OTHER, "idle")
        monkeypatch.setattr(fc, "FrontCompact", _blocked)
        assert fc.main([LID, "--transcript", fixture(home, "idle.jsonl"), "--force",
                        "--reason", "x"]) == fc.REFUSED
        assert "local_id_matches" in log_results(home)[-1]


class TestCallerAndRecheck:
    def test_a_short_self_id_refuses(self, home, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
        monkeypatch.setattr(fc, "FrontCompact", _blocked)
        fc.save_header(LID, GOOD)
        assert fc.main([LID, "--transcript", fixture(home, "idle.jsonl"),
                        "--self-id=owner"]) == fc.ERROR
        assert "'owner' is not a session id" in log_results(home)[-1]

    def test_a_question_after_the_compact_skips_the_resume(self, home, monkeypatch):
        path = fixture(home, "idle.jsonl")
        ask = json.dumps({"type": "assistant", "message": {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "q9", "name": "AskUserQuestion", "input": {}}]}}) + "\n"
        desk = FakeDesk(path, after_compact=ask)
        monkeypatch.setattr(fc, "_run", desk)
        run, _ = runner(path, resume="carry on")
        assert run.run() == fc.OK
        assert desk.typed() == ["/compact"]   # the resume line was never typed
        assert log_results(home)[-1] == "resume skipped: AskUserQuestion open"

    def test_the_cli_refuses_when_the_caller_is_unknown(self, home, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
        monkeypatch.setattr(fc, "FrontCompact", _blocked)
        monkeypatch.setattr("workman.orchestra_tools.caller_ids", lambda *a, **k: [])
        fc.save_header(LID, GOOD)
        assert fc.main([LID, "--transcript", fixture(home, "idle.jsonl")]) == fc.ERROR
        assert log_results(home) == ["abort: the caller's session id is unknown"]

    def test_a_subagent_end_turn_is_not_the_session_ending(self, home):
        path = home / "side.jsonl"
        path.write_text(
            '{"type":"user","message":{"role":"user","content":"go"}}\n'
            '{"type":"assistant","message":{"stop_reason":"tool_use","content":'
            '[{"type":"tool_use","id":"t","name":"Agent","input":{}}]}}\n'
            '{"type":"assistant","isSidechain":true,"message":{"stop_reason":"end_turn",'
            '"content":[{"type":"text","text":"sub done"}]}}\n')
        os.utime(path, (0, 0))
        assert not fc.transcript_idle(str(path))
        assert fc.full_ready(str(path))[0] is False

    def test_the_runner_rechecks_the_full_guards_before_the_first_click(self, home, monkeypatch):
        path = fixture(home, "idle.jsonl")
        desk = FakeDesk(path)
        monkeypatch.setattr(fc, "_run", desk)
        answers = iter([(True, ""), (False, "registry waiting")])
        run = fc.FrontCompact(LID, path, GOOD, sleep=lambda s: None, procs=lambda: [],
                              ready=lambda p: next(answers))
        assert run.run() == fc.BUSY_AGAIN
        assert desk.xdotool() == []
        assert log_results(home) == ["abort: target busy again: registry waiting"]

    def test_a_waiting_registry_keeps_the_runner_from_starting(self, home, monkeypatch):
        path = fixture(home, "idle.jsonl")
        reg = home / ".claude-x" / "sessions"
        reg.mkdir(parents=True)
        (home / ".claude-x" / "projects").mkdir()
        (reg / "1.json").write_text(json.dumps({"pid": 1, "sessionId": "idle",
                                                "status": "waiting"}))
        monkeypatch.setattr(fc, "IDLE_POLLS", 2)
        desk = FakeDesk(path)
        monkeypatch.setattr(fc, "_run", desk)
        run, _ = runner(path)
        assert run.run() == fc.NEVER_IDLE and desk.calls == []


class TestSpawn:
    def test_detaches_and_returns_the_log(self, home, monkeypatch):
        launched = {}

        class Proc:
            pid = 4242

        def popen(argv, **kw):
            launched.update(argv=argv, **kw)
            return Proc()
        monkeypatch.setattr(fc.subprocess, "Popen", popen)
        path = fixture(home, "idle.jsonl")
        out = fc.spawn(LID, path, resume="go", expect=GOOD)
        assert out["ok"] is True and out["pid"] == 4242
        assert out["log"] == str(home / "compact-log.jsonl")
        assert launched["start_new_session"] is True
        assert launched["argv"][1:] == ["-m", "workman.front_compact", LID,
                                        f"--transcript={path}", f"--expect={GOOD}",
                                        "--resume=go"]

    def test_the_launched_argv_parses_back_even_with_a_leading_dash(self, home, monkeypatch):
        launched = {}

        class Proc:
            pid = 1

        monkeypatch.setattr(fc.subprocess, "Popen",
                            lambda argv, **kw: launched.update(argv=argv) or Proc())
        path = fixture(home, "idle.jsonl")
        assert fc.spawn(LID, path, resume="-- carry on", expect=GOOD)["ok"] is True
        args = fc.parse_args(launched["argv"][3:])
        assert (args.local_id, args.transcript, args.expect, args.resume) == \
            (LID, path, GOOD, "-- carry on")

    def test_refuses_without_a_header(self, home):
        out = fc.spawn(LID, fixture(home, "idle.jsonl"))
        assert out["ok"] is False and "--learn" in out["error"]

    def test_refuses_bad_arguments(self, home):
        assert fc.spawn("local_nope", fixture(home, "idle.jsonl"), expect=GOOD)["ok"] is False
        assert fc.spawn(LID, str(home / "missing.jsonl"), expect=GOOD)["ok"] is False


class TestTool:
    def test_registered_and_delegates(self, home):
        # validation still comes first; the guards are covered in test_orchestra_control
        assert callable(getattr(server, "session_front_compact", None))
        out = server.session_front_compact(LID, str(home / "missing.jsonl"), expect=GOOD)
        assert out["ok"] is False and "transcript not found" in out["error"]
