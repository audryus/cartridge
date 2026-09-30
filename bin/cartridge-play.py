#!/usr/bin/env python3
"""Launch one rom in RetroArch.

RetroArch cannot open a zip inside a zip, and a cue sheet is useless without
the .bin files next to it, so the game is staged on disk first: the members
that make it up are streamed out of their archive into a private directory
under /tmp, and RetroArch is pointed at the real file. The staged copy is
reused until the archive it came from changes, so replaying a 1.5 GB iso is
instant the second time.

  cartridge-play <pluginRoot> <romId>

stdout is a single JSON line: {"ok": true, ...} or {"ok": false, "error": ...}
"""

import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cartridge_lib as lib  # noqa: E402

RETROARCH = "retroarch"
MARKER = ".cartridge-staged.json"
# Staging the largest thing in the library must not blow up the disk.
MAX_STAGED_BYTES = 8 << 30


def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def staging_dir(rom_id):
    """A private directory under /tmp, 0700, named after the rom.

    /tmp is world-writable, so the directory is created with restrictive
    permissions and never reused if it already exists with wider ones."""
    base = lib.extract_root()
    if not os.access("/tmp", os.W_OK):
        runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
        base = os.path.join(runtime, "cartridge")
    target = os.path.join(base, rom_id)
    os.makedirs(base, mode=0o700, exist_ok=True)
    if os.path.isdir(target):
        try:
            os.chmod(target, 0o700)
        except OSError:
            pass
    os.makedirs(target, mode=0o700, exist_ok=True)
    return target


def source_signature(rom):
    """(size, mtime) of the file the rom lives in, or of the archive that holds
    it. If this has not moved, the staged copy is still correct."""
    try:
        info = os.stat(rom["path"])
        return [int(info.st_size), int(info.st_mtime)]
    except OSError:
        return None


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

    if marker and marker.get("signature") == signature \
            and marker.get("wanted") == wanted \
            and all(os.path.exists(os.path.join(target_dir, os.path.basename(n)))
                    for n in wanted):
        return os.path.join(target_dir, os.path.basename(wanted[0])), False

    # Start from an empty directory: a renamed or removed member must not leave
    # a stale file behind for RetroArch to open instead.
    for name in os.listdir(target_dir):
        if name == MARKER:
            continue
        try:
            os.unlink(os.path.join(target_dir, name))
        except OSError:
            pass

    if rom["depth"] == 1:
        archive = lib.Archive(path=rom["path"])
        written = 0
        for name in wanted:
            destination = os.path.join(target_dir, os.path.basename(name))
            if not archive.extract(name, destination):
                raise ValueError("cannot extract %s from %s"
                                 % (name, os.path.basename(rom["path"])))
            written += os.path.getsize(destination)
            if written > MAX_STAGED_BYTES:
                raise ValueError("needs more than %d GB of free space in %s"
                                 % (MAX_STAGED_BYTES >> 30, lib.extract_root()))
    else:
        inner = lib.Archive(path=rom["path"]).nested(rom["parent"])
        if inner.data is None:
            raise ValueError("cannot read %s inside %s"
                             % (rom["parent"], os.path.basename(rom["path"])))
        written = 0
        for name in wanted:
            destination = os.path.join(target_dir, os.path.basename(name))
            if not inner.extract(name, destination):
                raise ValueError("cannot extract %s from %s"
                                 % (name, rom["parent"]))
            written += os.path.getsize(destination)
            if written > MAX_STAGED_BYTES:
                raise ValueError("needs more than %d GB of free space in %s"
                                 % (MAX_STAGED_BYTES >> 30, lib.extract_root()))

    lib.write_json(marker_path, {"signature": signature, "wanted": wanted,
                                 "staged": lib.now()})
    return os.path.join(target_dir, os.path.basename(wanted[0])), True


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
    def mutate(document):
        for rom in document.get("roms") or []:
            if rom.get("id") == rom_id:
                rom["lastPlayed"] = when
                return
    lib.update_json(lib.roms_json(root), mutate, default=lambda: {"roms": []})


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

    # The log lives under cartridge's own /tmp directory. Next to the rom would
    # put a file in the user's library, which is not cartridge's to litter.
    log_dir = os.path.join(lib.extract_root(), "logs")
    os.makedirs(log_dir, mode=0o700, exist_ok=True)
    log = os.path.join(log_dir, "%s.log" % rom_id)
    try:
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
