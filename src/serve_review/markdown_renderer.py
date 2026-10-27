"""Render PR descriptions as safe Markdown HTML."""

from __future__ import annotations

from typing import cast

from markdown_it import MarkdownIt


def _is_safe_link(url: str) -> bool:
    """Allow relative URLs and links with explicitly safe schemes."""
    if ":" not in url.split("/", 1)[0]:
        return True
    scheme = url.split(":", 1)[0].lower()
    return scheme in {"http", "https", "mailto"}


class SafeMarkdownIt(MarkdownIt):
    def validateLink(self, url: str) -> bool:
        return _is_safe_link(url)


_MARKDOWN = SafeMarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})


def render_markdown(markdown: str) -> str:
    """Render CommonMark without raw HTML or unsafe link protocols."""
    return cast("str", _MARKDOWN.render(markdown))
