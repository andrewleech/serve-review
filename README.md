# serve-review

A pre-push review gate that blocks `git push` until a human approves the diff in a mobile-friendly web UI.

## Why this exists

I had an AI coding agent fabricate a copyright attribution in a commit that got pushed to a public upstream repo under my name. The maintainer asked "Who is Blake Garner??" and trust was damaged. The agent had invented the name from nothing, buried it in a license header, and I'd approved the push without reading the diff line-by-line.

Claude Code's permission prompt gives you a chance to approve or deny a `git push`, but it doesn't show you the actual diff. You're approving blind. This tool fills that gap by serving a proper diff viewer on a static port so you can review on your phone (or any device on your network) before the push goes through.
