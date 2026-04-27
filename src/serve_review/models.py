"""Data models for serve-review."""

from __future__ import annotations

import enum
import hashlib
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


def serialize_for_json(obj: Any) -> Any:
    """Recursively convert enums to their values for JSON serialization.

    ``dataclasses.asdict()`` walks dataclasses and produces nested dicts/lists,
    but leaves Enum instances as-is. This walks the result and unwraps them.
    Public helper because both the standalone server and the daemon need it.
    """
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: serialize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [serialize_for_json(v) for v in obj]
    return obj


class Decision(enum.Enum):
    APPROVE = "approve"
    DENY = "deny"


class AttentionKind(enum.Enum):
    """Kinds of content that should draw reviewer attention."""

    EMAIL = "email"
    URL = "url"
    COPYRIGHT = "copyright"
    AUTHOR = "author"
    LICENSE = "license"
    NAME_ATTRIBUTION = "name_attribution"


@dataclass(frozen=True)
class AttentionFlag:
    """A span in a diff line flagged for reviewer attention."""

    kind: AttentionKind
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    message: str
    author: str
    date: str
    body: str = ""
    files: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DiffLine:
    line_type: str  # "+", "-", " " (context)
    content: str
    old_line_no: int | None
    new_line_no: int | None
    flags: list[AttentionFlag] = field(default_factory=list)


@dataclass(frozen=True)
class DiffHunk:
    header: str
    lines: list[DiffLine]


@dataclass(frozen=True)
class FileDiff:
    old_path: str
    new_path: str
    is_new: bool
    is_deleted: bool
    is_rename: bool
    hunks: list[DiffHunk]
    language: str  # guessed from extension


@dataclass(frozen=True)
class PushInfo:
    local_ref: str
    local_sha: str
    remote_ref: str
    remote_sha: str
    remote_name: str
    remote_url: str
    is_force_push: bool


@dataclass
class ReviewRequest:
    push_info: PushInfo
    commits: list[CommitInfo]
    files: list[FileDiff]
    has_attention_flags: bool = False

    def to_dict(self) -> dict[str, Any]:
        return serialize_for_json(asdict(self))  # type: ignore[no-any-return]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReviewRequest:
        return cls(
            push_info=PushInfo(**data["push_info"]),
            commits=[CommitInfo(**c) for c in data.get("commits", [])],
            files=[_file_diff_from_dict(f) for f in data.get("files", [])],
            has_attention_flags=data.get("has_attention_flags", False),
        )


@dataclass
class ReviewComment:
    body: str
    file: str | None = None
    line: int | None = None


@dataclass
class ReviewDecision:
    decision: Decision
    comments: list[ReviewComment] = field(default_factory=list)
    overall_comment: str = ""

    def to_dict(self) -> dict[str, Any]:
        return serialize_for_json(asdict(self))  # type: ignore[no-any-return]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReviewDecision:
        return cls(
            decision=Decision(data["decision"]),
            overall_comment=data.get("overall_comment", ""),
            comments=[
                ReviewComment(
                    body=c["body"],
                    file=c.get("file"),
                    line=c.get("line"),
                )
                for c in data.get("comments", [])
            ],
        )

    @classmethod
    def from_approve_body(cls, body: dict[str, Any]) -> ReviewDecision:
        """Build an APPROVE decision from an HTTP approve-endpoint body."""
        return cls(
            decision=Decision.APPROVE,
            overall_comment=body.get("overall_comment", ""),
            comments=[
                ReviewComment(
                    body=c["body"],
                    file=c.get("file"),
                    line=c.get("line"),
                )
                for c in body.get("comments", [])
            ],
        )

    @classmethod
    def from_deny_body(cls, body: dict[str, Any]) -> ReviewDecision:
        """Build a DENY decision from an HTTP deny-endpoint body."""
        return cls(
            decision=Decision.DENY,
            overall_comment=body.get("overall_comment", ""),
            comments=[
                ReviewComment(
                    body=c["body"],
                    file=c.get("file"),
                    line=c.get("line"),
                )
                for c in body.get("comments", [])
            ],
        )


@dataclass
class ReviewQueueItem:
    """A review in the daemon's queue. Tracks lifecycle through the status field."""

    id: str
    diff_hash: str
    review: ReviewRequest
    status: str  # "pending", "decided", "orphaned"
    decision: ReviewDecision | None
    submitted_at: float
    decided_at: float | None


@dataclass(frozen=True)
class CachedDecision:
    """A decision persisted to the cache for replay on identical diffs."""

    diff_hash: str
    timestamp: str  # ISO 8601 in UTC
    decision: ReviewDecision
    branch: str  # informational
    remote: str  # informational

    def to_dict(self) -> dict[str, Any]:
        return {
            "diff_hash": self.diff_hash,
            "timestamp": self.timestamp,
            "decision": self.decision.to_dict(),
            "branch": self.branch,
            "remote": self.remote,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CachedDecision:
        return cls(
            diff_hash=data["diff_hash"],
            timestamp=data["timestamp"],
            decision=ReviewDecision.from_dict(data["decision"]),
            branch=data.get("branch", ""),
            remote=data.get("remote", ""),
        )


def _file_diff_from_dict(data: dict[str, Any]) -> FileDiff:
    return FileDiff(
        old_path=data["old_path"],
        new_path=data["new_path"],
        is_new=data["is_new"],
        is_deleted=data["is_deleted"],
        is_rename=data["is_rename"],
        language=data["language"],
        hunks=[
            DiffHunk(
                header=h["header"],
                lines=[
                    DiffLine(
                        line_type=line["line_type"],
                        content=line["content"],
                        old_line_no=line["old_line_no"],
                        new_line_no=line["new_line_no"],
                        flags=[
                            AttentionFlag(
                                kind=AttentionKind(flag["kind"]),
                                start=flag["start"],
                                end=flag["end"],
                                text=flag["text"],
                            )
                            for flag in line.get("flags", [])
                        ],
                    )
                    for line in h["lines"]
                ],
            )
            for h in data["hunks"]
        ],
    )


# --- Diff hashing ---

# Byte separator placed between every field fed to the hash. Without separators,
# concatenating "ab" + "c" hashes the same as "a" + "bc", which would let
# unrelated diffs collide.
_HASH_SEP = b"\x00"


def compute_diff_hash(files: list[FileDiff]) -> str:
    """SHA256 over diff content only, stable across rebases that don't change content.

    Hashes new_path + line_type + content for every line, with a NUL separator
    between every field and a double NUL between files. Excludes commit SHAs,
    timestamps, and push metadata so a rebase that keeps the same final content
    hits the same cache entry as the original review.
    """
    h = hashlib.sha256()
    for f in sorted(files, key=lambda x: x.new_path):
        h.update(f.new_path.encode("utf-8"))
        h.update(_HASH_SEP)
        for hunk in f.hunks:
            for line in hunk.lines:
                h.update(line.line_type.encode("utf-8"))
                h.update(_HASH_SEP)
                h.update(line.content.encode("utf-8"))
                h.update(_HASH_SEP)
        h.update(_HASH_SEP)  # extra separator marks file boundary
    return h.hexdigest()


# --- Attention pattern scanning ---

_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+")
_URL_RE = re.compile(r"https?://[^\s\"'>)\]]+")
_COPYRIGHT_RE = re.compile(r"(?i)\bcopyright\b.*", re.IGNORECASE)
_AUTHOR_RE = re.compile(r"(?i)\b(?:author|written\s+by|maintained\s+by)\b.*")
_LICENSE_KEYWORD_RE = re.compile(
    r"(?i)\b(?:license|licence|licensed|spdx|mit|apache|gpl|bsd|mpl)\b"
)


def scan_attention(line: str) -> list[AttentionFlag]:
    """Scan a diff line for patterns that need reviewer attention."""
    flags: list[AttentionFlag] = []

    for m in _EMAIL_RE.finditer(line):
        flags.append(AttentionFlag(AttentionKind.EMAIL, m.start(), m.end(), m.group()))

    for m in _URL_RE.finditer(line):
        flags.append(AttentionFlag(AttentionKind.URL, m.start(), m.end(), m.group()))

    for m in _COPYRIGHT_RE.finditer(line):
        flags.append(AttentionFlag(AttentionKind.COPYRIGHT, m.start(), m.end(), m.group()))

    for m in _AUTHOR_RE.finditer(line):
        flags.append(AttentionFlag(AttentionKind.AUTHOR, m.start(), m.end(), m.group()))

    for m in _LICENSE_KEYWORD_RE.finditer(line):
        flags.append(AttentionFlag(AttentionKind.LICENSE, m.start(), m.end(), m.group()))

    return flags


# --- Language detection ---

_EXTENSION_LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "jsx",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".md": "markdown",
    ".html": "html",
    ".css": "css",
    ".sql": "sql",
    ".xml": "xml",
    ".cmake": "cmake",
    ".mk": "makefile",
    "Makefile": "makefile",
    "Dockerfile": "dockerfile",
}


def guess_language(path: str) -> str:
    """Guess the Prism.js language class from a file path."""
    import os

    basename = os.path.basename(path)
    if basename in _EXTENSION_LANGUAGES:
        return _EXTENSION_LANGUAGES[basename]
    _, ext = os.path.splitext(path)
    return _EXTENSION_LANGUAGES.get(ext, "plaintext")
