"""Tests for safe PR description Markdown rendering."""

from html.parser import HTMLParser

from serve_review.markdown_renderer import render_markdown


def test_raw_html_is_escaped_and_javascript_links_are_rejected() -> None:
    html = render_markdown("<script>alert(1)</script>\n\n[click](javascript:alert(1))")

    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert 'href="javascript:' not in html
    assert "<a " not in html


def test_standard_markdown_renders_lists_headings_quotes_and_code() -> None:
    html = render_markdown(
        "# Heading\n\n> quoted text\n\n- first\n- second\n\nInline `code`.\n\n"
        "```python\nprint(1)\n```"
    )

    assert "<h1>Heading</h1>" in html
    assert "<blockquote>" in html and "<p>quoted text</p>" in html
    assert "<ul>" in html and "<li>first</li>" in html and "<li>second</li>" in html
    assert "<code>code</code>" in html
    assert '<pre><code class="language-python">print(1)\n</code></pre>' in html


def test_link_attribute_injection_is_rejected() -> None:
    class TagCollector(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.tags: list[tuple[str, list[tuple[str, str | None]]]] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self.tags.append((tag, attrs))

    html = render_markdown('[link](https://example.com/" onmouseover="alert(1))')
    parser = TagCollector()
    parser.feed(html)

    anchors = [attrs for tag, attrs in parser.tags if tag == "a"]
    assert anchors
    assert all(name == "href" for attrs in anchors for name, _ in attrs)
    assert all(value and value.startswith("https://") for attrs in anchors for _, value in attrs)
    assert "&quot;" in html


def test_github_tables_strikethrough_and_task_lists() -> None:
    html = render_markdown(
        "| Status | Item |\n| --- | --- |\n| done | ~~old~~ |\n\n"
        "- [x] Complete\n- [ ] Pending\n  - [X] Nested"
    )

    assert "<table>" in html and "<th>Status</th>" in html
    assert "<td><s>old</s></td>" in html
    assert 'class="contains-task-list"' in html
    assert html.count('type="checkbox"') == 3
    assert html.count('disabled=""') == 3
    assert html.count('checked=""') == 2


def test_github_autolinks_and_alerts() -> None:
    html = render_markdown(
        "Visit https://example.com/path and www.example.org or email user@example.com.\n\n"
        "> [!WARNING]\n> Take **care**."
    )

    assert 'href="https://example.com/path"' in html
    assert 'href="http://www.example.org"' in html
    assert 'href="mailto:user@example.com"' in html
    assert 'class="markdown-alert markdown-alert-warning"' in html
    assert 'class="markdown-alert-title">Warning</p>' in html
    assert "<strong>care</strong>" in html


def test_unsafe_links_images_and_html_remain_inert_in_github_content() -> None:
    html = render_markdown(
        "| link | image |\n| --- | --- |\n"
        "| [bad](javascript:alert(1)) | ![bad](data:text/html,evil) |\n\n"
        "- [x] <img src=x onerror=alert(1)>\n\n"
        "> [!NOTE]\n> <script>alert(1)</script>"
    )

    assert 'href="javascript:' not in html
    assert 'src="data:' not in html
    assert "<script>" not in html and "<img " not in html
    assert "&lt;script&gt;" in html and "&lt;img " in html


def test_mermaid_fence_keeps_escaped_source_and_language() -> None:
    html = render_markdown('```mermaid\ngraph TD\n  A["<script>"] --> B\n```')

    assert (
        '<pre><code class="language-mermaid">'
        "graph TD\n  A[&quot;&lt;script&gt;&quot;] --&gt; B\n</code></pre>"
    ) in html


def test_relative_and_safe_links_remain_available() -> None:
    html = render_markdown(
        "[relative](../file) [fragment](#section) "
        "[https](https://example.com) [email](mailto:user@example.com)"
    )

    assert 'href="../file"' in html
    assert 'href="#section"' in html
    assert 'href="https://example.com"' in html
    assert 'href="mailto:user@example.com"' in html
