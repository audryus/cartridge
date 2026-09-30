// shell.qml
// The Omarchy bar widget entry point for cartridge, and the two windows it
// opens: the library, and the config popup with one core dropdown per console.
//
// The windows are PanelWindows with an Overlay layer and exclusive keyboard
// focus, not PopupCard. A PopupCard is an xdg-popup, which under Hyprland
// hands the keyboard to whatever is under the cursor -- and the cursor is in
// the bar, where nothing has focus, so typing in a dropdown went nowhere. A
// panel pulls focus for itself, so the search box and every dropdown work
// wherever the mouse happens to be. Clicking outside closes, Escape closes.
import QtQuick
import QtQml.Models
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

BarWidget {
    id: root
    moduleName: "audryus.cartridge"

    implicitWidth: button.implicitWidth
    implicitHeight: button.implicitHeight

    readonly property bool opened: libraryOpen
    readonly property bool configuring: configOpen

    // The store lives in the plugin's service (Service.qml): one for the whole
    // shell, shared by the bar on every monitor, instead of one per bar each
    // parsing the same library. If the host offers no service -- a replacement
    // bar gets no service facade -- this widget falls back to a store of its
    // own, which stays inert while the shared one is there.
    readonly property var service: root.bar && root.bar.shell
        && typeof root.bar.shell.serviceFor === "function"
        ? root.bar.shell.serviceFor("audryus.cartridge") : null
    readonly property bool shared: !!(root.service && root.service.store)
    readonly property var store: root.shared ? root.service.store : fallbackStore

    CartridgeData {
        id: fallbackStore
        active: !root.shared
    }

    property var registeredWith: null

    function syncRegistration() {
        if (root.registeredWith === root.service)
            return
        if (root.registeredWith && typeof root.registeredWith.unregister === "function")
            root.registeredWith.unregister(root)
        root.registeredWith = root.service
        if (root.service && typeof root.service.register === "function")
            root.service.register(root)
    }

    onServiceChanged: root.syncRegistration()
    Component.onCompleted: root.syncRegistration()
    Component.onDestruction: {
        if (root.registeredWith && typeof root.registeredWith.unregister === "function")
            root.registeredWith.unregister(root)
    }

    function markUsed() {
        if (root.shared && typeof root.service.used === "function")
            root.service.used(root)
    }

    property bool libraryOpen: false
    property bool configOpen: false

    function open() {
        root.markUsed()
        root.store.ensureLoaded()
        // A bar widget exists once per monitor. Closing the other instances
        // first is what keeps one click from opening the library twice.
        root.broadcast("closeWindows")
        configOpen = false
        libraryOpen = true
    }

    function close() {
        libraryOpen = false
        configOpen = false
    }

    // Leaving the config window goes back to the library rather than closing
    // everything: picking a core is a detour from browsing, not an exit.
    function closeConfig() {
        configOpen = false
        libraryOpen = true
    }

    // The peer half of the broadcast above.
    function closeWindows() {
        libraryOpen = false
        configOpen = false
    }

    function toggle() {
        if (libraryOpen || configOpen)
            close()
        else
            open()
    }

    function openConfig() {
        root.markUsed()
        root.store.ensureLoaded()
        root.broadcast("closeWindows")
        libraryOpen = false
        configOpen = true
    }

    function refresh() {
        open()
        root.store.refresh()
    }

    // Only without the shared service: Service.qml owns the IPC target, once
    // for the whole shell. The service loads asynchronously, so this waits
    // before deciding it is not coming -- a handler created up front would
    // register the same target once per monitor and race the service's.
    property bool serviceMissing: false

    Timer {
        interval: 3000
        running: !root.shared && !root.serviceMissing
        onTriggered: root.serviceMissing = !root.shared
    }

    Instantiator {
        model: root.serviceMissing && !root.shared ? 1 : 0
        delegate: IpcHandler {
            target: "audryus.cartridge"

            function open(): void { root.open() }
            function close(): void { root.close() }
            function toggle(): void { root.toggle() }
            function config(): void { root.openConfig() }
            function refresh(): void { root.refresh() }
        }
    }

    BarIconButton {
        id: button
        bar: root.bar
        text: "\uec17"            // gamepad, outside the nf-fa range
        tooltipText: "Cartridge"
        onPressed: function(mouseButton) {
            if (mouseButton === Qt.RightButton)
                return
            root.toggle()
        }
    }

    // ------------------------------------------------------------------ card
    //
    // Both windows are the same shape: a transparent full-screen surface, a
    // click-anywhere-to-close layer, and the card itself placed under the bar
    // button and clamped inside the screen.

    function cardGeometry(panel, cardWidth, cardHeight) {
        const bar = root.bar
        const win = button.QsWindow.window
        const margin = Style.gapsOut
        const below = root.barSize + margin
        const above = panel.height - cardHeight - below
        let x = margin
        let y = below
        if (win && win.contentItem) {
            const point = button.mapToItem(win.contentItem, button.width / 2, 0)
            x = point.x - cardWidth / 2
        }
        if (bar && bar.position === "bottom") {
            y = panel.height - cardHeight - below
        } else if (bar && (bar.position === "left" || bar.position === "right")) {
            y = panel.height / 2 - cardHeight / 2
            x = bar.position === "left" ? below : panel.width - cardWidth - below
        }
        return {
            x: Math.max(margin, Math.min(x, panel.width - cardWidth - margin)),
            y: Math.max(margin, Math.min(y, panel.height - cardHeight - margin))
        }
    }

    onLibraryOpenChanged: root.syncPopout()
    onConfigOpenChanged: root.syncPopout()

    // Read the two flags directly. `opened` and `configuring` are bindings, and
    // a panel's onVisibleChanged can fire before they have caught up with the
    // change that caused it -- which is exactly how the bar's open-panel
    // indicator ended up stuck on after the window closed.
    function syncPopout() {
        if (!root.bar)
            return
        if (root.libraryOpen || root.configOpen)
            root.bar.requestPopout(root)
        else if (root.bar.activePopout === root)
            root.bar.releasePopout(root)
    }

    // --------------------------------------------------------------- library

    PanelWindow {
        id: libraryPanel
        visible: root.libraryOpen
        anchors { top: true; bottom: true; left: true; right: true }
        color: "transparent"
        WlrLayershell.namespace: "omarchy-cartridge-library"
        WlrLayershell.layer: WlrLayer.Overlay
        WlrLayershell.keyboardFocus: root.libraryOpen
            ? WlrKeyboardFocus.Exclusive : WlrKeyboardFocus.None
        exclusionMode: ExclusionMode.Ignore

        onVisibleChanged: if (visible) Qt.callLater(() => libraryContent.focusSearch())

        MouseArea {
            anchors.fill: parent
            onClicked: root.close()
        }

        BorderSurface {
            id: libraryCard
            width: Math.min(Style.space(880), libraryPanel.width - Style.gapsOut * 2)
            height: Math.min(Style.space(560), libraryPanel.height - Style.gapsOut * 2)
            x: root.cardGeometry(libraryPanel, width, height).x
            y: root.cardGeometry(libraryPanel, width, height).y
            color: Color.popups.background
            borderSpec: Border.localOrSurfaceSpec("popups", "border",
                                                  Color.popups.border, Color.popups.border,
                                                  Math.max(1, Style.space(2)))
            padding: Style.spacing.popupPadding
            radius: Style.cornerRadius

            // Swallow clicks so they do not reach the dismiss layer behind.
            MouseArea { anchors.fill: parent; onClicked: {} }

            // The catcher wraps the content on purpose. An Escape key that a
            // control inside the window does not handle travels up through its
            // parents, so this has to be one of them -- as a sibling it only
            // caught keys pressed while nothing else had the focus.
            Item {
                id: libraryKeys
                anchors.fill: parent
                focus: true
                Keys.onEscapePressed: function(event) {
                    root.close()
                    event.accepted = true
                }

                Cartridge {
                    id: libraryContent
                    anchors.fill: parent
                    anchors.topMargin: libraryCard.contentTopInset
                    anchors.rightMargin: libraryCard.contentRightInset
                    anchors.bottomMargin: libraryCard.contentBottomInset
                    anchors.leftMargin: libraryCard.contentLeftInset
                    store: root.store
                    onConfigRequested: root.openConfig()
                    onCloseRequested: root.close()
                }
            }
        }
    }

    // ---------------------------------------------------------------- config

    PanelWindow {
        id: configPanel
        visible: root.configOpen
        anchors { top: true; bottom: true; left: true; right: true }
        color: "transparent"
        WlrLayershell.namespace: "omarchy-cartridge-config"
        WlrLayershell.layer: WlrLayer.Overlay
        WlrLayershell.keyboardFocus: root.configOpen
            ? WlrKeyboardFocus.Exclusive : WlrKeyboardFocus.None
        exclusionMode: ExclusionMode.Ignore

        // Click outside, Escape and the close button all go back to the
        // library, because the config window is a detour from browsing.
        MouseArea {
            anchors.fill: parent
            onClicked: root.closeConfig()
        }

        BorderSurface {
            id: configCard
            width: Math.min(Style.space(520), configPanel.width - Style.gapsOut * 2)
            height: Math.min(Style.space(480), configPanel.height - Style.gapsOut * 2)
            x: root.cardGeometry(configPanel, width, height).x
            y: root.cardGeometry(configPanel, width, height).y
            color: Color.popups.background
            borderSpec: Border.localOrSurfaceSpec("popups", "border",
                                                  Color.popups.border, Color.popups.border,
                                                  Math.max(1, Style.space(2)))
            padding: Style.spacing.popupPadding
            radius: Style.cornerRadius

            MouseArea { anchors.fill: parent; onClicked: {} }

            // Same shape as the library window: a parent, not a sibling, so
            // Escape still arrives after a dropdown or a button has taken the
            // focus.
            Item {
                id: configKeys
                anchors.fill: parent
                focus: true
                Keys.onEscapePressed: function(event) {
                    root.closeConfig()
                    event.accepted = true
                }

                // Only built while the window is open: hidden, it would
                // still rebuild its rows every time the library changed.
                Loader {
                    id: configContent
                    anchors.fill: parent
                    anchors.topMargin: configCard.contentTopInset
                    anchors.rightMargin: configCard.contentRightInset
                    anchors.bottomMargin: configCard.contentBottomInset
                    anchors.leftMargin: configCard.contentLeftInset
                    active: root.configOpen
                    sourceComponent: Component {
                        ConfigPopup {
                            store: root.store
                            onBackRequested: root.closeConfig()
                        }
                    }
                }
            }
        }
    }

    // A game is starting: get out of the way so RetroArch is visible.
    Connections {
        target: root.store
        function onPlayed() { root.close() }
    }
}
