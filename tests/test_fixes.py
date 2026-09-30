#!/usr/bin/env python3
"""Regression tests for the state, archive and staging fixes.

One class per problem, each named after what used to go wrong, so a failure
here says which fix regressed.

Run with:  python3 tests/test_fixes.py     (or: make test)
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from test_scan import (ROOT, lib, load_script, cartridge_scan,  # noqa: E402
                       cartridge_state, visible, write_zip, zip_bytes)

cartridge_play = load_script("cartridge-play.py")

HAVE_BSDTAR = shutil.which("bsdtar") is not None
SNES = "super-nintendo-entertainment-system"


def quiet(function, *args):
    """Run with stderr and stdout silenced: the scripts narrate."""
    noise_err, noise_out = os.dup(2), os.dup(1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        sys.stdout.flush()
        os.dup2(devnull, 2)
        os.dup2(devnull, 1)
        return function(*args)
    finally:
        sys.stdout.flush()
        os.dup2(noise_err, 2)
        os.dup2(noise_out, 1)
        os.close(devnull)
        os.close(noise_err)
        os.close(noise_out)


def bsdtar_archive(path, files, fmt="7zip"):
    """An archive of `files` ({name: bytes}) built by bsdtar, which is how 7z
    and rar sets are read."""
    stage = tempfile.mkdtemp(prefix="cartridge-src-")
    try:
        for name, payload in files.items():
            target = os.path.join(stage, name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as handle:
                handle.write(payload)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        subprocess.run(["bsdtar", "--format", fmt, "-cf", path, "-C", stage] + list(files),
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cartridge-fix-")
        self.library = os.path.join(self.dir, "roms")
        self.state = os.path.join(self.dir, "state")
        self.cache = os.path.join(self.dir, "cache")
        os.makedirs(self.library)
        self.env = mock.patch.dict(os.environ, {
            "CARTRIDGE_ROMS_ROOT": self.library,
            "CARTRIDGE_STATE_DIR": self.state,
            "CARTRIDGE_CACHE_DIR": self.cache,
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def scan(self):
        quiet(cartridge_scan.main)
        return lib.read_json(lib.roms_json())

    def loose(self, name, payload):
        path = os.path.join(self.library, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(payload)
        return path

    def rom_named(self, document, name):
        return next(rom for rom in document["roms"] if rom["name"] == name)


# ------------------------------------------------------------ 1. lost writes

class StateIsNotClobberedTest(Fixture):
    """A favorite set while a scan ran used to be overwritten by the scan's
    final write of roms.json."""

    def test_favorite_set_during_a_scan_survives_it(self):
        self.loose("A.smc", b"a" * 16)
        self.loose("B.smc", b"b" * 16)
        first = self.scan()
        target = self.rom_named(first, "A")["id"]
        # Make the folder look changed, so the second scan really reads it.
        self.loose("C.smc", b"c" * 16)

        real_walk = cartridge_scan.walk_library

        def walk_and_favorite(root):
            # The UI sends this while the scanner is mid-flight.
            cartridge_state.set_favorite(ROOT, target, True)
            return real_walk(root)

        with mock.patch.object(cartridge_scan, "walk_library", walk_and_favorite):
            self.scan()
        self.assertTrue(lib.load_user()["favorites"].get(target))

    def test_parallel_writers_do_not_lose_updates(self):
        for index in range(12):
            self.loose("G%02d.smc" % index, bytes([index]) * 16)
        document = self.scan()
        ids = [rom["id"] for rom in document["roms"]]
        script = os.path.join(ROOT, "bin", "cartridge-state.py")
        processes = [subprocess.Popen([sys.executable, script, "favorite", rom_id, "1"],
                                      stdout=subprocess.DEVNULL)
                     for rom_id in ids]
        for process in processes:
            self.assertEqual(process.wait(), 0)
        self.assertEqual(sorted(lib.load_user()["favorites"]), sorted(ids))

    def test_lock_is_reentrant_and_exclusive(self):
        order = []
        with lib.state_lock():
            with lib.state_lock():        # the same thread may nest
                order.append("inner")

            def other():
                with lib.state_lock():
                    order.append("other")
            thread = threading.Thread(target=other)
            thread.start()
            thread.join(0.3)
            self.assertTrue(thread.is_alive(), "another holder got in")
            order.append("outer")
        thread.join()
        self.assertEqual(order, ["inner", "outer", "other"])


# --------------------------------------------------------- 2. bsdtar globbing

@unittest.skipUnless(HAVE_BSDTAR, "bsdtar is not installed")
class BracketNamesTest(Fixture):
    """bsdtar reads member names as globs, so "[!]" matched nothing and the
    member could be neither read nor extracted from a 7z or rar."""

    NAME = "Game (U) [!].bin"

    def test_literal_escapes_every_glob_character(self):
        self.assertEqual(lib.bsdtar_literal("a[!]*?\\b"), "a\\[!\\]\\*\\?\\\\b")

    def test_read_and_extract_a_bracketed_member(self):
        path = os.path.join(self.dir, "set.7z")
        bsdtar_archive(path, {self.NAME: b"payload"})
        with lib.Archive(path=path) as archive:
            self.assertEqual(archive.read(self.NAME), b"payload")
            target = os.path.join(self.dir, "out.bin")
            self.assertTrue(archive.extract(self.NAME, target))
            with open(target, "rb") as handle:
                self.assertEqual(handle.read(), b"payload")

    def test_cue_sheet_with_brackets_inside_a_7z_is_one_disc(self):
        cue = ('FILE "%s" BINARY\n' % self.NAME).encode()
        bsdtar_archive(os.path.join(self.library, "psx.7z"), {
            "Game (U) [!].cue": cue,
            self.NAME: b"x" * 2048,
        })
        document = self.scan()
        rom = self.rom_named(document, "Game (U) [!]")
        self.assertEqual(rom["console"], "playstation")
        self.assertEqual(rom["extras"], [self.NAME])
        self.assertEqual(len(visible(document)), 1)


# ------------------------------------------------------------- 3. staging

class StagingTest(Fixture):
    """Staging used to go to /tmp (a tmpfs, so RAM), was never cleaned up, and
    checked the size limit only after writing the file."""

    def setUp(self):
        super().setUp()
        write_zip(os.path.join(self.library, "snes.zip"), {
            "One.smc": b"1" * 4096, "Two.smc": b"2" * 4096, "Three.smc": b"3" * 4096})
        self.document = self.scan()

    def stage(self, name):
        return cartridge_play.stage(self.rom_named(self.document, name))

    def test_stages_under_the_cache_not_tmp(self):
        content, staged = self.stage("One")
        self.assertTrue(staged)
        self.assertTrue(content.startswith(os.path.join(self.cache, "staging")))
        self.assertEqual(os.stat(os.path.dirname(content)).st_mode & 0o777, 0o700)
        again, staged = self.stage("One")
        self.assertEqual((again, staged), (content, False))

    def test_least_recently_played_make_room(self):
        with mock.patch.dict(os.environ, {"CARTRIDGE_STAGING_MAX": str(4096 * 2 + 1024)}):
            one, _ = self.stage("One")
            two, _ = self.stage("Two")
            # One is played again, so Two is now the oldest.
            marker = os.path.join(os.path.dirname(two), cartridge_play.MARKER)
            data = lib.read_json(marker)
            data["used"] = 1
            lib.write_json(marker, data)
            self.stage("Three")
        self.assertTrue(os.path.exists(one))
        self.assertFalse(os.path.exists(two))

    def test_refuses_before_writing_when_the_disk_is_full(self):
        full = mock.Mock(free=1024)
        with mock.patch.object(cartridge_play.shutil, "disk_usage", return_value=full):
            with self.assertRaises(ValueError) as caught:
                self.stage("One")
        self.assertIn("needs", str(caught.exception))
        staged = os.path.join(self.cache, "staging", self.rom_named(self.document, "One")["id"])
        self.assertEqual([n for n in os.listdir(staged)], [])

    def test_refuses_a_rom_over_the_limit_up_front(self):
        rom = dict(self.rom_named(self.document, "One"), size=cartridge_play.MAX_STAGED_BYTES + 1)
        with mock.patch.object(lib.Archive, "extract") as extract:
            with self.assertRaises(ValueError):
                cartridge_play.stage(rom)
            extract.assert_not_called()

    def test_a_failed_extraction_leaves_nothing_to_reuse(self):
        rom = self.rom_named(self.document, "One")
        self.stage("One")
        folder = os.path.join(self.cache, "staging", rom["id"])
        os.unlink(os.path.join(folder, "One.smc"))           # e.g. cleaned by hand
        with mock.patch.object(lib.Archive, "extract", return_value=False):
            with self.assertRaises(ValueError):
                cartridge_play.stage(rom)
        self.assertEqual(os.listdir(folder), [])              # marker gone too

    def test_a_rom_over_the_cap_is_refused_before_anything_is_evicted(self):
        other, _ = self.stage("Two")
        with mock.patch.dict(os.environ, {"CARTRIDGE_STAGING_MAX": "1024"}):
            with self.assertRaises(ValueError) as caught:
                self.stage("One")                            # 4096 bytes > 1024
        self.assertIn("staging cap", str(caught.exception))
        self.assertTrue(os.path.exists(other))

    def test_a_nonsense_cap_falls_back_to_the_default(self):
        for value in ("0", "-5", "lots"):
            with mock.patch.dict(os.environ, {"CARTRIDGE_STAGING_MAX": value}):
                self.assertEqual(cartridge_play.staging_cap(), cartridge_play.DEFAULT_STAGING_CAP)

    def test_a_change_inside_the_same_second_is_staged_again(self):
        path = os.path.join(self.library, "snes.zip")
        self.stage("One")
        before = os.stat(path).st_mtime_ns
        write_zip(path, {"One.smc": b"9" * 4096, "Two.smc": b"2" * 4096,
                         "Three.smc": b"3" * 4096})           # same size, new bytes
        os.utime(path, ns=(before + 1, before + 1))            # 1 ns later
        again, staged = self.stage("One")
        self.assertTrue(staged)
        with open(again, "rb") as handle:
            self.assertEqual(handle.read(1), b"9")

    def test_refuses_a_staging_dir_owned_by_someone_else(self):
        base = os.path.join(self.cache, "staging")
        os.makedirs(os.path.dirname(base), exist_ok=True)
        os.symlink(self.dir, base)
        with self.assertRaises(OSError):
            self.stage("One")


# ---------------------------------------------------- 4. duplicates come back

class DuplicatesTest(Fixture):
    """The archived copy of a game kept loose was dropped from roms.json, and
    its container cached as read -- so deleting the loose copy lost the game
    until the archive itself changed."""

    def test_archived_copy_returns_when_the_loose_one_goes(self):
        payload = b"n" * 64
        loose = self.loose("n64/1080.v64", payload)
        write_zip(os.path.join(self.library, "sets", "n64.zip"), {"1080.v64": payload})
        first = self.scan()
        self.assertEqual([rom["depth"] for rom in visible(first)], [0])

        os.unlink(loose)
        second = self.scan()
        self.assertEqual([rom["depth"] for rom in visible(second)], [1])
        self.assertEqual(second["roms"][0]["name"], "1080")

    def test_different_loose_files_of_the_same_size_are_both_kept(self):
        # Review #3: loose files carry no CRC, so equal size was taken as
        # equal bytes and one of two different games disappeared.
        self.loose("a/Game.smc", b"A" * 64)
        self.loose("b/Game.smc", b"B" * 64)
        self.assertEqual(len(visible(self.scan())), 2)

    def test_identical_loose_files_are_still_one_game(self):
        self.loose("a/Game.smc", b"A" * 64)
        self.loose("b/Game.smc", b"A" * 64)
        self.assertEqual(len(visible(self.scan())), 1)

    def test_loose_file_is_compared_with_a_zip_member_by_crc(self):
        self.loose("Game.smc", b"A" * 64)
        write_zip(os.path.join(self.library, "set.zip"), {"Game.smc": b"B" * 64})
        self.assertEqual(len(visible(self.scan())), 2)          # a conflict, not a duplicate
        shutil.rmtree(self.library)
        os.makedirs(self.library)
        self.loose("Game.smc", b"A" * 64)
        write_zip(os.path.join(self.library, "set.zip"), {"Game.smc": b"A" * 64})
        self.assertEqual(len(visible(self.scan())), 1)

    def test_a_loose_crc_is_not_worked_out_again_for_an_unchanged_folder(self):
        self.loose("a/Game.smc", b"A" * 64)
        self.loose("b/Game.smc", b"A" * 64)
        self.scan()
        with mock.patch.object(lib, "crc_of_files", side_effect=AssertionError("rehashed")):
            self.scan()

    def test_favorite_of_a_hidden_copy_moves_to_the_shown_one(self):
        payload = b"s" * 64
        write_zip(os.path.join(self.library, "a.zip"), {"Game.smc": payload})
        first = self.scan()
        archived = first["roms"][0]["id"]
        cartridge_state.set_favorite(ROOT, archived, True)
        self.loose("Game.smc", payload)                      # a loose copy appears
        second = self.scan()
        shown = visible(second)[0]
        self.assertEqual(shown["depth"], 0)
        self.assertTrue(lib.load_user()["favorites"].get(shown["id"]))


    def test_twin_of_a_placed_rom_is_not_left_in_unidentified(self):
        # Found on the real library: a rom placed by hand, and a byte-identical
        # copy of it elsewhere in the set that detection cannot place.
        write_zip(os.path.join(self.library, "set.zip"), {
            "A/Asterix.zip": zip_bytes({"ASTERIX.bin": b"z" * 64}),
            "B/Asterix (alt).zip": zip_bytes({"ASTERIX.bin": b"z" * 64})})
        first = self.scan()
        placed = visible(first)[0]["id"]
        cartridge_state.assign_console(ROOT, placed, "sega-genesis-mega-drive")
        os.utime(os.path.join(self.library, "set.zip"), ns=(1, 1))   # force a re-read
        second = self.scan()
        shown = visible(second)
        self.assertEqual([rom["id"] for rom in shown], [placed])


    def test_same_name_on_two_consoles_is_deduplicated_per_console(self):
        # "Aladdin (U) [!]" is a SNES game and a different Mega Drive game;
        # each is also stored twice. Found on the real library: grouping the
        # two consoles together stopped either pair being deduplicated.
        for folder, ext, byte in (("snes", "smc", b"s"), ("md", "md", b"m")):
            self.loose("%s/Aladdin.%s" % (folder, ext), byte * 64)
            write_zip(os.path.join(self.library, "packs", folder + ".zip"),
                      {"Aladdin.%s" % ext: byte * 64})
        shown = visible(self.scan())
        self.assertEqual(sorted((rom["console"], rom["depth"]) for rom in shown),
                         [("sega-genesis-mega-drive", 0), (SNES, 0)])


# ------------------------------------------------ 5. core choice is kept

class CoreChoiceTest(Fixture):
    """A console with no roms in one scan lost its row -- and the core picked
    for it -- in consoles.json."""

    def test_core_survives_a_scan_without_that_console(self):
        rom = self.loose("Game.smc", b"s" * 16)
        self.scan()
        cores = lib.read_json(lib.cores_json())["cores"]
        snes_core = next((core["id"] for core in cores if core["systemId"] == SNES), None)
        if snes_core is None:
            self.skipTest("no SNES core installed")
        cartridge_state.set_core(ROOT, SNES, snes_core)

        os.unlink(rom)
        self.loose("Other.v64", b"n" * 16)                   # the drive "unmounted"
        self.scan()
        entry = lib.read_json(lib.consoles_json())["consoles"][SNES]
        self.assertEqual((entry["core"], entry["present"]), (snes_core, False))

        self.loose("Game.smc", b"s" * 16)                    # and it is back
        self.scan()
        entry = lib.read_json(lib.consoles_json())["consoles"][SNES]
        self.assertEqual((entry["core"], entry["present"]), (snes_core, True))

    def test_a_console_without_a_core_is_still_dropped(self):
        rom = self.loose("Game.smc", b"s" * 16)
        self.scan()
        os.unlink(rom)
        self.loose("Other.v64", b"n" * 16)
        self.scan()
        self.assertNotIn(SNES, lib.read_json(lib.consoles_json())["consoles"])


# ------------------------------------------ 6 + 7. archives are opened once

class OpenCounter:
    """Counts how many times each archive path is opened as a zip."""

    def __init__(self):
        self.opens = {}
        self.real = zipfile.ZipFile

    def __call__(self, file, *args, **kwargs):
        if isinstance(file, str):
            self.opens[file] = self.opens.get(file, 0) + 1
        return self.real(file, *args, **kwargs)


class OpenOnceTest(Fixture):
    """Every archive inside a rom set reopened the rom set, re-parsing its
    central directory; every cue sheet reopened (or, two levels down, re-read
    into memory) the archive it was in."""

    INNER = 300

    def test_a_zip_of_zips_is_opened_once_per_job(self):
        path = os.path.join(self.library, "set.zip")
        write_zip(path, {"G%03d.zip" % i: zip_bytes({"G%03d.smc" % i: bytes([i % 256]) * 32})
                         for i in range(self.INNER)})
        counter = OpenCounter()
        with mock.patch.object(lib.zipfile, "ZipFile", counter):
            document = self.scan()
        self.assertEqual(len(visible(document)), self.INNER)
        workers = min(8, os.cpu_count() or 4)
        # One for the first level, one per job for the second.
        self.assertLessEqual(counter.opens[path], 1 + workers)

    def test_cue_sheets_do_not_reopen_their_archive(self):
        path = os.path.join(self.library, "psx.zip")
        discs = {}
        for i in range(20):
            discs["D%02d.zip" % i] = zip_bytes({
                "D%02d.cue" % i: b'FILE "D%02d.bin" BINARY\n' % i,
                "D%02d.bin" % i: bytes([i]) * 64})
        write_zip(path, discs)
        counter = OpenCounter()
        with mock.patch.object(lib.zipfile, "ZipFile", counter):
            document = self.scan()
        self.assertEqual(len([r for r in visible(document) if r["kind"] == "cue"]), 20)
        self.assertLessEqual(counter.opens[path], 1 + min(8, os.cpu_count() or 4))

    @unittest.skipUnless(HAVE_BSDTAR, "bsdtar is not installed")
    def test_a_7z_of_zips_is_decompressed_once(self):
        path = os.path.join(self.library, "set.7z")
        bsdtar_archive(path, {"G%02d [!].zip" % i: zip_bytes({"G%02d.smc" % i: bytes([i]) * 32})
                              for i in range(40)})
        calls = []
        real = lib.Archive._bsdtar

        def counting(command, *args, **kwargs):
            calls.append(command)
            return real(command, *args, **kwargs)

        with mock.patch.object(lib.Archive, "_bsdtar", staticmethod(counting)):
            document = self.scan()
        self.assertEqual(len(visible(document)), 40)
        # One listing, one extraction -- not one per inner archive.
        self.assertLessEqual(len(calls), 2)

    def test_a_big_inner_archive_is_spooled_not_truncated(self):
        big = os.urandom(300 * 1024)                          # incompressible
        write_zip(os.path.join(self.library, "set.zip"), {
            "Big.zip": zip_bytes({"Big.smc": big, "zz.txt": b"end"})})
        with mock.patch.object(lib, "IN_MEMORY_NESTED_BYTES", 64 * 1024):
            document = self.scan()
        self.assertEqual([rom["name"] for rom in visible(document)], ["Big"])
        self.assertEqual(os.listdir(os.path.join(self.cache, "scratch")), [])


# ------------------------------------------------------------ 8. user.json

class UserStateTest(Fixture):
    """Favoriting rewrote the whole library file (3.4 MB here), which the UI
    then re-read and re-parsed in full."""

    def test_favorite_does_not_touch_roms_json(self):
        self.loose("Game.smc", b"s" * 16)
        document = self.scan()
        before = os.stat(lib.roms_json())
        cartridge_state.set_favorite(ROOT, document["roms"][0]["id"], True)
        after = os.stat(lib.roms_json())
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))
        self.assertTrue(lib.load_user()["favorites"][document["roms"][0]["id"]])

    def test_play_records_the_time_in_user_json(self):
        self.loose("Game.smc", b"s" * 16)
        document = self.scan()
        rom_id = document["roms"][0]["id"]
        before = os.stat(lib.roms_json()).st_mtime_ns
        cartridge_play.mark_played(lib.plugin_root(), rom_id, 1234)
        self.assertEqual(os.stat(lib.roms_json()).st_mtime_ns, before)
        self.assertEqual(lib.load_user()["lastPlayed"][rom_id], 1234)

    def test_old_state_is_migrated_once(self):
        os.makedirs(self.state)
        lib.write_json(lib.roms_json(), {"version": 4, "roms": [
            {"id": "a", "favorite": True, "lastPlayed": 99, "console": "x", "reason": "assigned"},
            {"id": "b", "favorite": False, "lastPlayed": 0, "console": "y", "reason": "auto"},
        ]})
        user = lib.load_user()
        self.assertEqual((user["favorites"], user["lastPlayed"], user["assigned"]),
                         ({"a": True}, {"a": 99}, {"a": "x"}))
        self.assertTrue(os.path.exists(lib.user_json()))

    def test_auto_gives_detection_back_without_a_rescan(self):
        self.loose("orphan.bin", b"x" * 16)
        document = self.scan()
        rom = document["roms"][0]
        cartridge_state.assign_console(ROOT, rom["id"], "sega-genesis-mega-drive")
        self.assertEqual(lib.effective_console(rom, lib.load_user())[0], "sega-genesis-mega-drive")
        cartridge_state.assign_console(ROOT, rom["id"], "auto")
        self.assertEqual(lib.effective_console(rom, lib.load_user())[0], "unknown")

    def test_container_assignment_only_touches_the_unknown(self):
        write_zip(os.path.join(self.library, "mix.zip"), {
            "a.bin": b"a" * 16, "b.bin": b"b" * 16, "c.smc": b"c" * 16})
        self.scan()
        path = os.path.join(self.library, "mix.zip")
        result = cartridge_state.assign_container(ROOT, path, "sega-genesis-mega-drive")
        self.assertEqual((result["assigned"], result["skipped"]), (2, 1))
        undone = cartridge_state.assign_container(ROOT, path, "auto")
        self.assertEqual(undone["assigned"], 2)
        self.assertEqual(lib.load_user()["assigned"], {})

    def test_scan_stamps_consoles_json_for_the_ui(self):
        self.loose("Game.smc", b"s" * 16)
        roms = self.scan()
        consoles = lib.read_json(lib.consoles_json())
        self.assertEqual(consoles["romsStamp"], roms["stamp"])


# ---------------------------------------------------------------- misc

class MiscTest(Fixture):
    def test_bsdtar_runs_in_the_c_locale(self):
        self.assertEqual(lib._BSDTAR_ENV["LC_ALL"], "C")

    def test_fingerprint_has_nanosecond_resolution(self):
        path = self.loose("Game.smc", b"s")
        os.utime(path, ns=(10**18 + 1, 10**18 + 1))
        first = cartridge_scan.fingerprint(path)
        os.utime(path, ns=(10**18 + 2, 10**18 + 2))
        self.assertNotEqual(first, cartridge_scan.fingerprint(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
