# Building the .exe (dev notes)

Not linked from README on purpose - this is for you, not for people
downloading the app.

Run whenever the code changes and you need a new release build.

## Steps

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
