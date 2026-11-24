"""A doc site's own look (#121): logos, favicon, colours for light and dark, font, corners, links in the header and
custom CSS, so a site matches the website of its product. Stored as ``DocSite.theme``; empty is the look every site
has, and nothing of this reaches such a site's page.

The theme becomes a few CSS custom properties over slop2gether's tokens (designs/slop2gether.css), set in the served
page's head (no flash of the default look): the colours given, and what follows from them (lighter text, lines,
the accent's soft and readable shades) by ``color-mix``, so a single colour yields a whole consistent mode. The
custom CSS comes last and only ever on the public site (never in the app); ``<`` is escaped, so it cannot end its
``<style>``. The same CSS, scoped to an element, is the live preview in the settings (``css(theme, scope)``).
"""

from __future__ import annotations

import html
import re

from sqlalchemy.ext.asyncio import AsyncSession

from ..models import DocSite
from ..schemas import DocTheme
from . import docs as docs_service
from .errors import ServiceError

SITE_SCOPE = "html[data-docs]"
PREVIEW_SCOPE = ".doc-tp[data-design]"
FAMILY = re.compile(r"^[\w\s,'\"\-.]*$")
GENERIC = {"serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui", "ui-serif", "ui-sans-serif", "ui-monospace", "ui-rounded", "math", "emoji"}
LINK = re.compile(r"^(https?://[^\s<>\"]+|mailto:[^\s<>\"]+|/[^\s<>\"]*)$", re.IGNORECASE)
BASE_RADIUS = 14  # px: a card's corner in docs.css; the others are in proportion (--doc-rf)


def _family(value: str) -> str:
    """A font-family list with every name that needs quotes in them ("Source Sans 3")."""
    out = []
    for part in value.split(","):
        name = part.strip().strip("'\"").strip()
        if name:
            out.append(name if name.lower() in GENERIC else '"' + name.replace('"', "") + '"')
    return ",".join(out)


async def clean(session: AsyncSession, site: DocSite, theme: DocTheme) -> dict:
    """The theme as stored ({} when it is the default), or an error that says what is wrong."""
    for key in ("logo_light", "logo_dark", "favicon"):
        file_id = getattr(theme, key)
        if file_id is not None:
            attachment = await docs_service.get_file(session, site, file_id)
            if not attachment.content_type.startswith("image/"):
                raise ServiceError(f"{attachment.filename} is not a picture: logos and the favicon have to be images.")
    if theme.font_family and not FAMILY.match(theme.font_family):
        raise ServiceError("Font: only names separated by commas, e.g. Source Sans 3, sans-serif.")
    if theme.font_url and not re.match(r"^https://[^\s<>\"]+$", theme.font_url):
        raise ServiceError("Font stylesheet: an https address, e.g. https://fonts.googleapis.com/css2?family=…")
    for link in theme.links:
        if not LINK.match(link.url.strip()):
            raise ServiceError(f"Link {link.label!r}: an address starting with https://, http://, mailto: or /.")
    data = theme.model_dump()
    data["links"] = [{"label": link.label.strip(), "url": link.url.strip()} for link in theme.links]
    data["font_family"], data["font_url"] = theme.font_family.strip(), theme.font_url.strip()
    return {} if data == DocTheme().model_dump() else data


def _mode(colors: dict, dark: bool) -> list[str]:
    out = []
    if a := colors.get("accent"):
        out += [
            f"--accent:{a}",
            f"--accent-hover:color-mix(in oklab,{a} 85%,#fff)",
            f"--accent-fg:color-mix(in oklab,{a} {62 if dark else 80}%,var(--fg))",
            f"--accent-soft:color-mix(in oklab,{a} {24 if dark else 13}%,var(--bg))",
            f"--grad:linear-gradient(135deg,color-mix(in oklab,{a} 60%,#fff),{a} 55%,color-mix(in oklab,{a} 75%,#000))",
            f"--doc-glow:{a}",
        ]
    page, surface, text = colors.get("page"), colors.get("surface"), colors.get("text")
    if page:
        out.append(f"--page:{page}")
    if surface:
        out.append(f"--bg:{surface}")
    if text:
        out += [f"--fg:{text}", "--fg-2:color-mix(in oklab,var(--fg) 84%,var(--bg))", "--fg-3:color-mix(in oklab,var(--fg) 64%,var(--bg))"]
    if page or surface or text:
        out += [
            "--sunk:color-mix(in oklab,var(--fg) 7%,var(--bg))",
            "--line:color-mix(in oklab,var(--fg) 11%,var(--bg))",
            "--line-2:color-mix(in oklab,var(--fg) 22%,var(--bg))",
        ]
    return out


def css(theme: dict, scope: str = SITE_SCOPE) -> str:
    """The theme's custom properties for the element ``scope`` (and its light and dark mode); "" for no theme."""
    rules = []
    root = []
    if family := theme.get("font_family"):
        root.append(f"--font-sans:{_family(family)},system-ui,sans-serif")
    if (radius := theme.get("radius")) is not None:
        root.append(f"--doc-rf:{round(radius / BASE_RADIUS, 4)}")
    if root:
        rules.append(f"{scope}{{{';'.join(root)}}}")
    for mode in ("light", "dark"):
        if decl := _mode(theme.get(mode) or {}, mode == "dark"):
            rules.append(f'{scope}[data-theme="slop2gether-{mode}"]{{{";".join(decl)}}}')
    return "\n".join(rules)


def custom_css(theme: dict) -> str:
    return (theme.get("css") or "").replace("<", "\\3c ")


def head(theme: dict, files_base: str) -> str:
    """What a served page of the site gets at the end of its head: font, favicon, the theme, the custom CSS."""
    if not theme:
        return ""
    e = html.escape
    out = ""
    if url := theme.get("font_url"):
        out += f'<link rel="stylesheet" href="{e(url)}">'
    if favicon := theme.get("favicon"):
        out += f'<link rel="icon" href="{e(files_base)}/{int(favicon)}">'
    if rules := css(theme):
        out += f'<style id="pb-docs-theme">{rules}</style>'
    if extra := custom_css(theme):
        out += f'<style id="pb-docs-css">{extra}</style>'
    return out


def public(theme: dict, files_base: str) -> dict | None:
    """What the site's script needs of it: the logos' addresses, whether the name shows, the header's links."""
    if not theme:
        return None
    file = lambda key: f"{files_base}/{int(theme[key])}" if theme.get(key) else None  # noqa: E731
    return {"logo": file("logo_light"), "logo_dark": file("logo_dark") or file("logo_light"), "show_name": theme.get("show_name", True), "links": theme.get("links") or []}
