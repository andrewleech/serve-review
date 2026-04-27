"""Data models for serve-review."""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Any


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
        return {
            "decision": self.decision.value,
            "overall_comment": self.overall_comment,
            "comments": [{"body": c.body, "file": c.file, "line": c.line} for c in self.comments],
        }


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
