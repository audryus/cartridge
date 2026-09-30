#!/usr/bin/env python3
"""Change cartridge state: favorite a rom, tell cartridge what an unknown rom
is, pick the core for a console.

What the user decides goes to user.json, which the scanner only reads, and
every write happens under the state lock -- so a change made here survives the
next scan, even one already running, and a scan never drops it.

  cartridge-state favorite <romId> <0|1>
  cartridge-state assign   <romId> <consoleId|auto>
  cartridge-state container <path> <consoleId|auto>
  cartridge-state core     <consoleId> <coreId|->

"auto" forgets what cartridge was told and goes back to detection, which is the
way out of an answer you gave to the wrong file. Detection's answer is already
in roms.json, so it shows again straight away.

stdout is a single JSON line: {"ok": true, ...} or {"ok": false, "error": ...}
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cartridge_lib as lib  # noqa: E402


def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def find_rom(document, rom_id):
    for rom in document.get("roms") or []:
        if rom.get("id") == rom_id:
            return rom
    return None


def load_roms(root):
    return lib.read_json(lib.roms_json(root), {}) or {}


def set_favorite(root, rom_id, value):
    """Only user.json is written: a favorite is a few bytes, and must not cost
    a rewrite of the whole library.

    The rom is looked up inside the lock, so a scan that replaces roms.json
    cannot land between the check and the write."""
    def mutate(user):
        if find_rom(load_roms(root), rom_id) is None:
            raise KeyError(rom_id)
        if value:
            user["favorites"][rom_id] = True
        else:
            user["favorites"].pop(rom_id, None)
    lib.update_user(root, mutate)
    return {"ok": True, "id": rom_id, "favorite": bool(value)}


AUTO = "auto"


def check_console(root, console_id):
    consoles = lib.read_json(lib.consoles_json(root), {}) or {}
    catalog = consoles.get("catalog") or {}
    if console_id not in (lib.UNKNOWN_ID, AUTO) and console_id not in catalog:
        raise KeyError(console_id)
    return catalog


def assign_console(root, rom_id, console_id):
    """Tell cartridge what an unidentified rom is. The choice sticks: it lives
    in user.json, which the scanner never overwrites. "auto" gives the answer
    back and detection's answer -- already in roms.json -- shows again at once,
    with no rescan needed."""
    catalog = check_console(root, console_id)

    def mutate(user):
        if find_rom(load_roms(root), rom_id) is None:
            raise KeyError(rom_id)
        if console_id == AUTO:
            user["assigned"].pop(rom_id, None)
        else:
            user["assigned"][rom_id] = console_id
    lib.update_user(root, mutate)
    ensure_console(root, console_id, catalog)
    return {"ok": True, "id": rom_id, "console": console_id,
            "name": catalog.get(console_id, console_id), "rescan": False}


def ensure_console(root, console_id, catalog):
    """A console the library did not have before now has one, and it needs an
    entry so a core can be assigned to it without waiting for a rescan."""
    if console_id in (lib.UNKNOWN_ID, AUTO):
        return

    def mutate(document):
        consoles = document.get("consoles")
        if not isinstance(consoles, dict):
            consoles = {}
        entry = consoles.setdefault(console_id, {"id": console_id})
        entry.setdefault("core", "")
        entry["present"] = True
        entry["name"] = catalog.get(console_id, entry.get("name") or console_id)
        document["consoles"] = consoles
    lib.update_json(lib.consoles_json(root), mutate,
                    default=lambda: {"consoles": {}, "catalog": {}})


def assign_container(root, container, console_id):
    """Tell cartridge what every unidentified rom inside one file or folder is.

    A rom set of 5,000 unidentified .bin files is not something anyone should
    have to identify one at a time, but assigning a whole archive by accident
    is worse, so this is only ever offered for one container at a time and the
    reply says exactly how many roms it touched."""
    catalog = check_console(root, console_id)
    state = {"touched": 0, "left": 0}

    def mutate(user):
        # Read under the lock: the roms of this container as the latest scan
        # left them, not as they were before one finished.
        inside = [rom for rom in load_roms(root).get("roms") or []
                  if rom.get("path") == container and not rom.get("hidden")]
        assigned = user["assigned"]
        for rom in inside:
            if console_id == AUTO:
                if assigned.pop(rom["id"], None) is not None:
                    state["touched"] += 1
                continue
            if lib.effective_console(rom, user)[0] != lib.UNKNOWN_ID:
                state["left"] += 1
                continue
            assigned[rom["id"]] = console_id
            state["touched"] += 1
    lib.update_user(root, mutate)
    ensure_console(root, console_id, catalog)
    return {"ok": True, "container": container, "console": console_id,
            "name": catalog.get(console_id, console_id),
            "assigned": state["touched"], "skipped": state["left"]}


def set_core(root, console_id, core_id):
    cores = lib.read_json(lib.cores_json(root), {}) or {}
    known = {core.get("id") for core in cores.get("cores") or []}
    if core_id != "-" and core_id not in known:
        raise KeyError(core_id)
    state = {}

    def mutate(document):
        consoles = document.get("consoles")
        if not isinstance(consoles, dict):
            consoles = {}
        entry = consoles.get(console_id)
        if not isinstance(entry, dict):
            return document
        entry["core"] = "" if core_id == "-" else core_id
        state["entry"] = entry
    lib.update_json(lib.consoles_json(root), mutate,
                    default=lambda: {"consoles": {}, "catalog": {}})
    if "entry" not in state:
        raise KeyError(console_id)
    return {"ok": True, "console": console_id,
            "core": "" if core_id == "-" else core_id}


def main():
    if len(sys.argv) < 2:
        lib.fail(__doc__.strip(), 64)
    command = sys.argv[1]
    root = lib.plugin_root()

    try:
        if command == "favorite" and len(sys.argv) == 4:
            result = set_favorite(root, sys.argv[2], sys.argv[3] in ("1", "true", "yes"))
        elif command == "assign" and len(sys.argv) == 4:
            result = assign_console(root, sys.argv[2], sys.argv[3])
        elif command == "container" and len(sys.argv) == 4:
            result = assign_container(root, sys.argv[2], sys.argv[3])
        elif command == "core" and len(sys.argv) == 4:
            result = set_core(root, sys.argv[2], sys.argv[3])
        else:
            lib.fail(__doc__.strip(), 64)
    except KeyError as missing:
        emit({"ok": False, "error": "no such %s: %s" % (command, missing.args[0])})
        return 3
    except OSError as problem:
        emit({"ok": False, "error": str(problem)})
        return 4
    emit(result)


if __name__ == "__main__":
    sys.exit(main())
