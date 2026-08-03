"""HTML → readable markdown-ish text, stdlib only.

Deliberately no readability/bs4/lxml dependency. luxe's base install stays
lean (fallback-kit rule: it has to install and run on every fleet host), and
the job here is modest — drop the chrome, keep the prose, preserve the
structure a coding agent actually needs: headings, code blocks, list items,
and link targets.

Not a general-purpose reader-mode implementation. Pages that render their
content with JavaScript produce little or nothing here; that is what the
`render=true` Playwright path in `browser.py` is for, and `to_markdown`
reports emptiness rather than pretending.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# Dropped whole: their text content is never page content.
_SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "template",
              "iframe", "object", "embed"}
# Structural chrome. Removing these is what separates "the article" from
# "the article plus every nav link on the site".
_CHROME_TAGS = {"nav", "header", "footer", "aside", "form"}
_BLOCK_TAGS = {"p", "div", "section", "article", "main", "br", "hr", "tr",
               "table", "ul", "ol", "dl", "blockquote", "figure"}
_HEADINGS = {"h1": "#", "h2": "##", "h3": "###",
             "h4": "####", "h5": "#####", "h6": "######"}


class _Extractor(HTMLParser):
    def __init__(self, base_url: str = "", keep_links: bool = True):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.keep_links = keep_links
        self.out: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.title = ""
        self._skip_depth = 0
        self._chrome_depth = 0
        self._in_title = False
        self._in_pre = 0
        self._heading: str | None = None
        self._link_href: str | None = None
        self._link_text: list[str] = []

    # -- helpers --
    def _emit(self, text: str) -> None:
        if self._skip_depth or self._chrome_depth:
            return
        self.out.append(text)

    def _absolute(self, href: str) -> str:
        if not self.base_url:
            return href
        from urllib.parse import urljoin
        try:
            return urljoin(self.base_url, href)
        except ValueError:
            return href

    # -- parser hooks --
    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag in _CHROME_TAGS:
            self._chrome_depth += 1
            return
        if tag == "title":
            self._in_title = True
        elif tag == "pre":
            self._in_pre += 1
            self._emit("\n```\n")
        elif tag == "code" and not self._in_pre:
            self._emit("`")
        elif tag in _HEADINGS:
            self._heading = _HEADINGS[tag]
            self._emit(f"\n\n{self._heading} ")
        elif tag == "li":
            self._emit("\n- ")
        elif tag == "a" and self.keep_links:
            href = dict(attrs).get("href") or ""
            if href and not href.startswith(("javascript:", "#")):
                self._link_href = self._absolute(href)
                self._link_text = []
        elif tag == "img":
            alt = dict(attrs).get("alt") or ""
            if alt:
                self._emit(f"[image: {alt}]")
        elif tag in _BLOCK_TAGS:
            self._emit("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in _CHROME_TAGS:
            self._chrome_depth = max(0, self._chrome_depth - 1)
            return
        if tag == "title":
            self._in_title = False
        elif tag == "pre":
            self._in_pre = max(0, self._in_pre - 1)
            self._emit("\n```\n")
        elif tag == "code" and not self._in_pre:
            self._emit("`")
        elif tag in _HEADINGS:
            self._heading = None
            self._emit("\n")
        elif tag == "a" and self._link_href is not None:
            text = "".join(self._link_text).strip()
            if text:
                self._emit(f"[{text}]({self._link_href})")
                self.links.append((text, self._link_href))
            self._link_href = None
            self._link_text = []
        elif tag in _BLOCK_TAGS:
            self._emit("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
            return
        if self._skip_depth or self._chrome_depth:
            return
        if self._in_pre:
            self.out.append(data)
            return
        if self._link_href is not None:
            self._link_text.append(data)
            return
        # Collapse runs of whitespace; the block tags carry the structure.
        cleaned = re.sub(r"\s+", " ", data)
        if cleaned.strip():
            self.out.append(cleaned)
        elif self.out and not self.out[-1].endswith((" ", "\n")):
            self.out.append(" ")


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)          # at most one blank line
    text = re.sub(r"\n +", "\n", text)
    return text.strip()


def extract_text(html: str, *, base_url: str = "",
                 keep_links: bool = True) -> tuple[str, str, list[tuple[str, str]]]:
    """(title, markdown_text, links) for an HTML document."""
    parser = _Extractor(base_url=base_url, keep_links=keep_links)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        # A malformed document must degrade, not raise: partial text beats a
        # tool error, and HTMLParser can throw on genuinely broken markup.
        pass
    return parser.title.strip(), _tidy("".join(parser.out)), parser.links


def to_markdown(fetched, *, keep_links: bool = True,
                max_chars: int = 20_000) -> str:
    """Render a `FetchResult` as text for the model, with provenance.

    Non-HTML bodies (JSON, plain text, source files) pass through untouched —
    reformatting an API response as prose would corrupt it.
    """
    header = f"# {fetched.url}"
    if fetched.status != 200:
        header += f"  (HTTP {fetched.status})"

    if not fetched.is_html:
        body = fetched.text
        note = ""
    else:
        title, body, _links = extract_text(fetched.text, base_url=fetched.url,
                                           keep_links=keep_links)
        if title:
            header = f"# {title}\n{fetched.url}"
        note = ""
        # Say so plainly instead of handing back an empty-looking answer: the
        # model needs to know the render=true retry exists. Requiring a
        # <script> tag keeps the note off pages that are simply SHORT —
        # example.com extracts ~150 chars and is not JS-rendered at all.
        if len(body.strip()) < 200 and "<script" in fetched.text.lower():
            note = ("\n\n[note: little readable text was extracted and the page "
                    "carries scripts — it likely renders its content with "
                    "JavaScript. Retry with render=true to load it in a real "
                    "browser.]")

    if len(body) > max_chars:
        body = body[:max_chars]
        note += (f"\n\n[truncated to {max_chars} chars of "
                 f"{len(fetched.text)} fetched]")
    elif fetched.truncated:
        note += "\n\n[response truncated at the byte cap before extraction]"

    return f"{header}\n\n{body}{note}".strip()
