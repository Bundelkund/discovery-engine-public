import re
from html.parser import HTMLParser

_DROP_TAGS = {"script", "style", "noscript", "head", "svg"}
_BLOCK_TAGS = {
    "p", "br", "div", "li", "ul", "ol", "tr", "td", "th", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header",
    "footer", "blockquote", "pre",
}


class _TextExtractor(HTMLParser):
    """Strip tags to plain text. Drops script/style content, inserts a
    newline at block boundaries so words don't run together."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _DROP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _DROP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(html: str) -> str:
    """Convert an HTML fragment/page to collapsed plain text.

    Best-effort and dependency-free (stdlib HTMLParser). Returns "" on any
    parse failure. Whitespace is collapsed: runs of spaces/tabs -> single
    space, runs of blank lines -> single newline.
    """
    if not html:
        return ""
    try:
        parser = _TextExtractor()
        parser.feed(html)
        raw = parser.text()
    except Exception:
        return ""
    # Collapse intra-line whitespace, then trim each line, then squeeze blank lines.
    lines = [re.sub(r"[ \t\f\v]+", " ", ln).strip() for ln in raw.splitlines()]
    out: list[str] = []
    blank = False
    for ln in lines:
        if ln:
            out.append(ln)
            blank = False
        elif not blank:
            out.append("")
            blank = True
    return "\n".join(out).strip()
