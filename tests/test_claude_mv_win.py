"""Windows-specific tests for claude-mv.

Mirrors the structure of test_claude_mv.py but exercises the Windows code
paths: drive letters, backslash separators, JSON-escaped backslashes, and
the OpenProcess-based pid_alive helper. The whole module is skipped on
non-Windows platforms.
"""

import importlib.machinery
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows-only behavior",
)

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "claude-mv")


def _load_module():
    """Load claude-mv as a module so we can unit-test internals.

    The script has no .py extension, so we use SourceFileLoader directly.
    """
    loader = importlib.machinery.SourceFileLoader("claude_mv_module", SCRIPT)
    m = types.ModuleType(loader.name)
    loader.exec_module(m)
    return m


def run_claude_mv(*args: str, check: bool = False, env=None) -> subprocess.CompletedProcess:
    """Run claude-mv with given args."""
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )


# ── Unit tests for encode_path ───────────────────────────────────────


class TestEncodePath:
    """Test the Windows variant of the encoding function.

    On Windows the encoder collapses /, \\, :, ., _, and - all to '-'.
    """

    @staticmethod
    def encode(path: str) -> str:
        """Replicate the Windows encode_path for testing."""
        return "".join("-" if ch in "/\\:._-" else ch for ch in path)

    def test_basic_path(self):
        assert self.encode(r"C:\Users\foo") == "C--Users-foo"

    def test_drive_separator_collapses(self):
        """Both ':' and '\\' independently collapse to '-', so 'C:\\' -> 'C--'."""
        assert self.encode("C:" + "\\") == "C--"

    def test_dots(self):
        assert self.encode(r"C:\Users\.config") == "C--Users--config"

    def test_underscores(self):
        assert self.encode(r"C:\Users\my_project") == "C--Users-my-project"

    def test_hyphens(self):
        assert self.encode(r"C:\Users\my-project") == "C--Users-my-project"

    def test_mixed(self):
        assert self.encode(r"C:\u\my.project_v2-beta") == "C--u-my-project-v2-beta"

    def test_lossy_collision(self):
        """Paths differing only in the encoded character set encode identically."""
        assert self.encode(r"C:\a.b") == self.encode(r"C:\a-b") == self.encode(r"C:\a_b")

    def test_forward_slash_equivalent(self):
        """Forward slashes encode the same as backslashes on Windows."""
        assert self.encode(r"C:\a\b\c") == self.encode("C:/a/b/c")

    def test_matches_real_function(self):
        """The reference encoding here matches the script's encode_path."""
        m = _load_module()
        sample = r"C:\home\Works\replay\sandbox\claude\claude-mv"
        assert m.encode_path(sample) == self.encode(sample)
        assert m.encode_path(sample) == "C--home-Works-replay-sandbox-claude-claude-mv"


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

    def test_drive_root_path_guard(self):
        """A bare drive root has only one segment; the guard must reject it."""
        with tempfile.TemporaryDirectory() as d:
            r = run_claude_mv("C:\\", d, "--json")
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
        r = run_claude_mv(r"C:\fake\old", r"C:\fake\new", "--json")
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
    """Verify replacement ordering handles edge cases on Windows.

    On Windows, paths inside JSON files are stored with backslashes escaped
    (a single backslash on disk is two characters: \\\\). The rewriter must
    rewrite the JSON-escaped form too, not just the raw form.
    """

    @staticmethod
    def encode(path: str) -> str:
        return "".join("-" if ch in "/\\:._-" else ch for ch in path)

    def test_old_encoded_in_new_path(self):
        """When old_encoded appears in new_path, encoded must be replaced first."""
        old_path = r"C:\x"
        new_path = r"C:\y-x"  # contains old_encoded "C--x" as substring of "C--y-x"... no
        # Construct a case where old_encoded is a substring of new_path.
        old_path = r"C:\a"
        old_encoded = self.encode(old_path)  # "C--a"
        new_path = r"C:\b-C--a"  # literal "C--a" appears inside new_path
        new_encoded = self.encode(new_path)
        assert old_encoded in new_path
        # Correct order: encoded first to avoid corrupting the freshly-written
        # new_path.
        content = f'cwd: "{old_path}", encoded: "{old_encoded}"'
        result = content.replace(old_encoded, new_encoded).replace(old_path, new_path)
        # The freshly-inserted new_path should not have been re-rewritten.
        assert result.count(new_path) == 1

    def test_json_escaped_form_replaced_first(self):
        """JSON-escaped form is strictly longer than raw and must run first.

        The script's rewrite_paths_in_file does this; we replicate the
        ordering inline here to make the property explicit.
        """
        old_path = r"C:\foo"
        new_path = r"D:\bar"
        old_json = json.dumps(old_path)[1:-1]  # 'C:\\\\foo' (Python repr) = 7 chars on disk
        new_json = json.dumps(new_path)[1:-1]
        # File content as written by Claude Code (JSON-encoded path).
        content = f'{{"cwd": "{old_json}", "title": "demo"}}'
        # JSON-escaped first, then raw — same ordering as the script.
        result = content.replace(old_json, new_json).replace(old_path, new_path)
        # Round-trip through json: cwd must end up as new_path.
        parsed = json.loads(result)
        assert parsed["cwd"] == new_path


# ── History.jsonl splice tests ───────────────────────────────────────


class TestHistorySplice:
    """Test the Layer 5 key-anchored splice logic with Windows paths.

    The splice runs on the on-disk JSONL line where backslashes are escaped,
    and json.dumps + re.escape together produce a regex that matches the
    escaped form correctly.
    """

    def test_splice_correct_field(self):
        """Should only replace the 'project' field, not other fields with same value."""
        old_path = r"C:\Users\foo\app"
        new_path = r"C:\Users\foo\app-v2"
        line = json.dumps({"cwd": old_path, "project": old_path, "title": "test"})

        old_fragment = json.dumps(old_path)
        new_fragment = json.dumps(new_path)
        replacement = '"project": ' + new_fragment
        patched = re.sub(
            r'"project"\s*:\s*' + re.escape(old_fragment),
            lambda m: replacement,
            line,
            count=1,
        )
        data = json.loads(patched)
        assert data["project"] == new_path
        assert data["cwd"] == old_path  # should NOT be changed

    def test_splice_preserves_other_keys(self):
        """Original key ordering and formatting should be preserved."""
        old_path = r"C:\Users\foo\app"
        new_path = r"C:\Users\foo\newapp"

        # Use json.dumps to construct the line so backslashes are properly escaped.
        line = json.dumps({"project": old_path, "other": "value"})
        old_fragment = json.dumps(old_path)
        new_fragment = json.dumps(new_path)
        replacement = '"project": ' + new_fragment
        patched = re.sub(
            r'"project"\s*:\s*' + re.escape(old_fragment),
            lambda m: replacement,
            line,
            count=1,
        )
        data = json.loads(patched)
        assert data["project"] == new_path
        assert data["other"] == "value"


# ── End-to-end migration test ───────────────────────────────────────


class TestEndToEnd:
    """Full migration with mock ~/.claude structure on Windows.

    Note: Python on Windows resolves `~` from USERPROFILE (it ignores HOME).
    We override USERPROFILE plus HOMEDRIVE/HOMEPATH to redirect ~ to a tmpdir.
    """

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.fake_home = os.path.join(self.tmpdir, "home")
        self.claude_dir = os.path.join(self.fake_home, ".claude")
        self.old_path = os.path.join(self.fake_home, "projects", "myapp")
        self.new_path = os.path.join(self.fake_home, "projects", "myapp-v2")
        os.makedirs(self.new_path)  # new path must exist
        os.makedirs(self.claude_dir)

    def teardown_method(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _redirected_env(self, home: str) -> dict:
        env = os.environ.copy()
        env["USERPROFILE"] = home
        # ntpath.expanduser falls back to HOMEDRIVE+HOMEPATH if USERPROFILE
        # is empty; redirect those too for safety.
        drive, path = os.path.splitdrive(home)
        env["HOMEDRIVE"] = drive
        env["HOMEPATH"] = path or "\\"
        return env

    def test_no_claude_dir(self):
        """Should handle missing ~/.claude gracefully."""
        nohome = os.path.join(self.tmpdir, "nohome")
        os.makedirs(nohome)
        env = self._redirected_env(nohome)
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
    """Verify re.sub lambda replacement prevents backslash interpretation.

    Especially important on Windows where every path segment ends in '\\'
    and a careless replacement string could be parsed as a backreference.
    """

    def test_regex_replacement_no_backreference(self):
        """Lambda replacement should not interpret \\1 as backreference."""
        pattern = re.compile(r"old")
        replacement = r"new\1path"
        result = pattern.sub(lambda m: replacement, "old stuff")
        assert result == r"new\1path stuff"

    def test_windows_path_in_replacement_is_literal(self):
        """A Windows path containing \\1 must round-trip as literal."""
        pattern = re.compile(re.escape("OLD"))
        # Pathological case: path contains a digit right after a backslash —
        # exactly what re.sub would interpret as a backreference if treated
        # as a template string.
        windows_path = r"C:\1stdrive\foo"
        result = pattern.sub(lambda m, r=windows_path: r, "OLD here")
        assert result == windows_path + " here"


# ── Windows-specific helpers ────────────────────────────────────────


class TestPidAlive:
    """pid_alive must not actually terminate the process under inspection.

    On Windows, os.kill(pid, 0) calls TerminateProcess with exit code 0 —
    i.e. it KILLS the target. The script uses OpenProcess + GetExitCodeProcess
    via ctypes instead. This test verifies the helper reports liveness without
    side effects.
    """

    def test_none(self):
        m = _load_module()
        assert m.pid_alive(None) is False

    def test_self(self):
        m = _load_module()
        assert m.pid_alive(os.getpid()) is True
        # And the calling process is still alive after the check.
        assert m.pid_alive(os.getpid()) is True

    def test_nonexistent(self):
        m = _load_module()
        # An astronomical pid that cannot exist on a normal system.
        assert m.pid_alive(99_999_999) is False

    def test_does_not_terminate_subprocess(self):
        """Probing a real running subprocess must not kill it."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)"],
        )
        try:
            m = _load_module()
            assert m.pid_alive(proc.pid) is True
            # If pid_alive accidentally terminated the process, poll() would
            # return an exit code immediately.
            assert proc.poll() is None
        finally:
            proc.terminate()
            proc.wait(timeout=5)


class TestPathSeparators:
    """is_path_under and is_strictly_under accept both / and \\ on Windows."""

    def test_under_backslash(self):
        m = _load_module()
        assert m.is_path_under(r"C:\foo\bar", r"C:\foo") is True

    def test_under_forward_slash(self):
        m = _load_module()
        assert m.is_path_under("C:/foo/bar", "C:/foo") is True

    def test_under_equality(self):
        m = _load_module()
        assert m.is_path_under(r"C:\foo", r"C:\foo") is True

    def test_under_sibling_rejected(self):
        """C:\\foobar must NOT be reported as under C:\\foo."""
        m = _load_module()
        assert m.is_path_under(r"C:\foobar", r"C:\foo") is False

    def test_strictly_under_excludes_equality(self):
        m = _load_module()
        assert m.is_strictly_under(r"C:\foo", r"C:\foo") is False
        assert m.is_strictly_under(r"C:\foo\bar", r"C:\foo") is True


class TestPathSegments:
    def test_windows_backslash(self):
        m = _load_module()
        assert m.path_segments(r"C:\home\foo\bar") == ["C", "home", "foo", "bar"]

    def test_windows_forward_slash(self):
        m = _load_module()
        assert m.path_segments("C:/home/foo/bar") == ["C", "home", "foo", "bar"]

    def test_drive_only_too_short(self):
        """The path-too-short guard relies on this returning <2 segments."""
        m = _load_module()
        assert len(m.path_segments("C:")) < 2
        assert len(m.path_segments("C:" + "\\")) < 2


class TestJsonStringContent:
    """json_string_content doubles backslashes; this is the key Windows trick."""

    def test_backslash_doubled(self):
        m = _load_module()
        # r"C:\foo" is 6 chars; JSON-encoded it is 7 chars (one backslash
        # becomes two). Stripping the surrounding quotes leaves 7 chars.
        assert m.json_string_content(r"C:\foo") == "C:" + "\\\\" + "foo"

    def test_posix_unchanged(self):
        m = _load_module()
        assert m.json_string_content("/home/foo") == "/home/foo"


class TestLayer6JsonRegex:
    """The Layer 6 path_pattern operates on json.dumps() output.

    The pattern must match the JSON-escaped path form, the substitution
    must produce the JSON-escaped new form, and the boundary lookahead
    must accept JSON-escaped backslashes (not just '/').
    """

    def _build_pattern(self, m, old_path: str):
        old_in_json = m.json_string_content(old_path)
        boundary = r'(?=["/]|\\\\|$)' if m.IS_WINDOWS else r'(?=["/]|$)'
        return re.compile(re.escape(old_in_json) + boundary)

    def test_exact_match_replaced(self):
        m = _load_module()
        old_path = r"C:\home\Works\foo"
        new_path = r"D:\new\target"
        new_in_json = m.json_string_content(new_path)
        pattern = self._build_pattern(m, old_path)
        val_str = json.dumps({"cwd": old_path})
        out = pattern.sub(lambda _x, r=new_in_json: r, val_str)
        assert json.loads(out) == {"cwd": new_path}

    def test_subpath_match_replaced(self):
        m = _load_module()
        old_path = r"C:\home\Works\foo"
        new_path = r"D:\new\target"
        new_in_json = m.json_string_content(new_path)
        pattern = self._build_pattern(m, old_path)
        val_str = json.dumps({"cwd": old_path + r"\sub\dir"})
        out = pattern.sub(lambda _x, r=new_in_json: r, val_str)
        assert json.loads(out) == {"cwd": new_path + r"\sub\dir"}

    def test_sibling_not_corrupted(self):
        """A sibling path sharing a prefix must NOT be rewritten."""
        m = _load_module()
        old_path = r"C:\home\Works\foo"
        new_path = r"D:\new\target"
        new_in_json = m.json_string_content(new_path)
        sibling = r"C:\home\Works\foosibling\should-not-change"
        pattern = self._build_pattern(m, old_path)
        val_str = json.dumps({"cwd": old_path, "other": sibling})
        out = pattern.sub(lambda _x, r=new_in_json: r, val_str)
        parsed = json.loads(out)
        assert parsed["cwd"] == new_path
        assert parsed["other"] == sibling


class TestRewritePathsInJsonFile:
    """Round-trip rewrite_paths_in_file against a real JSON file on disk."""

    def test_jsonl_with_backslash_paths(self, tmp_path):
        m = _load_module()
        old_path = r"C:\home\Works\foo"
        new_path = r"D:\new\target"
        old_encoded = m.encode_path(old_path)
        new_encoded = m.encode_path(new_path)

        f = tmp_path / "session.jsonl"
        # Two JSONL records, each carrying a path inside a value. On disk
        # backslashes are JSON-escaped to '\\\\'.
        f.write_text(
            json.dumps({"type": "user", "cwd": old_path}) + "\n"
            + json.dumps({"type": "tool", "file_path": old_path + r"\sub\file.txt"}) + "\n",
            encoding="utf-8",
        )
        # The migration script normally calls this in non-dry-run mode; we
        # poke its globals to simulate that without a full migration.
        m.dry_run = False
        m.backup_dir = str(tmp_path / "_bak")
        os.makedirs(m.backup_dir, exist_ok=True)
        try:
            count = m.rewrite_paths_in_file(
                str(f), old_path, new_path, old_encoded, new_encoded
            )
        finally:
            m.dry_run = False
            m.backup_dir = None
        assert count > 0

        # Re-read and verify each record round-trips with the new path.
        lines = f.read_text(encoding="utf-8").strip().splitlines()
        rec0 = json.loads(lines[0])
        rec1 = json.loads(lines[1])
        assert rec0["cwd"] == new_path
        assert rec1["file_path"] == new_path + r"\sub\file.txt"
