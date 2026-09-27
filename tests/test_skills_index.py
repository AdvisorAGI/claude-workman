"""Skill finder: search the index, load one skill, add one.

What would hurt: a search that reads the real home's skills instead of the
sandbox, a stale cache after a SKILL.md changes, a section slice that runs into
the next section, and an add that overwrites a skill already there.
"""
from __future__ import annotations

import json
import os

import pytest

from workman import server, skills_index


def make_skill(root, dirname, name=None, description=None, body="", extra_files=()):
    d = root / dirname
    d.mkdir(parents=True, exist_ok=True)
    front = ""
    if name is not None or description is not None:
        front = "---\n"
        if name is not None:
            front += f"name: {name}\n"
        if description is not None:
            front += f"description: {description}\n"
        front += "---\n"
    (d / "SKILL.md").write_text(front + body, encoding="utf-8")
    for rel in extra_files:
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    return d


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / ".claude" / "skills").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv("WORKMAN_SKILL_ROOTS", raising=False)
    return h


@pytest.fixture
def default_root(home):
    return home / ".claude" / "skills"


class TestFrontmatter:
    def test_plain_keys_and_quotes(self):
        meta, body = skills_index.parse_frontmatter(
            '---\nname: demo-alpha\ndescription: "Does alpha things"\n---\n# Alpha\nbody\n')
        assert meta == {"name": "demo-alpha", "description": "Does alpha things"}
        assert body.startswith("# Alpha")

    def test_crlf_and_folded_description(self):
        text = "---\r\nname: demo\r\ndescription: >\r\n  first line\r\n  second line\r\n---\r\nbody"
        meta, body = skills_index.parse_frontmatter(text)
        assert meta["name"] == "demo"
        assert meta["description"] == "first line second line"
        assert body == "body"

    def test_no_frontmatter_falls_back_to_heading(self, default_root):
        make_skill(default_root, "loose", body="# Loose Notes\n\nFirst paragraph\ncontinues.\n\n## Later\n")
        s = skills_index.read_skill(default_root / "loose", default_root)
        assert s["name"] == "Loose Notes"
        assert s["description"] == "First paragraph continues."
        assert s["headings"] == ["Loose Notes", "Later"]

    def test_headings_inside_code_fences_are_ignored(self):
        assert skills_index.headings("# A\n```\n# not a heading\n```\n## B\n") == ["A", "B"]


class TestSearch:
    def test_name_hit_outranks_description_hit(self, default_root):
        make_skill(default_root, "demo-invoice", "demo-invoice", "Make documents.")
        make_skill(default_root, "demo-other", "demo-other", "Mentions invoice once.")
        r = skills_index.search("invoice")
        assert r["ok"] and [x["name"] for x in r["results"]] == ["demo-invoice", "demo-other"]
        assert r["results"][0]["score"] > r["results"][1]["score"]

    def test_heading_hit_counts_and_prefix_matches(self, default_root):
        make_skill(default_root, "demo-a", "demo-a", "Unrelated.", body="# A\n## Checkout flow\n")
        make_skill(default_root, "demo-b", "demo-b", "Unrelated.")
        r = skills_index.search("check")
        assert [x["name"] for x in r["results"]] == ["demo-a"]

    def test_results_never_carry_the_body(self, default_root):
        make_skill(default_root, "demo-a", "demo-a", "d " * 400, body="SECRET-BODY-TEXT\n")
        r = skills_index.search("demo")
        res = r["results"][0]
        assert set(res) == {"name", "description", "root", "path", "score"}
        assert len(res["description"]) <= skills_index.DESC_TRIM
        assert "SECRET-BODY-TEXT" not in json.dumps(r)

    def test_empty_query_is_refused(self, default_root):
        assert skills_index.search("  ")["ok"] is False

    def test_limit(self, default_root):
        for i in range(12):
            make_skill(default_root, f"demo-{i}", f"demo-{i}", "Demo skill.")
        assert skills_index.search("demo", limit=3)["count"] == 3


class TestCache:
    def test_cache_written_and_reused(self, default_root, home):
        make_skill(default_root, "demo-a", "demo-a", "Alpha.")
        first = skills_index.load_index()
        assert first["cached"] is False
        assert (home / ".cache" / "workman" / "skills-index.json").is_file()
        assert skills_index.load_index()["cached"] is True

    def test_mtime_change_rebuilds(self, default_root):
        d = make_skill(default_root, "demo-a", "demo-a", "Alpha.")
        skills_index.load_index()
        (d / "SKILL.md").write_text("---\nname: demo-a\ndescription: Renamed zebra.\n---\n")
        st = (d / "SKILL.md").stat()
        os.utime(d / "SKILL.md", ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        idx = skills_index.load_index()
        assert idx["cached"] is False
        assert skills_index.search("zebra")["results"][0]["name"] == "demo-a"

    def test_new_skill_dir_rebuilds(self, default_root):
        make_skill(default_root, "demo-a", "demo-a", "Alpha.")
        skills_index.load_index()
        make_skill(default_root, "demo-b", "demo-b", "Beta.")
        assert skills_index.load_index()["cached"] is False
        assert skills_index.search("beta")["count"] == 1

    def test_reindex(self, default_root):
        make_skill(default_root, "demo-a", "demo-a", "Alpha.")
        r = skills_index.reindex()
        assert r["ok"] and r["count"] == 1


class TestShow:
    BODY = ("# Demo\n\nIntro.\n\n## Setup\nstep one\n### Detail\nmore\n\n"
            "## Usage\nuse it\n")

    def test_full_body_and_siblings(self, default_root):
        make_skill(default_root, "demo-a", "demo-a", "Alpha.", body=self.BODY,
                   extra_files=("references/notes.md", "scripts/run.sh"))
        r = skills_index.show("demo-a")
        assert r["ok"] and r["body"] == self.BODY and r["truncated"] is False
        assert r["files"] == ["references/notes.md", "scripts/run.sh"]

    def test_one_section_stops_at_next_same_level(self, default_root):
        make_skill(default_root, "demo-a", "demo-a", "Alpha.", body=self.BODY)
        r = skills_index.show("demo-a", section="setup")
        assert r["body"] == "## Setup\nstep one\n### Detail\nmore\n"

    def test_missing_section_lists_sections(self, default_root):
        make_skill(default_root, "demo-a", "demo-a", "Alpha.", body=self.BODY)
        r = skills_index.show("demo-a", section="nope")
        assert r["ok"] is False and "Usage" in r["sections"]

    def test_truncation(self, default_root):
        make_skill(default_root, "demo-a", "demo-a", "Alpha.", body="x" * 5000)
        r = skills_index.show("demo-a", max_chars=1000)
        assert r["truncated"] is True and len(r["body"]) == 1000 and r["chars"] == 5000

    def test_not_found_suggests(self, default_root):
        make_skill(default_root, "demo-alpha", "demo-alpha", "Alpha.")
        r = skills_index.show("alpha")
        assert r["ok"] is False and r["did_you_mean"] == ["demo-alpha"]


class TestAdd:
    def test_add_copies_into_first_env_root(self, tmp_path, monkeypatch):
        target, second = tmp_path / "shared", tmp_path / "other"
        monkeypatch.setenv("WORKMAN_SKILL_ROOTS", os.pathsep.join([str(target), str(second)]))
        src = make_skill(tmp_path / "src", "whatever", "demo-new", "New one.",
                         extra_files=("references/a.md",))
        r = skills_index.add(str(src))
        assert r["ok"] and r["indexed"] is True
        assert (target / "demo-new" / "references" / "a.md").is_file()
        assert skills_index.search("demo new")["results"][0]["name"] == "demo-new"

    def test_add_refuses_overwrite(self, tmp_path, default_root):
        make_skill(default_root, "demo-new", "demo-new", "Original.")
        src = make_skill(tmp_path / "src", "demo-new", "demo-new", "Replacement.")
        r = skills_index.add(str(src), root=str(default_root))
        assert r["ok"] is False and r["error"] == "exists"
        assert "Original." in (default_root / "demo-new" / "SKILL.md").read_text()

    def test_add_needs_a_target_root(self, tmp_path, default_root):
        src = make_skill(tmp_path / "src", "demo-new", "demo-new", "New.")
        r = skills_index.add(str(src))
        assert r["ok"] is False and r["error"] == "no_target_root"
        assert not (default_root / "demo-new").exists()

    def test_add_validates_frontmatter(self, tmp_path):
        src = make_skill(tmp_path / "src", "demo-new", body="# No frontmatter\n")
        r = skills_index.add(str(src), root=str(tmp_path / "dest"))
        assert r["ok"] is False and r["error"] == "bad_frontmatter"


class TestRoots:
    def test_env_roots_are_searched_and_deduplicated(self, tmp_path, monkeypatch, default_root):
        extra = tmp_path / "extra"
        make_skill(extra, "demo-env", "demo-env", "From env root.")
        monkeypatch.setenv("WORKMAN_SKILL_ROOTS",
                           os.pathsep.join([str(extra), str(default_root), ""]))
        assert skills_index.roots() == [default_root, extra]
        r = skills_index.search("env root")
        assert r["results"][0]["root"] == str(extra)

    def test_changing_roots_rebuilds(self, tmp_path, monkeypatch, default_root):
        skills_index.load_index()
        extra = tmp_path / "extra"
        make_skill(extra, "demo-env", "demo-env", "From env root.")
        monkeypatch.setenv("WORKMAN_SKILL_ROOTS", str(extra))
        assert skills_index.search("env")["count"] == 1


class TestServerTools:
    def test_tools_are_registered_and_return_dicts(self, default_root):
        make_skill(default_root, "demo-a", "demo-a", "Alpha.", body="# A\n## One\nx\n")
        assert server.skill_search("alpha")["results"][0]["name"] == "demo-a"
        assert server.skill_show("demo-a", section="one")["body"].startswith("## One")
        assert server.skill_reindex()["count"] == 1
        assert server.skill_add("/nonexistent/dir")["ok"] is False


def test_roots_include_home_library_when_present(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("WORKMAN_SKILL_ROOTS", raising=False)
    monkeypatch.delenv("WORKMAN_SKILL_LIBRARY", raising=False)
    assert skills_index.roots() == [tmp_path / ".claude" / "skills"]
    lib = tmp_path / ".workman" / "library"
    lib.mkdir(parents=True)
    assert skills_index.roots() == [tmp_path / ".claude" / "skills", lib]
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("WORKMAN_SKILL_LIBRARY", str(other))
    assert skills_index.roots() == [tmp_path / ".claude" / "skills", other]
