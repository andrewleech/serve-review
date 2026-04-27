"""Git operations for collecting diff and commit information."""

from __future__ import annotations

import subprocess
from typing import TextIO

from unidiff import PatchSet

from serve_review.models import (
    CommitInfo,
    DiffHunk,
    DiffLine,
    FileDiff,
    PushInfo,
    ReviewRequest,
    guess_language,
    scan_attention,
)


def _strip_diff_prefix(path: str) -> str:
    """Strip the a/ or b/ prefix that git diff adds to file paths."""
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def run_git(*args: str) -> str:
    """Run a git command and return stripped stdout."""
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def parse_push_stdin(
    stream: TextIO,
    remote_name: str = "origin",
    remote_url: str = "",
) -> list[PushInfo]:
    """Parse pre-push hook stdin lines into PushInfo objects.

    Git pre-push hook receives lines on stdin in the format:
        <local ref> <local sha> <remote ref> <remote sha>
    The remote_name and remote_url are passed by git as command-line args
    to the hook, and should be forwarded here by the caller.
    """
    pushes: list[PushInfo] = []

    for line in stream:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 4:
            continue

        local_ref, local_sha, remote_ref, remote_sha = parts
        zero_sha = "0" * 40

        # Skip delete pushes
        if local_sha == zero_sha:
            continue

        # Detect force push: if remote_sha is not an ancestor of local_sha
        is_force = False
        if remote_sha != zero_sha:
            result = subprocess.run(
                ["git", "merge-base", "--is-ancestor", remote_sha, local_sha],
                capture_output=True,
            )
            is_force = result.returncode != 0

        pushes.append(
            PushInfo(
                local_ref=local_ref,
                local_sha=local_sha,
                remote_ref=remote_ref,
                remote_sha=remote_sha,
                remote_name=remote_name,
                remote_url=remote_url,
                is_force_push=is_force,
            )
        )

    return pushes


def get_commits(base_sha: str, head_sha: str) -> list[CommitInfo]:
    """Get commits between base and head, including full messages and file lists."""
    zero_sha = "0" * 40
    if base_sha == zero_sha:
        log_range = head_sha
        extra_args = ["--not", "--remotes"]
    else:
        log_range = f"{base_sha}..{head_sha}"
        extra_args = []

    # Pass 1: commit metadata with full body.
    # Use %x00 as field separator within a commit, %x01 as commit separator.
    try:
        output = run_git(
            "log",
            "--format=%x01%H%x00%s%x00%an%x00%ai%x00%B",
            log_range,
            *extra_args,
        )
    except subprocess.CalledProcessError:
        return []

    if not output:
        return []

    # Parse commit metadata
    meta: dict[str, tuple[str, str, str, str]] = {}  # sha -> (subject, author, date, body)
    order: list[str] = []
    for entry in output.split("\x01"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("\x00", 4)
        if len(parts) >= 5:
            sha, subject, author, date, body = parts
            meta[sha] = (subject, author, date, body.strip())
            order.append(sha)

    # Pass 2: per-commit file lists
    file_map: dict[str, list[str]] = {}
    try:
        file_output = run_git(
            "log",
            "--format=%x01%H",
            "--name-only",
            log_range,
            *extra_args,
        )
        for entry in file_output.split("\x01"):
            entry = entry.strip()
            if not entry:
                continue
            lines = entry.split("\n")
            sha = lines[0].strip()
            files = [f for f in lines[1:] if f.strip()]
            if sha:
                file_map[sha] = files
    except subprocess.CalledProcessError:
        pass

    commits: list[CommitInfo] = []
    for sha in order:
        if sha in meta:
            subject, author, date, body = meta[sha]
            commits.append(
                CommitInfo(
                    sha=sha,
                    message=subject,
                    author=author,
                    date=date,
                    body=body,
                    files=file_map.get(sha, []),
                )
            )

    return commits


def get_diff(base_sha: str, head_sha: str) -> list[FileDiff]:
    """Get parsed file diffs between base and head."""
    zero_sha = "0" * 40
    if base_sha == zero_sha:
        # New branch: diff against empty tree
        empty_tree = run_git("hash-object", "-t", "tree", "/dev/null")
        diff_range = [empty_tree, head_sha]
    else:
        diff_range = [base_sha, head_sha]

    try:
        raw_diff = run_git("diff", "--no-color", "-U3", *diff_range)
    except subprocess.CalledProcessError:
        return []

    if not raw_diff:
        return []

    try:
        patch = PatchSet(raw_diff)
    except Exception as exc:
        import sys

        print(f"serve-review: warning: failed to parse diff: {exc}", file=sys.stderr)
        return _fallback_diff(raw_diff, diff_range)

    files: list[FileDiff] = []

    for patched_file in patch:
        hunks: list[DiffHunk] = []
        for hunk in patched_file:
            diff_lines: list[DiffLine] = []
            for line in hunk:
                line_type = " "
                if line.is_added:
                    line_type = "+"
                elif line.is_removed:
                    line_type = "-"

                content = line.value.rstrip("\n")
                flags = scan_attention(content) if line.is_added else []

                diff_lines.append(
                    DiffLine(
                        line_type=line_type,
                        content=content,
                        old_line_no=line.source_line_no,
                        new_line_no=line.target_line_no,
                        flags=flags,
                    )
                )

            hunk_header = (
                f"@@ -{hunk.source_start},{hunk.source_length}"
                f" +{hunk.target_start},{hunk.target_length} @@"
            )
            if hunk.section_header:
                hunk_header += f" {hunk.section_header}"
            hunks.append(DiffHunk(header=hunk_header, lines=diff_lines))

        source = _strip_diff_prefix(patched_file.source_file or "")
        target = _strip_diff_prefix(patched_file.target_file or "")
        # For deleted files target is /dev/null; for new files source is /dev/null.
        # Use whichever is an actual path, preferring target for display.
        display_path = target if target and target != "/dev/null" else source
        files.append(
            FileDiff(
                old_path=source if source != "/dev/null" else display_path,
                new_path=display_path,
                is_new=patched_file.is_added_file,
                is_deleted=patched_file.is_removed_file,
                is_rename=bool(
                    patched_file.source_file
                    and patched_file.target_file
                    and patched_file.source_file != patched_file.target_file
                ),
                hunks=hunks,
                language=guess_language(display_path),
            )
        )

    return files


def _fallback_diff(raw_diff: str, diff_range: list[str]) -> list[FileDiff]:
    """Fallback when unidiff can't parse: show raw diff per file."""
    files: list[FileDiff] = []

    try:
        stat_output = run_git("diff", "--stat", "--name-only", *diff_range)
    except subprocess.CalledProcessError:
        stat_output = ""

    file_paths = [p.strip() for p in stat_output.split("\n") if p.strip()]

    # Split raw diff into per-file chunks
    chunks: dict[str, str] = {}
    current_file = None
    current_lines: list[str] = []
    for line in raw_diff.split("\n"):
        if line.startswith("diff --git"):
            if current_file:
                chunks[current_file] = "\n".join(current_lines)
            parts = line.split(" b/", 1)
            current_file = parts[1] if len(parts) > 1 else None
            current_lines = [line]
        elif current_file is not None:
            current_lines.append(line)
    if current_file:
        chunks[current_file] = "\n".join(current_lines)

    for path in file_paths or list(chunks.keys()):
        raw = chunks.get(path, "")
        diff_lines = []
        for idx, text in enumerate(raw.split("\n")):
            if not text:
                continue
            if text.startswith("+"):
                lt = "+"
            elif text.startswith("-"):
                lt = "-"
            else:
                lt = " "
            diff_lines.append(
                DiffLine(
                    line_type=lt,
                    content=text,
                    old_line_no=None,
                    new_line_no=idx + 1,
                    flags=scan_attention(text) if lt == "+" else [],
                )
            )
        hunks = (
            [DiffHunk(header="(raw diff, parser failed)", lines=diff_lines)] if diff_lines else []
        )
        files.append(
            FileDiff(
                old_path=path,
                new_path=path,
                is_new=False,
                is_deleted=False,
                is_rename=False,
                hunks=hunks,
                language=guess_language(path),
            )
        )

    return files


def build_review_request(push_info: PushInfo) -> ReviewRequest:
    """Build a complete ReviewRequest from push info."""
    commits = get_commits(push_info.remote_sha, push_info.local_sha)
    files = get_diff(push_info.remote_sha, push_info.local_sha)
    has_flags = any(
        flag for f in files for h in f.hunks for line in h.lines for flag in line.flags
    )
    return ReviewRequest(
        push_info=push_info,
        commits=commits,
        files=files,
        has_attention_flags=has_flags,
    )


def build_review_from_refs(base: str, head: str) -> ReviewRequest:
    """Build a ReviewRequest from explicit ref names (for manual invocation).

    Uses merge-base to find the actual fork point, so only commits on the
    branch are shown (not the entire divergence between two refs).
    """
    head_sha = run_git("rev-parse", head)
    raw_base = run_git("rev-parse", base)

    # Find the merge-base (fork point) between base and head.
    # This ensures we only see commits on the branch, not upstream history.
    try:
        base_sha = run_git("merge-base", raw_base, head_sha)
    except subprocess.CalledProcessError:
        base_sha = raw_base

    # Determine current branch and remote info
    try:
        branch = run_git("symbolic-ref", "--short", "HEAD")
    except subprocess.CalledProcessError:
        branch = head_sha

    try:
        remote = run_git("config", f"branch.{branch}.remote")
    except subprocess.CalledProcessError:
        remote = "origin"

    try:
        remote_url = run_git("remote", "get-url", remote)
    except subprocess.CalledProcessError:
        remote_url = ""

    is_force = False
    zero_sha = "0" * 40
    if base_sha != zero_sha:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base_sha, head_sha],
            capture_output=True,
        )
        is_force = result.returncode != 0

    push_info = PushInfo(
        local_ref=f"refs/heads/{branch}",
        local_sha=head_sha,
        remote_ref=f"refs/heads/{branch}",
        remote_sha=base_sha,
        remote_name=remote,
        remote_url=remote_url,
        is_force_push=is_force,
    )

    return build_review_request(push_info)
