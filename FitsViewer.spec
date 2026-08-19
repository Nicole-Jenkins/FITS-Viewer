# -*- mode: python ; coding: utf-8 -*-
# Build spec for FITS Viewer's standalone .exe.
#
# Both PySide6 (Qt platform plugins, e.g. qwindows.dll) and astropy
# (package data files) need their non-code files explicitly collected -
# a bare `pyinstaller app.py` will build something that LOOKS successful
# but crashes on launch on a machine without Python installed, because
# those files silently got left behind. collect_all() grabs everything.

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

for pkg in ("astropy", "PySide6"):
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="FITS Viewer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,       # GUI app - no terminal window behind it
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# On Mac specifically, wrap the raw binary in a proper .app bundle.
# Without this, Finder treats the output as a bare Unix executable
# rather than a real application - the standard Gatekeeper "right-click
# -> Open" workaround doesn't apply cleanly to that, which is why Mac
# users previously had to go through Terminal with xattr manually.
# A real .app bundle restores the normal right-click flow.
# This step is a no-op on Windows/Linux - only runs when PyInstaller
# itself is running on macOS, which the build workflow already ensures
# per-platform.
import sys

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name="FITS Viewer.app",
        icon=None,  # TODO: set once a custom app icon exists (see roadmap)
        bundle_identifier="com.nicolejenkins.fitsviewer",
        info_plist={
            "NSHighResolutionCapable": "True",
        },
    )
