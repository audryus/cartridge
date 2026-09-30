#!/usr/bin/env python3
"""Scan the ROM library and write cartridge's JSON state.

Walks the library root, opens zip/7z/rar archives without unpacking them, and
classifies every game it finds by file extension using the extension table
derived from libretro-core-info. Archives are read two levels deep, because a
ROM set is often a zip of zips.

Writes three files into <plugin>/state:
  roms.json     the library, plus per-rom favorites and play times
  consoles.json every console cartridge knows about, plus the core you picked
  cores.json    the RetroArch cores installed on this machine

Favorites, play times and hand-assigned consoles survive a rescan: the rom id
is a hash of where the file lives, so a rom that did not move keeps its state.

stdout is a single JSON summary line, for the UI to report. Progress goes to
stderr.
"""

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cartridge_lib as lib  # noqa: E402

STATE_VERSION = 4


def log(message):
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


class Detector:
    """Extension -> console, derived from libretro-core-info.

    An extension claimed by exactly one console is that console. An extension
    several consoles claim -- `.bin` on its own, Genesis or PlayStation -- is
    Unknown, because cartridge does not guess. A `.bin` next to a `.cue` never
    reaches this: that pairing is a PlayStation disc and nothing else.
    """

    def __init__(self):
        self.cores = lib.load_core_info()
        self.ext_map = lib.build_extension_map(self.cores)
        self.catalog = {}
        for core in self.cores.values():
            self.catalog.setdefault(core["systemId"], core["system"])

    def console_for(self, ext):
        ids = self.ext_map.get(ext)
        if not ids:
            return lib.UNKNOWN_ID, "unmapped"
        if len(ids) == 1:
            return ids[0], "auto"
        return lib.UNKNOWN_ID, "ambiguous"


# --------------------------------------------------------------- rom records

def make_rom(detector, name, container, entry, parent, depth, kind, extras, size=0, crc=""):
    member = entry["name"] if isinstance(entry, dict) else entry
    console, reason = (lib.PSX_SYSTEM_ID, "cuepair") if kind == "cue" \
        else detector.console_for(lib.ext_of(member))
    return {
        "id": lib.stable_id([container, parent or "", member or ""]),
        "name": name,
        "console": console,
        "reason": reason,
        "path": container,
        "entry": member or "",
        "parent": parent or "",
        "depth": depth,
        "ext": lib.ext_of(member) if member else "",
        "kind": kind,
        "extras": extras or [],
        # Size and CRC are what tell the same game stored in two places apart
        # from two different games that happen to share a name.
        "size": int(size or 0),
        "crc": crc or "",
        "favorite": False,
        "lastPlayed": 0,
    }


def read_cue(container, member, parent, depth):
    if depth == 2:
        return lib.Archive(path=container, ext=lib.ext_of(parent)) \
            .nested(parent).read(member)
    if depth == 0:
        try:
            with open(os.path.join(container, member), "rb") as handle:
                return handle.read(1 << 20)
        except OSError:
            return None
    return lib.Archive(path=container).read(member)


def process_listing(detector, container, entries, parent, depth, roms):
    """Turn one flat listing of files into ROM records.

    `entries` are {name, size, crc} relative to `container`, which is a
    directory for loose files and an archive path for archived ones. `parent`
    names the archive-inside-an-archive they came from, empty at the first
    level.
    """
    by_basename = {}
    size_of = {}
    for entry in entries:
        by_basename.setdefault(os.path.basename(entry["name"]), entry["name"])
        size_of[entry["name"]] = entry.get("size", 0)
    names = [entry["name"] for entry in entries]
    consumed = set()

    # PlayStation first. A cue sheet is not a game on its own; it is the
    # descriptor of a disc made of the files it points at, and those files must
    # not also turn up as separate Unknown entries.
    for entry in entries:
        name = entry["name"]
        if lib.ext_of(name) != "cue" or name in consumed:
            continue
        companions = lib.cue_companions(read_cue(container, name, parent, depth), names)
        for companion in companions:
            match = by_basename.get(companion)
            if match and match not in consumed:
                consumed.add(match)
        consumed.add(name)
        # A disc is the cue plus everything the cue points at, so that is what
        # gets compared when the same PlayStation game is in the library twice.
        total = entry.get("size", 0)
        for companion in companions:
            total += size_of.get(by_basename.get(companion, ""), 0)
        roms.append(make_rom(detector, lib.stem_of(name), container, entry,
                             parent, depth, "cue", companions, total, ""))

    for entry in entries:
        name = entry["name"]
        if name in consumed:
            continue
        ext = lib.ext_of(name)
        if not ext or ext in lib.IGNORED_EXTS or lib.is_archive_ext(ext):
            continue
        roms.append(make_rom(detector, lib.stem_of(name), container, entry,
                             parent, depth, "file", [], entry.get("size", 0),
                             entry.get("crc", "")))


# ------------------------------------------------------------------ scanning

def scan_archive(detector, archive, roms, nested_jobs):
    """Read one archive and note the archives nested inside it. Runs in a
    thread pool: reading a rom set means decompressing gigabytes."""
    entries = lib.Archive(path=archive).entries()
    if entries is None:
        log("cartridge: cannot read %s" % archive)
        return
    process_listing(detector, archive, entries, "", 1, roms)
    nested_jobs.extend((archive, entry["name"]) for entry in entries
                       if lib.is_archive_ext(lib.ext_of(entry["name"])))


def scan_nested(detector, job, roms):
    """Second level: a zip of zips, which is how ROM sets usually ship."""
    archive, member = job
    inner = lib.Archive(path=archive).nested(member)
    entries = inner.entries() if inner.data is not None else None
    if entries is None:
        return
    process_listing(detector, archive, entries, member, 2, roms)


def walk_library(root):
    """Every container: {path: [filenames]} for loose folders, plus every
    archive found. A container is the unit that gets rescanned."""
    loose = {}
    archives = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        here = []
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            if lib.is_archive_ext(lib.ext_of(name)):
                archives.append(os.path.join(dirpath, name))
            else:
                here.append(name)
        loose[dirpath] = here
    return loose, archives


def loose_entries(dirpath, names):
    """[{name, size, crc}] for the files sitting loose in a folder."""
    listing = []
    for name in names:
        try:
            size = os.path.getsize(os.path.join(dirpath, name))
        except OSError:
            size = 0
        listing.append({"name": name, "size": size, "crc": ""})
    return listing


def fingerprint(path):
    """(size, mtime) of a container. A folder's mtime moves when a file is
    added or removed, which is the only change that alters a folder listing."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return [int(info.st_size), int(info.st_mtime)]


# ------------------------------------------------------------------ persist

def previous_state(path):
    """roms.json from the last scan: the per-rom state, and which containers
    were read at which size and mtime."""
    document = lib.read_json(path, {}) or {}
    previous = {}
    for rom in document.get("roms") or []:
        if isinstance(rom, dict) and rom.get("id"):
            previous[rom["id"]] = rom
    # A state file written by an older cartridge describes containers in a
    # shape this build cannot trust, so it is read for the user's favorites and
    # play times but every container is rescanned.
    if document.get("version") != STATE_VERSION:
        return previous, {}
    containers = document.get("containers")
    if not isinstance(containers, dict):
        containers = {}
    return previous, containers


def carry_over(roms, previous):
    kept = 0
    for rom in roms:
        old = previous.get(rom["id"])
        if not old:
            continue
        rom["favorite"] = bool(old.get("favorite"))
        try:
            rom["lastPlayed"] = int(old.get("lastPlayed") or 0)
        except (TypeError, ValueError):
            rom["lastPlayed"] = 0
        # Only a console the user picked by hand outranks detection. An
        # automatic result is recomputed every scan, so installing a core that
        # disambiguates an extension takes effect on the next refresh.
        if old.get("reason") == "assigned" and old.get("console"):
            rom["console"] = old["console"]
            rom["reason"] = "assigned"
        kept += 1
    return kept


def write_consoles(path, detector, detected, ext_map):
    """Console table, minus the consoles this library has no roms for.

    Only the detected rows are rewritten, so a core the user picked for a
    console stays picked."""
    def mutate(document):
        consoles = document.get("consoles")
        if not isinstance(consoles, dict):
            consoles = {}
        for console_id in list(consoles):
            if console_id not in detected:
                del consoles[console_id]
        for console_id, name in detector.catalog.items():
            if console_id not in detected:
                continue
            entry = consoles.setdefault(console_id, {})
            entry["id"] = console_id
            entry["name"] = name
            entry.setdefault("core", "")
            entry["exts"] = sorted(ext for ext, ids in ext_map.items()
                                   if console_id in ids)
        document["consoles"] = consoles
        document["catalog"] = dict(sorted(detector.catalog.items()))
        document["generated"] = lib.now()
    lib.update_json(path, mutate, default=lambda: {"consoles": {}, "catalog": {}})


def write_cores(path, cores, installed):
    """Only cores the user actually has, so the config popup never offers
    something that is not on disk.

    `label` is what the dropdown shows: the corename from the database, which
    is how cores are known by name ("Mupen64Plus-Next"), falling back to the
    file name for a core with no usable metadata."""
    entries = []
    for core_id, so_path in sorted(installed.items()):
        info = cores.get(core_id)
        entries.append({
            "id": core_id,
            "label": info["corename"] if info else core_id,
            "name": info["name"] if info else core_id,
            "systemId": info["systemId"] if info else "",
            "system": info["system"] if info else "",
            "exts": info["exts"] if info else [],
            "path": so_path,
        })
    entries.sort(key=lambda item: (item["system"], item["label"].lower()))
    lib.write_json(path, {"generated": lib.now(), "cores": entries})


# ---------------------------------------------------------------------- main

def same_content(a, b):
    """Whether two roms with the same name are the same bytes.

    zips carry a CRC in their central directory, which is exact and free. For
    7z and rar there is no CRC, so equal size is the best signal available."""
    if a.get("size", 0) != b.get("size", 0):
        return False
    if a.get("crc") and b.get("crc"):
        return a["crc"] == b["crc"]
    return True


def prefer_shallow(roms):
    """Keep one copy of a game that is stored in more than one place.

    The same game loose in a folder and again inside a rom set is a duplicate,
    not a decision to make: the loose copy needs no unpacking, so it wins and
    the archived copies are dropped. Copies that share a name but not their
    contents are left alone -- that is a conflict, and the user picks.

    Anything the dropped copies had earned (a favorite, a play time) moves to
    the copy that stays, so a rescan never quietly loses it.
    """
    groups = {}
    for rom in roms:
        groups.setdefault((rom["console"], rom["name"].lower()), []).append(rom)

    kept = []
    dropped = 0
    for copies in groups.values():
        if len(copies) == 1:
            kept.append(copies[0])
            continue
        first = copies[0]
        if any(not same_content(first, other) for other in copies[1:]):
            kept.extend(copies)
            continue
        keeper = min(copies, key=lambda rom: (rom["depth"], len(rom["path"])))
        for copy in copies:
            if copy is keeper:
                continue
            if copy.get("favorite"):
                keeper["favorite"] = True
            if (copy.get("lastPlayed") or 0) > (keeper.get("lastPlayed") or 0):
                keeper["lastPlayed"] = copy["lastPlayed"]
            dropped += 1
        kept.append(keeper)
    return kept, dropped


def main():
    root = lib.plugin_root()
    library = lib.roms_root()

    if not os.path.isdir(library):
        lib.fail("cartridge: no ROM library at %s" % library, 2)

    log("cartridge: scanning %s" % library)
    detector = Detector()
    installed = lib.installed_cores()
    previous, known_containers = previous_state(lib.roms_json(root))

    # Roms from the last scan, grouped by the container they came out of, so a
    # container that has not been touched since can be reused untouched.
    by_container = {}
    for rom in previous.values():
        by_container.setdefault(rom.get("path"), []).append(rom)

    roms = []
    containers = {}
    reused = 0
    stale = []

    loose, archives = walk_library(library)
    for dirpath, names in loose.items():
        if not names:
            continue
        mark = fingerprint(dirpath)
        containers[dirpath] = mark
        if mark and known_containers.get(dirpath) == mark:
            roms.extend(by_container.get(dirpath, []))
            reused += 1
        else:
            stale.append((dirpath, loose_entries(dirpath, names), "", 0))

    for archive in archives:
        mark = fingerprint(archive)
        containers[archive] = mark
        if mark and known_containers.get(archive) == mark:
            roms.extend(by_container.get(archive, []))
            reused += 1
        else:
            stale.append((archive, None, "", 1))

    log("cartridge: %d containers, %d unchanged, %d to read"
        % (len(containers), reused, len(stale)))

    for container, names, parent, kind in stale:
        if kind == 0:
            process_listing(detector, container, names, parent, 0, roms)

    workers = min(8, os.cpu_count() or 4)
    nested_jobs = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda path: scan_archive(detector, path, roms, nested_jobs),
                      [container for container, _, _, kind in stale if kind == 1]))
    log("cartridge: %d archives inside archives" % len(nested_jobs))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda job: scan_nested(detector, job, roms), nested_jobs))

    carried = carry_over(roms, previous)
    roms, dropped = prefer_shallow(roms)
    roms.sort(key=lambda rom: (rom["console"], rom["name"].lower()))

    detected = {}
    for rom in roms:
        detected[rom["console"]] = detected.get(rom["console"], 0) + 1

    previous_ids = set(previous)
    current_ids = {rom["id"] for rom in roms}
    summary = {
        "root": library,
        "generated": lib.now(),
        "total": len(roms),
        "added": len(current_ids - previous_ids),
        "removed": len(previous_ids - current_ids),
        "carried": carried,
        "duplicates": dropped,
        "reused": reused,
        "consoles": len(detected),
        "unknown": detected.get(lib.UNKNOWN_ID, 0),
        "cores": len(installed),
        "byConsole": detected,
    }

    lib.write_json(lib.roms_json(root), {
        "version": STATE_VERSION,
        "generated": summary["generated"],
        "root": library,
        "containers": containers,
        "roms": roms,
    })
    write_consoles(lib.consoles_json(root), detector, detected, detector.ext_map)
    write_cores(lib.cores_json(root), detector.cores, installed)

    log("cartridge: %d roms, %d consoles, %d duplicates dropped, +%d -%d"
        % (summary["total"], summary["consoles"], dropped,
           summary["added"], summary["removed"]))
    sys.stdout.write(json.dumps(summary) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
