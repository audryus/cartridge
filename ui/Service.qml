// Service.qml
// The one process-wide half of cartridge. The shell mounts this once, however
// many monitors -- and so bar widgets -- there are, so the library is read,
// parsed and watched once, and a scan started from one bar is seen by all.
//
// It also owns the IPC target. With one IpcHandler per bar widget the same
// target was registered once per monitor; here it is registered once, and a
// call goes to the bar the library was last opened from.
import QtQuick
import Quickshell.Io

Item {
    id: service

    // Injected by the host.
    property var shell: null
    property var manifest: null

    readonly property var store: cartridgeStore

    CartridgeData {
        id: cartridgeStore
    }

    // ---- bar widgets

    property var widgets: []
    property var lastUsed: null

    function register(widget) {
        if (widget && widgets.indexOf(widget) === -1)
            widgets = widgets.concat([widget])
    }

    function unregister(widget) {
        widgets = widgets.filter(item => item !== widget)
        if (lastUsed === widget)
            lastUsed = null
    }

    function used(widget) {
        lastUsed = widget
    }

    function target() {
        if (lastUsed && widgets.indexOf(lastUsed) !== -1)
            return lastUsed
        return widgets.length > 0 ? widgets[0] : null
    }

    function call(method) {
        const widget = target()
        if (widget && typeof widget[method] === "function")
            widget[method]()
    }

    // Bind a key to these, e.g.
    //   omarchy-shell audryus.cartridge open
    IpcHandler {
        target: "audryus.cartridge"

        function open(): void { service.call("open") }
        function close(): void { service.call("close") }
        function toggle(): void { service.call("toggle") }
        function config(): void { service.call("openConfig") }
        function refresh(): void { service.call("refresh") }
        function status(): string {
            return JSON.stringify({
                scanning: cartridgeStore.scanning,
                roms: cartridgeStore.roms.length,
                problem: cartridgeStore.problem !== ""
            })
        }
    }
}
