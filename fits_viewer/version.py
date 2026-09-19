"""
Single source of truth for the app's current version number.

Compared against the latest published GitHub release tag on startup
(see update_checker.py) to decide whether to show an update notice.

This value is not read from the git tag automatically - it must be
bumped by hand before building each new release, or the update check
will compare against a stale number.
"""

APP_VERSION = "1.2.0"
