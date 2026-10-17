"""Character portraits and ship renders from CCP's image server.

Images are downloaded the first time a page needs them, kept in var/images
(never committed: CCP's artwork stays CCP's) and served by the dashboard
itself, so pages still load images only from their own address. Portraits
are fetched only for the owner's own characters. A missing image is
remembered for a day so it isn't asked for on every page view.
"""

import os
import time
from collections.abc import Callable
from pathlib import Path

import httpx

IMAGE_SERVER = "https://images.evetech.net"
SIZE = 128
KEEP_SECONDS = 7 * 24 * 3600  # portraits can change; renders rarely do
MISSING_SECONDS = 24 * 3600
KINDS = {
    "portrait": "/characters/{id}/portrait?size={size}",
    "render": "/types/{id}/render?size={size}",
}
# Each kind's folder under the cache: fixed names, never text from a request.
FOLDERS = {kind: kind for kind in KINDS}

# Shown when an image isn't available: a plain hexagon, no CCP artwork.
PLACEHOLDER_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<polygon points="32,4 56,18 56,46 32,60 8,46 8,18" fill="none" '
    'stroke="#5d6f80" stroke-width="3"/></svg>'
)

Fetch = Callable[[str], bytes | None]


def url(kind: str, item_id: int) -> str:
    return IMAGE_SERVER + KINDS[kind].format(id=int(item_id), size=SIZE)


def download(address: str, user_agent: str, timeout: float = 5.0) -> bytes | None:
    """One image from CCP's image server; None if it isn't there."""
    try:
        response = httpx.get(address, headers={"User-Agent": user_agent}, timeout=timeout)
    except httpx.HTTPError:
        return None
    if response.status_code != 200 or not response.headers.get("content-type", "").startswith(
        "image/"
    ):
        return None
    return response.content


def cached(cache_dir: Path, kind: str, item_id: int, fetch: Fetch | None) -> Path | None:
    """The image's file, downloading it if needed; None if it isn't available."""
    folder = FOLDERS.get(kind)
    if folder is None:
        return None
    base = os.path.realpath(cache_dir)
    full = os.path.realpath(os.path.join(base, folder, f"{int(item_id)}.png"))
    if not full.startswith(base + os.sep):  # stays inside the cache, whatever it's given
        return None
    path = Path(full)
    missing = path.with_suffix(".missing")
    now = time.time()
    if path.exists() and now - path.stat().st_mtime < KEEP_SECONDS:
        return path
    if missing.exists() and now - missing.stat().st_mtime < MISSING_SECONDS:
        return path if path.exists() else None
    if fetch is None:
        return path if path.exists() else None
    data = fetch(url(kind, item_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    if data is None:
        missing.touch()
        return path if path.exists() else None  # an older copy is better than none
    partial = path.with_suffix(".part")
    partial.write_bytes(data)
    partial.replace(path)
    missing.unlink(missing_ok=True)
    return path
