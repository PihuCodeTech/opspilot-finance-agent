"""Theme and 3D-view preferences read from cookies, so the server renders the
right `<html>` attributes and the page never flashes (`07-design.md` §4.1, §11.6)."""

from typing import Literal

from starlette.requests import Request

THEME_COOKIE = "theme"
VIEWS_3D_COOKIE = "views3d"

Theme = Literal["light", "dark"]


def explicit_theme(request: Request) -> Theme | None:
    """Return the chosen theme, or None for System (CSS follows prefers-color-scheme)."""
    value = request.cookies.get(THEME_COOKIE)
    if value == "light":
        return "light"
    if value == "dark":
        return "dark"
    return None


def views_3d_enabled(request: Request) -> bool:
    return request.cookies.get(VIEWS_3D_COOKIE) != "off"
