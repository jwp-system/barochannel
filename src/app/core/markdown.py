import html
from functools import lru_cache

import nh3
from markdown_it import MarkdownIt

CACHE_MAX_LENGTH = 4000

_parser = MarkdownIt("commonmark", {"html": False, "linkify": False, "breaks": True}).enable(
    ["table", "strikethrough"]
)


def _render(source: str) -> str:
    return nh3.clean(_parser.render(source), url_schemes={"http", "https", "mailto"})


# 같은 글을 여러 사람이 반복해서 본다. 결과는 원문만으로 정해지므로 캐시해도 안전하다.
_render_cached = lru_cache(maxsize=2048)(_render)


def render_markdown(source: str) -> str:
    """반환값만 템플릿에서 safe로 출력한다. 원문 HTML을 끄고 결과도 한 번 더 정제한다."""
    return _render_cached(source) if len(source) <= CACHE_MAX_LENGTH else _render(source)


@lru_cache(maxsize=2048)
def _summary(source: str) -> str:
    text = nh3.clean(_parser.render(source[:CACHE_MAX_LENGTH]), tags=set())
    return " ".join(html.unescape(text).split())


def plain_summary(source: str, limit: int = 80) -> str:
    """목록에 보여줄 한 줄 요약. 마크다운 기호 없이 글자만 남긴다."""
    return _summary(source)[:limit]
