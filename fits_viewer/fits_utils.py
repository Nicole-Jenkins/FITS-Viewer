"""
Core FITS reading and thumbnail generation.

Deliberately uses astropy.io.fits rather than a hand-rolled parser so that
compressed FITS (.fits.fz), multi-extension files, and non-standard header
formatting are handled correctly instead of silently breaking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import numpy as np
from astropy.io import fits


# Header keywords we care about, and friendly labels for the UI.
# Extend this list once we see real-world header dumps from the user's files.
HEADER_FIELDS: list[tuple[str, str]] = [
    ("OBJECT", "Object"),
    ("FILTER", "Filter"),
    ("EXPTIME", "Exposure (s)"),
    ("DATE-OBS", "Date/time"),
    ("TELESCOP", "Telescope"),
    ("INSTRUME", "Instrument"),
    ("XBINNING", "Binning"),
    ("FOCALLEN", "Focal length (mm)"),
    ("XPIXSZ", "Pixel size (um)"),
    ("RA", "RA"),
    ("DEC", "Dec"),
    ("NAXIS1", "Width (px)"),
    ("NAXIS2", "Height (px)"),
]


@dataclass
class FitsImageInfo:
    """Lightweight summary of a FITS file: header fields + enough to make a thumbnail."""
    path: str
    header: dict = field(default_factory=dict)
    error: str | None = None

    def display_object(self) -> str:
        obj = self.header.get("OBJECT")
        if obj and str(obj).strip():
            return str(obj).strip()
        return "(unknown target)"

    def display_filter(self) -> str:
        f = self.header.get("FILTER")
        return str(f).strip() if f and str(f).strip() else "-"


def _find_image_hdu(hdul: fits.HDUList):
    """
    Return the first HDU that actually contains 2D+ image data.
    Handles the common case where the primary HDU is empty and the real
    image lives in an extension (e.g. some compressed / MAST-processed files).
    """
    for hdu in hdul:
        data = hdu.data
        if data is not None and getattr(data, "ndim", 0) >= 2:
            return hdu
    return None


def _merged_header(hdul: fits.HDUList, hdu) -> dict:
    """Primary header first, then overlay the image HDU's own header on top.

    Multi-extension files (HST/WFC3 in particular) put pixel data in a
    named extension like SCI, but target/telescope metadata (OBJECT,
    TELESCOP, DATE-OBS, RA/DEC...) stays in the primary HDU's header.
    Reading only hdu.header misses all of that.
    """
    merged: dict = {}
    for k in hdul[0].header:
        if k in ("", "COMMENT", "HISTORY"):
            continue
        merged[k] = hdul[0].header[k]
    if hdu is not hdul[0]:
        for k in hdu.header:
            if k in ("", "COMMENT", "HISTORY"):
                continue
            merged[k] = hdu.header[k]
    return merged


def _is_memmap_scaling_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "memmap" in msg or "bzero" in msg or "bscale" in msg


def read_fits_info(path: str) -> FitsImageInfo:
    """Read header fields only (fast, no pixel decoding) for list/grid display."""
    try:
        try:
            with fits.open(path, memmap=True, ignore_missing_end=True) as hdul:
                hdu = _find_image_hdu(hdul)
                if hdu is None:
                    return FitsImageInfo(path=path, error="No image data found")
                hdr = _merged_header(hdul, hdu)
        except ValueError as exc:
            # BZERO/BSCALE/BLANK together — astropy only raises this once the
            # data is actually touched, which _find_image_hdu does while
            # checking ndim, so we land here even though we only wanted the
            # header. Retry fully unmapped.
            if not _is_memmap_scaling_error(exc):
                raise
            with fits.open(path, memmap=False, ignore_missing_end=True) as hdul:
                hdu = _find_image_hdu(hdul)
                if hdu is None:
                    return FitsImageInfo(path=path, error="No image data found")
                hdr = _merged_header(hdul, hdu)

        values = {}
        for key, _label in HEADER_FIELDS:
            if key in hdr:
                values[key] = hdr[key]
        return FitsImageInfo(path=path, header=values)
    except Exception as exc:  # noqa: BLE001 - surfaced to the UI, not swallowed
        return FitsImageInfo(path=path, error=str(exc))


def _mtf(x: np.ndarray, m: float) -> np.ndarray:
    """Midtones Transfer Function (the standard PixInsight/Siril auto-stretch curve).

    MTF(0, m) = 0, MTF(m, m) = 0.5, MTF(1, m) = 1 — it pins the black and white
    points and bends the midtones through m, which is exactly what "push the
    faint stuff up without blowing out the bright stuff" requires.
    """
    x = np.clip(x, 0.0, 1.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        denom = (2 * m - 1) * x - m
        out = np.where(denom != 0, ((m - 1) * x) / denom, 0.5)
    return np.clip(out, 0.0, 1.0)


def _solve_midtones_balance(x: float, target: float) -> float:
    """Given a normalised median x, solve for the m that maps MTF(x, m) = target."""
    denom = x * (1 - 2 * target) + target
    if abs(denom) < 1e-12:
        return 0.5
    m = x * (1 - target) / denom
    return float(np.clip(m, 1e-6, 1.0 - 1e-6))


def _percentile_stretch(
    data: np.ndarray,
    target_bg: float = 0.25,
    shadow_clip_sigma: float = -2.8,
) -> np.ndarray:
    """
    Auto-stretch for preview purposes, using the median/MAD-based "auto STF"
    approach (same family of algorithm PixInsight and Siril use for their
    screen-stretch preview) rather than a plain min/max or percentile clip.

    A plain percentile clip fails on real astro data because background sky
    noise is ~99% of the pixels — the percentile range ends up sitting almost
    entirely *inside* the noise distribution, so nothing actually gets pushed
    dark. Anchoring on the median and MAD (a noise-robust spread estimate)
    and bending the curve through a target background brightness fixes that:
    background lands near target_bg, faint signal lifts above it, and bright
    cores/hot pixels compress toward white instead of clipping the range.
    """
    finite_mask = np.isfinite(data)
    finite = data[finite_mask]
    if finite.size == 0:
        return np.zeros_like(data, dtype=np.float32)

    # Robust normalisation range (guards against a handful of hot pixels
    # setting the white point) before computing stats.
    lo, hi = np.percentile(finite, [0.01, 99.9])
    if hi <= lo:
        hi = lo + 1.0
    norm = np.clip((data.astype(np.float64) - lo) / (hi - lo), 0.0, 1.0)

    # IMPORTANT: drizzled HST products (and anything with a rotated footprint)
    # fill the area outside the actual image with NaN. np.median() propagates
    # a single NaN to the *entire* result, which silently corrupts the whole
    # stretch — every pixel comes out NaN, which then casts to black. Compute
    # stats only over the finite pixels, and only ever push NaN pixels to a
    # known value (black) at the very end, never into the statistics.
    finite_norm = norm[finite_mask]
    median = float(np.median(finite_norm))
    mad = float(np.median(np.abs(finite_norm - median)))
    sigma = 1.4826 * mad  # MAD -> std-equivalent for a roughly Gaussian background

    black_point = max(median + shadow_clip_sigma * sigma, 0.0)

    if median - black_point > 1e-6:
        rescaled_median = (median - black_point) / max(1.0 - black_point, 1e-6)
        m = _solve_midtones_balance(rescaled_median, target_bg)
    else:
        m = 0.5

    clipped = np.clip((norm - black_point) / max(1.0 - black_point, 1e-6), 0.0, 1.0)
    stretched = _mtf(clipped, m)
    stretched = np.where(finite_mask, stretched, 0.0)  # no-data areas render black, not NaN
    return stretched.astype(np.float32)


def make_thumbnail_array(path: str, max_size: int = 256) -> np.ndarray:
    """
    Decode a FITS file and return an (H, W) uint8 array, auto-stretched and
    downsampled to fit within max_size on the longest edge.

    max_size=0 means "no downsampling" - full native resolution. Used for
    export (Save Image As...), where the person wants the actual full-res
    stretched image rather than a fast preview.

    Downsampling happens via strided slicing on the raw array *before* the
    percentile computation touches every pixel where possible, to keep this
    cheap across a few hundred files.
    """
    try:
        with fits.open(path, memmap=True, ignore_missing_end=True) as hdul:
            hdu = _find_image_hdu(hdul)
            if hdu is None:
                raise ValueError("No image data found")
            data = np.asarray(hdu.data)
    except ValueError as exc:
        if not _is_memmap_scaling_error(exc):
            raise
        with fits.open(path, memmap=False, ignore_missing_end=True) as hdul:
            hdu = _find_image_hdu(hdul)
            if hdu is None:
                raise ValueError("No image data found")
            data = np.asarray(hdu.data)

    # Collapse extra dimensions (e.g. a (1, H, W) cube) down to 2D.
    while data.ndim > 2:
        data = data[0]

    h, w = data.shape
    if max_size > 0:
        scale = max(h, w) / max_size
        if scale > 1:
            step = int(np.ceil(scale))
            data = data[::step, ::step]

    stretched = _percentile_stretch(data)
    return (stretched * 255.0).astype(np.uint8)
