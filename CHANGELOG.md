# Changelog

All notable changes to claude-mv are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## [0.9.0]

### Added

- **`--with-history`.** In copy mode, also carries the project's `history.jsonl`
  lines (the reverse-search prompt history) to the new location. It is purely
  *additive*: the source's lines are read, their `project` field is rewritten to the
  new path, and the copies are **appended** to the destination history — originals
  are never modified. Cross-account (`--from-home`) pulls from the source account's
  history; same-account `--copy` duplicates within the one file. Off by default
  (the project's history doesn't carry unless asked), and rejected outside copy mode
  (a move carries history automatically).

### Fixed

- **`--copy` no longer strips the source's history association.** Same-account
  `--copy` previously ran Layer 5's in-place rewrite, which changed the *preserved*
  source project's `history.jsonl` lines to point at the new path — so the source
  you intentionally kept lost its reverse-search history. Copy mode now leaves
  `history.jsonl` untouched by default, and only *adds* lines under `--with-history`.
  Layer 5 is now copy-aware: rewrite-in-place on a move, append-or-skip on a copy.

### Changed

- Version bumped to 0.9.0; `pyproject.toml` and the in-script `VERSION` kept in
  lockstep.

## [0.8.0]

### Added

- **Cross-account copy mode (`--from-home`).** claude-mv can now relocate a
  project's state from *another account's* home into the current account, for the
  cross-user copy case (`/home/alice/Documents/app` →
  `/home/bob/Documents/app`). `--from-home SRC_HOME` reads source state from
  `SRC_HOME/.claude` and `SRC_HOME/.claude.json` and **implies `--copy`** (the
  source is left intact). Two layers gain a source/destination split:
  - **Layer 1** copies the source project directory into the destination
    (`shutil.copy2`, read-only on the source) instead of moving it. Rollback removes
    only what was created in the destination; the source is never touched.
  - **Layer 6** *injects* the source's `~/.claude.json` project entry into the
    destination under the new key. Previously this layer only **renamed** an existing
    key — in a fresh cross-account copy there is no key to rename, so the migrated
    project never got a `.claude.json` entry. Missing source entry is a no-op; a
    missing destination `.claude.json` is created minimally.

  The readability preflight now targets the correct source (source project dir and
  source `.claude.json`), so an unreadable source still fails up front rather than
  silently migrating zero references.
- **`--copy` flag.** Same-account copy: behaves like a move but copies the project
  directory and leaves the source state in place. Suppresses the "old directory
  still exists" warning (for a copy, the source is the intended end state) in favor
  of an informational note.
- **Source-account session warning.** Cross-account runs cannot reliably verify
  whether a live Claude session is running as the *source* user (the active-session
  check only sees the current account, and the source `sessions/` dir may be
  unreadable). claude-mv now emits a best-effort warning — never a block — when it
  finds, or cannot rule out, a source-account session on the project.

### Changed

- Version bumped to 0.8.0; `pyproject.toml` and the in-script `VERSION` kept in
  lockstep.

## [0.7.0]

### Added

- **Readability preflight.** Before mutating anything, claude-mv now scans every
  file it must *read* (project transcripts, subagent metadata, memory files,
  `history.jsonl`, `~/.claude.json`, `.mcp.json`, project settings) for
  readability by the current user. If any are unreadable it aborts up front
  instead of silently leaving stale paths — the rewrite helpers return 0 on a
  read error, so an unreadable transcript would otherwise be migrated to 0
  references with no hard failure. Each unreadable file is classified to give
  *correct* remediation rather than a one-size guess:
  - **mask** — `getfacl` confirms an ACL mask is clamping read off an entry
    (a 0600 create forcing `mask::` to `---` clamps a named-user grant to
    effective `---`). Recommends `setfacl -R -m mask::rwx`, and explains why
    `chmod o+r` is a no-op here (you match the named-user ACL entry, not
    `other`).
  - **mask?** — same mode signature but `getfacl` isn't installed to confirm;
    steers toward `setfacl` and suggests installing the `acl` package.
  - **ownership** — owned by another account; steers to fixing it *as that
    account*.
  - **mode** — you own it but the mode is too restrictive; recommends
    `chmod -R u+rX`.

  This replaces an earlier mode-bit-only heuristic that over-claimed "ACL mask
  clamp" for any group/other read bit. Reading an ACL needs only inode
  metadata, so detection works even on files whose content can't be read. Not
  bypassable by `--force`: an unreadable file can only be mis-migrated.
- **Completeness manifest in `--json`.** The machine-readable report now carries
  a `manifest` array of `{file, refs, bytesBefore, bytesAfter, linesBefore,
  linesAfter}` per rewritten file, so external pre-removal verification is
  trivial and correct. A naive byte-size diff falsely flags data loss because
  path rewrites change file size (usually *growing* it, since the new path is
  typically longer). The robust, direction-independent integrity check is
  **line-count preservation** (`linesAfter == linesBefore`) — paths contain no
  newlines, so rewriting can never add or drop a line — combined with a
  basename-set match. `bytesBefore`/`bytesAfter` are still reported for size
  accounting.

### Changed

- Version bumped to 0.7.0; `pyproject.toml` and the in-script `VERSION` kept in
  lockstep.
