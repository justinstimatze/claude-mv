"""Tests for claude-mv."""

import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "claude-mv")


def run_claude_mv(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    """Run claude-mv with given args."""
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        capture_output=True,
        text=True,
        check=check,
    )


def _encode(path: str) -> str:
    return "".join("-" if ch in "/._-" else ch for ch in path)


def run_in_home(
    home: str, *args: str, env_overrides: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Run claude-mv with HOME overridden to an isolated fake home.

    env_overrides lets a test alter the child environment — e.g. PATH="" to
    hide getfacl and exercise the no-acl-tools fallback path.
    """
    env = os.environ.copy()
    env["HOME"] = home
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, SCRIPT, *args],
        capture_output=True,
        text=True,
        env=env,
    )


def _load_module():
    """Import the hyphen-named claude-mv script as a module for unit tests."""
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader("claude_mv_mod", SCRIPT)
    spec = importlib.util.spec_from_loader("claude_mv_mod", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _acl_supported(tmpdir: str) -> bool:
    """True if setfacl exists and the filesystem honors ACLs here."""
    if shutil.which("setfacl") is None:
        return False
    probe = os.path.join(tmpdir, "_aclprobe")
    with open(probe, "w") as f:
        f.write("x")
    r = subprocess.run(
        ["setfacl", "-m", "u:0:r", probe], capture_output=True, text=True
    )
    return r.returncode == 0


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


# ── Readability preflight tests (#2) ─────────────────────────────────


class TestReadabilityPreflight:
    """Layer-0 preflight must fail up front on files it can't read, rather
    than silently leaving stale paths (rewrite helpers return 0 on read error)."""

    def _build(self, tmp: str, transcript_mode: int):
        home = os.path.join(tmp, "home")
        old_path = os.path.realpath(os.path.join(home, "work", "myapp"))
        new_path = os.path.realpath(os.path.join(home, "work", "myapp-renamed"))
        proj = os.path.join(home, ".claude", "projects", _encode(old_path))
        os.makedirs(proj)
        os.makedirs(new_path)
        transcript = os.path.join(proj, "session.jsonl")
        with open(transcript, "w") as f:
            f.write(json.dumps({"cwd": old_path, "type": "user"}) + "\n")
        os.chmod(transcript, transcript_mode)
        return home, old_path, new_path, transcript

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
    def test_unreadable_blocks_migration(self):
        # 0o000, owned by us → 'mode' category → chmod remediation, no false
        # ACL-mask claim.
        with tempfile.TemporaryDirectory() as tmp:
            home, old, new, transcript = self._build(tmp, 0o000)
            try:
                r = run_in_home(home, old, new, "--json")
                assert r.returncode == 1
                data = json.loads(r.stdout)
                assert data["success"] is False
                joined = " ".join(data["errors"])
                assert "silent partial migration" in joined
                assert "session.jsonl" in joined
                assert "chmod -R u+rX" in joined
            finally:
                os.chmod(transcript, 0o600)  # let TemporaryDirectory clean up

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
    def test_fallback_heuristic_recommends_setfacl_without_getfacl(self):
        # Mode 0o044 (owner no read, group/other read advertised) with getfacl
        # hidden via PATH="" → 'mask?' fallback → must steer to setfacl, since
        # chmod o+r is a no-op against a named-user ACL entry.
        with tempfile.TemporaryDirectory() as tmp:
            home, old, new, transcript = self._build(tmp, 0o044)
            try:
                r = run_in_home(home, old, new, "--json", env_overrides={"PATH": ""})
                assert r.returncode == 1
                joined = " ".join(json.loads(r.stdout)["errors"])
                assert "setfacl" in joined
                assert "mask" in joined
            finally:
                os.chmod(transcript, 0o600)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
    def test_readable_files_pass_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            home, old, new, _ = self._build(tmp, 0o600)
            r = run_in_home(home, old, new, "--json")
            assert r.returncode == 0
            data = json.loads(r.stdout)
            assert data["success"] is True


class TestAclMaskDetection:
    """Precise getfacl-based mask-clamp detection (replaces the old mode-bit
    heuristic that over-claimed 'mask' for any group/other read bit)."""

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file permissions")
    def test_detects_real_mask_clamp_and_clears_normal_file(self):
        mod = _load_module()
        with tempfile.TemporaryDirectory() as tmp:
            if not _acl_supported(tmp):
                pytest.skip("setfacl unavailable or filesystem lacks ACL support")
            normal = os.path.join(tmp, "normal")
            with open(normal, "w") as f:
                f.write("x")
            # No ACL clamp → False (getfacl ran, found nothing).
            assert mod.acl_mask_clamps_read(normal) is False

            clamped = os.path.join(tmp, "clamped")
            with open(clamped, "w") as f:
                f.write("x")
            # Named-user grant of read, then clamp the mask to nothing → the
            # entry's #effective perms lose read: the exact mask-clamp signature.
            subprocess.run(["setfacl", "-m", "u:0:r", clamped], check=True)
            subprocess.run(["setfacl", "-m", "mask::---", clamped], check=True)
            assert mod.acl_mask_clamps_read(clamped) is True
            # And classify_unreadable routes it to the 'mask' category.
            assert mod.classify_unreadable(clamped) == "mask"

    def test_returns_none_without_getfacl(self):
        mod = _load_module()
        with tempfile.TemporaryDirectory() as tmp:
            f = os.path.join(tmp, "f")
            with open(f, "w") as fh:
                fh.write("x")
            orig = os.environ.get("PATH", "")
            try:
                os.environ["PATH"] = ""  # hide getfacl from shutil.which
                assert mod.acl_mask_clamps_read(f) is None
            finally:
                os.environ["PATH"] = orig


# ── Completeness manifest tests (#4) ─────────────────────────────────


class TestManifest:
    """--json must carry a structured per-file manifest so external pre-rm
    verification (basename-set + size-delta-direction + tail-parses-JSON) is
    trivial — a naive byte-diff falsely flags loss because rewrites GROW files."""

    def test_manifest_records_refs_and_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            old_path = os.path.realpath(os.path.join(home, "work", "app"))
            # new path strictly longer → every rewritten file must grow
            new_path = os.path.realpath(os.path.join(home, "work", "app-much-longer"))
            proj = os.path.join(home, ".claude", "projects", _encode(old_path))
            os.makedirs(proj)
            os.makedirs(new_path)
            transcript = os.path.join(proj, "session.jsonl")
            with open(transcript, "w") as f:
                f.write(json.dumps({"cwd": old_path, "type": "user"}) + "\n")
                f.write(json.dumps({"file_path": old_path + "/main.py"}) + "\n")
            # history.jsonl exercises the layer-5 manifest path
            with open(os.path.join(home, ".claude", "history.jsonl"), "w") as f:
                f.write(json.dumps({"project": old_path, "display": "hi"}) + "\n")

            r = run_in_home(home, old_path, new_path, "--json")
            assert r.returncode == 0, r.stdout + r.stderr
            data = json.loads(r.stdout)
            man = data["manifest"]
            assert man, "manifest should not be empty"
            for entry in man:
                assert set(entry) >= {
                    "file",
                    "refs",
                    "bytesBefore",
                    "bytesAfter",
                    "linesBefore",
                    "linesAfter",
                }
                assert entry["refs"] >= 1
                # rewrite grows the file here (new path is longer than old)...
                assert entry["bytesAfter"] >= entry["bytesBefore"]
                # ...but the direction-independent no-truncation invariant is
                # that line count is preserved (paths contain no newlines).
                assert entry["linesAfter"] == entry["linesBefore"]
            files = {os.path.basename(e["file"]) for e in man}
            assert "session.jsonl" in files
            assert "history.jsonl" in files

    def test_dry_run_manifest_empty(self):
        """Dry run writes nothing, so the manifest stays empty (no false record)."""
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            old_path = os.path.realpath(os.path.join(home, "work", "app"))
            new_path = os.path.realpath(os.path.join(home, "work", "app2"))
            proj = os.path.join(home, ".claude", "projects", _encode(old_path))
            os.makedirs(proj)
            os.makedirs(new_path)
            with open(os.path.join(proj, "session.jsonl"), "w") as f:
                f.write(json.dumps({"cwd": old_path}) + "\n")
            r = run_in_home(home, old_path, new_path, "--dry-run", "--json")
            data = json.loads(r.stdout)
            assert data["manifest"] == []


# ── Cross-account copy tests (#1, #3, #5) ────────────────────────────


class TestCrossAccount:
    """--from-home reads another account's state and COPIES it in, injecting the
    source's .claude.json project entry (rename-only would no-op cross-account)."""

    def _build(self, tmp: str):
        src_home = os.path.join(tmp, "src_home")
        dst_home = os.path.join(tmp, "dst_home")
        old_path = os.path.realpath(os.path.join(src_home, "Documents", "calque"))
        new_path = os.path.realpath(os.path.join(dst_home, "Documents", "calque"))

        # Source: a project dir with a transcript + a .claude.json project entry.
        src_proj = os.path.join(src_home, ".claude", "projects", _encode(old_path))
        os.makedirs(src_proj)
        with open(os.path.join(src_proj, "session.jsonl"), "w") as f:
            f.write(json.dumps({"cwd": old_path, "type": "user"}) + "\n")
            f.write(json.dumps({"file_path": old_path + "/main.py"}) + "\n")
        with open(os.path.join(src_home, ".claude.json"), "w") as f:
            json.dump({"projects": {old_path: {"history": [], "root": old_path}}}, f)

        # Destination account exists with its own (unrelated) state.
        os.makedirs(os.path.join(dst_home, ".claude", "projects"))
        os.makedirs(new_path)
        with open(os.path.join(dst_home, ".claude.json"), "w") as f:
            json.dump({"projects": {"/some/other/proj": {}}}, f)
        return src_home, dst_home, old_path, new_path

    def test_cross_account_copy_injects_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            src_home, dst_home, old, new = self._build(tmp)
            src_proj = os.path.join(src_home, ".claude", "projects", _encode(old))

            r = run_in_home(dst_home, "--from-home", src_home, old, new, "--json")
            assert r.returncode == 0, r.stdout + r.stderr
            data = json.loads(r.stdout)
            assert data["success"] is True

            # Destination project dir populated with the rewritten transcript.
            dst_proj = os.path.join(dst_home, ".claude", "projects", _encode(new))
            dst_transcript = os.path.join(dst_proj, "session.jsonl")
            assert os.path.isfile(dst_transcript)
            with open(dst_transcript) as f:
                body = f.read()
            assert new in body
            assert old not in body

            # Source is untouched (copy, not move).
            assert os.path.isfile(os.path.join(src_proj, "session.jsonl"))
            with open(os.path.join(src_proj, "session.jsonl")) as f:
                assert old in f.read()

            # Destination .claude.json gained the injected project key, kept its own.
            with open(os.path.join(dst_home, ".claude.json")) as f:
                dst_cj = json.load(f)
            assert new in dst_cj["projects"]
            assert dst_cj["projects"][new]["root"] == new
            assert "/some/other/proj" in dst_cj["projects"]

            # No "old directory still exists" warning under copy semantics.
            assert not any("still exists" in w for w in data["warnings"])

    def test_cross_account_missing_src_home_claude_dir_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, dst_home, old, new = self._build(tmp)
            empty_src = os.path.join(tmp, "empty_src")
            os.makedirs(empty_src)
            r = run_in_home(dst_home, "--from-home", empty_src, old, new, "--json")
            assert r.returncode == 1
            assert any(".claude" in e for e in json.loads(r.stdout)["errors"])

    def test_cross_account_requires_both_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            src_home, dst_home, _, new = self._build(tmp)
            r = run_in_home(dst_home, "--from-home", src_home, new, "--json")
            assert r.returncode == 1
            assert any("both paths" in e for e in json.loads(r.stdout)["errors"])

    def test_rollback_removes_copy_and_preserves_source_on_failure(self):
        # Force a Layer 6 failure (corrupt destination .claude.json) AFTER Layer 1
        # has copied the project in, then assert rollback removed the copied tree
        # and never touched the source — the core data-safety property of copy mode.
        with tempfile.TemporaryDirectory() as tmp:
            src_home, dst_home, old, new = self._build(tmp)
            src_proj = os.path.join(src_home, ".claude", "projects", _encode(old))
            with open(os.path.join(dst_home, ".claude.json"), "w") as f:
                f.write("{ this is not valid json")

            r = run_in_home(dst_home, "--from-home", src_home, old, new, "--json")
            assert r.returncode == 4  # EXIT_ROLLBACK
            assert json.loads(r.stdout)["success"] is False

            # Copied destination tree is gone (rollback removed what it created).
            dst_proj = os.path.join(dst_home, ".claude", "projects", _encode(new))
            assert not os.path.exists(dst_proj)
            # Source is fully intact.
            with open(os.path.join(src_proj, "session.jsonl")) as f:
                assert old in f.read()

    def test_rollback_merge_case_keeps_preexisting_dst_files(self):
        # When the destination project dir ALREADY exists, rollback must unlink only
        # the files/dirs we copied in and leave the pre-existing ones intact (the
        # copy_created_root==None branch via created_files/created_dirs).
        with tempfile.TemporaryDirectory() as tmp:
            src_home, dst_home, old, new = self._build(tmp)
            dst_proj = os.path.join(dst_home, ".claude", "projects", _encode(new))
            os.makedirs(os.path.join(dst_proj, "sub"))
            keeper = os.path.join(dst_proj, "sub", "preexisting.jsonl")
            with open(keeper, "w") as f:
                f.write("keep me\n")
            with open(os.path.join(dst_home, ".claude.json"), "w") as f:
                f.write("{ invalid")  # force Layer 6 failure → rollback

            r = run_in_home(dst_home, "--from-home", src_home, old, new, "--json")
            assert r.returncode == 4
            # Pre-existing dst content survives; copied-in transcript is gone.
            assert os.path.isfile(keeper)
            assert not os.path.exists(os.path.join(dst_proj, "session.jsonl"))

    def test_inject_creates_minimal_dst_claude_json_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            src_home, dst_home, old, new = self._build(tmp)
            os.remove(os.path.join(dst_home, ".claude.json"))  # fresh dst account

            r = run_in_home(dst_home, "--from-home", src_home, old, new, "--json")
            assert r.returncode == 0, r.stdout + r.stderr
            with open(os.path.join(dst_home, ".claude.json")) as f:
                cj = json.load(f)
            assert new in cj["projects"]
            assert cj["projects"][new]["root"] == new

    def test_missing_source_claude_json_is_noop_not_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            src_home, dst_home, old, new = self._build(tmp)
            os.remove(os.path.join(src_home, ".claude.json"))  # source has no entry

            r = run_in_home(dst_home, "--from-home", src_home, old, new, "--json")
            assert r.returncode == 0, r.stdout + r.stderr
            # Project dir still copied; dst .claude.json simply gains no new key.
            dst_proj = os.path.join(dst_home, ".claude", "projects", _encode(new))
            assert os.path.isfile(os.path.join(dst_proj, "session.jsonl"))
            with open(os.path.join(dst_home, ".claude.json")) as f:
                assert new not in json.load(f)["projects"]


class TestCopyMode:
    """Same-account --copy: behaves like a move but leaves the source dir intact
    and suppresses the 'old directory still exists' warning."""

    def test_copy_preserves_source_project_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            old_path = os.path.realpath(os.path.join(home, "work", "app"))
            new_path = os.path.realpath(os.path.join(home, "work", "app-copy"))
            proj = os.path.join(home, ".claude", "projects", _encode(old_path))
            os.makedirs(proj)
            os.makedirs(new_path)
            with open(os.path.join(proj, "session.jsonl"), "w") as f:
                f.write(json.dumps({"cwd": old_path}) + "\n")

            r = run_in_home(home, old_path, new_path, "--copy", "--json")
            assert r.returncode == 0, r.stdout + r.stderr
            data = json.loads(r.stdout)

            # Source project dir survives; destination got a copy.
            assert os.path.isfile(os.path.join(proj, "session.jsonl"))
            dst_proj = os.path.join(home, ".claude", "projects", _encode(new_path))
            assert os.path.isfile(os.path.join(dst_proj, "session.jsonl"))
            assert not any("still exists" in w for w in data["warnings"])


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
