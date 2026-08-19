# FITS Viewer

[![Windows](https://img.shields.io/badge/Windows-0078D6?style=flat&logo=windows&logoColor=white)](../../releases)
[![macOS](https://img.shields.io/badge/macOS-000000?style=flat&logo=apple&logoColor=white)](../../releases)
[![Linux](https://img.shields.io/badge/Linux-FCC624?style=flat&logo=linux&logoColor=black)](../../releases)

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
   `FITS-Viewer-macOS.zip`, or `FITS-Viewer-Linux.AppImage`.
3. **Windows**: double-click it. No Python, no install, nothing else
   needed.
4. **Mac**: unzip the download, then right-click `FITS Viewer.app` ->
   **Open** -> **Open** (needed once, since this isn't a signed app -
   after that it opens normally by double-clicking).
5. **Linux**: right-click the `.AppImage` file -> Properties ->
   Permissions -> allow executing as a program, then double-click it.
   (Or from a terminal: `chmod +x FITS-Viewer-Linux.AppImage` then
   `./FITS-Viewer-Linux.AppImage`.)

## Troubleshooting

If Mac or Linux still won't open using the steps above (e.g. an older
system, unusual security settings, or a file manager that doesn't
support the "allow execute" toggle), these terminal commands are a
reliable fallback - thanks to **Dr Michael Cowley** for finding and
sharing these. (Written against the older raw-binary builds - if
you're on the current `.app`/`.AppImage` release, the file names below
won't match exactly, but `chmod +x` and, on Mac, `xattr -d
com.apple.quarantine` on whatever file you actually downloaded is the
same underlying fix.)

**Mac:**
```
chmod +x FITS-Viewer-macOS
xattr -d com.apple.quarantine FITS-Viewer-macOS
./FITS-Viewer-macOS
```
(First line adds execute permission, second removes the Gatekeeper
quarantine flag, third runs it. Same idea as running other unsigned
scientific tools like SAOImageDS9 or TOPCAT.)

**Linux:**
```
chmod +x FITS-Viewer-Linux
./FITS-Viewer-Linux
```

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

### Other planned improvements
- Custom app icon (currently the default PyInstaller icon)
- Heatmap/colormap display mode for derived data products (e.g. SAMI
  SFR/velocity/dispersion maps) rather than the grayscale auto-stretch
  used for raw exposures - needs a real sample file to design properly
  (scaling and colormap choice differ a lot between e.g. a flux map and
  a velocity map)

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
