# Bootstrap: Making Project Relocation a First-Class Operation in Claude Code

## Problem Statement

Claude Code stores project state (session transcripts, memory, settings, tool permissions) keyed by the absolute filesystem path of the working directory. When a project moves, everything breaks. There is no indirection — the path *is* the identity.

This is the root cause of anthropics/claude-code#1516, 11 community tools that each fix a subset of the breakage, and at least one documented incident (the Winze postmortem) where memory files were silently destroyed mid-session.

## What Actually Breaks (8 Layers)

Field audit from a real ~/.claude directory with 51 projects:

| Layer | Location | What's path-dependent | Breaks how |
|-------|----------|----------------------|------------|
| 1 | `~/.claude/projects/<encoded>/` | Directory name (path with `/` and `.` replaced by `-`) | Sessions not found |
| 2 | `*.jsonl` files | `"cwd"` field on every message | `--continue` / `--resume` fails to match |
| 3 | `*.jsonl` files | `"file_path"` in tool calls | Read/Edit tool results reference dead paths |
| 4 | `*.jsonl` subagent dirs | `*.jsonl` and `*.meta.json` inside `<session-uuid>/subagents/` | Subagent transcripts have stale paths |
| 5 | `memory/` dir | Memory file content may reference paths | Memory gives stale guidance |
| 6 | `~/.claude/history.jsonl` | `"project"` field per entry | History/resume picker shows orphaned entries |
| 7 | `~/.claude.json` | Project key in `"projects"` object (literal absolute path); `allowedTools`, `mcpServers`, `enabledMcpjsonServers` can contain paths | Tool permissions lost, MCP servers misconfigured |
| 8 | `<project>/.mcp.json`, `.claude/settings.json`, `.claude/settings.local.json` | Absolute paths in commands, env vars, permissions | MCP servers fail to start, permissions don't apply |

### The Winze Incident (2026-04-12)

An `mv` was run from within a Claude Code session. The sandbox's cwd was destroyed. Claude Code responded by silently recreating the project directory with a fresh transcript — **overwriting the memory directory** that contained 16 files. The user then manually renamed the (now memory-less) project directory to the new encoded path, completing the data loss. Memory was reconstructed from session context but the original phrasing was gone.

Key failure modes that no community tool handles:
1. **Mid-session moves destroy the sandbox cwd.** All subsequent tool calls fail. Claude Code may silently recreate project state, clobbering existing data.
2. **Race between old and new project directories.** If Claude Code creates a fresh dir at the new encoded path before the user migrates the old one, the memory directory doesn't transfer.
3. **Dot-encoding ambiguity.** Both `/` and `.` become `-`, so `github.com/foo` and `github/com/foo` encode identically. No tool checks for collisions.

## What a Green-Field Solution Looks Like

### Core Principle: Separate Identity from Location

Introduce a **project ID** (UUID) as the primary key for all project state. The filesystem path becomes a mutable attribute of the project, not its identity.

```
~/.claude/projects/
  <project-uuid>/
    project.json        # { "id": "...", "paths": ["/current/path"], "pathHistory": [...] }
    memory/
    <session-uuid>.jsonl
```

A lookup index maps paths to project IDs:

```
~/.claude/project-index.json
{
  "/home/user/Documents/myapp": "a1b2c3d4-...",
  "/home/user/Documents/town/myapp": "a1b2c3d4-..."   // same project, moved
}
```

### The `project mv` Operation

Whether exposed as `claude project mv`, an MCP tool, or a skill, the operation needs to be atomic and observable:

```
claude project mv <old-path> <new-path>

1. Verify new-path exists and is a plausible project root
2. Update project-index.json (old-path -> project-id, add new-path -> same project-id)
3. Update project.json paths array
4. Rewrite cwd/file_path in transcripts (or mark old-path as alias)
5. Update ~/.claude.json project key
6. Update history.jsonl entries
7. Optionally update .mcp.json if it contains old-path references
8. Report what changed
```

Importantly: **no data needs to move on disk.** The project directory stays where it is. Only the index changes. This is why the current design is so fragile — renaming a directory is inherently non-atomic and leaves a window where Claude Code can recreate state at the old or new path.

### Path Aliasing Instead of Rewriting

Transcript rewriting (sed over multi-megabyte JSONL files) is slow and risks corruption. An alternative:

```json
// project.json
{
  "id": "a1b2c3d4-...",
  "currentPath": "/home/user/new/location",
  "pathAliases": ["/home/user/old/location"]
}
```

When loading transcripts, Claude Code resolves any `cwd` or `file_path` that matches a known alias to the current path. No file modification needed. Old transcripts remain forensically intact.

### Mid-Session Safety

The Winze incident shows that `mv` during an active session is the most dangerous case. The solution:

1. **Detect cwd invalidation.** If the working directory ceases to exist, surface an error to the user rather than silently recreating project state.
2. **Lock the project directory during active sessions.** Use a lockfile or advisory lock to prevent concurrent modification.
3. **Refuse to `project mv` if a session is active.** Or: notify the active session that its cwd has changed and let it reattach.

### MCP Tool Design

```typescript
// claude-project-mv MCP server
tools: [
  {
    name: "project_mv",
    description: "Relocate a Claude Code project to a new directory path, preserving all state",
    inputSchema: {
      type: "object",
      properties: {
        oldPath: { type: "string", description: "Current/former project directory (absolute path)" },
        newPath: { type: "string", description: "New project directory (absolute path, must exist)" },
        dryRun:  { type: "boolean", default: false }
      },
      required: ["oldPath", "newPath"]
    }
  },
  {
    name: "project_ls",
    description: "List all known projects and their paths",
    inputSchema: { type: "object", properties: {} }
  },
  {
    name: "project_unlink",
    description: "Remove a path association without deleting project data",
    inputSchema: {
      type: "object",
      properties: {
        path: { type: "string" }
      },
      required: ["path"]
    }
  }
]
```

### Skill Design (Alternative)

A `/project-mv` skill could work within the current architecture without requiring changes to Claude Code's internals:

```
/project-mv ~/old/path ~/new/path

Skill steps:
1. Verify new path exists
2. Backup old project dir (cp -r)  
3. Rename project dir under ~/.claude/projects/
4. Rewrite JSONL files (cwd, file_path, encoded path refs)
5. Update ~/.claude.json project key (python3 JSON manipulation)
6. Update history.jsonl
7. Update .mcp.json if present
8. Verify: claude --continue works from new path
9. Report changes
```

This has the advantage of working today, without waiting for Claude Code to adopt project IDs. The disadvantage is that it's still doing string replacement on JSONL files, which is fundamentally fragile.

## Migration Path

Getting from the current architecture to the ideal one:

### Phase 1: Ship a `/project-mv` skill (today, no Claude Code changes)
- Wraps the `claude-mv` bash script
- Handles all 8 layers
- Users can install it immediately

### Phase 2: Add project-index.json to Claude Code
- Introduce UUIDs as primary keys
- Build the index from existing encoded directories on first run
- Path lookups go through the index
- Encoded directory names become an implementation detail, not the API

### Phase 3: Path aliasing
- Stop rewriting JSONL files entirely
- Resolve old paths at read time via aliases
- Makes `project mv` an O(1) index update instead of O(n) file rewrite

### Phase 4: Mid-session relocation
- Detect cwd invalidation
- Allow live reattach to new path
- Advisory locking to prevent data races

## Why This Hasn't Been Built

From the GitHub issue history:
- The issue (anthropics/claude-code#1516) was auto-closed after 60 days with zero Anthropic engagement
- 11 community members independently built partial solutions
- Each one reverse-engineered a different subset of the 8 layers
- The two most complete tools (ccmv in Rust, claude-code-project-mover-py) together cover all layers but neither one alone does
- The problem feels simple ("just rename a directory") but the actual surface area is large and poorly documented

The encoded-path-as-identity design is load-bearing throughout Claude Code's persistence layer. Fixing it properly requires changing how projects are identified internally, which is a significant architectural change. The community tools are all workarounds that operate on the current architecture's assumptions.

## References

- anthropics/claude-code#1516 — original feature request (auto-closed)
- [Winze repo move postmortem](/tmp/winze-repo-move-postmortem-2026-04-12.md) — documented data loss incident
- `seflue/ccmv` (Rust) — most comprehensive community tool for local moves
- `arak-git/claude-code-project-mover-py` (Python) — only tool that patches Layer 1 session metadata
- `Mahiler1909/claudepath` (PyPI) — best-packaged community tool
- `~/Documents/claude-mv` — bash script covering all 8 layers for this user's environment
