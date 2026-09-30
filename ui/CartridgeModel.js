// CartridgeModel.js
//
// Pure logic behind the cartridge UI, kept out of the QML so it can be
// reasoned about and tested on its own. No state, no side effects: every
// function takes the rom list and returns what to draw.

// The two pseudo-consoles the left column shows after the real ones.
var UNKNOWN = "unknown";
var CONFLICT = "conflict";

// Two roms with the same name in the same console are only a conflict when
// they are not the same bytes. A game you own twice -- once loose, once inside
// a rom set -- is a duplicate, not something to choose between. Sizes and CRCs
// come from the zip central directory, or the size alone for formats that do
// not carry a CRC.
function sameContent(a, b) {
    if (a.size !== b.size) return false;
    if (a.crc && b.crc) return a.crc === b.crc;
    return true;
}

// {conflicts: {id: true}, counts: {consoleId: count}} for the whole library.
function reindex(roms) {
    var groups = {};
    var i, rom, key;
    for (i = 0; i < roms.length; i++) {
        rom = roms[i];
        if (rom.hidden) continue;
        key = rom.console + "|" + rom.name.toLowerCase();
        (groups[key] = groups[key] || []).push(rom);
    }

    var conflicts = {};
    var counts = {};
    var inConflict = {};
    for (key in groups) {
        var copies = groups[key];
        var real = true;
        for (var j = 1; j < copies.length; j++) {
            if (!sameContent(copies[0], copies[j])) {
                real = false;
                break;
            }
        }
        if (!real) {
            for (i = 0; i < copies.length; i++) {
                conflicts[copies[i].id] = true;
                inConflict[copies[i].id] = true;
            }
        }
    }

    for (i = 0; i < roms.length; i++) {
        rom = roms[i];
        if (rom.hidden) continue;
        if (rom.console === UNKNOWN) {
            counts[UNKNOWN] = (counts[UNKNOWN] || 0) + 1;
            continue;
        }
        if (inConflict[rom.id]) {
            counts[CONFLICT] = (counts[CONFLICT] || 0) + 1;
            continue;
        }
        counts[rom.console] = (counts[rom.console] || 0) + 1;
    }

    return { conflicts: conflicts, counts: counts };
}

// Favorites first, then the most recently played, then alphabetical.
function compareRoms(a, b) {
    if (a.favorite !== b.favorite) return a.favorite ? -1 : 1;
    if (a.lastPlayed !== b.lastPlayed) return b.lastPlayed - a.lastPlayed;
    return String(a.name).localeCompare(String(b.name), undefined,
                                      { sensitivity: "base", numeric: true });
}

// Lower-cased name and path, the text a search looks in. Worked out once per
// rom when the library loads, not once per rom per keystroke.
function needleOf(rom) {
    return (String(rom.name) + "\n" + String(rom.where)).toLowerCase();
}

function matches(rom, query) {
    if (!query) return true;
    // The path matters in the unidentified column, where 5,000 roms are called
    // the same thing and only the archive they came from tells them apart.
    return (rom.needle || needleOf(rom)).indexOf(query.toLowerCase()) !== -1;
}

// Every rom of one console, as rows, sorted. The expensive part -- the walk
// over the whole library and the locale-aware sort -- depends only on the
// library and the console, so the store keeps this and a keystroke only
// filters it (see filterRows).
//
// Every row has the same keys, because a QML model takes its roles from the
// first element.
function consoleRomRows(roms, index, consoleId, isPlayable) {
    var rows = [];
    var wantsConflicts = consoleId === CONFLICT;
    var playable = {};
    for (var i = 0; i < roms.length; i++) {
        var rom = roms[i];
        if (rom.hidden) continue;
        var conflicted = index.conflicts[rom.id] === true;
        if (consoleId === UNKNOWN) {
            if (rom.console !== UNKNOWN) continue;
        } else if (wantsConflicts) {
            if (!conflicted || rom.console === UNKNOWN) continue;
        } else {
            if (rom.console !== consoleId || conflicted) continue;
        }
        if (!(rom.console in playable))
            playable[rom.console] = isPlayable(rom.console) === true;
        rows.push({
            id: rom.id,
            name: rom.name,
            console: rom.console,
            where: rom.where,
            ext: rom.ext,
            depth: rom.depth,
            kind: rom.kind,
            size: rom.size,
            favorite: rom.favorite === true,
            lastPlayed: rom.lastPlayed || 0,
            playable: playable[rom.console],
            conflicted: conflicted,
            why: rom.reason,
            needle: rom.needle || needleOf(rom)
        });
    }
    rows.sort(compareRoms);
    return rows;
}

// The rows that match a search, in the order they were already sorted in.
function filterRows(rows, query) {
    if (!query) return rows;
    var needle = query.toLowerCase();
    var out = [];
    for (var i = 0; i < rows.length; i++)
        if (rows[i].needle.indexOf(needle) !== -1) out.push(rows[i]);
    return out;
}

// The rows the right column draws.
function romRows(roms, index, consoleId, query, isPlayable) {
    return filterRows(consoleRomRows(roms, index, consoleId, isPlayable), query);
}

// Which consoles can be played right now: {consoleId: true} for every one
// whose chosen core is installed. Worked out when cores or consoles change,
// not once per row.
function playableMap(consoles, cores) {
    var installed = {};
    for (var i = 0; i < cores.length; i++) installed[cores[i].id] = true;
    var out = {};
    for (var id in consoles) {
        var entry = consoles[id];
        if (entry && entry.core && installed[entry.core]) out[id] = true;
    }
    return out;
}

// The catalog as dropdown options, sorted by name.
function catalogOptions(catalog) {
    var options = [];
    for (var key in catalog) options.push({ value: key, label: catalog[key] });
    options.sort(function (a, b) { return a.label.localeCompare(b.label); });
    return options;
}

// Apply what the user decided (user.json) on top of what the scanner
// detected. `rom.detected` keeps the scanner's answer so this can be applied
// again, and undone, without reading roms.json again.
function applyUser(rom, user) {
    var favorites = (user && user.favorites) || {};
    var played = (user && user.lastPlayed) || {};
    var assigned = (user && user.assigned) || {};
    rom.favorite = favorites[rom.id] === true;
    rom.lastPlayed = Number(played[rom.id]) || 0;
    var pick = assigned[rom.id];
    if (pick) {
        rom.console = pick;
        rom.reason = pick === UNKNOWN ? "unknown" : "assigned";
    } else {
        rom.console = rom.detected.console || UNKNOWN;
        rom.reason = rom.detected.reason;
    }
}

function prettify(id) {
    return String(id || "").split("-").map(function (word) {
        return word ? word.charAt(0).toUpperCase() + word.slice(1) : word;
    }).join(" ");
}

// The left column: real consoles by name, then the two odd ones out.
function consoleRows(index, catalog) {
    var rows = [];
    for (var id in index.counts) {
        if (id === UNKNOWN || id === CONFLICT) continue;
        rows.push({ id: id, name: catalog[id] || prettify(id), count: index.counts[id], kind: "console" });
    }
    rows.sort(function (a, b) {
        return a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
    });
    if (index.counts[CONFLICT])
        rows.push({ id: CONFLICT, name: "Conflicts", count: index.counts[CONFLICT], kind: "conflict" });
    if (index.counts[UNKNOWN])
        rows.push({ id: UNKNOWN, name: "Unidentified", count: index.counts[UNKNOWN], kind: "unknown" });
    return rows;
}

// Which files the unidentified roms came out of, biggest first. A rom set of
// 5,000 unidentified .bin files is not something to identify one at a time, so
// the whole file is offered at once.
//
// Grouped by `source` -- the archive or folder -- and not by `where`, which is
// the full path to a single rom and unique to it.
function unknownGroups(roms, index) {
    var groups = {};
    for (var i = 0; i < roms.length; i++) {
        var rom = roms[i];
        if (rom.hidden || rom.console !== UNKNOWN) continue;
        if (!groups[rom.source])
            groups[rom.source] = { source: rom.source, path: rom.sourcePath || rom.path,
                                   count: 0, exts: {} };
        groups[rom.source].count++;
        groups[rom.source].exts[rom.ext] = true;
    }
    var out = [];
    for (var source in groups) {
        var group = groups[source];
        var exts = [];
        for (var ext in group.exts) exts.push("." + ext);
        exts.sort();
        out.push({ source: group.source, path: group.path, count: group.count,
                   exts: exts.join(" ") });
    }
    out.sort(function (a, b) {
        if (a.count !== b.count) return b.count - a.count;
        return a.source.localeCompare(b.source);
    });
    return out;
}

// Cores that can run a console: the ones whose database entry is for that
// system, and that are actually installed.
function coreOptions(cores, consoleId) {
    var options = [];
    for (var i = 0; i < cores.length; i++) {
        if (cores[i].systemId === consoleId)
            options.push({ value: cores[i].id, label: cores[i].label });
    }
    options.sort(function (a, b) {
        return a.label.localeCompare(b.label, undefined, { sensitivity: "base" });
    });
    return options;
}

function hasCore(cores, coreId) {
    for (var i = 0; i < cores.length; i++)
        if (cores[i].id === coreId) return true;
    return false;
}

function coreLabel(cores, coreId) {
    for (var i = 0; i < cores.length; i++)
        if (cores[i].id === coreId) return cores[i].label;
    return "";
}

function formatSize(bytes) {
    var value = Number(bytes) || 0;
    if (value <= 0) return "";
    var units = ["B", "KB", "MB", "GB"];
    var unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
        value /= 1024;
        unit++;
    }
    return (unit === 0 ? value : value.toFixed(value < 10 ? 1 : 0)) + " " + units[unit];
}

// "played 3 days ago", because "most recent" is a sort order nobody can check
// without a date next to it.
//
// Both arguments are Unix timestamps in seconds, the same unit the scanner
// stores. Milliseconds in one and seconds in the other reads as 1970.
function formatWhen(epoch, now) {
    // Never played: no time to report, rather than a date in 1970.
    if (!epoch) return "";
    var seconds = Math.floor(now - Number(epoch));
    if (seconds < 60) return "just now";
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + "m ago";
    var hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + "h ago";
    var days = Math.floor(hours / 24);
    if (days < 30) return days + "d ago";
    var months = Math.floor(days / 30);
    if (months < 12) return months + "mo ago";
    return Math.floor(months / 12) + "y ago";
}
