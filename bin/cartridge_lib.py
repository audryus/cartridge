#!/usr/bin/env python3
"""Shared helpers for the cartridge scripts.

The scanner, the state mutator and the launcher all need the same three
things: where state lives on disk, how a file extension maps to a console,
and how to read an archive without unpacking it. That is what lives here so
the three entry points cannot drift apart.

Standard library only. External tools used: `bsdtar` (libarchive), which
reads zip, 7z and rar on this system.
"""

import contextlib
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

# ---------------------------------------------------------------- locations

CORE_INFO_DIR = "/usr/share/libretro/info"
CORE_DIR = "/usr/lib/libretro"
PSX_SYSTEM_ID = "playstation"
UNKNOWN_ID = "unknown"
CONFLICT_ID = "conflict"
CATALOG_SYSTEMS_KEY = "catalog"



def plugin_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def state_dir(root=None):
    """Where the three JSON files live: inside the plugin, unless
    CARTRIDGE_STATE_DIR says otherwise (the tests use that)."""
    override = os.environ.get("CARTRIDGE_STATE_DIR")
    if override:
        return override
    return os.path.join(root or plugin_root(), "state")


def roms_json(root=None):
    return os.path.join(state_dir(root), "roms.json")


def consoles_json(root=None):
    return os.path.join(state_dir(root), "consoles.json")


def cores_json(root=None):
    return os.path.join(state_dir(root), "cores.json")


def user_json(root=None):
    """What the user decided -- favorites, play times, hand-assigned consoles.
    Small and written often, so it lives apart from roms.json, which is large
    and only the scanner writes."""
    return os.path.join(state_dir(root), "user.json")


def roms_root():
    """Root of the ROM library. Overridable for testing and for users who
    keep their library somewhere else."""
    override = os.environ.get("CARTRIDGE_ROMS_ROOT")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), "Games", "roms")


def cache_root():
    """cartridge's cache: staged roms, launch logs and scan scratch space.

    Not /tmp: on Arch /tmp is a tmpfs, so a staged 1.5 GB iso there is 1.5 GB
    of RAM held until the next reboot."""
    override = os.environ.get("CARTRIDGE_CACHE_DIR")
    if override:
        return override
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "cartridge")


def extract_root():
    """Where a ROM is staged before RetroArch opens it."""
    return os.path.join(cache_root(), "staging")


def private_dir(path):
    """Create `path` (and its parents) and make sure it is a real directory,
    owned by this user, readable by nobody else. Refuses a symlink or a
    directory someone else planted there first."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError("%s is not a directory owned by you" % path)
    if info.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path



# ------------------------------------------------------------------ file io

def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json(path, payload):
    """Write via a same-directory temp file + rename so a crash or a full
    disk can never leave a half-written state file behind."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=directory, prefix=".cartridge-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(payload, out, separators=(",", ":"), ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_lock_depth = threading.local()


@contextlib.contextmanager
def state_lock(root=None):
    """Exclusive lock over the state directory, across processes.

    Every read-modify-write of a state file happens inside it, so the scanner
    and cartridge-state can never interleave and drop each other's change.
    Re-entrant within one thread, so helpers that lock can call each other."""
    depth = getattr(_lock_depth, "value", 0)
    if depth:
        _lock_depth.value = depth + 1
        try:
            yield
        finally:
            _lock_depth.value -= 1
        return
    directory = state_dir(root)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, ".lock"), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        _lock_depth.value = 1
        try:
            yield
        finally:
            _lock_depth.value = 0
            fcntl.flock(handle, fcntl.LOCK_UN)


def update_json(path, mutate, default=None):
    """Read, hand the document to `mutate`, write it back -- under the state
    lock, so two writers are serialized instead of racing."""
    with state_lock():
        document = read_json(path, None)
        if document is None:
            document = default() if callable(default) else (default or {})
        result = mutate(document)
        payload = document if result is None else result
        write_json(path, payload)
        return payload


# ---------------------------------------------------------------- user state

def empty_user():
    return {"version": 1, "favorites": {}, "lastPlayed": {}, "assigned": {}}


def _user_from_roms(root):
    """user.json for a state directory written before user.json existed: the
    favorites, play times and assignments lived inside roms.json then."""
    user = empty_user()
    document = read_json(roms_json(root), {}) or {}
    for rom in document.get("roms") or []:
        if not isinstance(rom, dict) or not rom.get("id"):
            continue
        if rom.get("favorite"):
            user["favorites"][rom["id"]] = True
        try:
            played = int(rom.get("lastPlayed") or 0)
        except (TypeError, ValueError):
            played = 0
        if played:
            user["lastPlayed"][rom["id"]] = played
        if rom.get("reason") == "assigned" and rom.get("console"):
            user["assigned"][rom["id"]] = rom["console"]
    return user


def load_user(root=None):
    """user.json, normalized. Created from an older roms.json the first time."""
    path = user_json(root)
    document = read_json(path, None)
    if not isinstance(document, dict):
        with state_lock(root):
            document = read_json(path, None)
            if not isinstance(document, dict):
                document = _user_from_roms(root)
                write_json(path, document)
    for key in ("favorites", "lastPlayed", "assigned"):
        if not isinstance(document.get(key), dict):
            document[key] = {}
    return document


def update_user(root, mutate):
    """Change user.json under the state lock."""
    with state_lock(root):
        document = load_user(root)
        result = mutate(document)
        write_json(user_json(root), document)
        return result


def effective_console(rom, user):
    """(console, reason) once what the user told cartridge is applied."""
    assigned = (user.get("assigned") or {}).get(rom.get("id"))
    if assigned:
        return assigned, ("unknown" if assigned == UNKNOWN_ID else "assigned")
    return rom.get("console") or UNKNOWN_ID, rom.get("reason") or ""


# ------------------------------------------------------------- console names

# libretro-core-info carries several `systemname` values for one physical
# machine: PicoDrive and BlastEm say "Sega 8/16-bit (Various)" where Genesis
# Plus GX says "Sega Genesis". Without merging, one console shows up as three
# rows in the left column and `.md` looks ambiguous. Every database name that
# is an alias of something else is listed here; anything absent falls through
# to its own name.
ALIASES = {
    "Sega Genesis": "Sega Genesis / Mega Drive",
    "Sega 8/16-bit (Various)": "Sega Genesis / Mega Drive",
    "Sega 8/16-bit + 32X (Various)": "Sega Genesis / Mega Drive",
    "Sega Master System": "Sega Master System / 8-bit",
    "Sega 8-bit": "Sega Master System / 8-bit",
    "Sega 8-bit (MS/GG/SG-1000)": "Sega Master System / 8-bit",
    "PC Engine/PCE-CD": "PC Engine / TurboGrafx-16",
    "PC Engine/SuperGrafx": "PC Engine / TurboGrafx-16",
    "PC Engine SuperGrafx": "PC Engine / TurboGrafx-16",
    "PC Engine/SuperGrafx/CD": "PC Engine / TurboGrafx-16",
    "PC Engine/PCE-CD/CD": "PC Engine / TurboGrafx-16",
    "Amiga": "Commodore Amiga",
    "Commodore Amiga": "Commodore Amiga",
    "Super Nintendo Entertainment System": "Super Nintendo Entertainment System",
    "Super Nintendo Entertainment System / Game Boy / Game Boy Color":
        "Super Nintendo Entertainment System",
    "Game Boy/Game Boy Color": "Game Boy / Game Boy Color",
    "Game Boy/Game Boy Color/Game Boy Advance": "Game Boy / Game Boy Color",
    "MSX": "MSX",
    "MSX/SVI/ColecoVision/SG-1000": "MSX",
    "ColecoVision": "ColecoVision",
    "ColecoVision/CreatiVision/My Vision": "ColecoVision",
    "CD-i": "CD-i",
    "CDi": "CD-i",
    "C64": "Commodore 64",
    "C64 SuperCPU": "Commodore 64",
    "C64DTV": "Commodore 64",
    "128": "Atari 8-bit Family",
    "Sony PlayStation 2": "PlayStation 2",
}


# A core whose database entry names several machines runs all of them, but
# ALIASES files it under one. These are the others it is offered for in the
# config popup, so mGBA can be the Game Boy Advance core too.
ALSO_RUNS = {
    "Game Boy/Game Boy Color/Game Boy Advance": ["Game Boy Advance"],
    "Super Nintendo Entertainment System / Game Boy / Game Boy Color":
        ["Game Boy / Game Boy Color"],
}


def canonical_system(systemname):
    name = (systemname or "").strip()
    return ALIASES.get(name, name)


def slug(value):
    out = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower())
    return out.strip("-") or "console"


# ------------------------------------------------------------- extension map

# `supported_extensions` in the core-info database is not a clean list. ScummVM
# alone contributes "blorb", "gcm", "pak" and hundreds of others; MAME lists
# drive and device formats; some entries contain junk like "$00" or "d$$".
# Only plausible container extensions are kept, and a core that lists a
# hundred of them is treated as a catch-all rather than as an owner.
EXT_RE = re.compile(r"^[a-z0-9]{1,6}$")

ARCHIVE_EXTS = {
    "zip", "7z", "rar", "tar", "gz", "tgz", "bz2", "tbz", "tbz2",
    "xz", "txz", "zst", "lz4", "lha", "lzh", "cab", "arj", "cpio",
}

# Extensions that show up inside ROM archives and are never a game on their
# own: readmes, artwork, playlists, cheats, save states, and the extra tracks
# that sit next to a PlayStation cue sheet. Anything unmatched that is *not*
# on this list still becomes an Unknown entry, because "we cannot tell" is
# exactly the case the Unknown column exists for.
IGNORED_EXTS = {
    # documentation and metadata
    "txt", "nfo", "dat", "idx", "log", "readme", "license", "changelog",
    "htm", "html",
    "todo", "info", "url", "lnk", "xml", "ini", "cfg", "conf", "sav",
    "srm", "save", "state", "st", "mss", "ps2", "psv", "ps1", "1sm",
    "gbs", "gbv", "psu", "sav2", "bic", "ss2", "es3", "gd3", "gdi",
    "czi", "szs", "mcs", "mcd", "gme",
    # playlists and checksums
    "m3u", "m3u8", "pls", "sfv", "md5", "sha1", "crc", "accurip",
    # images
    "jpg", "jpeg", "png", "gif", "bmp", "webp", "tbn", "svg", "pdf", "mp3",
    "mp4", "avi", "mkv",
    # bios and firmware blobs that ride along with romsets
    "bios", "rom", "fw", "flash", "eeprom", "bram", "sram", "vram", "nv",
    "ntsc", "pal",
    # playstation companion tracks
    "str", "sub", "at3", "cdda", "cdd", "mdf", "mds", "ccd", "cso",
    # arcade / tooling noise
    "lua", "py", "sh", "so", "dll", "exe", "dylib", "deb", "rpm", "apk",
}


# Cartridge formats that belong to one console, whatever else the database
# says. Other cores list them because they *can* run them -- bsnes and Mesen-S
# play .gb through the Super Game Boy, mGBA and VBA-M are filed under Game Boy
# but list .gba, NooDS runs .gba in the DS slot, Genesis Plus GX plays Master
# System and Game Gear, Frodo reads a C64 tape format that happens to be .lnx.
# None of that makes a .gba file anything but a Game Boy Advance game.
NATIVE_EXTS = {
    "gb": "Game Boy / Game Boy Color",
    "gbc": "Game Boy / Game Boy Color",
    "sgb": "Game Boy / Game Boy Color",
    "gba": "Game Boy Advance",
    "agb": "Game Boy Advance",
    "nes": "Nintendo Entertainment System",
    "fds": "Nintendo Entertainment System",
    "sfc": "Super Nintendo Entertainment System",
    "smc": "Super Nintendo Entertainment System",
    "swc": "Super Nintendo Entertainment System",
    "fig": "Super Nintendo Entertainment System",
    "n64": "Nintendo 64",
    "z64": "Nintendo 64",
    "v64": "Nintendo 64",
    "nds": "Nintendo DS",
    "vb": "Virtual Boy",
    "sms": "Sega Master System / 8-bit",
    "gg": "Sega Master System / 8-bit",
    "md": "Sega Genesis / Mega Drive",
    "gen": "Sega Genesis / Mega Drive",
    "smd": "Sega Genesis / Mega Drive",
    "32x": "Sega Genesis / Mega Drive",
    "pce": "PC Engine / TurboGrafx-16",
    "sgx": "PC Engine / TurboGrafx-16",
    "lnx": "Lynx",
    "a26": "Atari 2600",
    "a78": "Atari 7800",
    "j64": "Jaguar",
    "ws": "WonderSwan/Color",
    "wsc": "WonderSwan/Color",
    "ngp": "Neo Geo Pocket (Color)",
    "ngc": "Neo Geo Pocket (Color)",
}


def ext_of(name):
    base = os.path.basename(name or "")
    if "." not in base:
        return ""
    return base.rsplit(".", 1)[-1].lower()


def stem_of(name):
    base = os.path.basename(name or "")
    if "." not in base:
        return base
    return base.rsplit(".", 1)[0]


def is_archive_ext(ext):
    return ext in ARCHIVE_EXTS


def parse_info(path):
    fields = {}
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = re.match(r'^([a-z_]+)\s*=\s*"?([^"]*)"?$', line)
            if match:
                fields[match.group(1)] = match.group(2)
    return fields


def installed_cores():
    """{core_id: absolute path to the .so} for every libretro core installed
    on this machine. This is what the config popup offers."""
    found = {}
    try:
        entries = sorted(os.listdir(CORE_DIR))
    except OSError:
        return found
    for entry in entries:
        if not entry.endswith(".so"):
            continue
        core_id = entry[:-3]
        if core_id.endswith("_libretro"):
            core_id = core_id[: -len("_libretro")]
        found[normalize_core_id(core_id)] = os.path.join(CORE_DIR, entry)
    return found


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
            "systemIds": info["systemIds"] if info else [],
            "system": info["system"] if info else "",
            "exts": info["exts"] if info else [],
            "path": so_path,
        })
    entries.sort(key=lambda item: (item["system"], item["label"].lower()))
    write_json(path, {"generated": now(), "cores": entries})
    return entries


def normalize_core_id(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def load_core_info():
    """Every core-info file on the system, keyed by normalized corename and,
    separately, by file name.

    Both keys matter. Arch ships some cores under their old file names --
    mednafen_psx_libretro.so next to a mednafen_psx_libretro.info that calls
    itself "Beetle PSX HW" -- so matching a core by file name as well finds the
    metadata for all 42 installed cores instead of 35.

    Detection deliberately reads the whole database, not only the cores the
    user has installed: owning a PlayStation 2 game should still show
    PlayStation 2 in the left column even before a PS2 core is installed,
    with the config popup reporting that no core can run it.
    """
    cores = {}
    try:
        entries = sorted(os.listdir(CORE_INFO_DIR))
    except OSError:
        entries = []
    for entry in entries:
        if not entry.endswith(".info"):
            continue
        try:
            fields = parse_info(os.path.join(CORE_INFO_DIR, entry))
        except OSError:
            continue
        raw_system = (fields.get("systemname") or "").strip()
        systemname = canonical_system(raw_system)
        if not systemname:
            continue
        corename = fields.get("corename") or entry[:-5]
        file_id = entry[:-5]
        if file_id.endswith("_libretro"):
            file_id = file_id[: -len("_libretro")]
        exts = [e for e in (fields.get("supported_extensions") or "").lower().split("|")
                if e and EXT_RE.match(e)]
        record = {
            "id": normalize_core_id(corename),
            "name": fields.get("display_name") or corename,
            "corename": corename,
            "system": systemname,
            "systemId": slug(systemname),
            "systemIds": [slug(systemname)] + [slug(other) for other in ALSO_RUNS.get(raw_system, [])],
            "exts": exts,
            "catchAll": len(exts) > 24,
        }
        cores.setdefault(record["id"], record)
        cores[normalize_core_id(file_id)] = record
    return cores


def build_extension_map(cores):
    """{ext: [system_id, ...]} built from every core-info file.

    Cores that list an implausibly long extension list (ScummVM, MAME, FBNeo,
    Dolphin, PPSSPP, PCSX2, Flycast) are excluded: they claim whole swaths of
    the alphabet and would make everything ambiguous. A NATIVE_EXTS format
    goes to its own console alone.
    """
    ext_map = {}
    seen = set()
    for core in cores.values():
        if core["catchAll"] or id(core) in seen:
            continue
        seen.add(id(core))
        for ext in core["exts"]:
            if ext in ARCHIVE_EXTS or ext in IGNORED_EXTS:
                continue
            bucket = ext_map.setdefault(ext, [])
            if core["systemId"] not in bucket:
                bucket.append(core["systemId"])
    for ext, system in NATIVE_EXTS.items():
        ext_map[ext] = [slug(system)]
    return ext_map


# ------------------------------------------------------------------ archives
#
# A ROM set is usually a zip of zips, so archives are read twice, without ever
# unpacking them. zips go through Python's zipfile; everything else goes
# through bsdtar, since libarchive is what reads 7z and rar on this system.
#
# zipfile is not only faster. bsdtar treats a member name as a glob, so a game
# called "Fists of Steel (J) [b1].zip" matches nothing and its rom silently
# disappears: 4552 of the 5292 inner archives in the 32X set were being skipped
# before this was found.
#
# Two shapes only: a file on disk, or the bytes of an archive that lives inside
# another one. An inner archive is small (a single game), so holding it in
# memory is cheap; the game itself is never held in memory, only streamed to
# disk at play time.


def bsdtar_literal(member):
    """A member name as a bsdtar pattern that matches only itself.

    bsdtar reads the names on its command line as globs, so "Game (U) [!].bin"
    matches nothing and the extraction fails. Escaping the glob characters is
    what makes it a literal again."""
    return re.sub(r"([\\*?\[\]])", r"\\\1", member)


# The listing parser depends on bsdtar's date column, which follows the
# locale. Pinning it keeps the column the width the parser expects.
_BSDTAR_ENV = dict(os.environ, LC_ALL="C")

# An inner archive bigger than this is spooled to a file in the cache instead
# of being held in memory. With eight scanner threads that bounds the memory
# the scan can take, and nothing is ever silently truncated.
IN_MEMORY_NESTED_BYTES = 64 << 20


def scratch_dir():
    return private_dir(os.path.join(cache_root(), "scratch"))


class Archive:
    """One archive, opened once.

    A zip keeps its ZipFile open for as long as this object lives: parsing the
    central directory of a 5,000 member rom set costs more than reading one of
    its members, so it must not happen once per member. Use it as a context
    manager, or call close()."""

    def __init__(self, path=None, data=None, ext=None, owned=False):
        self.path = path
        self.data = data
        self.ext = (ext or ext_of(path or "")).lower()
        self._owned = owned          # path is a temp file this object deletes
        self._zipfile = None
        self._zip_failed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        if self._zipfile is not None:
            try:
                self._zipfile.close()
            except Exception:
                pass
            self._zipfile = None
        if self._owned and self.path:
            try:
                os.unlink(self.path)
            except OSError:
                pass
            self._owned = False

    @property
    def readable(self):
        return self.data is not None or bool(self.path)

    @property
    def is_zip(self):
        return self.ext == "zip"

    @property
    def opens_as_zip(self):
        """A zip that zipfile can read -- members come out without bsdtar."""
        return self.is_zip and self._zip() is not None

    def _zip(self):
        """The open ZipFile, or None when this is not a readable zip."""
        if self._zipfile is not None or self._zip_failed:
            return self._zipfile
        try:
            if self.data is not None:
                self._zipfile = zipfile.ZipFile(io.BytesIO(self.data))
            else:
                self._zipfile = zipfile.ZipFile(self.path, "r")
        except Exception:
            self._zip_failed = True
        return self._zipfile

    def entries(self):
        """[{name, size, crc}] for every file member, directories removed.
        None when the archive cannot be read at all.

        Size and CRC come free from a zip's central directory. For the other
        formats the size is parsed out of `bsdtar -tvf`, which is how cartridge
        tells two same-named roms apart: a game stored twice under the same
        name is not a conflict, the same name with different bytes is."""
        if not self.readable:
            return None
        if self.opens_as_zip:
            try:
                return [{"name": info.filename.replace("\\", "/"),
                         "size": int(info.file_size),
                         "crc": "%08x" % (info.CRC & 0xFFFFFFFF)}
                        for info in self._zip().infolist()
                        if not info.filename.endswith("/")]
            except Exception:
                pass  # damaged: let bsdtar try
        command = ["bsdtar", "-tvf", "-"] if self.data is not None \
            else ["bsdtar", "-tvf", self.path]
        raw = self._bsdtar(command, self.data)
        return None if raw is None else _parse_listing(raw)

    def names(self):
        entries = self.entries()
        return None if entries is None else [entry["name"] for entry in entries]

    def read(self, member, limit=1 << 20):
        """One member's bytes, capped. Used for cue sheets, which are tiny."""
        if not self.readable:
            return None
        if self.opens_as_zip:
            try:
                with self._zip().open(member) as handle:
                    return handle.read(limit)
            except Exception:
                pass
        raw = self._bsdtar(self._extract_command(member), self.data)
        return None if raw is None else raw[:limit]

    def _extract_command(self, member):
        source = "-" if self.data is not None else self.path
        return ["bsdtar", "-xOf", source, bsdtar_literal(member)]

    def extract(self, member, destination):
        """Stream one member to a file. Returns True on success. Never buffers
        the member: a PlayStation 2 iso is over a gigabyte."""
        if not self.readable:
            return False
        ok = False
        if self.opens_as_zip:
            try:
                with self._zip().open(member) as source:
                    with open(destination, "wb") as target:
                        shutil.copyfileobj(source, target, 1 << 20)
                ok = True
            except Exception:
                ok = False
        else:
            try:
                with open(destination, "wb") as target:
                    ok = self._bsdtar(self._extract_command(member), self.data, target) is True
            except OSError:
                ok = False
        if not ok:
            # Never leave a half-written file where a later run could take it
            # for the real thing.
            try:
                os.unlink(destination)
            except OSError:
                pass
        return ok

    def member_size(self, member):
        if self.opens_as_zip:
            try:
                return int(self._zip().getinfo(member).file_size)
            except Exception:
                return None
        return None

    def nested(self, member):
        """The archive stored inside this one, as an Archive to close.

        A small one is held in memory. A big one -- or one whose size a zip
        directory does not state -- is streamed to a scratch file first."""
        ext = ext_of(member)
        size = self.member_size(member)
        if size is not None and size <= IN_MEMORY_NESTED_BYTES:
            return Archive(data=self.read(member, size + 1), ext=ext)
        handle, spool = tempfile.mkstemp(dir=scratch_dir(), suffix="." + (ext or "bin"))
        os.close(handle)
        if not self.extract(member, spool):
            return Archive(ext=ext)
        return Archive(path=spool, ext=ext, owned=True)

    def extract_many(self, members, directory):
        """Extract several members into `directory` in one pass over the
        archive, keeping their relative paths. For 7z and rar this is the
        difference between decompressing a solid archive once and once per
        member. Returns {member: path on disk} for what came out."""
        if not members:
            return {}
        if self.opens_as_zip:
            out = {}
            for member in members:
                target = _safe_join(directory, member)
                if target is None:
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                if self.extract(member, target):
                    out[member] = target
            return out
        handle, listing = tempfile.mkstemp(dir=directory, prefix=".members-")
        with os.fdopen(handle, "w", encoding="utf-8") as out_list:
            for member in members:
                out_list.write(bsdtar_literal(member) + "\n")
        source = "-" if self.data is not None else self.path
        self._bsdtar(["bsdtar", "-xf", source, "-C", directory, "-T", listing],
                     self.data, subprocess.DEVNULL)
        os.unlink(listing)
        out = {}
        for member in members:
            target = _safe_join(directory, member)
            if target and os.path.isfile(target):
                out[member] = target
        return out

    @staticmethod
    def _bsdtar(command, stdin_bytes=None, target=None):
        """Run bsdtar. With no `target`, stdout is returned as bytes; otherwise
        stdout is written to that file object and True/False is returned."""
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE if target is None else target,
                stderr=subprocess.DEVNULL,
                env=_BSDTAR_ENV)
        except (OSError, ValueError):
            return None if target is None else False
        try:
            out, _ = process.communicate(stdin_bytes)
        except subprocess.SubprocessError:
            process.kill()
            process.wait()
            return None if target is None else False
        if target is not None:
            return process.returncode == 0
        return out if process.returncode == 0 else None


def _safe_join(directory, member):
    """directory/member, or None when the member would land outside it."""
    target = os.path.normpath(os.path.join(directory, member))
    if not target.startswith(os.path.normpath(directory) + os.sep):
        return None
    return target


# `bsdtar -tvf` prints: perms, uid, gid, nlink, size, a 12 character date, name.
# The date is fixed width ("Jul  3  1997", "Jul 10 05:59"), which is what makes
# a name with spaces in it safe to recover.
_LISTING_RE = re.compile(
    r"^\S+\s+\S+\s+\S+\s+\S+\s+(?P<size>\d+)\s(?P<stamp>.{12})\s(?P<name>.+)$")


def _parse_listing(raw):
    entries = []
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.strip():
            continue
        match = _LISTING_RE.match(line)
        if not match:
            continue
        name = match.group("name").strip().replace("\\", "/")
        if not name or name.endswith("/"):
            continue
        entries.append({"name": name, "size": int(match.group("size")), "crc": ""})
    return entries


# ---------------------------------------------------------------- cue sheets

_CUE_FILE_RE = re.compile(rb'^\s*FILE\s+"?([^"\r\n]+)"?', re.IGNORECASE | re.MULTILINE)


def cue_companions(cue_bytes, member_names):
    """The data files a cue sheet points at, as bare names.

    Cue sheets are frequently Shift-JIS, so they are decoded as latin-1 and
    only the ASCII FILE lines are read. When a sheet references nothing
    usable, same-basename members are used instead.
    """
    referenced = []
    if cue_bytes:
        for raw in _CUE_FILE_RE.findall(cue_bytes):
            try:
                name = raw.decode("utf-8", "replace")
            except Exception:
                continue
            name = name.strip().replace("\\", "/").split("/")[-1]
            if name and name not in referenced:
                referenced.append(name)
    present = [m.split("/")[-1] for m in member_names]
    matched = [r for r in referenced if r in present]
    if matched:
        return matched
    # Fall back to same-basename members, then to anything image-like.
    stem = None
    base = referenced[0] if referenced else None
    for name in present:
        if base and name.lower() == base.lower():
            stem = name
            break
    if stem:
        wanted = os.path.splitext(stem)[0].lower()
        return [n for n in present if os.path.splitext(n)[0].lower() == wanted and n != stem]
    return [n for n in present if ext_of(n) in ("bin", "iso", "img", "raw", "iso9660")]


# --------------------------------------------------------------------- misc

def crc_of_files(paths):
    """CRC32 of the files in `paths`, read one after the other, as the
    lower-case hex a zip central directory gives. None if one is unreadable."""
    import zlib
    crc = 0
    try:
        for path in paths:
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    crc = zlib.crc32(block, crc)
    except OSError:
        return None
    return "%08x" % (crc & 0xFFFFFFFF)


def stable_id(parts):
    digest = hashlib.sha1("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()
    return digest[:16]


def now():
    return int(time.time())


def fail(message, code=1):
    sys.stderr.write(message.rstrip() + "\n")
    raise SystemExit(code)
