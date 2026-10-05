import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

BarWidget {
  id: root
  moduleName: "yt-music"

  property var musicStatus: null
  property bool menuOpen: false
  readonly property string ctlPath: Quickshell.env("HOME") + "/.local/bin/yt-music-ctl"
  readonly property int iconPx: 12
  readonly property bool playing: Model.isPlaying(root.musicStatus)
  readonly property bool paused: !!(root.musicStatus && root.musicStatus.paused && Model.isActive(root.musicStatus))
  readonly property string barText: Model.barLabel(root.musicStatus)

  function tooltipText() {
    return Model.tooltipText(root.musicStatus)
  }

  function menuRun(command) {
    if (root.bar) root.bar.run("yt-music-ctl " + command + " 2>/dev/null")
  }

  function closeMenu() { root.menuOpen = false }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
  }

  function refresh() {
    if (panelLoader.item && panelLoader.item.refresh) panelLoader.item.refresh()
  }

  function togglePanel() {
    if (panelLoader.item && panelLoader.item.toggle) panelLoader.item.toggle()
  }

  function reloadState() {
    statusFile.reload()
  }

  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false

  function open() {
    if (panelLoader.item) panelLoader.item.open()
  }

  function close() {
    if (panelLoader.item) panelLoader.item.close()
  }

  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false

  function closeForPopoutSwitch() {
    if (panelLoader.item) panelLoader.item.closeForPopoutSwitch()
  }

  visible: true
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  FileView {
    id: statusFile
    path: Quickshell.env("HOME") + "/.local/state/yt-music/status.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.musicStatus = Model.parseStatus(text())
    onLoadFailed: root.musicStatus = null
  }

  Process {
    id: daemonProc
    command: [root.ctlPath, "ensure-daemon"]
    running: true
  }

  Timer {
    interval: 60000
    running: true
    repeat: true
    onTriggered: daemonProc.running = true
  }

  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  Item {
    id: button
    anchors.centerIn: parent
    implicitWidth: root.barText !== ""
      ? Math.max(Style.bar.iconSlot,
          noteIcon.implicitWidth + iconRow.spacing + statusLabel.implicitWidth + Style.space(10))
      : Style.bar.iconSlot
    implicitHeight: root.bar ? root.bar.barSize : Style.bar.sizeHorizontal
    width: implicitWidth
    height: implicitHeight

    Row {
      id: iconRow
      anchors.centerIn: parent
      spacing: Style.space(2)

      Text {
        id: noteIcon
        text: root.paused ? Model.ICON.pause : Model.ICON.note
        color: root.playing
          ? Color.accent
          : root.paused
            ? Qt.darker(Color.accent, 1.4)
            : (root.bar ? root.bar.foreground : Style.text)
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.icon
        anchors.verticalCenter: parent.verticalCenter
      }

      Text {
        id: statusLabel
        visible: root.barText !== ""
        text: root.barText
        color: root.bar ? root.bar.foreground : Style.text
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.caption
        anchors.verticalCenter: parent.verticalCenter
      }
    }

    MouseArea {
      anchors.fill: parent
      acceptedButtons: Qt.LeftButton | Qt.RightButton | Qt.MiddleButton
      hoverEnabled: true
      cursorShape: Qt.PointingHandCursor
      onEntered: if (root.bar) root.bar.showTooltip(button, root.tooltipText())
      onExited: if (root.bar) root.bar.hideTooltip(button)
      onClicked: function(mouse) {
        if (!root.bar) return
        if (mouse.button === Qt.RightButton) {
          root.menuOpen = !root.menuOpen
        } else if (mouse.button === Qt.MiddleButton) {
          root.bar.run("yt-music-ctl toggle 2>/dev/null")
        } else {
          root.togglePanel()
        }
      }
    }
  }

  QtObject {
    id: menuOwner
    function close() { root.closeMenu() }
  }

  PopupCard {
    id: transportMenu
    anchorItem: button
    bar: root.bar
    owner: menuOwner
    open: root.menuOpen
    contentWidth: transportMenu.fittedContentWidth(Style.space(220))
    contentHeight: transportMenu.fittedContentHeight(menuColumn.implicitHeight)

    Column {
      id: menuColumn
      anchors.fill: parent
      spacing: Style.space(4)

      Text {
        width: parent.width
        textFormat: Text.PlainText
        elide: Text.ElideRight
        text: root.barText !== "" ? root.barText : "YouTube Music"
        color: root.bar ? root.bar.foreground : Style.text
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.bodySmall
        font.bold: true
      }

      Text {
        width: parent.width
        textFormat: Text.PlainText
        elide: Text.ElideRight
        visible: root.musicStatus !== null
        text: root.paused ? "Paused" : (root.playing ? "Playing" : "Idle")
        color: Qt.darker(root.bar ? root.bar.foreground : Style.text, 1.4)
        font.family: root.bar ? root.bar.fontFamily : Style.font.family
        font.pixelSize: Style.font.caption
      }

      PanelSeparator { foreground: root.bar ? root.bar.foreground : Style.text }

      Button {
        width: menuColumn.width
        leftAlign: true
        iconText: root.playing ? Model.ICON.pause : Model.ICON.play
        text: root.playing ? "Pause" : (root.paused ? "Resume" : "Play")
        foreground: root.bar ? root.bar.foreground : Style.text
        fontFamily: root.bar ? root.bar.fontFamily : Style.font.family
        onClicked: { root.menuRun("toggle"); root.closeMenu() }
      }

      Button {
        width: menuColumn.width
        leftAlign: true
        iconText: Model.ICON.prev
        text: "Previous"
        foreground: root.bar ? root.bar.foreground : Style.text
        fontFamily: root.bar ? root.bar.fontFamily : Style.font.family
        onClicked: { root.menuRun("prev"); root.closeMenu() }
      }

      Button {
        width: menuColumn.width
        leftAlign: true
        iconText: Model.ICON.next
        text: "Next"
        foreground: root.bar ? root.bar.foreground : Style.text
        fontFamily: root.bar ? root.bar.fontFamily : Style.font.family
        onClicked: { root.menuRun("next"); root.closeMenu() }
      }

      Button {
        width: menuColumn.width
        leftAlign: true
        iconText: Model.ICON.stop
        text: "Stop"
        foreground: root.bar ? root.bar.foreground : Style.text
        fontFamily: root.bar ? root.bar.fontFamily : Style.font.family
        onClicked: { root.menuRun("stop"); root.closeMenu() }
      }

      PanelSeparator { foreground: root.bar ? root.bar.foreground : Style.text }

      Button {
        width: menuColumn.width
        leftAlign: true
        iconText: Model.ICON.music
        text: root.opened ? "Close player" : "Open player"
        foreground: root.bar ? root.bar.foreground : Style.text
        fontFamily: root.bar ? root.bar.fontFamily : Style.font.family
        onClicked: { root.togglePanel(); root.closeMenu() }
      }
    }
  }
}
