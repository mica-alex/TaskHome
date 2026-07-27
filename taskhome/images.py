"""Fetching and preparing photos for the printer (MASTER_PLAN P4-7).

A thermal printer has one ink level: a dot is burned or it is not. Photographs
therefore have to be reduced to pure black and white, and *how* that reduction
is done is the whole difference between a recognisable picture and a black
smear. Plain thresholding produces the smear; Floyd-Steinberg error diffusion
trades spatial resolution for apparent grey levels and is what makes a face
readable at 203 dpi.

Everything here is defensive, because this code sits on the print path and runs
against a URL supplied by a third-party API:

* the download is capped in bytes and in seconds, so a slow or enormous image
  delays one receipt rather than wedging the scheduler;
* the result is cached, since the queue can retry a receipt several times and
  re-fetching the same photo each attempt is wasteful;
* every failure returns None. A receipt with the photo missing is a good
  outcome; a receipt that failed to print because a CDN was down is not.
"""
import io
import os
import time

import requests

from . import constants
from .logsetup import log

MAX_BYTES = 2 * 1024 * 1024      # 2 MB
TIMEOUT_SECONDS = 5
CACHE_DIRNAME = 'media'
CACHE_MAX_FILES = 200

#: The API redirects through Rails ActiveStorage, so a couple of hops is
#: normal; more than that is a redirect loop dressed up.
MAX_REDIRECTS = 4


def cache_dir():
    return os.path.join(constants.DATA_DIR, 'cache', CACHE_DIRNAME)


def _cache_path(url, width):
    import hashlib
    digest = hashlib.sha256(f'{url}@{width}'.encode()).hexdigest()[:32]
    return os.path.join(cache_dir(), f'{digest}.png')


def fetch(url, timeout=TIMEOUT_SECONDS, max_bytes=MAX_BYTES):
    """Download an image, refusing anything too large or too slow.

    Streamed and counted rather than trusting Content-Length, which is absent
    on a chunked response and is in any case a claim rather than a fact.
    """
    session = requests.Session()
    session.max_redirects = MAX_REDIRECTS
    try:
        response = session.get(url, timeout=timeout, stream=True)
        response.raise_for_status()

        content_type = (response.headers.get('content-type') or '').lower()
        if not content_type.startswith('image/'):
            log.warning(f"Not an image ({content_type or 'no content-type'}): {url[:80]}")
            return None

        chunks, total = [], 0
        for chunk in response.iter_content(8192):
            total += len(chunk)
            if total > max_bytes:
                log.warning(f"Image exceeds {max_bytes} bytes, skipping: {url[:80]}")
                return None
            chunks.append(chunk)
        return b''.join(chunks)
    except Exception as e:
        log.warning(f"Could not fetch image: {e}")
        return None
    finally:
        session.close()


def prepare(data, width, max_height):
    """Bytes -> a 1-bit PIL image sized for the paper, or None.

    Aspect ratio is preserved and the height cap applies afterwards, so a tall
    portrait photo is scaled to fit rather than cropped -- a cropped photo of a
    pothole may not contain the pothole.
    """
    try:
        from PIL import Image
    except ImportError:      # pragma: no cover - Pillow is a hard dependency
        log.warning('Pillow is not installed; cannot print images')
        return None

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as e:
        log.warning(f"Unreadable image data: {e}")
        return None

    try:
        # EXIF orientation: a phone photo is very often stored rotated with a
        # tag saying which way is up. Ignoring it prints the picture sideways.
        from PIL import ImageOps
        image = ImageOps.exif_transpose(image) or image
    except Exception:
        pass

    image = image.convert('L')

    width = max(int(width), 8)
    if image.width != width:
        height = max(1, round(image.height * width / image.width))
        image = image.resize((width, height), _resample())

    if max_height and image.height > max_height:
        scaled_width = max(8, round(image.width * max_height / image.height))
        image = image.resize((scaled_width, int(max_height)), _resample())

    # Floyd-Steinberg. This is the step that makes a photograph legible on a
    # one-bit device; Image.convert('1') applies it by default.
    return image.convert('1')


def _resample():
    from PIL import Image
    return getattr(Image, 'LANCZOS', None) or Image.Resampling.LANCZOS


def load(url, width, max_height=None):
    """Fetch, prepare and cache. Returns a 1-bit image or None.

    Cached because the print queue can retry a receipt many times, and each
    retry would otherwise re-download the same photo.
    """
    if not url:
        return None

    path = _cache_path(url, width)
    if os.path.exists(path):
        try:
            from PIL import Image
            cached = Image.open(path)
            cached.load()
            return cached
        except Exception:
            # A truncated cache file must not be fatal; fall through and
            # fetch it again.
            log.warning(f"Discarding unreadable cached image {path}")

    data = fetch(url)
    if data is None:
        return None
    image = prepare(data, width, max_height)
    if image is None:
        return None

    try:
        os.makedirs(cache_dir(), exist_ok=True)
        image.save(path)
        _prune_cache()
    except OSError as e:
        # Caching is an optimisation; failing to cache must not stop a print.
        log.warning(f"Could not cache image: {e}")
    return image


def _prune_cache(keep=CACHE_MAX_FILES):
    """Oldest-first, so an appliance running for a year does not fill a disk
    with photographs of potholes."""
    try:
        entries = [os.path.join(cache_dir(), name) for name in os.listdir(cache_dir())]
        entries = [e for e in entries if os.path.isfile(e)]
        if len(entries) <= keep:
            return
        entries.sort(key=lambda p: os.path.getmtime(p))
        for path in entries[:len(entries) - keep]:
            os.remove(path)
    except OSError as e:
        log.debug(f"Image cache prune skipped: {e}")


def clear_cache():
    """Remove every cached image. Derived data -- safe to delete at any time."""
    removed = 0
    try:
        for name in os.listdir(cache_dir()):
            path = os.path.join(cache_dir(), name)
            if os.path.isfile(path):
                os.remove(path)
                removed += 1
    except OSError:
        pass
    return removed


def describe(url):
    """A one-line summary for logs, without dumping a 400-character URL."""
    return f'{url[:60]}...' if url and len(url) > 60 else (url or 'no image')


_last_fetch = 0.0


def throttle(min_interval=0.5):
    """Space out consecutive fetches.

    A catch-up burst can hold twenty issues, each with a photo. Firing twenty
    downloads back to back at someone else's CDN is rude and is the sort of
    thing that gets an appliance rate-limited.
    """
    global _last_fetch
    wait = min_interval - (time.monotonic() - _last_fetch)
    if wait > 0:
        time.sleep(wait)
    _last_fetch = time.monotonic()
