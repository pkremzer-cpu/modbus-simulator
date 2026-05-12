# Modbus Simulator

Modbus TCP simulator with a PyQt6 GUI for macOS. Includes a full-featured server (slave),
a client (master), value simulation (ramp / sine / random / script), fault injection,
live traffic log, and a register trend chart.

**Status:** scaffolding only — core and GUI not yet implemented. See the roadmap below.

---

## Requirements

- macOS 13 (Ventura) or newer
- Python 3.12
- [uv](https://docs.astral.sh/uv/) — dependency and venv manager
- [Homebrew](https://brew.sh) (for `create-dmg` at release time)

```sh
brew install uv              # if not yet installed
brew install create-dmg      # only needed when building the DMG
```

## Development setup

```sh
cd /Users/KremzerPeter/Claude/Modbus
uv sync                      # creates .venv and installs runtime + dev deps
./scripts/dev_run.sh         # runs the app from source
```

Run the test suite:

```sh
uv run pytest                # all tests
uv run pytest -m "not integration"   # unit only
uv run pytest --cov          # with coverage (target: ≥80% on core/)
```

Lint and type-check:

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

## Building the macOS `.app` bundle and DMG

```sh
./scripts/build_app.sh       # produces dist/ModbusSimulator.app
./scripts/build_dmg.sh       # produces dist/ModbusSimulator-<version>.dmg
```

### First-run note (Gatekeeper)

The app is **not code-signed**. On first launch macOS will block it. Either:

1. Right-click the `.app` → *Open* → *Open* in the dialog, or
2. Remove the quarantine attribute:

    ```sh
    xattr -dr com.apple.quarantine /Applications/ModbusSimulator.app
    ```

If you have an Apple Developer ID and want a signed/notarised build, let us know —
the build script is scaffolded for it but signing is off by default.

## Running on Windows 11

The code is platform-independent — every path uses `pathlib.Path`, the
script-sandbox `SIGALRM` timeout is `hasattr`-guarded (silently no-op on
Windows), and the Qt UI runs natively on Win32.

### Development mode

```pwsh
# 1. Install uv (any one of these)
winget install Astral.Uv
# -or-: irm https://astral.sh/uv/install.ps1 | iex

# 2. Sync deps and run from source
uv sync
.\scripts\dev_run.bat
```

### Building a Windows `.exe`

PyInstaller bundles the app into a standalone folder + `.exe` (no system Python required to run):

```pwsh
.\scripts\build_exe.ps1
# → dist\ModbusSimulator\ModbusSimulator.exe
```

PyInstaller is installed automatically as a Windows-only dev dep
(see `pyproject.toml` markers).

### Building a Windows installer (`KremzerPeterModbusTCP-Setup-x.y.z.exe`)

Requires [Inno Setup 6](https://jrsoftware.org/isinfo.php):

```pwsh
winget install JRSoftware.InnoSetup
.\scripts\build_installer.ps1
# → dist\KremzerPeterModbusTCP-Setup-0.1.0.exe
```

### CI-built artefacts (no Windows machine needed)

Push the repo to GitHub — `.github/workflows/build-windows.yml` runs on every
push and PR using a `windows-latest` runner. It runs the full test suite, then
PyInstaller, then (optionally) Inno Setup, and uploads the `.exe` bundle and
installer as workflow artefacts that you can download.

### Known Windows caveats

| Item | Note |
|---|---|
| Port 502 | Privileged — run the app `As administrator` or use port ≥ 1024. |
| Firewall prompt | First server start asks for "Allow access"; grant it (or pre-add in Windows Defender Firewall). |
| Script-sandbox timeout | `SIGALRM` is Unix-only; on Windows the AST whitelist remains the authoritative gate (timeout silently no-op). |
| Font | macOS default `Menlo` falls back to Windows default `Consolas`. |

## Known macOS limitations

- **Port 502** (the standard Modbus TCP port) is privileged. Either run the app with
  `sudo`, use port **5020** (non-privileged, default in the UI), or set the
  `CAP_NET_BIND_SERVICE`-equivalent via `pfctl` port redirect.
- **Firewall prompt:** macOS will ask for incoming-connection permission the first
  time the server starts. Allow it, or add the app manually under
  *System Settings → Network → Firewall → Options*.

## Project layout

```
src/modbus_simulator/
  core/       protocol + data-plane (server, client, datastore, simulator, codec, ...)
  gui/        PyQt6 widgets, one tab per major feature
  config/     pydantic schema + JSON persistence
  i18n/       translations (hu, en)
tests/
  unit/       fast, no network
  integration/ real TCP loopback, marked with pytest.mark.integration
scripts/      dev_run, build_app, build_dmg
resources/    icons, DMG background
docs/         user guide
```

## Roadmap

1. ✅ Project skeleton, deps, build scripts
2. ⬜ `core/` modules (TDD) — datastore, codec, simulator, exception rule engine, script sandbox
3. ⬜ `core/` network wrappers — server, client, traffic log
4. ⬜ `gui/` — main window, all six tabs
5. ⬜ `.app` bundle + DMG — first successful build
6. ⬜ Integration tests — every FC, every exception code, load test
7. ⬜ User guide with screenshots

## License

MIT — see [LICENSE](LICENSE).
