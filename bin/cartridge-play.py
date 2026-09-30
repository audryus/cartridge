#!/usr/bin/env python3
"""Launch one rom in RetroArch.

RetroArch cannot open a zip inside a zip, and a cue sheet is useless without
the .bin files next to it, so the game is staged on disk first: the members
that make it up are streamed out of their archive into a private directory
under ~/.cache/cartridge/staging, and RetroArch is pointed at the real file.
Staged roms are capped in total; the least recently played make room. The staged copy is
reused until the archive it came from changes, so replaying a 1.5 GB iso is
instant the second time.

  cartridge-play <pluginRoot> <romId>

stdout is a single JSON line: {"ok": true, ...} or {"ok": false, "error": ...}
"""

import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cartridge_lib as lib  # noqa: E402

RETROARCH = "retroarch"
MARKER = ".cartridge-staged.json"
# Staging the largest thing in the library must not blow up the disk.
MAX_STAGED_BYTES = 8 << 30
# All staged roms together. Past this, the least recently played are removed
# to make room. CARTRIDGE_STAGING_MAX (bytes) overrides it.
DEFAULT_STAGING_CAP = 16 << 30
# Room to leave on the disk after staging.
FREE_MARGIN = 512 << 20


def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def staging_cap():
    try:
        return int(os.environ.get("CARTRIDGE_STAGING_MAX") or DEFAULT_STAGING_CAP)
    except ValueError:
        return DEFAULT_STAGING_CAP


def staging_dir(rom_id):
    """A private directory for one rom, under ~/.cache/cartridge/staging.

    Not /tmp: on this system /tmp is a tmpfs, so every staged game there was
    held in RAM until the next reboot."""
    base = lib.private_dir(lib.extract_root())
    return lib.private_dir(os.path.join(base, rom_id))


def source_signature(rom):
    """(size, mtime) of the file the rom lives in, or of the archive that holds
    it. If this has not moved, the staged copy is still correct."""
    try:
        info = os.stat(rom["path"])
        return [int(info.st_size), int(info.st_mtime)]
    except OSError:
        return None


def dir_size(path):
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def last_used(path):
    marker = lib.read_json(os.path.join(path, MARKER), {}) or {}
    try:
        return int(marker.get("used") or marker.get("staged") or 0)
    except (TypeError, ValueError):
        return 0


def make_room(keep, needed):
    """Delete the least recently played staged roms until `needed` more bytes
    fit under the staging cap. `keep` (the rom being staged) is never
    touched. Returns the ids removed."""
    base = lib.extract_root()
    try:
        names = os.listdir(base)
    except OSError:
        return []
    others = []
    total = 0
    for name in names:
        path = os.path.join(base, name)
        if name == keep or not os.path.isdir(path) or os.path.islink(path):
            continue
        size = dir_size(path)
        total += size
        others.append((last_used(path), name, path, size))
    removed = []
    others.sort()
    cap = staging_cap()
    for _, name, path, size in others:
        if total + needed <= cap:
            break
        shutil.rmtree(path, ignore_errors=True)
        total -= size
        removed.append(name)
    return removed


def check_space(rom, target_dir):
    """Refuse before writing anything, not after: the size of what is about
    to be staged is known from the scan."""
    needed = int(rom.get("size") or 0)
    if needed > MAX_STAGED_BYTES:
        raise ValueError("%s is %d GB, over the %d GB staging limit"
                         % (rom.get("name"), needed >> 30, MAX_STAGED_BYTES >> 30))
    make_room(os.path.basename(target_dir), needed)
    free = shutil.disk_usage(target_dir).free
    if needed + FREE_MARGIN > free:
        raise ValueError("needs %d MB free in %s, only %d MB left"
                         % ((needed + FREE_MARGIN) >> 20, lib.extract_root(), free >> 20))


def stage(rom):
    """Put the playable file, and anything it needs, where RetroArch can open
    it. Returns (path, staged) -- staged is False when the file was already
    there and did not have to be written again."""
    if rom["depth"] == 0:
        # A loose file is already where RetroArch wants it.
        content = os.path.join(rom["path"], rom["entry"])
        if not os.path.exists(content):
            raise ValueError("missing file: %s" % content)
        return content, False

    wanted = [rom["entry"]] + list(rom.get("extras") or [])
    wanted = [name for name in wanted if name]
    if not wanted:
        raise ValueError("rom has no file to open")

    signature = source_signature(rom)
    target_dir = staging_dir(rom["id"])
    marker_path = os.path.join(target_dir, MARKER)
    marker = lib.read_json(marker_path, None)
    content = os.path.join(target_dir, os.path.basename(wanted[0]))

    if marker and marker.get("signature") == signature \
            and marker.get("wanted") == wanted \
            and all(os.path.exists(os.path.join(target_dir, os.path.basename(n)))
                    for n in wanted):
        marker["used"] = lib.now()
        lib.write_json(marker_path, marker)
        return content, False

    # Start from an empty directory, marker included: until every member is
    # written, nothing in here may pass for a complete copy.
    for name in os.listdir(target_dir):
        try:
            os.unlink(os.path.join(target_dir, name))
        except OSError:
            pass

    check_space(rom, target_dir)

    outer = lib.Archive(path=rom["path"])
    source = outer if rom["depth"] == 1 else outer.nested(rom["parent"])
    try:
        if not source.readable:
            raise ValueError("cannot read %s inside %s"
                             % (rom["parent"], os.path.basename(rom["path"])))
        written = 0
        for name in wanted:
            destination = os.path.join(target_dir, os.path.basename(name))
            partial = destination + ".part"
            if not source.extract(name, partial):
                raise ValueError("cannot extract %s from %s"
                                 % (name, rom["parent"] or os.path.basename(rom["path"])))
            os.replace(partial, destination)
            written += os.path.getsize(destination)
            if written > MAX_STAGED_BYTES:
                raise ValueError("%s is over the %d GB staging limit"
                                 % (rom.get("name"), MAX_STAGED_BYTES >> 30))
    finally:
        if source is not outer:
            source.close()
        outer.close()

    lib.write_json(marker_path, {"signature": signature, "wanted": wanted,
                                 "staged": lib.now(), "used": lib.now()})
    return content, True


def core_for(rom, consoles, cores):
    """The installed core the user assigned to this rom's console."""
    console_id = rom.get("console")
    if not console_id or console_id == lib.UNKNOWN_ID:
        raise ValueError("this rom has no console yet - configure it first")

    entry = (consoles.get("consoles") or {}).get(console_id) or {}
    core_id = entry.get("core") or ""
    if not core_id:
        raise ValueError("no core set for %s - pick one in Cartridge's config"
                         % (entry.get("name") or console_id))

    for core in cores.get("cores") or []:
        if core.get("id") == core_id:
            if not os.path.exists(core.get("path") or ""):
                raise ValueError("core %s is no longer installed" % core.get("label"))
            return core
    raise ValueError("core %s is no longer installed" % core_id)


def mark_played(root, rom_id, when):
    """Into user.json: a few bytes, not a rewrite of the library."""
    lib.update_user(root, lambda user: user["lastPlayed"].__setitem__(rom_id, when))


def main():
    if len(sys.argv) != 3:
        lib.fail(__doc__.strip(), 64)
    root = sys.argv[1]
    rom_id = sys.argv[2]

    state = lib.read_json(lib.roms_json(root), None)
    if not state:
        emit({"ok": False, "error": "no library scanned yet - press refresh"})
        return 2

    rom = None
    for candidate in state.get("roms") or []:
        if candidate.get("id") == rom_id:
            rom = candidate
            break
    if rom is None:
        emit({"ok": False, "error": "that rom is no longer in the library"})
        return 2
    rom = dict(rom)
    rom["console"], rom["reason"] = lib.effective_console(rom, lib.load_user(root))

    consoles = lib.read_json(lib.consoles_json(root), {}) or {}
    cores = lib.read_json(lib.cores_json(root), {}) or {}

    try:
        core = core_for(rom, consoles, cores)
        content, staged = stage(rom)
    except (ValueError, OSError) as problem:
        emit({"ok": False, "error": str(problem)})
        return 5

    if os.environ.get("CARTRIDGE_DRY_RUN") == "1":
        emit({"ok": True, "dryRun": True, "id": rom_id, "name": rom.get("name"),
              "core": core.get("label"), "content": content, "staged": staged})
        return 0

    # The log lives in cartridge's own cache. Next to the rom would put a file
    # in the user's library, which is not cartridge's to litter.
    try:
        log_dir = lib.private_dir(os.path.join(lib.cache_root(), "logs"))
        log = os.path.join(log_dir, "%s.log" % rom_id)
        with open(log, "ab") as sink:
            process = subprocess.Popen(
                [RETROARCH, "-L", core["path"], content],
                stdin=subprocess.DEVNULL, stdout=sink, stderr=sink,
                start_new_session=True)
    except (OSError, ValueError) as problem:
        emit({"ok": False, "error": "cannot start %s: %s" % (RETROARCH, problem)})
        return 6

    mark_played(root, rom_id, lib.now())
    emit({"ok": True, "id": rom_id, "name": rom.get("name"),
          "core": core.get("label"), "content": content,
          "staged": staged, "pid": process.pid, "at": int(time.time())})


if __name__ == "__main__":
    sys.exit(main())
