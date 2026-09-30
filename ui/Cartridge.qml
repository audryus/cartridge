// Cartridge.qml
// The content of the library popup: toolbar on top, a rule, then the two
// columns with a rule between them.
import QtQuick
import qs.Commons
import qs.Ui

Item {
    id: root

    required property var store
    signal configRequested()
    signal closeRequested()

    // An ancestor of both columns, so Escape reaches this even when a control
    // inside them has the focus and does not handle it itself.
    Keys.onEscapePressed: function(event) {
        root.closeRequested()
        event.accepted = true
    }

    // The console the right column is showing. Starts on whatever the library
    // holds most of, and falls back to the first thing there is.
    property string selected: ""

    // The window hands focus here when it opens, so typing goes into the search
    // box without the mouse having to be anywhere in particular.
    function focusSearch() {
        if (roms)
            roms.focusSearch()
    }

    function pickFirstConsole() {
        if (root.selected.length > 0)
            return
        const rows = root.store.consoleRows()
        if (rows.length === 0)
            return
        const first = rows[0]
        root.selected = first.id
    }

    onSelectedChanged: if (selected.length > 0) roms.clearSearch()

    Connections {
        target: root.store
        function onRevisionChanged() { root.pickFirstConsole() }
    }

    Component.onCompleted: {
        root.store.ensureLoaded()
        root.pickFirstConsole()
    }

    Column {
        id: column
        anchors.fill: parent
        spacing: Style.spacing.md

        Toolbar {
            id: toolbar
            width: column.width
            store: root.store
            onConfigRequested: root.configRequested()
        }

        PanelSeparator {
            id: separator
            width: column.width
            strength: 0.18
        }

        // The two columns and the rule between them.
        Item {
            id: body
            width: column.width
            height: column.height - toolbar.height - separator.height - Style.spacing.md * 2

            ConsoleColumn {
                id: consoles
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: Style.space(232)
                store: root.store
                selected: root.selected
                onPicked: id => { root.selected = id }
            }

            Rectangle {
                id: divider
                anchors.left: consoles.right
                anchors.leftMargin: Style.spacing.md
                anchors.verticalCenter: parent.verticalCenter
                width: 1
                height: parent.height
                color: Util.alpha(Color.foreground, 0.14)
            }

            RomColumn {
                id: roms
                anchors.left: divider.right
                anchors.leftMargin: Style.spacing.md
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                store: root.store
                consoleId: root.selected
                onCloseRequested: root.closeRequested()
            }
        }
    }
}
