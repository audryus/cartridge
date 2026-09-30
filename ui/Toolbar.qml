// Toolbar.qml
// The top strip of the cartridge popup: the two buttons, and a line that says
// what the library looks like right now.
import QtQuick
import qs.Commons
import qs.Ui

Item {
    id: root

    required property var store

    // The config popup lives in the shell, next to this one; the shell wires it
    // to whatever this toolbar asks for.
    signal configRequested()

    readonly property int rowHeight: Style.spacing.controlHeight
    readonly property int gap: Style.spacing.md

    readonly property string summary: {
        if (!root.store.loaded)
            return "no library scanned yet"
        const totals = root.store.totals()
        return totals.total + " rom" + (totals.total === 1 ? "" : "s") +
               " · " + totals.consoles + " console" + (totals.consoles === 1 ? "" : "s") +
               " · " + totals.favorites + " favorite" + (totals.favorites === 1 ? "" : "s")
    }

    // One line, most important thing first: an error the scripts reported, then
    // the result of the last scan, then what the library holds.
    readonly property string report: {
        if (root.store.problem.length > 0)
            return root.store.problem
        if (root.store.status.length > 0)
            return root.store.status
        return root.summary
    }

    readonly property bool problem: root.store.problem.length > 0

    implicitHeight: rowHeight

    Button {
        id: configButton
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
        height: root.rowHeight
        text: "Config"
        iconText: "\uF013"        // nf-fa-cog
        bordered: true
        onClicked: root.configRequested()
    }

    Button {
        id: refreshButton
        anchors.left: configButton.right
        anchors.leftMargin: root.gap
        anchors.verticalCenter: parent.verticalCenter
        height: root.rowHeight
        text: root.store.scanning ? "Scanning…" : "Refresh"
        iconText: "\uF021"        // nf-fa-repeat
        bordered: true
        active: root.store.scanning
        onClicked: root.store.refresh()
    }

    Text {
        anchors.left: refreshButton.right
        anchors.leftMargin: Style.spacing.lg
        anchors.right: parent.right
        anchors.rightMargin: Style.spacing.xs
        anchors.verticalCenter: parent.verticalCenter
        text: root.store.scanning
            ? "reading " + root.store.libraryRoot + "…"
            : root.report
        visible: text.length > 0
        color: root.problem ? Color.urgent : Util.alpha(Color.foreground, 0.62)
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
        horizontalAlignment: Text.AlignRight
        elide: Text.ElideLeft
    }
}
