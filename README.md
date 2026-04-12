# claude-mv

Update Claude Code's internal state when a project directory moves.

When you `mv` or rename a project directory, Claude Code loses track of your
sessions, memory, and settings because its internal state is keyed to the
absolute path. `claude-mv` rewrites all 9 layers of path-dependent state so
that `claude --continue` works seamlessly from the new location.

This solves [anthropics/claude-code#1516](https://github.com/anthropics/claude-code/issues/1516).

## Installation

claude-mv is a single Python script with no dependencies beyond Python 3.11+.

```bash
# Download the script
curl -fsSL https://raw.githubusercontent.com/justinstimatze/claude-mv/main/claude-mv -o claude-mv
chmod +x claude-mv

# Move it somewhere on your PATH
mv claude-mv ~/.local/bin/
```

## Usage

First, move your project directory with `mv`, then run `claude-mv` to update
Claude Code's state:

```bash
# Move the directory yourself
mv ~/projects/myapp ~/work/myapp

# Then update Claude Code's state
claude-mv ~/projects/myapp ~/work/myapp
```

### Auto-detect old path

If you only provide the new path, claude-mv will scan Claude Code's state to
find the old path automatically:

```bash
claude-mv ~/work/myapp
```

### Dry run

Preview what would change without modifying anything:

```bash
claude-mv ~/projects/myapp ~/work/myapp --dry-run
```

### JSON output

Get machine-readable output on stdout (logs still go to stderr):

```bash
claude-mv ~/projects/myapp ~/work/myapp --json
```

### Force mode

Proceed even if active Claude Code sessions are detected:

```bash
claude-mv ~/projects/myapp ~/work/myapp --force
```

## What It Covers

claude-mv updates all 9 layers of Claude Code's path-dependent state:

1. **Project directory** -- `~/.claude/projects/<encoded>/` rename and deep merge
2. **JSONL transcripts** -- `cwd` and `file_path` fields in session logs
3. **Subagent transcripts** -- stale paths in subagent `.jsonl` and `.meta.json` files
4. **Memory files** -- path references in `memory/` content
5. **Global history** -- `project` field in `~/.claude/history.jsonl`
6. **Claude config** -- project key in the `projects` object of `~/.claude.json`
7. **MCP config** -- absolute paths in `<project>/.mcp.json`
8. **Project settings** -- path refs in `.claude/settings.json` and `.claude/settings.local.json`
9. **Session tracking** -- stale `cwd` in `~/.claude/sessions/*.json`

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| `0`  | Success -- state updated |
| `1`  | Fatal error |
| `2`  | No-op -- nothing to change |
| `3`  | Active session detected (use `--force` to override) |
| `4`  | Error occurred, rolled back to pre-migration state |

## How It Works

1. **Backup** -- Before any changes, all files that will be modified are copied
   to a timestamped backup directory.
2. **Atomic writes** -- Each file is written to a temporary location and renamed
   into place, so a crash mid-write never leaves a half-written file.
3. **Rollback** -- If any layer fails or the process is interrupted
   (SIGINT/SIGTERM), all changes are reverted from the backup automatically.
4. **Verification** -- After migration, a residual-path scan checks that no
   old-path references remain.

## License

[MIT](LICENSE)
