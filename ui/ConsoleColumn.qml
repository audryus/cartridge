// ConsoleColumn.qml
// The left column: every console cartridge found in the library, then the two
// odd ones out — duplicates that are not really duplicates, and the roms
// nothing could identify.
import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui

Item {
    id: root

    required property var store
    required property string selected
    signal picked(string consoleId)

    readonly property int rowHeight: Style.spacing.popupRowHeight + Style.space(10)

    // Rebuilt whenever the library changes. revision is named explicitly so the
    // binding re-runs when the store says so, not only when it guesses right.
    property var consoles: root.store.revision, root.store.consoleRows()

    implicitWidth: Style.space(232)

    function isSelected(id) {
        return id === root.selected
    }

    Rectangle {
        id: header
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        height: Style.spacing.lg + Style.font.caption
        color: "transparent"

        Text {
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            text: "CONSOLES"
            color: Util.alpha(Color.foreground, 0.55)
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            font.weight: Font.DemiBold
            font.letterSpacing: 0.6
        }

        Text {
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            text: root.consoles.length
            color: Util.alpha(Color.foreground, 0.55)
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
        }
    }

    ListView {
        id: list
        anchors.top: header.bottom
        anchors.topMargin: Style.spacing.xs
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        clip: true
        model: root.consoles
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar {}

        delegate: Item {
            id: entry
            required property var modelData
            required property int index

            width: ListView.view.width
            height: root.rowHeight

            readonly property bool active: root.isSelected(entry.modelData.id)
            readonly property bool odd: entry.modelData.kind !== "console"

            Rectangle {
                anchors.fill: parent
                anchors.leftMargin: Style.spacing.xs
                anchors.rightMargin: Style.spacing.xs
                radius: Style.cornerRadius
                color: entry.active ? Style.selectionFill
                                    : (hover.hovered ? Style.hoverFill : "transparent")
            }

            HoverHandler {
                id: hover
                cursorShape: Qt.PointingHandCursor
            }

            TapHandler {
                onTapped: root.picked(entry.modelData.id)
            }

            Column {
                anchors.left: parent.left
                anchors.leftMargin: Style.spacing.md
                anchors.right: countLabel.left
                anchors.rightMargin: Style.spacing.xs
                anchors.verticalCenter: parent.verticalCenter
                spacing: 1

                Text {
                    width: parent.width
                    text: entry.modelData.name
                    color: Color.foreground
                    opacity: entry.odd ? 0.8 : 1
                    font.family: Style.font.family
                    font.pixelSize: Style.font.body
                    font.weight: entry.active ? Font.DemiBold : Font.Normal
                    elide: Text.ElideRight
                }

                // The core this console runs on, or why it cannot run yet.
                Text {
                    width: parent.width
                    visible: text.length > 0
                    text: {
                        if (entry.modelData.kind === "unknown")
                            return "tell cartridge what these are"
                        if (entry.modelData.kind === "conflict")
                            return "same name, different bytes"
                        const core = root.store.coreName(entry.modelData.id)
                        return core ? core : "no core set"
                    }
                    // Readable on the selected row too: a selected row is
                    // painted with a selection fill, and a flat grey that
                    // reads fine on the background vanishes on it.
                    color: Util.alpha(Color.foreground, entry.active ? 0.78 : 0.55)
                    font.family: Style.font.family
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideRight
                }
            }

            Text {
                id: countLabel
                anchors.right: parent.right
                anchors.rightMargin: Style.spacing.md
                anchors.verticalCenter: parent.verticalCenter
                text: entry.modelData.count
                color: Util.alpha(Color.foreground, entry.active ? 0.8 : 0.55)
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
            }
        }
    }

    // Nothing at all to show: say so instead of drawing an empty column.
    Text {
        anchors.centerIn: parent
        width: parent.width - Style.spacing.xl
        visible: root.consoles.length === 0
        text: root.store.loaded
            ? "No roms found in " + root.store.libraryRoot
            : "Press Refresh to scan your library"
        color: Util.alpha(Color.foreground, 0.6)
        font.family: Style.font.family
        font.pixelSize: Style.font.bodySmall
        wrapMode: Text.WordWrap
        horizontalAlignment: Text.AlignHCenter
    }
}
