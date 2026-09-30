// RomColumn.qml
// The right column: the search box, then the roms of the selected console —
// favorites first, then the most recently played, then alphabetical.
import QtQuick
import QtQuick.Controls
import qs.Commons
import qs.Ui

import "CartridgeModel.js" as Model

Item {
    id: root

    required property var store
    required property string consoleId
    signal played()
    // Escape closes the window from here too, not just from the empty parts of
    // it. The search field holds the focus as soon as the window opens, so a
    // handler anywhere else would never see the key.
    signal closeRequested()

    property string query: ""

    // Rebuilt on selection, on search text and whenever the library changes.
    // The row list can be several thousand entries, so it is a plain array
    // rather than a ListModel: the ListView recycles delegates, so only the
    // visible ones ever exist.
    property var rows: root.store.revision, root.query, root.consoleId,
                      root.store.rowsFor(root.consoleId, root.query)

    readonly property bool unidentified: root.consoleId === "unknown"
    readonly property var sources: root.store.revision,
                                   root.unidentified ? root.store.unknownGroups() : []

    function clearSearch() {
        queryTimer.stop()
        root.query = ""
        if (searchField)
            searchField.text = ""
    }

    // A search re-filters thousands of rows, so it waits for a pause in the
    // typing instead of running once per key.
    Timer {
        id: queryTimer
        interval: 120
        onTriggered: root.query = searchField.text
    }

    function focusSearch() {
        if (searchField)
            searchField.forceActiveFocus()
    }

    Column {
        anchors.fill: parent
        spacing: 0

        // --- search ---------------------------------------------------
        Item {
            id: searchRow
            width: parent.width
            height: Style.spacing.controlHeight + Style.spacing.md

            TextField {
                id: searchField
                anchors.left: parent.left
                anchors.right: searchHint.left
                anchors.rightMargin: Style.spacing.sm
                anchors.verticalCenter: parent.verticalCenter
                height: Style.spacing.controlHeight
                placeholderText: root.consoleId === "unknown"
                    ? "Search unidentified roms"
                    : "Search this console"
                font.pixelSize: Style.font.bodySmall
                verticalPadding: Style.spacing.xs
                onTextChanged: queryTimer.restart()
                Keys.onEscapePressed: function(event) {
                    root.closeRequested()
                    event.accepted = true
                }
            }

            Text {
                id: searchHint
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                text: root.rows.length + (root.rows.length === 1 ? " rom" : " roms")
                color: Util.alpha(Color.foreground, 0.55)
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
            }
        }

        // --- unidentified by source -----------------------------------
        //
        // A rom set of 5,000 unidentified .bin files is not something anyone
        // should identify one at a time, so each file they came out of gets one
        // dropdown. Never a whole-library button: assigning a console to
        // thousands of roms by accident is worse than not having it.
        Rectangle {
            id: bulk
            width: parent.width
            height: root.sources.length > 0 ? bulkColumn.implicitHeight + Style.spacing.md : 0
            visible: height > 0
            color: Util.alpha(Color.foreground, 0.07)
            radius: Style.cornerRadius

            Column {
                id: bulkColumn
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: Style.spacing.sm
                spacing: Style.spacing.xs

                Text {
                    text: "UNIDENTIFIED — assign a console"
                    color: Util.alpha(Color.foreground, 0.6)
                    font.family: Style.font.family
                    font.pixelSize: Style.font.caption
                    font.weight: Font.DemiBold
                    font.letterSpacing: 0.6
                }

                Repeater {
                    model: root.sources.slice(0, 4)

                    delegate: Item {
                        required property var modelData
                        width: bulkColumn.width
                        height: bulkRow.implicitHeight

                        Column {
                            id: bulkRow
                            width: parent.width
                            spacing: 1

                            Text {
                                width: parent.width
                                text: modelData.count + " × " + (modelData.exts || "no extension")
                                color: Color.foreground
                                font.family: Style.font.family
                                font.pixelSize: Style.font.bodySmall
                            }

                            Text {
                                width: parent.width
                                text: modelData.source
                                color: Util.alpha(Color.foreground, 0.55)
                                font.family: Style.font.family
                                font.pixelSize: Style.font.caption
                                elide: Text.ElideMiddle
                            }

                            SearchableDropdown {
                                id: bulkDropdown
                                width: Math.min(parent.width, Style.spacing.searchableDropdownWidth)
                                showLabel: false
                                placeholderText: "Assign all " + modelData.count + " to…"
                                value: ""
                                options: root.store.catalogOptions
                                onChanged: next => {
                                    // The absolute path, which is what the
                                    // state script matches rom records on.
                                    root.store.assignContainer(modelData.path, next)
                                    bulkDropdown.value = ""
                                }
                            }
                        }
                    }
                }

                Text {
                    width: parent.width
                    visible: root.sources.length > 4
                    text: "+" + (root.sources.length - 4) + " more files"
                    color: Util.alpha(Color.foreground, 0.55)
                    font.family: Style.font.family
                    font.pixelSize: Style.font.caption
                }
            }
        }

        // --- the list ---------------------------------------------------
        ListView {
            id: list
            width: parent.width
            height: parent.height - searchRow.height - bulk.height
            clip: true
            model: root.rows
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar {}

            delegate: Item {
                id: row
                required property var modelData
                required property int index

                width: ListView.view.width
                height: Style.spacing.popupRowHeight + Style.space(8)

                Rectangle {
                    anchors.fill: parent
                    anchors.leftMargin: Style.spacing.xs
                    anchors.rightMargin: Style.spacing.xs
                    radius: Style.cornerRadius
                    color: rowHover.hovered ? Style.hoverFill : "transparent"
                }

                HoverHandler {
                    id: rowHover
                    cursorShape: Qt.PointingHandCursor
                }

                // --- favorite
                Item {
                    id: star
                    anchors.left: parent.left
                    anchors.leftMargin: Style.spacing.sm
                    anchors.verticalCenter: parent.verticalCenter
                    width: Style.space(20)
                    height: width

                    Text {
                        anchors.centerIn: parent
                        text: row.modelData.favorite ? "\uF005" : "\uF006"   // nf-fa-star / star_o
                        color: row.modelData.favorite ? Color.accent : Util.alpha(Color.foreground, 0.45)
                        font.family: Style.font.family
                        font.pixelSize: Style.font.body
                    }

                    TapHandler {
                        onTapped: root.store.toggleFavorite(row.modelData.id)
                    }
                }

                // --- name and where it came from
                Column {
                    anchors.left: star.right
                    anchors.leftMargin: Style.spacing.xs
                    anchors.right: actionRow.left
                    anchors.rightMargin: Style.spacing.sm
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 1

                    Text {
                        width: parent.width
                        text: row.modelData.name
                        color: Color.foreground
                        font.family: Style.font.family
                        font.pixelSize: Style.font.body
                        elide: Text.ElideRight
                    }

                    Text {
                        width: parent.width
                        text: root.subtitleFor(row.modelData)
                        color: Util.alpha(Color.foreground, 0.58)
                        font.family: Style.font.family
                        font.pixelSize: Style.font.caption
                        elide: Text.ElideMiddle
                    }
                }

                // --- play, or say what this is
                Item {
                    id: actionRow
                    anchors.right: parent.right
                    anchors.rightMargin: Style.spacing.sm
                    anchors.verticalCenter: parent.verticalCenter
                    readonly property bool unidentified: row.modelData.console === "unknown"
                    width: unidentified ? Style.space(140)
                                        : (row.modelData.playable ? Style.spacing.controlHeight : 0)
                    height: Style.spacing.controlHeight

                    // An unidentified rom cannot be played until it is identified.
                    SearchableDropdown {
                        id: consolePicker
                        anchors.fill: parent
                        visible: actionRow.unidentified
                        showLabel: false
                        placeholderText: "Which console?"
                        value: ""
                        options: root.store.catalogOptions
                        onChanged: next => {
                            root.store.assignConsole(row.modelData.id, next)
                            consolePicker.value = ""
                        }
                    }

                    Button {
                        anchors.fill: parent
                        visible: !actionRow.unidentified
                        enabled: row.modelData.playable
                        text: ""
                        iconText: "\uF04B"                 // nf-fa-play-circle
                        iconSize: Style.font.body
                        tooltipText: row.modelData.playable
                            ? "Play in RetroArch"
                            : root.store.playableReason(row.modelData.console)
                        onClicked: {
                            root.store.play(row.modelData.id)
                            root.played()
                        }
                    }
                }
            }
        }

        // --- nothing to show -------------------------------------------
        Text {
            width: parent.width
            height: parent.height - searchRow.height - bulk.height
            visible: root.rows.length === 0
            text: root.emptyText()
            color: Util.alpha(Color.foreground, 0.6)
            font.family: Style.font.family
            font.pixelSize: Style.font.bodySmall
            wrapMode: Text.WordWrap
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
        }
    }

    // lastPlayed is a Unix timestamp in seconds, and so is the "now" it is
    // compared against -- Date.now() is milliseconds, and mixing the two makes
    // a game played a minute ago look 57 years old.
    function subtitleFor(row) {
        if (row.lastPlayed > 0)
            return Model.formatWhen(row.lastPlayed, Math.floor(Date.now() / 1000)) + " · " + row.where
        return row.where
    }

    function emptyText() {
        if (!root.store.loaded)
            return "Press Refresh to scan your library"
        if (root.consoleId === "unknown")
            return root.query.length > 0
                ? "Nothing unidentified matches “" + root.query + "”"
                : "Every rom in the library was identified"
        if (root.consoleId === "conflict")
            return root.query.length > 0
                ? "No conflict matches “" + root.query + "”"
                : "No duplicates with different contents"
        if (root.query.length > 0)
            return "No rom matches “" + root.query + "”"
        return "This console has no roms"
    }
}
