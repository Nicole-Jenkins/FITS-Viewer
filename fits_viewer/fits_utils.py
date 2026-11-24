"""
Core FITS reading and thumbnail generation.

Deliberately uses astropy.io.fits rather than a hand-rolled parser so that
compressed FITS (.fits.fz), multi-extension files, and non-standard header
formatting are handled correctly instead of silently breaking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
import numpy as np
from astropy.io import fits

FITS_EXTENSIONS = {".fits", ".fit", ".fts"}


def is_fits_filename(name: str) -> bool:
    """True for .fits/.fit/.fts files, including gzip-compressed variants
    (.fits.gz etc). astropy reads gzip-compressed FITS transparently;
    this only handles recognition during folder listing, since a plain
    extension check sees only the trailing ".gz"."""
    lower = name.lower()
    if lower.endswith(".gz"):
        lower = lower[:-3]
    return os.path.splitext(lower)[1] in FITS_EXTENSIONS


# Header keywords displayed in the UI, with display labels.
# Extend as additional instruments/pipelines require new fields.
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
    ("FOV", "Field of view"),
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


def _format_angle(deg: float) -> str:
    """Format a sky angle in degrees if >= 1 degree, arcminutes if >= 1',
    otherwise arcseconds."""
    if deg >= 1.0:
        return f"{deg:.2f}\u00b0"
    arcmin = deg * 60.0
    if arcmin >= 1.0:
        return f"{arcmin:.1f}'"
    arcsec = deg * 3600.0
    return f'{arcsec:.1f}"'


def _fov_from_wcs(hdr: dict, naxis1: float, naxis2: float) -> str | None:
    """Field of view from the WCS (plate-solve) pixel scale, or None."""
    from astropy.wcs import WCS
    from astropy.wcs.utils import proj_plane_pixel_scales
    import warnings

    with warnings.catch_warnings():
        # relax=True already handles non-standard WCS headers correctly;
        # suppress the associated validation warnings.
        warnings.simplefilter("ignore")
        wcs = WCS(hdr, relax=True, naxis=2)
        if not wcs.has_celestial:
            return None
        scales_deg = proj_plane_pixel_scales(wcs.celestial)

    sx, sy = abs(float(scales_deg[0])), abs(float(scales_deg[1]))
    # A header with RA/Dec axis names but no real scale makes astropy
    # assume 1 degree per pixel - obviously not a real plate-solve.
    if not (0 < sx < 0.5 and 0 < sy < 0.5):
        return None
    return f"{_format_angle(sx * naxis1)} x {_format_angle(sy * naxis2)}"


def _fov_from_optics(hdr: dict, naxis1: float, naxis2: float) -> str | None:
    """Estimated field of view from the telescope's focal length and the
    camera's pixel size, for files with no plate-solve (e.g. raw Seestar
    sub-frames):  angle = pixels x pixel size / focal length.

    XPIXSZ/YPIXSZ are in microns and, by the usual convention (MaxIm DL,
    N.I.N.A., Seestar), already include any binning; FOCALLEN is in mm."""
    try:
        focal_mm = float(hdr.get("FOCALLEN") or 0)
        px_um = float(hdr.get("XPIXSZ") or hdr.get("PIXSIZE1") or 0)
        py_um = float(hdr.get("YPIXSZ") or hdr.get("PIXSIZE2") or px_um or 0)
    except (TypeError, ValueError):
        return None
    if focal_mm <= 0 or px_um <= 0 or py_um <= 0:
        return None
    rad_to_deg = 57.29577951308232
    fov_x = naxis1 * px_um * 1e-3 / focal_mm * rad_to_deg
    fov_y = naxis2 * py_um * 1e-3 / focal_mm * rad_to_deg
    return f"{_format_angle(fov_x)} x {_format_angle(fov_y)} (from optics)"


def _compute_fov(hdr: dict) -> str | None:
    """Field of view: from the WCS (plate-solve) pixel scale when there is
    one, otherwise estimated from focal length + pixel size.

    Returns None if neither is available; the field is then shown as "-"
    rather than raised as an error.
    """
    try:
        naxis1 = hdr.get("NAXIS1")
        naxis2 = hdr.get("NAXIS2")
        if not naxis1 or not naxis2:
            return None
        naxis1, naxis2 = float(naxis1), float(naxis2)
        try:
            fov = _fov_from_wcs(hdr, naxis1, naxis2)
        except Exception:  # noqa: BLE001 - broken WCS: fall back to optics
            fov = None
        return fov or _fov_from_optics(hdr, naxis1, naxis2)
    except Exception:  # noqa: BLE001 - non-critical field, never raised
        return None


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
            # BZERO/BSCALE/BLANK combinations raise a ValueError on first
            # data access rather than at open time; _find_image_hdu triggers
            # this while checking ndim. Retry without memmap.
            if not _is_memmap_scaling_error(exc):
                raise
            with fits.open(path, memmap=False, ignore_missing_end=True) as hdul:
                hdu = _find_image_hdu(hdul)
                if hdu is None:
                    return FitsImageInfo(path=path, error="No image data found")
                hdr = _merged_header(hdul, hdu)

        values = {}
        for key, _label in HEADER_FIELDS:
            if key == "FOV":
                continue  # not a literal header keyword - computed below
            if key in hdr:
                values[key] = hdr[key]

        fov = _compute_fov(hdr)
        if fov is not None:
            values["FOV"] = fov

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


# Above this many pixels, a cheap stride is taken first so the block
# average below never has to hold a gigantic float copy in memory (e.g. a
# 20k x 20k mosaic). Normal camera frames are well under this.
_MAX_AVERAGE_PIXELS = 64_000_000


def _downsample(plane: np.ndarray, max_size: int) -> np.ndarray:
    """Shrinks a 2D array to roughly max_size on its longest edge (never
    smaller - see below), by averaging blocks of pixels (binning) rather
    than keeping every Nth pixel.

    Keeping every Nth pixel (the old approach) throws away most of the
    stars in a star field at random and keeps single noisy pixels, which is
    why thumbnails looked "crunchy". Averaging each block keeps every star's
    light and genuinely reduces noise. NaN pixels (e.g. the empty border of
    a drizzled HST image) are ignored within a block; a block that is
    entirely NaN stays NaN, so it still renders black later.

    max_size <= 0 means "no downsampling" (full-resolution export)."""
    if max_size <= 0:
        return plane
    h, w = plane.shape
    if max(h, w) <= max_size:
        return plane

    # Very large images: take a coarse stride first, purely to bound memory.
    pixels = h * w
    if pixels > _MAX_AVERAGE_PIXELS:
        pre = int(np.ceil(np.sqrt(pixels / _MAX_AVERAGE_PIXELS)))
        plane = plane[::pre, ::pre]
        h, w = plane.shape
        if max(h, w) <= max_size:
            return plane

    # Round the averaging factor *down*, so the result is always at least
    # max_size on its long edge (up to 2x bigger). The display then only
    # ever scales down a little, which looks smooth; rounding up would
    # leave it too small and get stretched back up.
    factor = max(1, int(max(h, w) // max_size))
    if factor == 1:
        return plane
    hh, ww = (h // factor) * factor, (w // factor) * factor
    if hh == 0 or ww == 0:
        return plane[::factor, ::factor]
    block = np.asarray(plane[:hh, :ww], dtype=np.float32).reshape(
        hh // factor, factor, ww // factor, factor
    )
    finite = np.isfinite(block)
    if finite.all():
        return block.mean(axis=(1, 3))
    total = np.where(finite, block, 0.0).sum(axis=(1, 3))
    count = finite.sum(axis=(1, 3))
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(count > 0, total / np.maximum(count, 1), np.nan).astype(np.float32)


def _stretch_rgb(planes, max_size: int) -> np.ndarray:
    """Downsamples and stretches three colour planes, returning an
    (H, W, 3) uint8 array. Each channel is stretched independently - the
    same approach the standalone channel-mapping tool on the website uses
    for combining separate R/G/B exposures - which also acts as a simple
    automatic colour balance for preview purposes."""
    channels = []
    for plane in planes:
        stretched = _percentile_stretch(_downsample(plane, max_size))
        channels.append((stretched * 255.0).astype(np.uint8))
    return np.stack(channels, axis=-1)


_BAYER_PATTERNS = {"RGGB", "BGGR", "GRBG", "GBRG"}


def _bayer_pattern(hdr, shape) -> str | None:
    """Returns the colour-filter pattern for a raw one-shot-colour frame
    (e.g. a Seestar sub-frame), or None for a normal mono image.

    Raw colour-camera frames are stored as a single grey plane where each
    pixel sat under a red, green or blue filter in a repeating 2x2 pattern;
    the BAYERPAT keyword says which. XBAYROFF/YBAYROFF shift the pattern,
    and ROWORDER = BOTTOM-UP means the rows were written in reverse, which
    flips the pattern vertically (only matters for an odd shift)."""
    pattern = str(hdr.get("BAYERPAT", "") or "").strip().upper()
    if pattern not in _BAYER_PATTERNS:
        return None
    grid = [[pattern[0], pattern[1]], [pattern[2], pattern[3]]]
    xoff = int(hdr.get("XBAYROFF", 0) or 0) % 2
    yoff = int(hdr.get("YBAYROFF", 0) or 0) % 2
    if str(hdr.get("ROWORDER", "") or "").strip().upper() == "BOTTOM-UP" and shape[0] % 2 == 1:
        yoff ^= 1
    if xoff:
        grid = [[row[1], row[0]] for row in grid]
    if yoff:
        grid = [grid[1], grid[0]]
    return grid[0][0] + grid[0][1] + grid[1][0] + grid[1][1]


def _debayer_superpixel(data: np.ndarray, pattern: str):
    """Turns a raw colour-filter frame into R, G, B planes at half
    resolution: each 2x2 block becomes one colour pixel (its red, the
    average of its two greens, and its blue). This "superpixel" method is
    the simplest and fastest way to debayer, and half resolution is plenty
    for thumbnails and the preview popup."""
    h, w = data.shape
    data = np.asarray(data[: h - h % 2, : w - w % 2], dtype=np.float32)
    sub = {
        (0, 0): data[0::2, 0::2], (0, 1): data[0::2, 1::2],
        (1, 0): data[1::2, 0::2], (1, 1): data[1::2, 1::2],
    }
    positions = {(0, 0): pattern[0], (0, 1): pattern[1], (1, 0): pattern[2], (1, 1): pattern[3]}
    greens = [sub[pos] for pos, colour in positions.items() if colour == "G"]
    red = next(sub[pos] for pos, colour in positions.items() if colour == "R")
    blue = next(sub[pos] for pos, colour in positions.items() if colour == "B")
    return red, (greens[0] + greens[1]) / 2.0, blue


def make_thumbnail_array(path: str, max_size: int = 256) -> np.ndarray:
    """
    Decode a FITS file and return an auto-stretched uint8 array that fits
    within max_size on the longest edge: (H, W) for mono images, (H, W, 3)
    for colour.

    Colour comes from two kinds of file:
      - a genuine 3-plane RGB cube (FITS NAXIS3 = 3), e.g. Seestar's
        stacked output;
      - a raw one-shot-colour frame with a BAYERPAT keyword, e.g. a Seestar
        sub-frame, which is debayered (superpixel, half resolution).

    max_size=0 disables downsampling (full native resolution), used for
    full-resolution image export.
    """
    try:
        with fits.open(path, memmap=True, ignore_missing_end=True) as hdul:
            hdu = _find_image_hdu(hdul)
            if hdu is None:
                raise ValueError("No image data found")
            data = np.asarray(hdu.data)
            hdr = hdu.header.copy()
    except ValueError as exc:
        if not _is_memmap_scaling_error(exc):
            raise
        with fits.open(path, memmap=False, ignore_missing_end=True) as hdul:
            hdu = _find_image_hdu(hdul)
            if hdu is None:
                raise ValueError("No image data found")
            data = np.asarray(hdu.data)
            hdr = hdu.header.copy()

    # A genuine RGB image stored as a FITS cube has exactly 3 planes on the
    # leading axis (astropy/numpy shape (3, H, W) for FITS NAXIS3=3) - e.g.
    # Seestar's stacked color output. That's different from an arbitrary
    # multi-plane cube (a MUSE-style spectral cube, or a (1, H, W) wrapper
    # around a single frame), which still collapses to one plane below.
    if data.ndim == 3 and data.shape[0] == 3:
        return _stretch_rgb([data[0], data[1], data[2]], max_size)

    # Collapse any other extra dimensions (e.g. a (1, H, W) cube) down to 2D.
    while data.ndim > 2:
        data = data[0]

    pattern = _bayer_pattern(hdr, data.shape)
    if pattern is not None and min(data.shape) >= 2:
        r, g, b = _debayer_superpixel(data, pattern)
        return _stretch_rgb([r, g, b], max_size)

    stretched = _percentile_stretch(_downsample(data, max_size))
    return (stretched * 255.0).astype(np.uint8)
