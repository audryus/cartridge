// CartridgeData.qml
// The bridge between the QML UI and the three Python scripts. It owns the
// parsed state, keeps it in step with the JSON files on disk, and runs the
// scanner, the state mutator and the launcher.
//
// There is one of these for the whole shell (see Service.qml), not one per
// monitor: roms.json is several megabytes, and parsing it once per bar was
// work thrown away.
//
// Every script answers with one JSON line on stdout, so nothing here has to
// guess what happened: a refused core, a missing file and a scan that added
// nine thousand roms all come back the same way.
import QtQuick
import Quickshell.Io

import "CartridgeModel.js" as Model

QtObject {
    id: store

    // ui/ lives inside the plugin, so the plugin root is this file's folder's
    // parent. Resolving it relatively is what lets the plugin work from a
    // symlink in ~/.config/omarchy/plugins.
    //
    // Qt.resolvedUrl("..") is swallowed by Quickshell's URL interceptor, so
    // resolve "." and walk up a level.
    readonly property string pluginRoot: {
        let s = Qt.resolvedUrl(".").toString()
        if (s.startsWith("file://"))
            s = decodeURIComponent(s.slice("file://".length))
        if (s.endsWith("/"))
            s = s.slice(0, -1)
        const cut = s.lastIndexOf("/")
        return cut > 0 ? s.slice(0, cut) : s
    }

    readonly property string stateDir: pluginRoot + "/state"
    readonly property string binDir: pluginRoot + "/bin"
    readonly property string romsFile: stateDir + "/roms.json"
    readonly property string consolesFile: stateDir + "/consoles.json"
    readonly property string coresFile: stateDir + "/cores.json"
    readonly property string userFile: stateDir + "/user.json"

    // False for the fallback store a bar widget keeps in case the shared
    // service is unavailable: an inactive store reads nothing and runs nothing.
    property bool active: true

    // ---- what the UI draws from
    property var roms: []
    property var index: ({ conflicts: {}, counts: {} })
    property var consoles: ({})
    property var catalog: ({})
    property var cores: []
    property var user: ({ favorites: {}, lastPlayed: {}, assigned: {} })
    property string libraryRoot: ""
    property bool loaded: false
    property bool everScanned: false

    // Derived once when their inputs change, not once per row that asks.
    readonly property var playable: Model.playableMap(consoles, cores)
    readonly property var catalogOptions: Model.catalogOptions(catalog)

    // ---- what the toolbar reports
    property bool scanning: false
    property string status: ""
    property string problem: ""
    property int lastScan: 0
    // Which scan roms.json came from. consoles.json names the latest one, so
    // an open can tell whether the big file needs reading again at all.
    property string romsStamp: ""

    // Bumped whenever the list changes, so views know to rebuild. Several
    // changes in one go -- three files arriving together -- are one bump.
    property int revision: 0
    property var byId: ({})
    property var rowCache: ({})

    signal played(string name, string core)

    function bump() {
        Qt.callLater(store.bumpNow)
    }

    function bumpNow() {
        rowCache = ({})
        revision++
    }

    // ------------------------------------------------------------------ load

    // The library lives in memory only while a window shows it. The service
    // outlives every window -- it is mounted with the shell -- and the parsed
    // roms, plus the text they were parsed from, are tens of megabytes of heap
    // that nothing reads while the windows are closed.
    //
    // Each open window holds the store (acquire), and closing it lets go
    // (release). Once nothing has held it for unloadDelay the roms are
    // dropped; the next open reads roms.json again.
    property int holds: 0
    readonly property int unloadDelay: 60000

    function acquire() {
        holds++
        unloadTimer.stop()
        reload()
    }

    function release() {
        holds = Math.max(0, holds - 1)
        if (holds === 0)
            unloadTimer.restart()
    }

    property Timer unloadTimer: Timer {
        id: unloadTimer
        interval: store.unloadDelay
        onTriggered: store.unloadRoms()
    }

    function unloadRoms() {
        if (holds > 0 || !loaded)
            return
        roms = []
        byId = ({})
        index = ({ conflicts: {}, counts: {} })
        loaded = false
        romsStamp = ""
        bump()
        // The roms are garbage now; collect them while nobody is looking
        // rather than whenever the engine next feels like it.
        Qt.callLater(gc)
    }

    // Called every time a window opens. cartridge-state.py replaces the state
    // files with a rename, which a file watcher sitting on the old inode may
    // not see, so re-reading on open is what makes the window agree with what
    // is on disk -- including a rescan started elsewhere.
    //
    // Only the small files are read every time. roms.json is read when
    // consoles.json says a scan has replaced it (applyConsoles), or when it
    // is not in memory.
    //
    // FileView's watchChanges only emits fileChanged; each view below turns
    // that into a reload, and applies what it read on `loaded`.
    function ensureLoaded() {
        reload()
    }

    function reload() {
        if (!active)
            return
        if (!loaded)
            readRoms()
        userView.reload()
        consolesView.reload()
        coresView.reload()
    }

    function reloadAll() {
        if (!active)
            return
        readRoms()
        reload()
    }

    // roms.json is read by a FileView made for the one read and destroyed
    // right after, so the 3.5 MB of text it holds goes with it instead of
    // sitting next to the parsed roms for as long as the shell runs. Nothing
    // watches it: every scan rewrites consoles.json too, whose romsStamp says
    // when the library needs reading again.
    property var romsReader: null
    property bool romsAgain: false

    function readRoms() {
        if (!active || holds === 0)
            return
        if (romsReader) {
            romsAgain = true
            return
        }
        romsReader = romsReaderComponent.createObject(store, { path: romsFile })
    }

    function romsRead(reader, raw) {
        reader.destroy()
        romsReader = null
        if (romsAgain) {
            romsAgain = false
            readRoms()
            return
        }
        // Closed while it was reading: nobody to show it to.
        if (holds > 0 && raw !== null)
            applyRoms(raw)
        else if (raw === null)
            setProblem("no library scanned yet - press refresh")
    }

    property Component romsReaderComponent: Component {
        FileView {
            id: reader
            blockLoading: false
            printErrors: false
            onLoaded: store.romsRead(reader, reader.text())
            onLoadFailed: store.romsRead(reader, null)
        }
    }

    // decorate gives every rom what the UI needs that is cheaper to work out
    // once here than in the file or on every keystroke: `where` is the human
    // path to the rom, `source` the file or folder it came out of, `needle`
    // the lower-cased text a search looks in, and `detected` the scanner's
    // answer, kept so user.json can be applied over it and taken back.
    function decorate(rom) {
        const root = libraryRoot
        let source = rom.path
        if (root && source.startsWith(root))
            source = source.slice(root.length).replace(/^\//, "")
        if (!source)
            source = "(no library)"
        rom.source = source
        // `source` is relative, for showing. The scripts compare against the
        // absolute path a rom record carries, so keep that too.
        rom.sourcePath = rom.path
        rom.where = rom.depth > 0
            ? source + " › " + (rom.parent ? rom.parent + " › " : "") + rom.entry
            : source
        rom.needle = Model.needleOf(rom)
        rom.detected = { console: rom.console, reason: rom.reason }
        Model.applyUser(rom, user)
        return rom
    }

    function parse(raw, what) {
        if (!raw)
            return null
        try {
            return JSON.parse(raw)
        } catch (error) {
            if (what)
                problem = what + " could not be read: " + error
            return null
        }
    }

    function applyRoms(raw) {
        const document = parse(raw, "roms.json")
        if (!document || !Array.isArray(document.roms)) return

        libraryRoot = document.root || ""
        const rows = []
        const ids = {}
        for (let i = 0; i < document.roms.length; i++) {
            const rom = document.roms[i]
            // A duplicate the scanner hid is only on disk so it can come back
            // when the copy shown goes away; the UI never draws it.
            if (rom.hidden) continue
            rows.push(decorate(rom))
            ids[rom.id] = rom
        }
        roms = rows
        byId = ids
        index = Model.reindex(rows)
        everScanned = true
        loaded = true
        lastScan = document.generated || 0
        romsStamp = document.stamp || ""
        clearProblem()
        bump()
    }

    function applyUser(raw) {
        const document = parse(raw, "")
        if (!document) return
        user = {
            favorites: document.favorites || {},
            lastPlayed: document.lastPlayed || {},
            assigned: document.assigned || {}
        }
        // The same file arrives after every change this window made itself;
        // only redraw when it says something the roms do not already show.
        let changed = false
        let moved = false
        for (let i = 0; i < roms.length; i++) {
            const rom = roms[i]
            const before = rom.favorite + "|" + rom.lastPlayed + "|" + rom.console
            const was = rom.console
            Model.applyUser(rom, user)
            if (before !== rom.favorite + "|" + rom.lastPlayed + "|" + rom.console)
                changed = true
            if (was !== rom.console)
                moved = true
        }
        if (moved)
            index = Model.reindex(roms)
        if (changed)
            bump()
    }

    function applyConsoles(raw) {
        const document = parse(raw, "")
        if (!document) return
        consoles = document.consoles || {}
        catalog = document.catalog || {}
        if (loaded && document.romsStamp && document.romsStamp !== romsStamp)
            readRoms()
        bump()
    }

    function applyCores(raw) {
        const document = parse(raw, "")
        if (!document) return
        cores = Array.isArray(document.cores) ? document.cores : []
        bump()
    }

    function clearProblem() {
        if (problem)
            problem = ""
    }

    function setProblem(text) {
        problem = text
        status = ""
    }

    // ----------------------------------------------------------------- views

    // Declared as properties, not as bare children: a QtObject has no default
    // property, so a child object with no home is a load error.
    property FileView userView: FileView {
        id: userView
        path: store.active ? store.userFile : ""
        blockLoading: false
        preload: store.active
        printErrors: false
        watchChanges: true
        onFileChanged: reload()
        onLoaded: store.applyUser(userView.text())
    }

    property FileView consolesView: FileView {
        id: consolesView
        path: store.active ? store.consolesFile : ""
        blockLoading: false
        preload: store.active
        printErrors: false
        watchChanges: true
        onFileChanged: reload()
        onLoaded: store.applyConsoles(consolesView.text())
        onLoadFailed: console.warn("[cartridge] no consoles.json yet")
    }

    property FileView coresView: FileView {
        id: coresView
        path: store.active ? store.coresFile : ""
        blockLoading: false
        preload: store.active
        printErrors: false
        watchChanges: true
        onFileChanged: reload()
        onLoaded: store.applyCores(coresView.text())
        onLoadFailed: console.warn("[cartridge] no cores.json yet")
    }

    // ----------------------------------------------------------------- queue
    //
    // One process at a time. Favouriting while a scan runs is rare, but two
    // cartridge-state.py runs rewriting the same file at once is not something
    // the UI should allow.

    property var queue: []

    function run(argv, onDone) {
        queue.push({ argv: argv, onDone: onDone })
        pump()
    }

    function pump() {
        if (actionProc.running || queue.length === 0)
            return
        const job = queue.shift()
        actionProc.job = job
        actionProc.output = ""
        actionProc.running = true
    }

    function finish(job, raw) {
        if (!job) return
        let result = null
        const text = String(raw || "").trim()
        if (text.length > 0) {
            const lines = text.split("\n")
            try {
                result = JSON.parse(lines[lines.length - 1])
            } catch (error) {
                result = null
            }
        }
        if (job.onDone)
            job.onDone(result)
        pump()
    }

    property bool busy: actionProc.running || scanning

    property Process actionProc: Process {
        id: actionProc
        property var job: null
        property string output: ""
        command: job ? job.argv : []
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: actionProc.output = String(text || "")
        }
        onExited: (exitCode, exitStatus) => {
            const job = actionProc.job
            const raw = actionProc.output
            actionProc.job = null
            actionProc.output = ""
            store.finish(job, raw)
        }
        stderr: SplitParser {
            onRead: data => console.warn("[cartridge]", data)
        }
    }

    // --------------------------------------------------------------- actions

    function refresh() {
        if (scanning || !active)
            return
        scanning = true
        status = "scanning " + libraryRoot + "…"
        clearProblem()
        scanProc.running = true
    }

    // Only cores.json, not the library: a core installed or removed since the
    // last scan, picked up without reading every archive again.
    //
    // coresReport is the answer, for the config window, which does not show
    // the toolbar's status line.
    property bool rescanningCores: false
    property string coresReport: ""

    function rescanCores() {
        if (scanning || rescanningCores || !active)
            return
        rescanningCores = true
        coresReport = "looking for installed cores…"
        run([binDir + "/cartridge-state.py", "cores"], result => {
            rescanningCores = false
            if (!result || result.ok === false) {
                coresReport = result ? result.error : "cartridge-state.py said nothing"
                return
            }
            coresView.reload()
            const bits = [result.cores + " cores installed"]
            if (result.added.length) bits.push("new: " + result.added.join(", "))
            if (result.removed.length) bits.push("gone: " + result.removed.join(", "))
            if (!result.added.length && !result.removed.length) bits.push("nothing changed")
            coresReport = bits.join(" · ")
        })
    }

    function toggleFavorite(id) {
        const rom = findRom(id)
        if (!rom)
            return
        const next = !rom.favorite
        setFavorite(rom, next)
        run([binDir + "/cartridge-state.py", "favorite", id, next ? "1" : "0"], result => {
            if (result && result.ok === false) {
                setFavorite(rom, !next)
                setProblem(result.error)
            }
        })
    }

    function setFavorite(rom, value) {
        if (value)
            user.favorites[rom.id] = true
        else
            delete user.favorites[rom.id]
        rom.favorite = value
        bump()
    }

    function setAssigned(rom, consoleId) {
        if (consoleId)
            user.assigned[rom.id] = consoleId
        else
            delete user.assigned[rom.id]
        Model.applyUser(rom, user)
        index = Model.reindex(roms)
        bump()
    }

    function assignConsole(id, consoleId) {
        const rom = findRom(id)
        if (!rom)
            return
        const before = user.assigned[id] || ""
        setAssigned(rom, consoleId === "auto" ? "" : consoleId)
        run([binDir + "/cartridge-state.py", "assign", id, consoleId], result => {
            if (result && result.ok === false) {
                setAssigned(rom, before)
                setProblem(result.error)
            }
        })
    }

    // One archive at a time, never the whole library: assigning a console to
    // 5,000 roms by accident is worse than not having the button.
    function assignContainer(source, consoleId) {
        if (scanning)
            return
        status = "assigning " + consoleId + " to " + source + "…"
        run([binDir + "/cartridge-state.py", "container", source, consoleId], result => {
            if (!result || result.ok === false) {
                setProblem(result ? result.error : "cartridge-state.py said nothing")
                return
            }
            status = result.assigned + " roms in " + source + " are now " + result.name
            // Only user.json changed; the library itself did not.
            userView.reload()
            consolesView.reload()
        })
    }

    function setCore(consoleId, coreId) {
        const entry = consoles[consoleId]
        if (!entry)
            return
        const before = entry.core
        entry.core = coreId
        // A new object, so `playable` -- a binding on `consoles` -- follows.
        consoles = Object.assign({}, consoles)
        bump()
        run([binDir + "/cartridge-state.py", "core", consoleId, coreId || "-"], result => {
            if (result && result.ok === false) {
                entry.core = before
                consoles = Object.assign({}, consoles)
                bump()
                setProblem(result.error)
            }
        })
    }

    function play(id) {
        const rom = findRom(id)
        if (!rom)
            return
        if (scanning) {
            setProblem("wait for the scan to finish")
            return
        }
        status = "starting " + rom.name + "…"
        run([binDir + "/cartridge-play.py", pluginRoot, id], result => {
            if (!result || result.ok === false) {
                setProblem(result ? result.error : "cartridge-play.py said nothing")
                return
            }
            const now = Math.floor(Date.now() / 1000)
            user.lastPlayed[rom.id] = now
            rom.lastPlayed = now
            bump()
            status = rom.name + " → " + result.core
            played(rom.name, result.core)
        })
    }

    // ----------------------------------------------------------------- reads

    function findRom(id) {
        return byId[id] || null
    }

    function coreFor(consoleId) {
        const entry = consoles[consoleId]
        return entry ? (entry.core || "") : ""
    }

    function coreName(consoleId) {
        const id = coreFor(consoleId)
        if (!id)
            return ""
        for (let i = 0; i < cores.length; i++)
            if (cores[i].id === id)
                return cores[i].label
        return ""
    }

    // A rom can only be played once its console has a core that is still
    // installed. Everything else is a clear reason, not a disabled button.
    function isPlayable(consoleId) {
        return playable[consoleId] === true
    }

    function playableReason(consoleId) {
        const entry = consoles[consoleId]
        const name = entry ? entry.name : consoleId
        if (!coreFor(consoleId))
            return "no core set for " + name
        if (!isPlayable(consoleId))
            return "core " + coreName(consoleId) + " is not installed"
        return ""
    }

    function consoleRows() {
        return Model.consoleRows(index, catalog)
    }

    // The sorted rows of one console are built once per revision; a search
    // only filters them.
    function rowsFor(consoleId, query) {
        let base = rowCache[consoleId]
        if (!base) {
            base = Model.consoleRomRows(roms, index, consoleId, isPlayable)
            rowCache[consoleId] = base
        }
        return Model.filterRows(base, query)
    }

    function unknownGroups() {
        return Model.unknownGroups(roms, index)
    }

    function coreOptions(consoleId) {
        return Model.coreOptions(cores, consoleId)
    }

    function totals() {
        let favorites = 0
        for (let i = 0; i < roms.length; i++)
            if (roms[i].favorite)
                favorites++
        return { total: roms.length, favorites: favorites,
                 consoles: consoleRows().filter(c => c.kind === "console").length }
    }

    // ----------------------------------------------------------- scan process

    property Process scanProc: Process {
        id: scanProc
        command: [store.binDir + "/cartridge-scan.py"]
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: store.finishScan(String(text || ""))
        }
        stderr: SplitParser {
            // The scanner narrates its progress on stderr: information, not
            // a warning.
            onRead: data => console.info("[cartridge]", data)
        }
        // A process that cannot start still reports an exit, so a missing or
        // non-executable script surfaces as "the scan did not report back"
        // rather than a button that does nothing.
    }

    function finishScan(raw) {
        scanning = false
        let result = null
        const text = String(raw || "").trim()
        if (text.length > 0) {
            const lines = text.split("\n")
            try {
                result = JSON.parse(lines[lines.length - 1])
            } catch (error) {
                result = null
            }
        }
        if (!result) {
            setProblem("the scan did not report back")
            return
        }
        reloadAll()
        const bits = []
        bits.push(result.total + " roms")
        if (result.added) bits.push("+" + result.added + " new")
        if (result.removed) bits.push(result.removed + " gone")
        if (result.unknown) bits.push(result.unknown + " unidentified")
        status = bits.join(" · ")
        clearProblem()
    }
}
