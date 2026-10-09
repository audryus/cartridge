# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Cartridge is an Omarchy 4 (Quattro / Quickshell) shell plugin: a bar widget plus a
process-wide service that browses a ROM library (`~/Games/roms`) and launches games in
RetroArch. `README.md` is the detailed design record — read the relevant section before
changing detection, deduplication, staging or core handling.

## Commands

```bash
make test          # python3 tests/test_scan.py && python3 tests/test_fixes.py && node tests/test_model.js
make lint-qml      # qmllint every ui/*.qml (prints pre-existing missing-property warnings; compare counts before/after)
make smoke         # drive the *running* shell over IPC and check its log (SMOKE_REFRESH=1 also rescans the real library)
make check         # test + lint-qml
make scan          # run the scanner against the real library, no UI
make restart-shell # QML changes only take effect after this
```

Single test: `python3 tests/test_fixes.py CoreChoiceTest.test_core_survives_a_scan_without_that_console`
(plain `unittest`; same for `test_scan.py`). `test_model.js` has no filter — it runs in node
against `ui/CartridgeModel.js` loaded into a `vm` sandbox; new exports must be added to the
`globalThis.api` list at its top.

CI (`.github/workflows/test.yml`) runs `make test` in an Arch container with
`libretro-core-info`, `libarchive` and only `libretro-snes9x` installed — tests that need a
particular core must mock `lib.installed_cores` or `skipTest` when core-info is missing.

## Live install and state — be careful

The plugin is installed as a symlink: `~/.config/omarchy/plugins/audryus.cartridge -> this repo`.
So `state/` here (gitignored) is the user's **real** library state, and running `bin/*` scripts
without overrides mutates it. Tests isolate themselves via `CARTRIDGE_ROMS_ROOT`,
`CARTRIDGE_STATE_DIR` and `CARTRIDGE_CACHE_DIR` (see `Fixture` in `tests/test_fixes.py`); do the
same for any ad-hoc experiment.

## Architecture

Two halves that communicate only through JSON files in `state/` and one-line JSON on stdout:

**Python (`bin/`)** does all real work. `cartridge_lib.py` is shared: paths, atomic
`write_json`, the cross-process `state_lock` (re-entrant, `fcntl` on `state/.lock`) and
`update_json` read-modify-write, the extension table, archive reading, core metadata.
- `cartridge-scan.py` — walks the library, opens archives two levels deep (zip via `zipfile`,
  7z/rar via `bsdtar`), detects consoles, dedups, writes `roms.json`, `consoles.json`, `cores.json`.
  Incremental: each container is fingerprinted (size+mtime) in `roms.json` and reused if unchanged;
  bump `STATE_VERSION` when the stored rom/container shape changes.
- `cartridge-state.py` — small mutations from the UI (favorite, assign console, assign a whole
  container, set core, re-read installed cores). Each prints `{"ok": ...}` JSON.
- `cartridge-play.py` — stages archived roms to `~/.cache/cartridge/staging/`, resolves the
  console's core from `consoles.json`/`cores.json`, starts `retroarch -L <core> <file>` with
  per-core quirks (Play! gets XWayland and a `rom0:ROMVER`).

**State files** (each has one owner of its shape): `roms.json` (scanner only — detection
results), `user.json` (user decisions keyed by rom id: favorites, play times, hand-assigned
consoles; rom id = hash of location), `consoles.json` (per-console core choice + catalog +
`romsStamp`), `cores.json` (installed cores only). Effective console = `user.assigned` over
detection (`lib.effective_console`; mirrored in JS `applyUser`).

**Console detection** has no hard-coded extension table: it is built from
`/usr/share/libretro/info/*.info` (`load_core_info`, `build_extension_map`). Hand-maintained
corrections live in `cartridge_lib.py`: `ALIASES` (merge database system names into one
console), `ALSO_RUNS` (multi-system cores), catch-all cores excluded, `NATIVE_EXTS` (a console's
own cartridge formats win), and ambiguous extensions go to Unidentified rather than guessing.
Console ids are `slug(canonical system name)`. Core ids are `normalize_core_id` of the `.so`
file name (Arch uses old names, e.g. `mednafen_psx_hw` = Beetle PSX HW), matched to metadata by
both corename and file name.

**Core defaults**: `RECOMMENDED_CORES` (console id → ordered core ids) is applied by
`apply_default_cores` during scan, `ensure_console` and `cartridge-state.py cores`, only to
consoles without `coreChosen`. `set_core` sets `coreChosen`, so "No core" is a sticky choice.
A default only applies if the core "runs" the console — `lib.runs_console` must stay in sync
with `runsConsole` in `CartridgeModel.js`.

**QML (`ui/`)**: `Service.qml` is mounted once by the shell (owns the single `CartridgeData`
store and the `IpcHandler` used by `omarchy-shell audryus.cartridge <open|close|toggle|config|refresh|status>`);
`shell.qml` is the per-bar widget and the two windows. `CartridgeData.qml` holds state, watches
the JSON files with `FileView`, and calls the Python scripts via `run(argv, cb)` (parses the
stdout JSON line); it reloads the large `roms.json` only when `consoles.json`'s `romsStamp`
changes. `CartridgeModel.js` is pure, import-free logic (sorting, search, conflicts, core
options) so it is testable in node — put logic there, not in QML. Windows are `PanelWindow`s
with exclusive keyboard focus (not `PopupCard`s) on purpose; secondary text uses an alpha of
`Color.foreground`, not a fixed grey.

## Conventions

Comments explain *why* (often citing the real-library incident that motivated the code) in
full sentences; match that style. Regression tests in `tests/test_fixes.py` are grouped one
class per problem, named after what used to go wrong. When behaviour changes, update the
matching README section too.
