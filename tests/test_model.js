// Tests for the QML-side model: sorting, search, conflicts, the console list.
//
// The model is a plain .js file with no imports, so it can be run by node
// against fixtures with no shell, no display and no library.
//
//   node tests/test_model.js

const fs = require("fs");
const vm = require("vm");
const path = require("path");
const assert = require("assert");

const root = path.dirname(__dirname);
const sandbox = { console };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
const file = path.join(root, "ui", "CartridgeModel.js");
vm.runInContext(fs.readFileSync(file, "utf8") + `
;globalThis.api = { reindex, sameContent, romRows, consoleRows, compareRoms,
    coreOptions, unknownGroups, matches, formatWhen, formatSize, prettify,
    hasCore, coreLabel, UNKNOWN, CONFLICT };
`, sandbox, { filename: file });
const M = sandbox.api;

let failures = 0;
let ran = 0;

function test(name, body) {
    ran++;
    try {
        body();
        console.log("ok   " + name);
    } catch (error) {
        failures++;
        console.log("FAIL " + name);
        console.log("     " + (error && error.message ? error.message : error));
    }
}

let nextId = 0;
function rom(extra) {
    const base = {
        id: "id" + nextId++,
        name: "Game",
        console: "nintendo-64",
        reason: "auto",
        path: "/roms/Game.V64",
        entry: "Game.V64",
        parent: "",
        depth: 0,
        ext: "v64",
        kind: "file",
        size: 1024,
        crc: "aaaa",
        favorite: false,
        lastPlayed: 0,
        source: "Game.V64",
        where: "Game.V64"
    };
    return Object.assign(base, extra);
}

const always = () => true;

// ---------------------------------------------------------------- sorting

test("favorites come first, then most recently played, then by name", () => {
    const roms = [
        rom({ name: "Zelda", lastPlayed: 100 }),
        rom({ name: "Alpha", favorite: true, lastPlayed: 5 }),
        rom({ name: "Mario", lastPlayed: 900 }),
        rom({ name: "Beta", favorite: true, lastPlayed: 500 }),
        rom({ name: "Ancient", lastPlayed: 0 })
    ];
    const rows = M.romRows(roms, M.reindex(roms), "nintendo-64", "", always);
    assert.deepStrictEqual(Array.from(rows, r => r.name),
        ["Beta", "Alpha", "Mario", "Zelda", "Ancient"]);
});

test("names sort with numbers inside them", () => {
    const roms = [rom({ name: "Game 10" }), rom({ name: "Game 2" }),
                  rom({ name: "Game 1" })];
    const rows = M.romRows(roms, M.reindex(roms), "nintendo-64", "", always);
    assert.deepStrictEqual(Array.from(rows, r => r.name), ["Game 1", "Game 2", "Game 10"]);
});

// ----------------------------------------------------------------- search

test("search matches the name, ignoring case", () => {
    const roms = [rom({ name: "Super Mario World" }), rom({ name: "Alien 3" })];
    const rows = M.romRows(roms, M.reindex(roms), "nintendo-64", "MARIO", always);
    assert.deepStrictEqual(Array.from(rows, r => r.name), ["Super Mario World"]);
});

test("search also matches where the rom came from", () => {
    // In the Unidentified column thousands of roms share a name, and only the
    // archive tells them apart.
    const roms = [rom({ name: "16 Tiles Mahjong", console: M.UNKNOWN,
                        where: "mega/Romset.zip › A/16 Tiles Mahjong.bin",
                        source: "mega/Romset.zip" }),
                  rom({ name: "16 Tiles Mahjong", console: M.UNKNOWN,
                        where: "mega/Other.zip › B/16 Tiles Mahjong.bin",
                        source: "mega/Other.zip" })];
    const rows = M.romRows(roms, M.reindex(roms), M.UNKNOWN, "Other", always);
    assert.strictEqual(rows.length, 1);
    assert.strictEqual(rows[0].where, "mega/Other.zip › B/16 Tiles Mahjong.bin");
});

// --------------------------------------------------------- content and conflicts

test("the same game stored twice is not a conflict", () => {
    const roms = [rom({ name: "1080F", path: "/roms/1080F.V64" }),
                  rom({ name: "1080f", path: "/roms/pack.zip", depth: 2 })];
    const index = M.reindex(roms);
    assert.deepStrictEqual(Object.keys(index.conflicts), []);
    assert.strictEqual(index.counts["nintendo-64"], 2);
    assert.strictEqual(index.counts[M.CONFLICT], undefined);
});

test("the same name with different bytes is a conflict", () => {
    const roms = [rom({ name: "Fatal Labyrinth", crc: "aaaa", size: 131072 }),
                  rom({ name: "Fatal Labyrinth", crc: "bbbb", size: 131072 })];
    const index = M.reindex(roms);
    assert.strictEqual(Object.keys(index.conflicts).length, 2);
    assert.strictEqual(index.counts[M.CONFLICT], 2);
    assert.strictEqual(index.counts["nintendo-64"], undefined);

    // Conflicting roms live in the Conflict column, not in their console.
    const real = M.romRows(roms, index, "nintendo-64", "", always);
    assert.strictEqual(real.length, 0);
    const conflict = M.romRows(roms, index, M.CONFLICT, "", always);
    assert.strictEqual(conflict.length, 2);
});

test("a size difference alone is enough to call it a conflict", () => {
    const roms = [rom({ name: "ResQ", size: 1048576 }), rom({ name: "ResQ", size: 1046048 })];
    assert.strictEqual(Object.keys(M.reindex(roms).conflicts).length, 2);
});

test("without a crc, equal size is taken as the same bytes", () => {
    assert.strictEqual(M.sameContent({ size: 10, crc: "" }, { size: 10, crc: "" }), true);
    assert.strictEqual(M.sameContent({ size: 10, crc: "a" }, { size: 10, crc: "" }), true);
    assert.strictEqual(M.sameContent({ size: 10, crc: "a" }, { size: 10, crc: "b" }), false);
});

test("unidentified roms are never counted as conflicts", () => {
    // They cannot be compared until you know what they are, so they stay in
    // Unidentified and the count matches what the list shows.
    const roms = [rom({ name: "Boom", console: M.UNKNOWN, crc: "a" }),
                  rom({ name: "Boom", console: M.UNKNOWN, crc: "b" })];
    const index = M.reindex(roms);
    assert.strictEqual(index.counts[M.UNKNOWN], 2);
    assert.strictEqual(index.counts[M.CONFLICT], undefined);
    assert.strictEqual(M.romRows(roms, index, M.CONFLICT, "", always).length, 0);
});

// ------------------------------------------------------------- the columns

test("the console list is alphabetical, with the two odd ones last", () => {
    const roms = [
        rom({ name: "A", console: "super-nintendo-entertainment-system" }),
        rom({ name: "B", console: "nintendo-64" }),
        rom({ name: "C", console: M.UNKNOWN }),
        rom({ name: "D", console: "sega-genesis-mega-drive" }),
        rom({ name: "E", console: "super-nintendo-entertainment-system" }),
        rom({ name: "F", console: "sega-genesis-mega-drive" })
    ];
    const index = M.reindex(roms);
    const rows = M.consoleRows(index, {
        "nintendo-64": "Nintendo 64",
        "sega-genesis-mega-drive": "Sega Genesis / Mega Drive",
        "super-nintendo-entertainment-system": "Super Nintendo Entertainment System"
    });
    assert.deepStrictEqual(Array.from(rows, r => r.name), [
        "Nintendo 64",
        "Sega Genesis / Mega Drive",
        "Super Nintendo Entertainment System",
        "Unidentified"
    ]);
    assert.deepStrictEqual(Array.from(rows, r => r.count), [1, 2, 2, 1]);
    assert.strictEqual(rows[0].kind, "console");
    assert.strictEqual(rows[3].kind, "unknown");
});

test("a console the catalog has no name for still shows something", () => {
    const roms = [rom({ console: "some-new-console" })];
    const rows = M.consoleRows(M.reindex(roms), {});
    assert.strictEqual(rows[0].name, "Some New Console");
});

test("unidentified roms are grouped by the file they came out of", () => {
    const roms = [
        rom({ name: "A", console: M.UNKNOWN, ext: "bin", source: "megaromset.zip",
              where: "megaromset.zip › A/A.bin" }),
        rom({ name: "B", console: M.UNKNOWN, ext: "bin", source: "megaromset.zip",
              where: "megaromset.zip › B/B.bin" }),
        rom({ name: "C", console: M.UNKNOWN, ext: "iso", source: "ps2/God Hand.7z",
              where: "ps2/God Hand.7z › God Hand.iso" })
    ];
    const groups = M.unknownGroups(roms, M.reindex(roms));
    assert.strictEqual(groups.length, 2);
    assert.strictEqual(groups[0].source, "megaromset.zip");
    assert.strictEqual(groups[0].count, 2);
    assert.strictEqual(groups[0].exts, ".bin");
    assert.strictEqual(groups[1].source, "ps2/God Hand.7z");
    assert.strictEqual(groups[1].exts, ".iso");
});

// ------------------------------------------------------------------ cores

test("the dropdown offers only the cores that can run that console", () => {
    const cores = [
        { id: "mupen64plusnext", label: "Mupen64Plus-Next", systemId: "nintendo-64" },
        { id: "paralleln64", label: "ParaLLEl N64", systemId: "nintendo-64" },
        { id: "genesisplusgx", label: "Genesis Plus GX", systemId: "sega-genesis-mega-drive" },
        { id: "orphan", label: "Orphan", systemId: "" }
    ];
    const options = M.coreOptions(cores, "nintendo-64");
    assert.deepStrictEqual(Array.from(options, o => o.value), ["mupen64plusnext", "paralleln64"]);
    assert.deepStrictEqual(Array.from(M.coreOptions(cores, "playstation-2")), []);
    assert.strictEqual(M.hasCore(cores, "mupen64plusnext"), true);
    assert.strictEqual(M.hasCore(cores, "nosuchcore"), false);
    assert.strictEqual(M.coreLabel(cores, "paralleln64"), "ParaLLEl N64");
});

// -------------------------------------------------------------- formatting

test("sizes read the way a person would say them", () => {
    assert.strictEqual(M.formatSize(0), "");
    assert.strictEqual(M.formatSize(900), "900 B");
    assert.strictEqual(M.formatSize(1536), "1.5 KB");
    assert.strictEqual(M.formatSize(5 * 1024 * 1024), "5.0 MB");
    assert.strictEqual(M.formatSize(1567686656), "1.5 GB");
});

test("play times are relative, in seconds, like the stored timestamps", () => {
    // Seconds, like the timestamps the scanner writes.
    const now = 1_700_000_000;
    const ago = seconds => now - seconds;
    assert.strictEqual(M.formatWhen(ago(5), now), "just now");
    assert.strictEqual(M.formatWhen(ago(600), now), "10m ago");
    assert.strictEqual(M.formatWhen(ago(7200), now), "2h ago");
    assert.strictEqual(M.formatWhen(ago(86400 * 3), now), "3d ago");
    assert.strictEqual(M.formatWhen(ago(86400 * 60), now), "2mo ago");
    assert.strictEqual(M.formatWhen(0, now), "");
});

console.log("\n" + (ran - failures) + "/" + ran + " passed");
process.exit(failures ? 1 : 0);
