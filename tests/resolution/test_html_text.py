from app.resolution.html_text import html_to_text


def test_strips_tags():
    html = "<div><h1>Title</h1><p>Hello <b>world</b></p></div>"
    out = html_to_text(html)
    assert "Title" in out
    assert "Hello world" in out
    assert "<" not in out and ">" not in out


def test_drops_script_and_style():
    html = (
        "<html><head><style>.x{color:red}</style></head>"
        "<body><script>var a=1;</script><p>Real content here</p></body></html>"
    )
    out = html_to_text(html)
    assert "Real content here" in out
    assert "color:red" not in out
    assert "var a=1" not in out


def test_collapses_whitespace():
    html = "<p>line    one</p>\n\n\n<p>line   two</p>"
    out = html_to_text(html)
    assert "line one" in out
    assert "line two" in out
    assert "    " not in out
    assert "\n\n\n" not in out


def test_empty_input():
    assert html_to_text("") == ""
    assert html_to_text(None) == ""
