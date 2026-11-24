"""
Disk cache for generated thumbnails, so reopening a folder of a few hundred
FITS files doesn't mean re-decoding every one of them every time.

Cache key is derived from the file's absolute path + file size + mtime +
requested thumbnail size, so a changed or replaced file is regenerated
automatically without needing any manual "clear cache" step, and asking
for the same file at a different resolution (e.g. grid thumbnail vs.
full-size enlarge view) doesn't collide with a smaller cached entry.

CACHE_VERSION is part of the key too. None of the file details change when
the *code* that makes thumbnails changes, so without it, old thumbnails
(e.g. greyscale ones made before colour support) would keep being reused
forever. Bump CACHE_VERSION whenever thumbnail generation changes; old
entries are simply never looked up again.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from PIL import Image
import numpy as np

# History: 1 = original (implicit, not in the key); 2 = Seestar colour
# (RGB cubes + debayered raw frames) and block-averaged thumbnails.
CACHE_VERSION = 2


def _default_cache_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / ".cache"
    cache_dir = base / "FitsViewer" / "thumbnails"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


class ThumbnailCache:
    def __init__(self, cache_dir: Path | None = None):
        self.cache_dir = cache_dir or _default_cache_dir()

    def _key_for(self, path: str, size: int) -> str:
        st = os.stat(path)
        raw = f"v{CACHE_VERSION}|{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}|{size}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _cache_path(self, path: str, size: int) -> Path:
        return self.cache_dir / f"{self._key_for(path, size)}.png"

    def get(self, path: str, size: int) -> np.ndarray | None:
        try:
            cache_file = self._cache_path(path, size)
        except OSError:
            return None
        if not cache_file.exists():
            return None
        try:
            with Image.open(cache_file) as im:
                # Preserves whichever mode was actually saved - "L" (mono)
                # comes back as a 2D array, "RGB" as (H, W, 3). Forcing
                # convert("L") here would silently discard color on every
                # cached RGB thumbnail (e.g. Seestar's stacked color output).
                return np.array(im)
        except Exception:  # noqa: BLE001 - corrupt cache entry, just regenerate
            return None

    def put(self, path: str, thumb: np.ndarray, size: int) -> None:
        try:
            cache_file = self._cache_path(path, size)
        except OSError:
            return
        try:
            mode = "RGB" if thumb.ndim == 3 else "L"
            Image.fromarray(thumb, mode=mode).save(cache_file, format="PNG")
        except Exception:  # noqa: BLE001 - caching is a nice-to-have, never fatal
            pass
