# FITS Viewer

A lightweight desktop browser for FITS astronomy image files - built with
PySide6 (Qt) and Astropy. Point it at a folder, get a thumbnail grid with
auto-stretched previews and readable FITS headers, without opening a
full processing suite just to see what's in a directory of exposures.

## Features (current)

- Folder tree browser, filtered to FITS files (`.fits`, `.fit`, `.fts`)
- Favourites panel - right-click any folder in the tree to pin it for
  quick access, right-click a favourite to remove it (persists between
  sessions)
- Auto-stretched thumbnail grid (median/MAD-based stretch, the same
  family of algorithm PixInsight and Siril use for their screen-stretch
  preview - handles real astro data properly instead of a naive min/max)
- Header panel showing key fields (Object, Filter, Exposure, Telescope,
  Instrument, RA/Dec, dimensions, etc.), merged correctly across
  multi-extension files (e.g. Hubble WFC3, where target metadata lives
  in the primary HDU but pixel data lives in a `SCI` extension)
- Background decoding via a Qt thread pool - the UI never freezes while
  a folder of hundreds of files is being processed
- Disk-backed thumbnail cache, keyed by file path + size + modified time
  + requested resolution, so reopening a folder doesn't mean re-decoding
  every file again
- Adjustable thumbnail size (slider)
- Full-size zoomable/pannable preview (select a file, press Space;
  scroll to zoom, drag to pan, Space/Esc to close)
- Export the auto-stretched image as PNG, JPEG, or TIFF ("Export
  Image..." toolbar button) - decoded at full native resolution, not
  the thumbnail preview size
- Handles NaN-filled regions correctly (e.g. drizzled HST products with
  rotated footprints) instead of rendering solid black/corrupted frames

## Download and run

1. Go to the [Releases](../../releases) page.
2. Download the file for your system: `FITS-Viewer-Windows.exe`,
   `FITS-Viewer-macOS`, or `FITS-Viewer-Linux`.
3. **Windows**: double-click it. No Python, no install, nothing else
   needed.
4. **Mac**: the first time you open it, macOS will warn about an
   "unidentified developer" - this is normal for free/unsigned
   software. Right-click the file -> **Open** -> **Open** to confirm,
   and it'll launch normally every time after that.
5. **Linux**: mark it executable first (`chmod +x FITS-Viewer-Linux`),
   then run it.

## Known limitations

- No color/RGB composite tool yet (see Roadmap)
- Alignment/registration between files is not yet implemented anywhere
  in the desktop app
- Very large FITS files (e.g. large HST mosaics) are downsampled for
  the thumbnail/preview but not specially optimised for memory - a
  huge single frame could be slow to open in the full-size viewer
- Thumbnail slider upscales the cached 220px thumbnail above that size
  rather than re-decoding, so it softens past the default size - this
  is a deliberate responsiveness/quality tradeoff, not a bug

## Roadmap

### Color composite tool (next major feature)
Porting the [web-based channel-mapping tool](channel_mapping.html) into
the desktop app, generalised to handle three distinct input cases:

- **Same-scope RGB triplets** (e.g. three separate R/G/B exposures from
  the same telescope): basic translation-only alignment (cross-correlation),
  then stack into an RGB composite. No rotation/scale correction planned
  initially - frames are expected to be roughly co-aligned already.
- **Single-filter files**: explicitly rejected from the composite tool -
  one filter is one channel of data, there's no color information to
  extract from it alone.
- **IFU datacubes** (e.g. MUSE): a different pipeline entirely. Requires
  redshift-aware extraction of specific narrowband wavelength slices
  (Hα, [O III], [S II]) from the cube rather than simple RGB stacking,
  since the target's actual emission lines are redshifted away from
  their rest wavelengths. Will be based on existing extraction logic,
  ported into this app.

## Project structure

```
app.py                    # Entry point - starts the Qt event loop
fits_viewer/
  fits_utils.py            # FITS reading, header merging, auto-stretch
  cache.py                 # Disk-backed thumbnail cache
  thumbnail_worker.py       # Background decode worker (QRunnable)
  main_window.py            # Main GUI - tree, grid, header panel
  image_viewer.py           # Full-size zoom/pan popup (Space key)
```

## Contributing

This is currently a solo project and not open to external code
contributions at this stage. Bug reports and feature suggestions via GitHub Issues
are welcome, but pull requests won't be merged - see LICENSE. Contact me to request 
to contribute.

## License

Custom license - see `LICENSE`. Free to download and run for personal
use. Modification and redistribution are not permitted.
