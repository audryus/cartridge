#!/usr/bin/env python3
"""Tests for the scanner, against a library built in a temp directory.

Run with:  python3 tests/test_scan.py     (or: make test)

No network, no real library, no real RetroArch. The fixtures are built here so
the expectations are about cartridge's rules, not about whatever happens to be
in ~/Games/roms.
"""

import importlib.machinery
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "bin"))

import cartridge_lib as lib          # noqa: E402


def load_script(name):
    """Import one of the extensionless scripts in bin/ as a module."""
    import importlib.util
    spec = importlib.util.spec_from_loader(
        name, importlib.machinery.SourceFileLoader(name, os.path.join(ROOT, "bin", name)))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cartridge_scan = load_script("cartridge-scan.py")


def zip_bytes(entries):
    """A zip in memory: {name: bytes}."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def write_zip(path, entries):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(zip_bytes(entries))


class ScannerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cartridge-test-")
        self.library = os.path.join(self.dir, "roms")
        self.state = os.path.join(self.dir, "state")
        os.makedirs(self.library)
        os.environ["CARTRIDGE_ROMS_ROOT"] = self.library
        os.environ["CARTRIDGE_STATE_DIR"] = self.state

    def tearDown(self):
        os.environ.pop("CARTRIDGE_ROMS_ROOT", None)
        os.environ.pop("CARTRIDGE_STATE_DIR", None)
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- helpers ---------------------------------------------------------

    def scan(self):
        # The scanner narrates on stderr; the test output is not the story.
        noise = os.dup(2)
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, 2)
            cartridge_scan.main()
        finally:
            sys.stdout.flush()
            os.dup2(noise, 2)
            os.close(devnull)
            os.close(noise)
        with open(os.path.join(self.state, "roms.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def names(self, document, console=None):
        roms = document["roms"]
        if console is not None:
            roms = [rom for rom in roms if rom["console"] == console]
        return sorted(rom["name"] for rom in roms)

    # -- classification --------------------------------------------------

    def test_extension_picks_the_console(self):
        with open(os.path.join(self.library, "game.smc"), "wb") as handle:
            handle.write(b"snes")
        with open(os.path.join(self.library, "game.v64"), "wb") as handle:
            handle.write(b"n64")
        with open(os.path.join(self.library, "game.md"), "wb") as handle:
            handle.write(b"mega drive")
        document = self.scan()
        self.assertEqual(self.names(document, "super-nintendo-entertainment-system"),
                         ["game"])
        self.assertEqual(self.names(document, "nintendo-64"), ["game"])
        self.assertEqual(self.names(document, "sega-genesis-mega-drive"), ["game"])

    def test_loose_bin_is_ambiguous_not_a_guess(self):
        with open(os.path.join(self.library, "orphan.bin"), "wb") as handle:
            handle.write(b"x" * 16)
        document = self.scan()
        self.assertEqual(self.names(document), ["orphan"])
        self.assertEqual(document["roms"][0]["reason"], "ambiguous")

    def test_cue_and_bin_are_one_playstation_game(self):
        os.makedirs(os.path.join(self.library, "psx"))
        base = os.path.join(self.library, "psx", "A Game")
        with open(base + ".bin", "wb") as handle:
            handle.write(b"x" * 2048)
        with open(base + ".cue", "wb") as handle:
            handle.write(b'FILE "A Game.bin" BINARY\n')
        document = self.scan()
        self.assertEqual(self.names(document), ["A Game"])
        rom = document["roms"][0]
        self.assertEqual(rom["console"], "playstation")
        self.assertEqual(rom["kind"], "cue")
        self.assertEqual(rom["extras"], ["A Game.bin"])
        # The disc is the cue plus what it points at, which is what makes two
        # copies of the same game comparable.
        self.assertEqual(rom["size"], 2048 + len(b'FILE "A Game.bin" BINARY\n'))

    def test_junk_next_to_a_rom_is_not_a_rom(self):
        write_zip(os.path.join(self.library, "set.zip"), {
            "Real Game.smc": b"x" * 32,
            "Read me.txt": b"thanks",
            "cover.jpg": b"x" * 8,
            "cheats.srm": b"x" * 8,
        })
        document = self.scan()
        self.assertEqual(self.names(document), ["Real Game"])

    def test_unknown_extension_is_reported_not_guessed(self):
        with open(os.path.join(self.library, "mystery.qqq"), "wb") as handle:
            handle.write(b"x")
        document = self.scan()
        self.assertEqual(document["roms"][0]["console"], "unknown")
        self.assertEqual(document["roms"][0]["reason"], "unmapped")

    # -- two levels of archive -------------------------------------------

    def test_zip_inside_zip_is_found(self):
        write_zip(os.path.join(self.library, "romset.zip"), {
            "A/Game One.zip": zip_bytes({"Game One.smc": b"x" * 16}),
            "B/Game Two.zip": zip_bytes({"Game Two.smc": b"y" * 16}),
        })
        document = self.scan()
        self.assertEqual(self.names(document, "super-nintendo-entertainment-system"),
                         ["Game One", "Game Two"])
        depths = {rom["name"]: rom["depth"] for rom in document["roms"]}
        self.assertEqual(depths, {"Game One": 2, "Game Two": 2})
        parents = {rom["name"]: rom["parent"] for rom in document["roms"]}
        self.assertEqual(parents["Game One"], "A/Game One.zip")

    def test_bracket_in_a_member_name_does_not_hide_the_rom(self):
        # bsdtar treats a member name as a glob, so "[b1]" used to match
        # nothing and this rom silently disappeared.
        write_zip(os.path.join(self.library, "romset.zip"), {
            "Ka-Ge-Ki - Fists of Steel (J) [b1].zip": zip_bytes(
                {"Ka-Ge-Ki - Fists of Steel (J) [b1].md": b"x" * 16}),
        })
        document = self.scan()
        self.assertEqual(self.names(document, "sega-genesis-mega-drive"),
                         ["Ka-Ge-Ki - Fists of Steel (J) [b1]"])

    # -- duplicates and conflicts ----------------------------------------

    def test_same_game_loose_and_archived_keeps_the_loose_one(self):
        payload = b"x" * 64
        with open(os.path.join(self.library, "1080F.V64"), "wb") as handle:
            handle.write(payload)
        write_zip(os.path.join(self.library, "n64.zip"), {"1080F.V64": payload})
        document = self.scan()
        self.assertEqual(len(document["roms"]), 1)
        self.assertEqual(document["roms"][0]["depth"], 0)

    def test_same_name_different_bytes_is_kept_both_times(self):
        write_zip(os.path.join(self.library, "dupes.zip"), {
            "D/A.smc": b"x" * 16,
            "D/B.smc": b"y" * 16,
        })
        document = self.scan()
        self.assertEqual(len(document["roms"]), 2)

    # -- state that has to survive a rescan -------------------------------

    def test_favorite_and_assignment_survive_a_rescan(self):
        with open(os.path.join(self.library, "game.smc"), "wb") as handle:
            handle.write(b"snes")
        document = self.scan()
        rom = document["roms"][0]

        path = lib.roms_json()
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
        state["roms"][0]["favorite"] = True
        state["roms"][0]["reason"] = "assigned"
        state["roms"][0]["console"] = "sega-genesis-mega-drive"
        lib.write_json(path, state)

        again = self.scan()
        kept = again["roms"][0]
        self.assertTrue(kept["favorite"])
        self.assertEqual(kept["console"], "sega-genesis-mega-drive")
        self.assertEqual(kept["reason"], "assigned")
        self.assertEqual(rom["id"], kept["id"])

    def test_unchanged_containers_are_not_read_again(self):
        write_zip(os.path.join(self.library, "set.zip"), {"Game.smc": b"x" * 16})
        first = self.scan()
        second = self.scan()
        self.assertEqual(first["roms"][0]["id"], second["roms"][0]["id"])
        # The second pass reuses everything, and says so.
        self.assertEqual(len(second["containers"]), 1)


class ConsoleMapTest(unittest.TestCase):
    """The extension table comes from libretro-core-info, so these assertions
    only hold on a machine that has it installed."""

    def setUp(self):
        self.cores = lib.load_core_info()
        if not self.cores:
            self.skipTest("libretro-core-info is not installed")
        self.map = lib.build_extension_map(self.cores)

    def test_db_aliases_are_merged(self):
        self.assertEqual(lib.canonical_system("Sega 8/16-bit (Various)"),
                         "Sega Genesis / Mega Drive")
        self.assertEqual(lib.canonical_system("Sega Genesis"),
                         "Sega Genesis / Mega Drive")
        self.assertEqual(lib.canonical_system("Sega Master System"),
                         "Sega Master System / 8-bit")

    def test_unambiguous_extensions(self):
        self.assertEqual(self.map.get("smc"), ["super-nintendo-entertainment-system"])
        self.assertEqual(self.map.get("v64"), ["nintendo-64"])

    def test_mega_drive_is_unambiguous_after_merging(self):
        # `.md` is claimed by Genesis Plus GX ("Sega Genesis") and PicoDrive
        # ("Sega 8/16-bit (Various)"). Merged, it is one console.
        self.assertEqual(len(self.map.get("md", [])), 1)

    def test_junk_extensions_are_dropped(self):
        # Not extensions at all, plus the ones only catch-all cores claim:
        # ScummVM lists blorb and pak, and listing them would make an
        # unrelated file look like a game.
        for ext in ("$00", "d$$", "#02", "gcmc", "lha", "vfs"):
            self.assertIsNone(self.map.get(ext), ext)

    def test_real_extensions_of_other_systems_survive(self):
        # Dropping junk must not throw away a system cartridge along with it.
        self.assertEqual(self.map.get("gcm"), ["gamecube-wii"])
        self.assertEqual(self.map.get("nes"), ["nintendo-entertainment-system"])

    def test_a_broken_database_claim_makes_an_extension_ambiguous(self):
        # Several Game Boy emulators list "gba" in their supported extensions,
        # so cartridge cannot call a .gba file a Game Boy Advance game without
        # guessing. This records the consequence rather than hiding it: those
        # roms land in Unidentified for the user to place.
        self.assertIn("game-boy-game-boy-color", self.map.get("gba", []))

    def test_catch_all_cores_do_not_claim_the_alphabet(self):
        for ext in ("bin", "iso", "chd", "cue"):
            self.assertGreater(len(self.map.get(ext, [])), 1, ext)


if __name__ == "__main__":
    unittest.main(verbosity=2)
