# Building the .exe (dev notes)

Not linked from README on purpose - this is for you, not for people
downloading the app.

## Automated builds (Windows + Mac + Linux, via GitHub Actions)

`.github/workflows/build.yml` builds all three platforms automatically
in the cloud (GitHub's own servers, not your machine) whenever you
publish a GitHub Release. The built files get attached to that release
automatically - no manual `dist\` copying needed for any platform.

You can also trigger a test build without publishing a release: go to
your repo's **Actions** tab -> **Build Executables** -> **Run workflow**.
The results appear as downloadable "artifacts" on that run.

Known limitation: the Mac build is a raw unsigned binary, not a proper
`.app` bundle with an icon. It'll run, but macOS Gatekeeper shows an
"unidentified developer" warning on first launch per download - normal
for free/unsigned software, works around itself with one right-click ->
Open. Avoiding this warning entirely requires Apple's paid developer
program ($99/year) for code signing, which isn't set up here.

## Manual build (what the Actions workflow above also does, if you
## ever need to do it yourself on your own Windows machine)

1. Open cmd in the project folder.
2. Activate the venv and make sure pyinstaller is installed:
```
venv\Scripts\activate
pip install pyinstaller
```
3. Build:
```
pyinstaller FitsViewer.spec
```
4. Find `FITS Viewer.exe` inside the new `dist\` folder.
5. Test it from *outside* the project folder (e.g. copy to Desktop and
   run it there) before trusting it - running it from inside `dist`
   can accidentally still work using files from your venv that won't
   exist on someone else's machine.
6. Upload it to a new GitHub Release (tag it, e.g. `v1.1.0`).

`pyinstaller` is a build-only tool - deliberately not in
`requirements.txt`, since people running the app from source (i.e. you,
during development) install it separately, and end users never need it
at all since they just get the `.exe`.

`dist/` and `build/` are gitignored - the exe itself never gets
committed to the repo, it only ever lives in GitHub Releases.
