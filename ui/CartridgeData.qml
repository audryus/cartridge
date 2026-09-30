// CartridgeData.qml
// The bridge between the QML UI and the three Python scripts. It owns the
// parsed state, keeps it in step with the JSON files on disk, and runs the
// scanner, the state mutator and the launcher.
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

    // ---- what the UI draws from
    property var roms: []
    property var index: ({ conflicts: {}, counts: {} })
    property var consoles: ({})
    property var catalog: ({})
    property var cores: []
    property string libraryRoot: ""
    property bool loaded: false
    property bool everScanned: false

    // ---- what the toolbar reports
    property bool scanning: false
    property string status: ""
    property string problem: ""
    property int lastScan: 0

    // Bumped whenever the list changes, so views know to rebuild.
    property int revision: 0

    signal played(string name, string core)

    // ------------------------------------------------------------------ load

    // Called every time the window opens, not once per session. cartridge-state.py
    // replaces the state files with a rename, which a file watcher sitting on
    // the old inode never sees, so re-reading on open is what makes the window
    // agree with what is on disk -- including a rescan started elsewhere.
    //
    // watchChanges is kept as well: it catches a plain rewrite immediately, and
    // when it misses, the next open is still correct.
    function ensureLoaded() {
        reload()
    }

    function reload() {
        romsView.reload()
        consolesView.reload()
        coresView.reload()
    }

    // decorate gives every rom the two things the UI needs that are cheaper to
    // work out here than in the file: `where` is the human path to the rom,
    // and `source` is the file or folder it came out of.
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
        return rom
    }

    function applyRoms(raw) {
        if (!raw) return
        let document
        try {
            document = JSON.parse(raw)
        } catch (error) {
            problem = "roms.json could not be read: " + error
            return
        }
        if (!document || !Array.isArray(document.roms)) return

        libraryRoot = document.root || ""
        const rows = document.roms
        for (let i = 0; i < rows.length; i++) decorate(rows[i])
        roms = rows
        index = Model.reindex(rows)
        everScanned = true
        loaded = true
        lastScan = document.generated || 0
        clearProblem()
        revision++
    }

    function applyConsoles(raw) {
        if (!raw) return
        let document
        try {
            document = JSON.parse(raw)
        } catch (error) {
            return
        }
        consoles = document.consoles || {}
        catalog = document.catalog || {}
        revision++
    }

    function applyCores(raw) {
        if (!raw) return
        let document
        try {
            document = JSON.parse(raw)
        } catch (error) {
            return
        }
        cores = Array.isArray(document.cores) ? document.cores : []
        revision++
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
    property FileView romsView: FileView {
        id: romsView
        path: store.romsFile
        blockLoading: false
        preload: false
        printErrors: false
        watchChanges: true
        onTextChanged: romsTimer.restart()
        onLoadFailed: store.setProblem("no library scanned yet - press refresh")
    }

    property Timer romsTimer: Timer {
        id: romsTimer
        interval: 60
        onTriggered: store.applyRoms(romsView.text())
    }

    property FileView consolesView: FileView {
        id: consolesView
        path: store.consolesFile
        blockLoading: false
        preload: false
        printErrors: false
        watchChanges: true
        onTextChanged: store.applyConsoles(consolesView.text())
        onLoadFailed: console.warn("[cartridge] no consoles.json yet")
    }

    property FileView coresView: FileView {
        id: coresView
        path: store.coresFile
        blockLoading: false
        preload: false
        printErrors: false
        watchChanges: true
        onTextChanged: store.applyCores(coresView.text())
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
        if (scanning)
            return
        scanning = true
        status = "scanning " + libraryRoot + "…"
        clearProblem()
        scanProc.running = true
    }

    function toggleFavorite(id) {
        const rom = findRom(id)
        if (!rom)
            return
        const next = !rom.favorite
        rom.favorite = next
        revision++
        run([binDir + "/cartridge-state.py", "favorite", id, next ? "1" : "0"], result => {
            if (result && result.ok === false) {
                rom.favorite = !next
                revision++
                setProblem(result.error)
            }
        })
    }

    function assignConsole(id, consoleId) {
        const rom = findRom(id)
        if (!rom)
            return
        const before = rom.console
        rom.console = consoleId
        rom.reason = consoleId === Model.UNKNOWN ? "unknown" : "assigned"
        index = Model.reindex(roms)
        revision++
        run([binDir + "/cartridge-state.py", "assign", id, consoleId], result => {
            if (result && result.ok === false) {
                rom.console = before
                index = Model.reindex(roms)
                revision++
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
            if (result && result.ok === false) {
                setProblem(result.error)
                return
            }
            const count = result ? result.assigned : 0
            status = count + " roms in " + source + " are now " +
                     (result ? result.name : consoleId)
            reload()
        })
    }

    function setCore(consoleId, coreId) {
        const entry = consoles[consoleId]
        if (!entry)
            return
        const before = entry.core
        entry.core = coreId
        revision++
        run([binDir + "/cartridge-state.py", "core", consoleId, coreId || "-"], result => {
            if (result && result.ok === false) {
                entry.core = before
                revision++
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
            rom.lastPlayed = Math.floor(Date.now() / 1000)
            revision++
            status = rom.name + " → " + result.core
            played(rom.name, result.core)
        })
    }

    // ----------------------------------------------------------------- reads

    function findRom(id) {
        for (let i = 0; i < roms.length; i++)
            if (roms[i].id === id)
                return roms[i]
        return null
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
        return Model.hasCore(cores, coreFor(consoleId))
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

    function rowsFor(consoleId, query) {
        return Model.romRows(roms, index, consoleId, query, isPlayable)
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
        stdout: StdioCollector {
            waitForEnd: true
            onStreamFinished: store.finishScan(String(text || ""))
        }
        stderr: SplitParser {
            onRead: data => console.warn("[cartridge]", data)
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
        reload()
        const bits = []
        bits.push(result.total + " roms")
        if (result.added) bits.push("+" + result.added + " new")
        if (result.removed) bits.push(result.removed + " gone")
        if (result.unknown) bits.push(result.unknown + " unidentified")
        status = bits.join(" · ")
        clearProblem()
    }
}
