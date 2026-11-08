"""HTML -> plain text that keeps structure.

Output is one block per line:
  "## Heading"   for <h1>-<h6>, or a block whose only text is bold/strong
  "- item"       for <li>
  "plain text"   for paragraphs / divs
"""
from __future__ import annotations

import html as htmllib
import re
from html.parser import HTMLParser

_BLOCK = {"p", "div", "li", "ul", "ol", "br", "tr", "table", "section", "article",
          "header", "footer", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "hr"}
_HEADING = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BOLD = {"strong", "b"}
MAX_HEADING_CHARS = 90


# "SKILLS & EXPERIENCE WE'RE LOOKING FOR": a short all-capitals line with no sentence end is a heading
_CAPS_HEADING = re.compile(r"^(?=(?:.*[A-Z]){4})[A-Z0-9&/,'’()\- ]+$")


class _Converter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lines: list[str] = []
        self._buf: list[tuple[str, bool]] = []  # (text, is_bold)
        self._bold = 0
        self._heading = 0
        self._li = False
        self._skip = 0

    def _flush(self) -> None:
        text = "".join(t for t, _ in self._buf)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            visible = [(t, b) for t, b in self._buf if t.strip()]
            all_bold = bool(visible) and all(b for _, b in visible)
            if self._li:
                self.lines.append("- " + text)
            elif self._heading or ((all_bold or _CAPS_HEADING.match(text)) and len(text) <= MAX_HEADING_CHARS):
                self.lines.append("## " + text.rstrip(":").strip())
            else:
                self.lines.append(text)
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
            return
        if tag in _BLOCK:
            self._flush()
            self._li = tag == "li"
        if tag in _HEADING:
            self._heading += 1
        if tag in _BOLD:
            self._bold += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
            return
        if tag in _BOLD:
            self._bold = max(0, self._bold - 1)
        if tag in _BLOCK:
            self._flush()
            if tag == "li":
                self._li = False
        if tag in _HEADING:
            self._heading = max(0, self._heading - 1)

    def handle_data(self, data):
        if self._skip:
            return
        self._buf.append((data, self._bold > 0 or self._heading > 0))

    def close(self):
        super().close()
        self._flush()


def html_to_lines(html: str | None, unescape_first: bool = False) -> list[str]:
    if not html:
        return []
    if unescape_first:
        html = htmllib.unescape(html)
    conv = _Converter()
    conv.feed(html)
    conv.close()
    return conv.lines


def html_to_text(html: str | None, unescape_first: bool = False) -> str:
    return "\n".join(html_to_lines(html, unescape_first))


def plain_to_lines(text: str | None) -> list[str]:
    """Plain text -> lines; short lines ending with ':' become headings."""
    out = []
    for raw in (text or "").split("\n"):
        line = re.sub(r"\s+", " ", raw.replace(" ", " ")).strip()
        if not line:
            continue
        if line[:1] in "•*-•":
            out.append("- " + line.lstrip("•*-• ").strip())
        elif (line.endswith(":") or _CAPS_HEADING.match(line)) and len(line) <= MAX_HEADING_CHARS:
            out.append("## " + line.rstrip(":").strip())
        else:
            out.append(line)
    return out
