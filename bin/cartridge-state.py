#!/usr/bin/env python3
"""Change cartridge state: favorite a rom, tell cartridge what an unknown rom
is, pick the core for a console.

Both the scanner and this script write state, and both go through
lib.update_json, so a change made here survives the next scan and a scan never
drops a choice made here.

  cartridge-state favorite <romId> <0|1>
  cartridge-state assign   <romId> <consoleId|auto>
  cartridge-state container <path> <consoleId|auto>
  cartridge-state core     <consoleId> <coreId|->

"auto" forgets what cartridge was told and goes back to detection, which is the
way out of an answer you gave to the wrong file. The change lands on the next
scan; cartridge-state re-detects nothing by itself.

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


def set_favorite(root, rom_id, value):
    state = {}

    def mutate(document):
        rom = find_rom(document, rom_id)
        if rom is None:
            return document
        rom["favorite"] = bool(value)
        state["rom"] = rom
    lib.update_json(lib.roms_json(root), mutate, default=lambda: {"roms": []})
    if "rom" not in state:
        raise KeyError(rom_id)
    return {"ok": True, "id": rom_id, "favorite": bool(value)}


AUTO = "auto"


def forget_container(root, path):
    """Make the next scan read this file again.

    A forgotten answer lives in the rom records the last scan cached for that
    archive. Clearing the console in place is not enough: the reused records
    still hold the emptied value. Dropping the fingerprint is what sends the
    archive back through detection, which is the only place that can answer it
    again."""
    if not path:
        return

    def mutate(document):
        containers = document.get("containers")
        if isinstance(containers, dict):
            containers.pop(path, None)
    lib.update_json(lib.roms_json(root), mutate, default=lambda: {"roms": []})


def assign_console(root, rom_id, console_id):
    """Tell cartridge what an unidentified rom is. The choice sticks: the
    scanner keeps any rom whose reason is 'assigned'. "auto" gives the answer
    back and lets detection decide again."""
    consoles = lib.read_json(lib.consoles_json(root), {}) or {}
    catalog = consoles.get("catalog") or {}
    if console_id not in (lib.UNKNOWN_ID, AUTO) and console_id not in catalog:
        raise KeyError(console_id)
    state = {}

    def mutate(document):
        rom = find_rom(document, rom_id)
        if rom is None:
            return document
        if console_id == AUTO:
            rom["console"] = ""
            rom["reason"] = "auto"
        else:
            rom["console"] = console_id
            rom["reason"] = "unknown" if console_id == lib.UNKNOWN_ID else "assigned"
        state["rom"] = rom
    lib.update_json(lib.roms_json(root), mutate, default=lambda: {"roms": []})
    if "rom" not in state:
        raise KeyError(rom_id)
    rom = state["rom"]
    ensure_console(root, console_id, catalog)
    if console_id == AUTO:
        forget_container(root, rom.get("path"))
    return {"ok": True, "id": rom_id, "console": console_id,
            "name": catalog.get(console_id, console_id), "rescan": console_id == AUTO}


def ensure_console(root, console_id, catalog):
    """A console the library did not have before now has one, and it needs an
    entry so a core can be assigned to it without waiting for a rescan."""
    if console_id == lib.UNKNOWN_ID:
        return

    def mutate(document):
        consoles = document.get("consoles")
        if not isinstance(consoles, dict):
            consoles = {}
        entry = consoles.setdefault(console_id, {"id": console_id})
        entry.setdefault("core", "")
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
    consoles = lib.read_json(lib.consoles_json(root), {}) or {}
    catalog = consoles.get("catalog") or {}
    if console_id not in (lib.UNKNOWN_ID, AUTO) and console_id not in catalog:
        raise KeyError(console_id)
    state = {"touched": 0, "left": 0}

    def mutate(document):
        for rom in document.get("roms") or []:
            if rom.get("path") != container:
                continue
            if console_id == AUTO:
                rom["console"] = ""
                rom["reason"] = "auto"
                state["touched"] += 1
                continue
            if rom.get("console") != lib.UNKNOWN_ID:
                state["left"] += 1
                continue
            rom["console"] = console_id
            rom["reason"] = "unknown" if console_id == lib.UNKNOWN_ID else "assigned"
            state["touched"] += 1
    lib.update_json(lib.roms_json(root), mutate, default=lambda: {"roms": []})
    ensure_console(root, console_id, catalog)
    if console_id == AUTO:
        forget_container(root, container)
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
