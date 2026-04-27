"""Smoke checks that the JS/CSS rewrite for daemon mode landed.

These do not exercise behavior; they just verify the substrings that
indicate the daemon-mode multi-review rewrite is present, while preserving
the standalone fallback path.
"""

from __future__ import annotations

import importlib.resources


def _read_static(name: str) -> str:
    return (
        importlib.resources.files("serve_review")
        .joinpath("static")
        .joinpath(name)
        .read_text(encoding="utf-8")
    )


def test_app_js_has_daemon_and_standalone_paths() -> None:
    src = _read_static("app.js")
    for needle in (
        "/api/queue",
        "EventSource",
        "review_added",
        "review_decided",
        "review_orphaned",
        "review_removed",
        "standalone",
        "/api/review",
    ):
        assert needle in src, f"missing {needle!r} in app.js"


def test_style_css_has_tab_bar_rules() -> None:
    src = _read_static("style.css")
    for needle in (
        "#tab-bar",
        ".review-tab",
        ".review-tab.active",
        ".review-tab.decided",
        ".review-tab.orphaned",
        "#empty-state",
    ):
        assert needle in src, f"missing {needle!r} in style.css"
