#!/usr/bin/env python3
"""Shared helpers for the cartridge scripts.

The scanner, the state mutator and the launcher all need the same three
things: where state lives on disk, how a file extension maps to a console,
and how to read an archive without unpacking it. That is what lives here so
the three entry points cannot drift apart.

Standard library only. External tools used: `bsdtar` (libarchive), which
reads zip, 7z and rar on this system.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

# ---------------------------------------------------------------- locations

CORE_INFO_DIR = "/usr/share/libretro/info"
CORE_DIR = "/usr/lib/libretro"
PSX_SYSTEM_ID = "playstation"
UNKNOWN_ID = "unknown"
CONFLICT_ID = "conflict"
CATALOG_SYSTEMS_KEY = "catalog"

_ARCHIVE_TIMEOUT = 60
# An archive-inside-an-archive is one game, so it is small enough to hold in
# memory while its contents are listed. The game itself never is.
MAX_NESTED_BYTES = 192 << 20


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


def roms_root():
    """Root of the ROM library. Overridable for testing and for users who
    keep their library somewhere else."""
    override = os.environ.get("CARTRIDGE_ROMS_ROOT")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), "Games", "roms")


def extract_root():
    """Where a ROM is staged before RetroArch opens it. Reboot-cleared by
    choice; the directory is created 0700 and owned by this user only."""
    return os.path.join("/tmp", "cartridge-%d" % os.getuid())



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


def update_json(path, mutate, default=None):
    """Read, hand the document to `mutate`, write it back. Both the scanner
    and the UI mutate state through here, so a field written by one is never
    clobbered by the other."""
    document = read_json(path, None)
    if document is None:
        document = default() if callable(default) else (default or {})
    result = mutate(document)
    payload = document if result is None else result
    write_json(path, payload)
    return payload


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
        systemname = canonical_system(fields.get("systemname"))
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
    the alphabet and would make everything ambiguous.
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


class Archive:
    def __init__(self, path=None, data=None, ext=None):
        self.path = path
        self.data = data
        self.ext = (ext or ext_of(path or "")).lower()

    @property
    def is_zip(self):
        return self.ext == "zip"

    def _zip(self):
        import io
        import zipfile
        if self.data is not None:
            return zipfile.ZipFile(io.BytesIO(self.data))
        return zipfile.ZipFile(self.path, "r")

    def entries(self):
        """[{name, size, crc}] for every file member, directories removed.
        None when the archive cannot be read at all.

        Size and CRC come free from a zip's central directory. For the other
        formats the size is parsed out of `bsdtar -tvf`, which is how cartridge
        tells two same-named roms apart: a game stored twice under the same
        name is not a conflict, the same name with different bytes is."""
        if self.is_zip:
            try:
                with self._zip() as archive:
                    return [{"name": info.filename.replace("\\", "/"),
                             "size": int(info.file_size),
                             "crc": "%08x" % (info.CRC & 0xFFFFFFFF)}
                            for info in archive.infolist()
                            if not info.filename.endswith("/")]
            except Exception:
                pass  # not a zip after all, or damaged: let bsdtar try
        command = ["bsdtar", "-tvf", "-"] if self.data is not None \
            else ["bsdtar", "-tvf", self.path]
        raw = self._bsdtar(command, self.data)
        return None if raw is None else _parse_listing(raw)

    def names(self):
        entries = self.entries()
        return None if entries is None else [entry["name"] for entry in entries]

    def read(self, member, limit=1 << 20):
        """One member's bytes, capped. Used for cue sheets, which are tiny."""
        if self.is_zip:
            try:
                with self._zip() as archive, archive.open(member) as handle:
                    return handle.read(limit)
            except Exception:
                pass
        command = ["bsdtar", "-xOf", "-", member] if self.data is not None \
            else ["bsdtar", "-xOf", self.path, member]
        raw = self._bsdtar(command, self.data)
        return None if raw is None else raw[:limit]

    def extract(self, member, destination):
        """Stream one member to a file. Returns True on success. Never buffers
        the member: a PlayStation 2 iso is over a gigabyte."""
        if self.is_zip:
            try:
                with self._zip() as archive, archive.open(member) as source:
                    with open(destination, "wb") as target:
                        shutil.copyfileobj(source, target, 1 << 20)
                return True
            except Exception:
                return False
        command = ["bsdtar", "-xOf", "-", member] if self.data is not None \
            else ["bsdtar", "-xOf", self.path, member]
        with open(destination, "wb") as target:
            ok = self._bsdtar(command, self.data, target)
        if ok is not True:
            try:
                os.unlink(destination)
            except OSError:
                pass
        return ok is True

    def nested(self, member):
        """A view of the archive stored inside this one."""
        return Archive(data=self.read(member, MAX_NESTED_BYTES), ext=ext_of(member))

    @staticmethod
    def _bsdtar(command, stdin_bytes=None, target=None):
        """Run bsdtar. With no `target`, stdout is returned as bytes; otherwise
        stdout is written to that file object and True/False is returned."""
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE if target is None else target,
                stderr=subprocess.DEVNULL)
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

def stable_id(parts):
    digest = hashlib.sha1("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()
    return digest[:16]


def now():
    return int(time.time())


def fail(message, code=1):
    sys.stderr.write(message.rstrip() + "\n")
    raise SystemExit(code)
