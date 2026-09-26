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
  readonly property int iconPx: 12
  readonly property bool playing: Model.isPlaying(root.musicStatus)
  readonly property string barText: Model.barLabel(root.musicStatus)

  function tooltipText() {
    return Model.tooltipText(root.musicStatus)
  }

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
        text: Model.ICON.note
        color: root.playing
          ? Color.accent
          : root.bar ? root.bar.foreground : Style.text
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
           if (Model.isActive(root.musicStatus))
            root.bar.run("yt-music-ctl stop 2>/dev/null")
          else
            root.bar.run("yt-music-ctl status 2>/dev/null")
        } else if (mouse.button === Qt.MiddleButton) {
          root.refresh()
        } else {
          root.togglePanel()
        }
      }
    }
  }
}
