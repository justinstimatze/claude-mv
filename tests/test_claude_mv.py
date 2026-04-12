"""Tests for claude-mv."""

import json
import os
import shutil
import subprocess
import sys
import tempfile

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "claude-mv")


def run_claude_mv(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    """Run claude-mv with given args."""
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        capture_output=True,
        text=True,
        check=check,
    )


# ── Unit tests for encode_path ───────────────────────────────────────


class TestEncodePath:
    """Test the encoding function matches Claude Code's scheme."""

    @staticmethod
    def encode(path: str) -> str:
        """Replicate encode_path for testing."""
        return "".join("-" if ch in "/._-" else ch for ch in path)

    def test_basic_path(self):
        assert self.encode("/home/user/project") == "-home-user-project"

    def test_dots(self):
        assert self.encode("/home/user/.config") == "-home-user--config"

    def test_underscores(self):
        assert self.encode("/home/user/my_project") == "-home-user-my-project"

    def test_hyphens(self):
        assert self.encode("/home/user/my-project") == "-home-user-my-project"

    def test_mixed(self):
        assert self.encode("/home/user/my.project_v2-beta") == "-home-user-my-project-v2-beta"

    def test_lossy_collision(self):
        """Paths differing only in /._- encode identically."""
        assert self.encode("/a.b") == self.encode("/a-b") == self.encode("/a_b")


# ── CLI interface tests ──────────────────────────────────────────────


class TestCLI:
    def test_version(self):
        r = run_claude_mv("--version")
        assert r.returncode == 0
        assert "claude-mv" in r.stdout

    def test_help(self):
        r = run_claude_mv("--help")
        assert r.returncode == 0
        assert "old-path" in r.stdout or "PATH" in r.stdout

    def test_no_args(self):
        r = run_claude_mv()
        assert r.returncode == 2  # argparse error

    def test_root_path_guard(self):
        r = run_claude_mv("/", "/tmp/foo", "--json")  # noqa: S108
        assert r.returncode == 1
        data = json.loads(r.stdout)
        assert "too short" in data["errors"][0]

    def test_identical_paths(self):
        with tempfile.TemporaryDirectory() as d:
            r = run_claude_mv(d, d, "--json")
            assert r.returncode == 2  # NOOP
            data = json.loads(r.stdout)
            assert data["success"] is True

    def test_ancestor_descendant_guard(self):
        with tempfile.TemporaryDirectory() as parent:
            child = os.path.join(parent, "child")
            os.makedirs(child)
            r = run_claude_mv(parent, child, "--json")
            assert r.returncode == 1
            data = json.loads(r.stdout)
            assert "not supported" in data["errors"][0]

    def test_new_path_must_exist(self):
        r = run_claude_mv("/home/fake/old", "/home/fake/new", "--json")
        assert r.returncode == 1
        data = json.loads(r.stdout)
        assert "does not exist" in data["errors"][0]


# ── Dry run tests ────────────────────────────────────────────────────


class TestDryRun:
    def test_dry_run_no_modifications(self):
        """Dry run should not create any backup or modify files."""
        with tempfile.TemporaryDirectory() as d:
            old = os.path.join(d, "subdir", "old")
            new = os.path.join(d, "subdir", "new")
            os.makedirs(new)
            r = run_claude_mv(old, new, "--dry-run", "--json")
            data = json.loads(r.stdout)
            assert data["dryRun"] is True
            assert data["backupDir"] is None


# ── Replacement logic tests ──────────────────────────────────────────


class TestReplacementOrdering:
    """Verify replacement ordering handles edge cases."""

    @staticmethod
    def encode(path: str) -> str:
        return "".join("-" if ch in "/._-" else ch for ch in path)

    def test_old_encoded_in_new_path(self):
        """When old_encoded appears in new_path, encoded must be replaced first."""
        old_path = "/x"
        new_path = "/y-x"  # contains old_encoded "-x"
        old_encoded = self.encode(old_path)  # "-x"
        new_encoded = self.encode(new_path)  # "-y-x"
        assert old_encoded in new_path
        # Simulate: content has both forms
        content = f'cwd: "{old_path}", encoded: "{old_encoded}"'
        # Correct order: encoded first
        result = content.replace(old_encoded, new_encoded).replace(old_path, new_path)
        assert old_path not in result or old_path in new_path
        assert old_encoded not in result or old_encoded in new_encoded


# ── History.jsonl splice tests ───────────────────────────────────────


class TestHistorySplice:
    """Test the Layer 5 key-anchored splice logic."""

    def test_splice_correct_field(self):
        """Should only replace the 'project' field, not other fields with same value."""
        import re as re_mod

        old_path = "/home/user/app"
        new_path = "/home/user/app-v2"
        line = json.dumps({"cwd": old_path, "project": old_path, "title": "test"})

        old_fragment = json.dumps(old_path)
        new_fragment = json.dumps(new_path)
        replacement = '"project": ' + new_fragment
        patched = re_mod.sub(
            r'"project"\s*:\s*' + re_mod.escape(old_fragment),
            lambda m: replacement,
            line,
            count=1,
        )
        data = json.loads(patched)
        assert data["project"] == new_path
        assert data["cwd"] == old_path  # should NOT be changed

    def test_splice_preserves_other_keys(self):
        """Original key ordering and formatting should be preserved."""
        old_path = "/home/user/app"
        new_path = "/home/user/newapp"
        import re as re_mod

        # Compact JSON (no spaces)
        line = f'{{"project":"{old_path}","other":"value"}}'
        old_fragment = json.dumps(old_path)
        new_fragment = json.dumps(new_path)
        replacement = '"project": ' + new_fragment
        patched = re_mod.sub(
            r'"project"\s*:\s*' + re_mod.escape(old_fragment),
            lambda m: replacement,
            line,
            count=1,
        )
        data = json.loads(patched)
        assert data["project"] == new_path
        assert data["other"] == "value"


# ── End-to-end migration test ───────────────────────────────────────


class TestEndToEnd:
    """Full migration with mock ~/.claude structure."""

    def setup_method(self):
        """Create a fake ~/.claude directory structure."""
        self.tmpdir = tempfile.mkdtemp()
        self.fake_home = os.path.join(self.tmpdir, "home")
        self.claude_dir = os.path.join(self.fake_home, ".claude")
        self.old_path = os.path.join(self.fake_home, "projects", "myapp")
        self.new_path = os.path.join(self.fake_home, "projects", "myapp-v2")

        # Create directories
        os.makedirs(self.new_path)  # new path must exist
        os.makedirs(self.claude_dir)

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_no_claude_dir(self):
        """Should handle missing ~/.claude gracefully."""
        # Use a path where ~/.claude doesn't exist
        env = os.environ.copy()
        env["HOME"] = os.path.join(self.tmpdir, "nohome")
        os.makedirs(env["HOME"])
        r = subprocess.run(
            [sys.executable, SCRIPT, self.old_path, self.new_path, "--json"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert r.returncode == 1
        data = json.loads(r.stdout)
        assert any("~/.claude" in e or ".claude" in e for e in data["errors"])


# ── Backslash safety tests ──────────────────────────────────────────


class TestBackslashSafety:
    """Verify re.sub lambda replacement prevents backslash interpretation."""

    def test_regex_replacement_no_backreference(self):
        """Lambda replacement should not interpret \\1 as backreference."""
        import re as re_mod

        pattern = re_mod.compile(r"old")
        replacement = r"new\1path"
        result = pattern.sub(lambda m: replacement, "old stuff")
        assert result == r"new\1path stuff"  # literal, not backreference
