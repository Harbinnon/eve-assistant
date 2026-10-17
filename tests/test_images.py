"""Portraits and ship renders: downloaded once, kept in var/images, served by us."""

import os
import time

import httpx

from eve_assistant import images

PNG = b"\x89PNG\r\n\x1a\nfake"


def _counting(result):
    calls = []

    def fetch(address):
        calls.append(address)
        return result

    fetch.calls = calls
    return fetch


def test_downloaded_once_then_kept(tmp_path):
    fetch = _counting(PNG)
    first = images.cached(tmp_path, "portrait", 7, fetch)
    again = images.cached(tmp_path, "portrait", 7, fetch)
    assert first == again == tmp_path / "portrait" / "7.png"
    assert first.read_bytes() == PNG
    assert fetch.calls == ["https://images.evetech.net/characters/7/portrait?size=128"]


def test_a_missing_image_is_not_asked_for_again_for_a_day(tmp_path):
    fetch = _counting(None)
    assert images.cached(tmp_path, "render", 32880, fetch) is None
    assert images.cached(tmp_path, "render", 32880, fetch) is None
    assert len(fetch.calls) == 1
    marker = tmp_path / "render" / "32880.missing"
    day_ago = time.time() - images.MISSING_SECONDS - 10
    os.utime(marker, (day_ago, day_ago))
    images.cached(tmp_path, "render", 32880, fetch)
    assert len(fetch.calls) == 2


def test_an_old_copy_beats_none(tmp_path):
    images.cached(tmp_path, "portrait", 7, _counting(PNG))
    path = tmp_path / "portrait" / "7.png"
    week_ago = time.time() - images.KEEP_SECONDS - 10
    os.utime(path, (week_ago, week_ago))
    assert images.cached(tmp_path, "portrait", 7, _counting(None)) == path


def test_unknown_kinds_and_no_fetcher(tmp_path):
    assert images.cached(tmp_path, "logo", 1, _counting(PNG)) is None
    assert images.cached(tmp_path, "portrait", 7, None) is None


def test_download_accepts_only_images(monkeypatch):
    responses = {
        "ok": httpx.Response(200, content=PNG, headers={"content-type": "image/png"}),
        "html": httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}),
        "gone": httpx.Response(404),
    }
    seen = []

    def fake_get(address, headers, timeout):
        seen.append(headers["User-Agent"])
        if address == "boom":
            raise httpx.ConnectError("no network")
        return responses[address]

    monkeypatch.setattr(images.httpx, "get", fake_get)
    assert images.download("ok", "eve-assistant/test") == PNG
    assert images.download("html", "eve-assistant/test") is None
    assert images.download("gone", "eve-assistant/test") is None
    assert images.download("boom", "eve-assistant/test") is None
    assert set(seen) == {"eve-assistant/test"}
