"""The design system: colour contrast in every theme, and the theme switch."""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eve_assistant import store, web
from eve_assistant.web import create_app

CSS = (Path(web.__file__).parent / "static" / "style.css").read_text(encoding="utf-8")
LOCAL = "http://127.0.0.1"
AA = 4.5  # WCAG AA for normal text

# Text colour on what it sits on, bottom layer first (soft colours are
# translucent and sit on a surface). Each pair is a real use in the pages.
PAIRS = [
    ("fg", ["bg"]),  # page text
    ("fg", ["surface"]),  # text in panels and tables
    ("fg", ["raised"]),
    ("fg", ["field"]),  # what you type
    ("fg", ["surface", "row-hover"]),  # a hovered table row
    ("heading", ["bg"]),
    ("heading", ["surface"]),
    ("muted", ["bg"]),  # lede, notes, footer
    ("muted", ["surface"]),
    ("muted", ["raised"]),  # table headings
    ("action", ["bg"]),  # links
    ("action", ["surface"]),
    ("action-hover", ["surface"]),
    ("action", ["surface", "action-soft"]),  # step numbers, "new to you", chosen theme
    ("on-action", ["action"]),  # primary buttons
    ("on-action", ["action-hover"]),
    ("isk", ["surface"]),  # ISK figures
    ("isk", ["bg"]),
    ("ok", ["surface", "ok-soft"]),  # green tags
    ("bad", ["surface", "bad-soft"]),  # red tags
    ("bad", ["bg"]),  # problem text in the footer
    ("attention", ["surface", "attention-soft"]),  # amber tags
    ("attention", ["bg", "attention-soft"]),  # the banner's heading
    ("fg", ["bg", "attention-soft"]),  # banner text
    ("fg", ["bg", "ok-soft"]),  # notices
    ("fg", ["bg", "bad-soft"]),  # errors
]


def _block(selector: str, css: str = CSS) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^{}]*)\}", css)
    assert match, f"no {selector} block in style.css"
    return match.group(1)


def _tokens(block: str) -> dict[str, str]:
    return {name: value.strip() for name, value in re.findall(r"--([\w-]+):\s*([^;]+);", block)}


def _system_block() -> str:
    media = re.search(r"@media \(prefers-color-scheme: light\)\s*\{(.*?\})\s*\}", CSS, re.S)
    assert media, "no system theme block"
    return _block('[data-theme="system"]', media.group(1))


DARK = _tokens(_block(":root"))
THEMES = {"dark": DARK, "light": {**DARK, **_tokens(_block('[data-theme="light"]'))}}


def _rgba(value: str) -> tuple[float, float, float, float]:
    value = value.strip()
    if value == "transparent":
        return (0.0, 0.0, 0.0, 0.0)
    if hex_match := re.fullmatch(r"#([0-9a-fA-F]{6})", value):
        h = hex_match.group(1)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0)
    rgb = re.fullmatch(r"rgb\((\d+) (\d+) (\d+)(?: / (\d+)%)?\)", value)
    assert rgb, f"colour not understood: {value}"
    r, g, b, alpha = rgb.groups()
    return (int(r), int(g), int(b), int(alpha) / 100 if alpha else 1.0)


def _over(layers: list[str], tokens: dict[str, str]) -> tuple[float, float, float]:
    r = g = b = 0.0
    for name in layers:
        lr, lg, lb, alpha = _rgba(tokens[name])
        r, g, b = (
            lr * alpha + r * (1 - alpha),
            lg * alpha + g * (1 - alpha),
            lb * alpha + b * (1 - alpha),
        )
    return (r, g, b)


def _luminance(rgb) -> float:
    def channel(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a, b) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("theme", sorted(THEMES))
@pytest.mark.parametrize(("text", "layers"), PAIRS, ids=[f"{t}-on-{'+'.join(b)}" for t, b in PAIRS])
def test_text_contrast(theme, text, layers):
    tokens = THEMES[theme]
    background = _over(layers, tokens)
    foreground = _over([*layers, text], tokens)  # a translucent text colour would blend too
    ratio = _contrast(foreground, background)
    assert ratio >= AA, f"{theme}: --{text} on {' + '.join(layers)} is {ratio:.2f}:1"


def test_system_theme_is_the_light_theme():
    assert _tokens(_system_block()) == _tokens(_block('[data-theme="light"]'))


def test_every_colour_used_is_defined_in_every_theme():
    used = set(re.findall(r"var\(--([\w-]+)", CSS)) - {"sec"}  # --sec is set per security class
    light = _tokens(_block('[data-theme="light"]'))
    assert used <= set(DARK), used - set(DARK)
    colour_tokens = {n for n, v in DARK.items() if v.startswith(("#", "rgb", "transparent"))}
    assert colour_tokens <= set(light), colour_tokens - set(light)


def test_no_colours_outside_the_tokens():
    """Colours belong in the theme blocks, so a theme can't miss one. EVE's
    security colours are the exception: they're the same in every theme."""
    rules = re.sub(r":root\s*\{[^}]*\}|\[data-theme=\"(light|system)\"\]\s*\{[^}]*\}", "", CSS)
    rules = re.sub(r"\.sec-\d\d \{ --sec: #[0-9a-f]{6}; \}", "", rules)
    rules = re.sub(r"/\*.*?\*/", "", rules, flags=re.S)
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", rules)


@pytest.fixture
def client(tmp_path):
    path = tmp_path / "design.sqlite3"
    store.connect(path).close()
    with TestClient(create_app(path), base_url=LOCAL) as test_client:
        yield test_client


def _token(html: str) -> str:
    return re.search(r'name="token" value="([^"]+)"', html).group(1)


def test_dark_is_the_default_theme(client):
    page = client.get("/today").text
    assert 'data-theme="dark"' in page
    assert 'value="dark" aria-pressed="true"' in page


@pytest.mark.parametrize("theme", ["light", "system", "dark"])
def test_choosing_a_theme_sets_a_cookie_and_returns(client, theme):
    token = _token(client.get("/goals").text)
    response = client.post(
        "/theme",
        data={"token": token, "theme": theme, "return_to": "/skills?item=Retriever"},
        headers={"Origin": LOCAL},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/skills?item=Retriever"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"theme={theme};")
    assert "samesite=strict" in cookie.lower() and "httponly" in cookie.lower()
    client.cookies.set("theme", theme)
    page = client.get("/skills").text
    assert f'data-theme="{theme}"' in page
    assert f'value="{theme}" aria-pressed="true"' in page


@pytest.mark.parametrize(
    "return_to",
    ["https://evil.example/", "//evil.example/", "/\\evil.example", "/x\r\nSet-Cookie: a=b", ""],
)
def test_theme_never_returns_to_another_site(client, return_to):
    token = _token(client.get("/today").text)
    response = client.post(
        "/theme",
        data={"token": token, "theme": "light", "return_to": return_to},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/today"


def test_unknown_theme_is_refused_and_unknown_cookie_ignored(client):
    token = _token(client.get("/today").text)
    response = client.post("/theme", data={"token": token, "theme": "neon"})
    assert response.status_code == 400
    client.cookies.set("theme", '"><script>')
    assert 'data-theme="dark"' in client.get("/today").text


def test_theme_form_returns_without_the_one_off_message(client):
    page = client.get("/goals?msg=goal_added&x=1").text
    assert 'name="return_to" value="/goals?x=1"' in page


def test_theme_form_needs_the_form_token(client):
    response = client.post("/theme", data={"token": "wrong", "theme": "light"})
    assert response.status_code == 403
