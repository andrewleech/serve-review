"""Tests for safe PR description Markdown rendering."""

from serve_review.markdown_renderer import render_markdown


def test_raw_html_is_escaped_and_javascript_links_are_rejected() -> None:
    html = render_markdown('<script>alert(1)</script>\n\n[click](javascript:alert(1))')

    assert '<script>' not in html
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in html
    assert 'href="javascript:' not in html
    assert '<a ' not in html


def test_standard_markdown_renders_lists_headings_quotes_and_code() -> None:
    html = render_markdown(
        '# Heading\n\n> quoted text\n\n- first\n- second\n\nInline `code`.\n\n'
        '```python\nprint(1)\n```'
    )

    assert '<h1>Heading</h1>' in html
    assert '<blockquote>' in html and '<p>quoted text</p>' in html
    assert '<ul>' in html and '<li>first</li>' in html and '<li>second</li>' in html
    assert '<code>code</code>' in html
    assert '<pre><code class="language-python">print(1)\n</code></pre>' in html


def test_link_attribute_injection_is_rejected() -> None:
    html = render_markdown('[link](https://example.com/" onmouseover="alert(1))')

    assert "<a " not in html
    assert "&quot;" in html
