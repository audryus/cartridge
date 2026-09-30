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

Favorites, play times and hand-assigned consoles are not the scanner's: they
live in user.json, keyed by rom id, and the scanner only reads them. The rom id
is a hash of where the file lives, so a rom that did not move keeps its state.

stdout is a single JSON summary line, for the UI to report. Progress goes to
stderr.
"""

import json
import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cartridge_lib as lib  # noqa: E402

# 5: favorites and play times moved to user.json, duplicates are kept (hidden)
# instead of dropped, fingerprints are in nanoseconds.
STATE_VERSION = 5

# A rom set's nested archives are shared out between threads in slices this
# size at least, each slice opening the outer archive once.
MIN_NESTED_SLICE = 64


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
    }


def folder_reader(directory):
    def read(member):
        try:
            with open(os.path.join(directory, member), "rb") as handle:
                return handle.read(1 << 20)
        except OSError:
            return None
    return read


def process_listing(detector, container, entries, parent, depth, read):
    """Turn one flat listing of files into ROM records, and return them.

    `entries` are {name, size, crc} relative to `container`, which is a
    directory for loose files and an archive path for archived ones. `parent`
    names the archive-inside-an-archive they came from, empty at the first
    level. `read(member)` returns a member's bytes, from the archive that is
    already open -- a cue sheet must not cost a second pass over it.
    """
    roms = []
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
        companions = lib.cue_companions(read(name), names)
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
    return roms


# ------------------------------------------------------------------ scanning

def scan_archive(detector, path):
    """First level of one archive: (roms, nested archive members). Runs in a
    thread pool: reading a rom set means decompressing gigabytes."""
    with lib.Archive(path=path) as archive:
        entries = archive.entries()
        if entries is None:
            log("cartridge: cannot read %s" % path)
            return [], []
        roms = process_listing(detector, path, entries, "", 1, archive.read)
    nested = [entry["name"] for entry in entries
              if lib.is_archive_ext(lib.ext_of(entry["name"]))]
    return roms, nested


def scan_nested(detector, job):
    """Second level -- a zip of zips, which is how rom sets usually ship.

    One job is one outer archive and a slice of the archives inside it. The
    outer archive is opened once per job, never once per inner archive: for a
    5,000 member zip that is the central directory parsed once instead of
    5,000 times, and for a solid 7z it is one decompression instead of one
    per member."""
    path, members = job
    roms = []
    with lib.Archive(path=path) as outer:
        if outer.opens_as_zip:
            for member in members:
                with outer.nested(member) as inner:
                    entries = inner.entries()
                    if entries is None:
                        continue
                    roms.extend(process_listing(detector, path, entries, member, 2, inner.read))
            return roms

        scratch = tempfile.mkdtemp(dir=lib.scratch_dir(), prefix="nested-")
        try:
            extracted = outer.extract_many(members, scratch)
            for member in members:
                spot = extracted.get(member)
                if not spot:
                    log("cartridge: cannot read %s inside %s" % (member, path))
                    continue
                with lib.Archive(path=spot, ext=lib.ext_of(member)) as inner:
                    entries = inner.entries()
                    if entries is None:
                        continue
                    roms.extend(process_listing(detector, path, entries, member, 2, inner.read))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
    return roms


def nested_jobs(nested_by_archive, workers):
    """Share every outer archive's nested members out into jobs. A zip is
    sliced so its members spread over the threads; anything bsdtar reads is
    one job, since each job would decompress the whole archive again."""
    jobs = []
    for path, members in nested_by_archive:
        if not members:
            continue
        if lib.ext_of(path) != "zip":
            jobs.append((path, members))
            continue
        size = max(MIN_NESTED_SLICE, -(-len(members) // workers))
        for start in range(0, len(members), size):
            jobs.append((path, members[start:start + size]))
    return jobs


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
    """(size, mtime in ns) of a container. A folder's mtime moves when a file
    is added or removed, which is the only change that alters a folder
    listing. Nanoseconds, so two changes inside one second still count."""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return [int(info.st_size), int(info.st_mtime_ns)]


# ------------------------------------------------------------------ persist

def previous_state(path):
    """roms.json from the last scan: the rom records, and which containers
    were read at which size and mtime."""
    document = lib.read_json(path, {}) or {}
    previous = {}
    for rom in document.get("roms") or []:
        if isinstance(rom, dict) and rom.get("id"):
            # Per-user fields belong to user.json now; a record read back from
            # an older file must not smuggle them into the new one.
            for key in ("favorite", "lastPlayed"):
                rom.pop(key, None)
            previous[rom["id"]] = rom
    # A state file written by an older cartridge describes containers in a
    # shape this build cannot trust, so every container is read again.
    if document.get("version") != STATE_VERSION:
        return previous, {}
    containers = document.get("containers")
    if not isinstance(containers, dict):
        containers = {}
    return previous, containers


def write_consoles(path, detector, detected, ext_map, roms_stamp):
    """Console table for the consoles this library has roms for.

    Only the detected rows are rewritten, so a core the user picked for a
    console stays picked. A console that has no roms right now -- a drive not
    mounted, a folder being moved -- keeps its row and its core, marked absent,
    rather than losing the choice. A row with no core to remember is dropped."""
    def mutate(document):
        consoles = document.get("consoles")
        if not isinstance(consoles, dict):
            consoles = {}
        for console_id in list(consoles):
            if console_id in detected:
                continue
            entry = consoles[console_id]
            if isinstance(entry, dict) and entry.get("core"):
                entry["present"] = False
            else:
                del consoles[console_id]
        for console_id, name in detector.catalog.items():
            if console_id not in detected:
                continue
            entry = consoles.setdefault(console_id, {})
            entry["id"] = console_id
            entry["name"] = name
            entry["present"] = True
            entry.setdefault("core", "")
            entry["exts"] = sorted(ext for ext, ids in ext_map.items()
                                   if console_id in ids)
        document["consoles"] = consoles
        document["catalog"] = dict(sorted(detector.catalog.items()))
        document["generated"] = lib.now()
        # The UI reads this small file on every open, and reloads the big
        # roms.json only when this stamp says a scan has replaced it.
        document["romsStamp"] = roms_stamp
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

    zips carry a CRC in their central directory, which is exact and free, and
    a loose file gets one worked out when it needs comparing (see
    fill_loose_crcs). 7z and rar members have no CRC, so against one of those
    equal size is the best signal available."""
    if a.get("size", 0) != b.get("size", 0):
        return False
    if a.get("crc") and b.get("crc"):
        return a["crc"] == b["crc"]
    return True


def fill_loose_crcs(copies):
    """Give the loose copies in a group of same-name, same-size roms a CRC, so
    they are compared by their bytes and not by their size alone.

    Only when there is something exact to compare with -- another loose copy,
    or a zip member, which carries a CRC. Against a 7z or rar member (no CRC)
    hashing would buy nothing. The CRC is kept in the rom record, so an
    unchanged folder is not hashed again on the next scan."""
    loose = [rom for rom in copies if rom["depth"] == 0 and not rom.get("crc")]
    if not loose:
        return
    exact = [rom for rom in copies if rom.get("crc")]
    if len(loose) < 2 and not exact:
        return
    for rom in loose:
        names = [rom["entry"]] + (list(rom.get("extras") or []) if rom["kind"] == "cue" else [])
        crc = lib.crc_of_files([os.path.join(rom["path"], name) for name in names])
        if crc:
            rom["crc"] = crc


def mark_duplicates(roms, user):
    """Keep one visible copy of a game that is stored in more than one place.

    The same game loose in a folder and again inside a rom set is a duplicate,
    not a decision to make: the loose copy needs no unpacking, so it is the one
    shown. The other copies stay in roms.json with `hidden` naming the one
    shown -- not dropped: their container is cached as read, so a dropped copy
    would never come back if the loose one were deleted later.

    Copies that share a name but not their contents are left alone -- that is
    a conflict, and the user picks. Returns {hidden id: kept id}."""
    groups = {}
    for rom in roms:
        rom.pop("hidden", None)
        console, _ = lib.effective_console(rom, user)
        groups.setdefault((console, rom["name"].lower()), []).append(rom)

    hidden = {}
    for copies in groups.values():
        if len(copies) == 1:
            continue
        first = copies[0]
        if any(first.get("size", 0) != other.get("size", 0) for other in copies[1:]):
            continue
        fill_loose_crcs(copies)
        if any(not same_content(first, other) for other in copies[1:]):
            continue
        keeper = min(copies, key=lambda rom: (rom["depth"], len(rom["path"]), rom["id"]))
        for copy in copies:
            if copy is not keeper:
                copy["hidden"] = keeper["id"]
                hidden[copy["id"]] = keeper["id"]

    # An Unidentified rom byte-identical to one filed under a console is that
    # same game -- typically the twin of a rom you placed by hand. It is hidden
    # under the placed one instead of waiting in Unidentified to be placed again.
    placed = {}
    for rom in roms:
        if not rom.get("hidden") and lib.effective_console(rom, user)[0] != lib.UNKNOWN_ID:
            placed.setdefault(rom["name"].lower(), []).append(rom)
    for rom in roms:
        if rom.get("hidden") or lib.effective_console(rom, user)[0] != lib.UNKNOWN_ID:
            continue
        twin = next((other for other in placed.get(rom["name"].lower(), [])
                     if same_content(rom, other) and rom.get("crc") == other.get("crc")), None)
        if twin is not None:
            rom["hidden"] = twin["id"]
            hidden[rom["id"]] = twin["id"]
    return hidden


def adopt_hidden(user, hidden):
    """A favorite or a play time earned by a copy that is now hidden moves to
    the copy that is shown, so a rescan never quietly loses it. Returns
    whether anything changed."""
    changed = False
    favorites = user["favorites"]
    played = user["lastPlayed"]
    for copy_id, keeper_id in hidden.items():
        if favorites.get(copy_id) and not favorites.get(keeper_id):
            favorites[keeper_id] = True
            changed = True
        if (played.get(copy_id) or 0) > (played.get(keeper_id) or 0):
            played[keeper_id] = played[copy_id]
            changed = True
    return changed


def main():
    root = lib.plugin_root()
    library = lib.roms_root()

    if not os.path.isdir(library):
        lib.fail("cartridge: no ROM library at %s" % library, 2)

    log("cartridge: scanning %s" % library)
    detector = Detector()
    installed = lib.installed_cores()
    user = lib.load_user(root)           # before roms.json is replaced: migrates
    previous, known_containers = previous_state(lib.roms_json(root))

    # Roms from the last scan, grouped by the container they came out of, so a
    # container that has not been touched since can be reused untouched.
    by_container = {}
    for rom in previous.values():
        by_container.setdefault(rom.get("path"), []).append(rom)

    roms = []
    containers = {}
    reused = 0
    stale_folders = []
    stale_archives = []

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
            stale_folders.append((dirpath, names))

    for archive in archives:
        mark = fingerprint(archive)
        containers[archive] = mark
        if mark and known_containers.get(archive) == mark:
            roms.extend(by_container.get(archive, []))
            reused += 1
        else:
            stale_archives.append(archive)

    log("cartridge: %d containers, %d unchanged, %d to read"
        % (len(containers), reused, len(stale_folders) + len(stale_archives)))

    for dirpath, names in stale_folders:
        roms.extend(process_listing(detector, dirpath, loose_entries(dirpath, names),
                                    "", 0, folder_reader(dirpath)))

    workers = min(8, os.cpu_count() or 4)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        first = list(pool.map(lambda path: scan_archive(detector, path), stale_archives))
    nested_by_archive = []
    for path, (found, nested) in zip(stale_archives, first):
        roms.extend(found)
        nested_by_archive.append((path, nested))
    jobs = nested_jobs(nested_by_archive, workers)
    log("cartridge: %d archives inside archives, in %d jobs"
        % (sum(len(members) for _, members in jobs), len(jobs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for found in pool.map(lambda job: scan_nested(detector, job), jobs):
            roms.extend(found)

    roms.sort(key=lambda rom: (rom["console"], rom["name"].lower(), rom["id"]))

    previous_ids = set(previous)
    current_ids = {rom["id"] for rom in roms}

    # Everything below reads or writes shared state, so it happens under the
    # lock: a favorite set while the scan ran is in the user.json read here,
    # and cannot land between this read and the write.
    with lib.state_lock(root):
        user = lib.load_user(root)
        hidden = mark_duplicates(roms, user)
        if adopt_hidden(user, hidden):
            lib.write_json(lib.user_json(root), user)

        detected = {}
        for rom in roms:
            if rom.get("hidden"):
                continue
            console, _ = lib.effective_console(rom, user)
            detected[console] = detected.get(console, 0) + 1

        summary = {
            "root": library,
            "generated": lib.now(),
            "total": len(roms) - len(hidden),
            "added": len(current_ids - previous_ids),
            "removed": len(previous_ids - current_ids),
            "carried": len(current_ids & previous_ids),
            "duplicates": len(hidden),
            "reused": reused,
            "consoles": len(detected),
            "unknown": detected.get(lib.UNKNOWN_ID, 0),
            "cores": len(installed),
            "byConsole": detected,
        }

        # Unique per scan (a string: nanoseconds do not fit a JS number).
        stamp = str(time.time_ns())
        lib.write_json(lib.roms_json(root), {
            "version": STATE_VERSION,
            "stamp": stamp,
            "generated": summary["generated"],
            "root": library,
            "containers": containers,
            "roms": roms,
        })
        write_consoles(lib.consoles_json(root), detector, detected, detector.ext_map, stamp)
        write_cores(lib.cores_json(root), detector.cores, installed)

    log("cartridge: %d roms, %d consoles, %d duplicates hidden, +%d -%d"
        % (summary["total"], summary["consoles"], len(hidden),
           summary["added"], summary["removed"]))
    sys.stdout.write(json.dumps(summary) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
