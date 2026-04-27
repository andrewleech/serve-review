"""Shared test fixtures."""

from __future__ import annotations

import pytest

from serve_review.models import (
    AttentionFlag,
    AttentionKind,
    CommitInfo,
    DiffHunk,
    DiffLine,
    FileDiff,
    PushInfo,
    ReviewRequest,
)


@pytest.fixture
def sample_push_info() -> PushInfo:
    return PushInfo(
        local_ref="refs/heads/feature",
        local_sha="a" * 40,
        remote_ref="refs/heads/feature",
        remote_sha="b" * 40,
        remote_name="origin",
        remote_url="git@github.com:user/repo.git",
        is_force_push=True,
    )


@pytest.fixture
def sample_review(sample_push_info: PushInfo) -> ReviewRequest:
    return ReviewRequest(
        push_info=sample_push_info,
        commits=[
            CommitInfo(
                sha="a" * 40,
                message="Add new feature",
                author="Test User",
                date="2024-01-15 10:30:00 +0000",
                body="This adds a new header file.\n\nSigned-off-by: Test User",
                files=["lib/header.h"],
            ),
        ],
        files=[
            FileDiff(
                old_path="a/lib/header.h",
                new_path="b/lib/header.h",
                is_new=True,
                is_deleted=False,
                is_rename=False,
                language="c",
                hunks=[
                    DiffHunk(
                        header="@@ -0,0 +1,5 @@",
                        lines=[
                            DiffLine(
                                line_type="+",
                                content="// Copyright (c) 2024 Fake Name <fake@example.com>",
                                old_line_no=None,
                                new_line_no=1,
                                flags=[
                                    AttentionFlag(
                                        kind=AttentionKind.COPYRIGHT,
                                        start=3,
                                        end=50,
                                        text="Copyright (c) 2024 Fake Name <fake@example.com>",
                                    ),
                                    AttentionFlag(
                                        kind=AttentionKind.EMAIL,
                                        start=34,
                                        end=50,
                                        text="fake@example.com",
                                    ),
                                ],
                            ),
                            DiffLine(
                                line_type="+",
                                content="#ifndef HEADER_H",
                                old_line_no=None,
                                new_line_no=2,
                                flags=[],
                            ),
                            DiffLine(
                                line_type="+",
                                content="#define HEADER_H",
                                old_line_no=None,
                                new_line_no=3,
                                flags=[],
                            ),
                            DiffLine(
                                line_type="+",
                                content="",
                                old_line_no=None,
                                new_line_no=4,
                                flags=[],
                            ),
                            DiffLine(
                                line_type="+",
                                content="#endif",
                                old_line_no=None,
                                new_line_no=5,
                                flags=[],
                            ),
                        ],
                    ),
                ],
            ),
        ],
        has_attention_flags=True,
    )
