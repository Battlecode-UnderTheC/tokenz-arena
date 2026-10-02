"""/docs: the teammates' guide (docs/TEAMMATES.md) and the agents' brief (docs/AGENTS.md),
rendered with the site's layout. {ARENA} in the docs becomes this arena's public URL."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from arena.config import ROOT
from arena.web import markdown

DOCS = {"": ("TEAMMATES.md", "Guide"), "agents": ("AGENTS.md", "Agent brief"),
        "metrics": ("METRICS.md", "Metrics"),
        "evals": ("EVALS.md", "Evals")}

EXTRA_CSS = """
.doc { max-width: 900px; line-height: 1.55; }
.doc pre { padding: 10px 12px; overflow-x: auto; border-radius: 6px;
  background: var(--code-bg, rgba(127,127,127,.12)); }
.doc code { font-size: .92em; }
.doc :not(pre) > code { padding: 1px 4px; border-radius: 4px; background: rgba(127,127,127,.15); }
.doc blockquote { margin: 0; padding: 4px 12px; border-left: 3px solid rgba(127,127,127,.5); }
.doc table { margin: 8px 0 16px; }
.doc h2 { margin-top: 28px; }
"""


def public_url(arena, request: Request) -> str:
    return (arena.cfg.get("api", "public_url") or str(request.base_url)).rstrip("/")


def make_router(arena) -> APIRouter:
    from arena.web.pages import layout

    r = APIRouter(include_in_schema=False)

    def text(name: str, request: Request) -> str:
        return (ROOT / "docs" / name).read_text().replace("{ARENA}", public_url(arena, request))

    def page(key: str, request: Request):
        if key not in DOCS:
            raise HTTPException(404)
        name, title = DOCS[key]
        tabs = " · ".join(f'<a href="/docs{"/" + k if k else ""}">{t}</a>' for k, (_, t) in DOCS.items())
        body = (f'<style>{EXTRA_CSS}</style><p class="muted">{tabs} · '
                f'<a href="/docs{"/" + key if key else ""}.md">markdown</a></p>'
                f'<div class="doc">{markdown.render(text(name, request))}</div>')
        return layout(title, body, None, "/docs")

    @r.get("/docs")
    def guide(request: Request):
        return page("", request)

    @r.get("/docs.md", response_class=PlainTextResponse)
    def guide_md(request: Request):
        return text(DOCS[""][0], request)

    @r.get("/docs/{key}.md", response_class=PlainTextResponse)
    def doc_md(key: str, request: Request):
        if key not in DOCS:
            raise HTTPException(404)
        return text(DOCS[key][0], request)

    @r.get("/docs/{key}")
    def doc(key: str, request: Request):
        return page(key, request)

    return r
