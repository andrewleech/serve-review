---
name: serve-review
description: Launch a serve-review web UI code review gate. This skill should be used when the user asks to "serve a review", "launch a serve-review", "local code review", "review before pushing", or any variant of starting a web-based local code review session.
---

# serve-review

## Tool disambiguation (read first)

Three similarly-named tools exist - pick the right one:

| Tool | What it is | Agent-launchable? |
|------|-----------|-------------------|
| `serve-review` CLI | Web UI review gate. **This is what "serve a review" means.** | Yes |
| `git-review` CLI | Terminal TUI reviewer | No - needs a real TTY; errors with "No such device or address" from a non-interactive shell |
| `git-serve-review` claude-net agent | Owns the tool / handles bugs | No - message it only to report bugs, not to launch reviews |

**To launch a review: use the `serve-review` CLI directly. Never use `git-review`. Never message the `git-serve-review` agent to do this.**

## Agent launch pattern

`serve-review` blocks in the foreground and prints the review URL to **stderr** on startup. To use it as an agent:

1. Run it as a managed background task with stderr captured
2. Read the URL from the captured output
3. Hand the URL to the user and wait for their decision

```bash
# The tool prints to stderr: "Review: https://carbon.story-kettle.ts.net:8567"
# Capture that, give it to the user, then wait for the process to exit.
```

Note: `--help` says "do not background" - that warning is about naive `&` which drops the URL. Capturing stderr in a managed background task is the correct approach.

## Scoping the review range

```bash
# Default: current branch vs its upstream
serve-review

# Explicit range, served through the shared daemon
serve-review --base <ref> --head <ref>

# Review one commit
serve-review --base <commit>^ --head <commit>

# Include a PR description from a Markdown file
serve-review --base origin/main --head HEAD --pr-description-file description.md

# Or pass the description directly
serve-review --base origin/main --head HEAD --pr-description "## Summary: short description"
```

The daemon uses the explicit base and head refs. `--head` can name a branch or commit that is not checked out.
Use `--pr-description-file` to review a PR body with the diff, or pass its Markdown directly with `--pr-description`. The description appears at the top of the review and accepts a separate comment. GitHub-style Markdown and Mermaid code fences render in the description; invalid diagrams display their source.

## Reading the verdict

- **Exit 0** = approved, push can proceed
- **Exit 1** = changes requested / denied

On denial, **stdout** contains JSON with the reviewer's comments:

```json
{
  "decision": "deny",
  "overall_comment": "...",
  "comments": [
    {"body": "...", "file": "path/to/file.py", "line": 42},
    {"body": "...", "file": null, "line": null, "section": "pr_description"}
  ]
}
```

Parse this JSON to know exactly what to fix.

## Workflow

1. Make commits locally
2. Run `serve-review` (scoped as needed)
3. User reviews at the URL - approves or requests changes
4. If denied: read the JSON comments, fix the issues, re-run serve-review
5. On approval: `git push`
