"""Tests for data models and attention scanning."""

from __future__ import annotations

from serve_review.models import (
    AttentionKind,
    Decision,
    ReviewComment,
    ReviewDecision,
    guess_language,
    scan_attention,
)


class TestScanAttention:
    def test_email(self) -> None:
        flags = scan_attention("Author: user@example.com")
        kinds = {f.kind for f in flags}
        assert AttentionKind.EMAIL in kinds
        email_flag = next(f for f in flags if f.kind == AttentionKind.EMAIL)
        assert email_flag.text == "user@example.com"

    def test_url(self) -> None:
        flags = scan_attention("See https://example.com/docs for details")
        kinds = {f.kind for f in flags}
        assert AttentionKind.URL in kinds

    def test_copyright(self) -> None:
        flags = scan_attention("Copyright (c) 2024 Some Person")
        kinds = {f.kind for f in flags}
        assert AttentionKind.COPYRIGHT in kinds

    def test_author_line(self) -> None:
        flags = scan_attention("Author: Jane Doe")
        kinds = {f.kind for f in flags}
        assert AttentionKind.AUTHOR in kinds

    def test_license_keyword(self) -> None:
        flags = scan_attention("# SPDX-License-Identifier: MIT")
        kinds = {f.kind for f in flags}
        assert AttentionKind.LICENSE in kinds

    def test_no_flags_on_plain_code(self) -> None:
        flags = scan_attention("x = foo(bar, baz)")
        assert flags == []

    def test_multiple_flags_same_line(self) -> None:
        flags = scan_attention("Copyright (c) 2024 Foo <foo@bar.com>")
        kinds = {f.kind for f in flags}
        assert AttentionKind.COPYRIGHT in kinds
        assert AttentionKind.EMAIL in kinds

    def test_positions_are_correct(self) -> None:
        line = "email: test@example.org here"
        flags = scan_attention(line)
        email_flag = next(f for f in flags if f.kind == AttentionKind.EMAIL)
        assert line[email_flag.start : email_flag.end] == "test@example.org"


class TestGuessLanguage:
    def test_python(self) -> None:
        assert guess_language("src/module.py") == "python"

    def test_c_header(self) -> None:
        assert guess_language("include/thing.h") == "c"

    def test_makefile(self) -> None:
        assert guess_language("Makefile") == "makefile"

    def test_unknown(self) -> None:
        assert guess_language("file.xyz") == "plaintext"

    def test_nested_path(self) -> None:
        assert guess_language("a/b/c/script.sh") == "bash"


class TestReviewDecision:
    def test_approve_to_dict(self) -> None:
        d = ReviewDecision(decision=Decision.APPROVE)
        result = d.to_dict()
        assert result["decision"] == "approve"
        assert result["comments"] == []

    def test_deny_with_comments(self) -> None:
        d = ReviewDecision(
            decision=Decision.DENY,
            overall_comment="Needs work",
            comments=[
                ReviewComment(body="Wrong name", file="header.h", line=1),
                ReviewComment(body="General issue"),
            ],
        )
        result = d.to_dict()
        assert result["decision"] == "deny"
        assert result["overall_comment"] == "Needs work"
        assert len(result["comments"]) == 2
        assert result["comments"][0]["file"] == "header.h"
        assert result["comments"][1]["file"] is None
