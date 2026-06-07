# Changelog

All notable changes to claude-mv are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

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
