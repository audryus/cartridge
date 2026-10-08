// ConfigPopup.qml
// The content of the config window: one row per console cartridge found in the
// library, each with a dropdown of the cores installed on this machine that can
// run it.
//
// Only consoles found in the library are listed. A console with roms but no
// core installed says so instead of offering an empty list, which is the
// point: the library is the truth about what you own, not what you can launch
// today.
import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui

import "CartridgeModel.js" as Model

Item {
    id: root

    required property var store

    // Both buttons go back to the library. Choosing a core is a detour from
    // browsing, so leaving the config window is not leaving cartridge.
    signal backRequested()

    // Back, title, close. A window you can only leave with Escape or a click
    // outside the card looks stuck.
    readonly property int headerHeight: Style.space(34)
    readonly property int headerButton: Style.spacing.controlHeight + Style.space(6)

    // Real consoles only: Unidentified and Conflicts are not consoles, and
    // neither is anything cartridge knows about but the library does not hold.
    readonly property var consoles: root.store.revision, root.store.consoleRows()
        .filter(entry => entry.kind === "console")

    readonly property int rowHeight: Style.spacing.controlHeight + Style.space(26)

    Item {
        id: header
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: root.headerHeight

        Button {
            id: back
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            width: root.headerButton
            height: root.headerButton
            text: ""
            iconText: "\uF100"       // nf-fa-angle-double-left
            iconSize: Style.font.body
            tooltipText: "Back to the library"
            onClicked: root.backRequested()
        }

        Text {
            anchors.centerIn: parent
            width: parent.width - 2 * (closeButton.width + rescan.width) - Style.spacing.xl
            text: "Core per console"
            color: Color.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.title
            font.weight: Font.Medium
            horizontalAlignment: Text.AlignHCenter
            elide: Text.ElideRight
        }

        // Installed a core while cartridge was open: look again at what is on
        // disk, without the full library scan Refresh does.
        Button {
            id: rescan
            anchors.right: closeButton.left
            anchors.rightMargin: Style.spacing.xs
            anchors.verticalCenter: parent.verticalCenter
            width: root.headerButton
            height: root.headerButton
            text: ""
            iconText: "\uF021"       // nf-fa-repeat
            iconSize: Style.font.body
            tooltipText: "Look again for installed cores"
            iconSpinning: root.store.rescanningCores
            enabled: !root.store.scanning
            onClicked: root.store.rescanCores()
        }

        Button {
            id: closeButton
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            width: root.headerButton
            height: root.headerButton
            text: ""
            iconText: "\uF00D"       // nf-fa-times
            iconSize: Style.font.title
            tooltipText: "Close"
            onClicked: root.backRequested()
        }
    }

    // What the rescan button found. Empty, and so taking no room, until it is
    // pressed.
    Text {
        id: report
        anchors.top: header.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.leftMargin: Style.spacing.md
        anchors.rightMargin: Style.spacing.md
        height: text.length > 0 ? implicitHeight + Style.spacing.xs : 0
        text: root.store.coresReport
        color: Util.alpha(Color.foreground, 0.62)
        font.family: Style.font.family
        font.pixelSize: Style.font.caption
        horizontalAlignment: Text.AlignRight
        elide: Text.ElideRight
    }

    Text {
        anchors.top: report.bottom
        anchors.topMargin: Style.spacing.xs
        anchors.left: parent.left
        anchors.right: parent.right
        visible: root.consoles.length === 0
        text: root.store.loaded
            ? "No consoles found yet. Press Refresh in the library window."
            : "Reading the library…"
        color: Util.alpha(Color.foreground, 0.6)
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.WordWrap
    }

    // A ListView rather than a Column: the card has a fixed height, and a
    // library with thirty consoles must scroll rather than run off the screen.
    ListView {
        id: list
        anchors.top: report.bottom
        anchors.topMargin: Style.spacing.sm
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        clip: true
        model: root.consoles
        spacing: Style.spacing.xs
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar {}
        visible: root.consoles.length > 0

        delegate: Item {
            id: entry
            required property var modelData

            width: ListView.view.width
            height: root.rowHeight

            readonly property var options: [{ value: "", label: "No core" }]
                .concat(Model.coreOptions(root.store.cores, entry.modelData.id))
            readonly property int installed: options.length - 1

            Rectangle {
                anchors.fill: parent
                anchors.leftMargin: Style.spacing.xs
                anchors.rightMargin: Style.spacing.xs
                radius: Style.cornerRadius
                color: hover.hovered ? Style.hoverFill : "transparent"
            }

            HoverHandler {
                id: hover
                cursorShape: Qt.PointingHandCursor
            }

            Column {
                anchors.left: parent.left
                anchors.leftMargin: Style.spacing.md
                anchors.right: dropdown.left
                anchors.rightMargin: Style.spacing.md
                anchors.verticalCenter: parent.verticalCenter
                spacing: 1

                Text {
                    width: parent.width
                    text: entry.modelData.name
                    color: Color.foreground
                    font.family: Style.font.family
                    font.pixelSize: Style.font.body
                    elide: Text.ElideRight
                }

                // The reason, not just the absence: "no core installed" and
                // "no core set yet" are different problems.
                Text {
                    width: parent.width
                    text: {
                        const roms = entry.modelData.count +
                                     (entry.modelData.count === 1 ? " rom" : " roms")
                        if (entry.installed === 0)
                            return roms + " · no core for this console is installed"
                        const plural = entry.installed === 1 ? "core" : "cores"
                        const chosen = root.store.coreFor(entry.modelData.id)
                        if (!chosen)
                            return roms + " · " + entry.installed + " " + plural + " installed"
                        return roms + " · " + root.store.coreName(entry.modelData.id) +
                               " · " + entry.installed + " " + plural + " installed"
                    }
                    color: Util.alpha(Color.foreground, 0.58)
                    font.family: Style.font.family
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideRight
                }
            }

            SearchableDropdown {
                id: dropdown
                anchors.right: parent.right
                anchors.rightMargin: Style.spacing.md
                anchors.verticalCenter: parent.verticalCenter
                width: Style.space(196)
                showLabel: false
                placeholderText: "Choose a core"
                options: entry.options
                enabled: entry.installed > 0
                value: root.store.coreFor(entry.modelData.id)
                onChanged: next => {
                    if (next !== root.store.coreFor(entry.modelData.id))
                        root.store.setCore(entry.modelData.id, next)
                }
            }
        }
    }
}
