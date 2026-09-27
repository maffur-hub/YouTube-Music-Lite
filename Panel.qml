import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "yt-music"
  ipcTarget: "yt-music"
  manageIpc: false

  // Register the panel with the shell (the base IpcHandler is disabled by
  // manageIpc: false); without this the shell does not track the popup.
  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
  }

  property var anchorItem: null
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root
  property var musicStatus: hostWidget ? hostWidget.musicStatus : null

  property bool openedFromHotkey: false
  property bool busy: false
  property bool refreshing: false
  property string statusText: ""

  readonly property string ctlPath: Quickshell.env("HOME") + "/.local/bin/yt-music-ctl"
  readonly property color fg: root.barForeground
  readonly property string fam: root.bar ? root.bar.fontFamily : Style.font.family

  property bool loggedIn: false
  property var playlists: []
  readonly property var playlistOptions: root.playlists.map(function(playlist) {
    return { value: playlist.id, label: playlist.title }
  })
  property var playlistTracks: []
  property string activePlaylistTitle: ""
  property string activePlaylistId: ""
  property var searchResults: []
  property string searchQuery: ""
  property bool searching: false
  property int selectedIndex: -1
  property int lastVolume: 100
  readonly property int currentVolume: root.musicStatus && root.musicStatus.volume !== undefined
    ? Math.round(Number(root.musicStatus.volume))
    : 100
  property var queueTracks: []
  property int queuePosition: -1
  property bool queueOpen: true
  property int contextQueueIndex: -1
  property string queueKey: ""
  readonly property string activeListKind: root.searchResults.length > 0 ? "search"
    : (root.playlistTracks.length > 0 ? "playlist"
    : (root.queueOpen && root.queueTracks.length > 0 ? "queue" : ""))
  readonly property var activeList: root.activeListKind === "search" ? root.searchResults
    : (root.activeListKind === "playlist" ? root.playlistTracks
    : (root.activeListKind === "queue" ? root.queueTracks : []))
  readonly property bool looping: !!(root.musicStatus && root.musicStatus.loop
    && root.musicStatus.loop !== "no")
  onSearchResultsChanged: root.selectedIndex = -1
  onActiveListKindChanged: root.selectedIndex = -1
  property var likedVideoIds: ({})
  property string newPlaylistName: ""
  readonly property int maxProcessOutput: 65536
  readonly property int commandTimeout: 15000
  property var processOutput: ({})
  property string thumbnailSource: ""
  property string thumbnailVideoId: ""

  property string contextVideoId: ""
  property string contextTitle: ""
  property string contextArtist: ""
  property string contextSource: ""
  property real contextX: 0
  property real contextY: 0

  function open() {
    statusText = ""
    root.controller.show()
    root.refresh()
    root.refreshQueue()
  }

  function openFromHotkey() {
    root.openedFromHotkey = true
    root.open()
  }

  function close() {
    root.controller.hide()
  }

  function toggle() {
    root.opened ? root.close() : root.openFromHotkey()
  }

  function refresh() {
    if (root.refreshing) return
    root.refreshing = true
    root.startProcess(statusProc, "status")
  }

  function refreshQueue() {
    if (!root.opened) return
    if (queueListProc.running) return
    if (!root.musicStatus || !Model.isActive(root.musicStatus)) {
      root.queueTracks = []
      root.queuePosition = -1
      return
    }
    root.startProcess(queueListProc, "queueList")
  }

  function startProcess(proc, key) {
    root.processOutput[key] = ""
    proc.running = true
  }

  function appendProcessOutput(key, chunk) {
    var current = String(root.processOutput[key] || "")
    var remaining = root.maxProcessOutput - current.length
    if (remaining <= 0) return
    root.processOutput[key] = current + String(chunk || "").slice(0, remaining)
  }

  function processText(key) {
    return String(root.processOutput[key] || "")
  }

  function boundedString(value, limit) {
    return String(value === undefined || value === null ? "" : value).slice(0, limit)
  }

  function isVideoId(value) {
    return /^[A-Za-z0-9_-]{11}$/.test(String(value || ""))
  }

  function normalizeSong(song) {
    if (!song || !isVideoId(song.videoId)) return null
    return {
      videoId: String(song.videoId),
      title: root.boundedString(song.title, 256),
      artist: root.boundedString(song.artist, 256),
      duration: Math.max(0, Math.min(86400, Number(song.duration) || 0))
    }
  }

  function normalizeSongs(items, limit) {
    var result = []
    for (var i = 0; i < Math.min(Array.isArray(items) ? items.length : 0, limit); i++) {
      var song = root.normalizeSong(items[i])
      if (song) result.push(song)
    }
    return result
  }

  function normalizePlaylists(items) {
    var result = []
    for (var i = 0; i < Math.min(Array.isArray(items) ? items.length : 0, 100); i++) {
      var playlist = items[i]
      if (!playlist || !playlist.id) continue
      result.push({
        id: root.boundedString(playlist.id, 256),
        title: root.boundedString(playlist.title, 256)
      })
    }
    return result
  }

  function parseProcessJson(raw) {
    try {
      return JSON.parse(String(raw || "{}"))
    } catch (e) {
      root.statusText = "Invalid backend response"
      return null
    }
  }

  function loadPlaylists() {
    if (playlistsProc.running) return
    root.startProcess(playlistsProc, "playlists")
  }

  function openPlaylist(id, title) {
    root.activePlaylistId = id
    root.activePlaylistTitle = title
    root.playlistTracks = []
    root.selectedIndex = -1
    root.statusText = ""
    tracksProc.command = [root.ctlPath, "playlist", id]
    root.startProcess(tracksProc, "tracks")
  }

  function playSelectedPlaylist() {
    if (!root.activePlaylistId || root.busy) return
    root.busy = true
    queueProc.command = [root.ctlPath, "queue", root.activePlaylistId]
    root.startProcess(queueProc, "queue")
  }

  function selectPlaylist(id) {
    for (var i = 0; i < root.playlists.length; i++) {
      if (root.playlists[i].id === id) {
        root.openPlaylist(id, root.playlists[i].title)
        return
      }
    }
  }

  function closePlaylist() {
    root.activePlaylistId = ""
    root.activePlaylistTitle = ""
    root.playlistTracks = []
    root.selectedIndex = -1
  }

  function logout() {
    if (root.busy) return
    root.busy = true
    root.startProcess(logoutProc, "logout")
  }

  function createPlaylist() {
    var title = root.newPlaylistName.trim()
    if (!title || root.busy) return
    root.busy = true
    root.statusText = "Creating playlist…"
    createPlaylistProc.command = [root.ctlPath, "create-playlist", title]
    root.startProcess(createPlaylistProc, "create")
  }

  function search(query) {
    if (query === undefined || query.trim() === "") return
    root.searchQuery = query.trim()
    root.searchResults = []
    root.searching = true
    searchProc.command = [root.ctlPath, "search", root.searchQuery]
    root.startProcess(searchProc, "search")
  }

  function clearSearch() {
    searchField.text = ""
    root.searchQuery = ""
    root.searchResults = []
    root.searching = false
    root.selectedIndex = -1
  }

  function playNow(videoId) {
    if (root.busy || !root.isVideoId(videoId)) return
    root.busy = true
    playNowProc.command = [root.ctlPath, "play", videoId]
    root.startProcess(playNowProc, "play")
  }

  function playMix(videoId) {
    if (root.busy || !root.isVideoId(videoId)) return
    root.busy = true
    mixProc.command = [root.ctlPath, "mix", videoId]
    root.startProcess(mixProc, "mix")
  }

  function sendCmd(command, args) {
    if (root.busy) return
    root.busy = true
    statusText = "Sending " + command + "…"
    cmdProc.command = [root.ctlPath, command].concat((args || []).map(String))
    root.startProcess(cmdProc, "cmd")
  }

  function queueJump(index) {
    if (root.busy) return
    root.sendCmd("queue-jump", [String(index)])
  }

  function queueRemove(index) {
    if (root.busy) return
    root.sendCmd("queue-remove", [String(index)])
  }

  function queueMove(from, to) {
    if (root.busy) return
    root.sendCmd("queue-move", [String(from), String(to)])
  }

  function openContextMenu(videoId, title, artist, source, x, y, queueIndex) {
    if (!root.isVideoId(videoId)) return
    root.contextVideoId = String(videoId)
    root.contextTitle = title || ""
    root.contextArtist = artist || ""
    root.contextSource = source || ""
    root.contextQueueIndex = (source === "queue") ? Number(queueIndex) : -1
    root.contextX = x; root.contextY = y
    root.rebuildContextMenu()
    contextMenu.popup(panelFlick, x, y)
  }

  function openPlaylistPicker() {
    if (!root.loggedIn || root.playlists.length === 0) return
    rebuildPlaylistPicker()
    Qt.callLater(function() {
      playlistPickerMenu.popup(panelFlick, root.contextX, root.contextY)
    })
  }

  function clearMenu(menu) {
    // Menu.clear() does not exist in Qt 6.11; remove items manually.
    var guard = 0
    while (menu.count > 0 && guard++ < 500) {
      var old = menu.takeItem(0)
      if (!old) break
      old.destroy()
    }
  }

  function addContextItem(text, handler) {
    var it = contextMenuItemComponent.createObject(null, { "text": text, "runAction": handler })
    if (it) contextMenu.addItem(it)
  }

  function rebuildContextMenu() {
    clearMenu(contextMenu)
    addContextItem("Play now", function() { root.sendCmd("play", [root.contextVideoId]) })
    addContextItem("Play next", function() { root.sendCmd("play-next", [root.contextVideoId, root.contextTitle, root.contextArtist]) })
    addContextItem("Add to queue", function() { root.sendCmd("queue-add", [root.contextVideoId, root.contextTitle, root.contextArtist]) })
    addContextItem("Start mix", function() { root.playMix(root.contextVideoId) })
    if (root.loggedIn)
      addContextItem("Like", function() { root.sendCmd("like", [root.contextVideoId]) })
    if (root.loggedIn && root.contextSource === "nowplaying")
      addContextItem("Dislike", function() { root.sendCmd("dislike", [root.contextVideoId]) })
    if (root.loggedIn && root.playlists.length > 0)
      addContextItem("Add to playlist…", function() { root.openPlaylistPicker() })
    if (root.contextSource === "track" && root.activePlaylistId !== "")
      addContextItem("Remove from playlist", function() { root.sendCmd("remove", [root.activePlaylistId, root.contextVideoId]) })
    if (root.contextSource === "queue" && root.contextQueueIndex >= 0) {
      var qi = root.contextQueueIndex
      if (qi > 0)
        addContextItem("Move up", function() { root.queueMove(qi, qi - 1) })
      if (qi < root.queueTracks.length - 1)
        addContextItem("Move down", function() { root.queueMove(qi, qi + 1) })
      addContextItem("Remove from queue", function() { root.queueRemove(qi) })
    }
  }

  function rebuildPlaylistPicker() {
    clearMenu(playlistPickerMenu)
    for (var i = 0; i < root.playlists.length; i++) {
      var pl = root.playlists[i]
      if (!pl || !pl.id) continue
      var it = playlistMenuItemComponent.createObject(null, { "text": pl.title, "playlistId": pl.id })
      if (it) playlistPickerMenu.addItem(it)
    }
  }

  function likeCurrent() {
    if (!root.musicStatus || !root.isVideoId(root.musicStatus.videoId)) return
    sendCmd("like", [root.musicStatus.videoId])
  }

  function dislikeCurrent() {
    if (!root.musicStatus || !root.isVideoId(root.musicStatus.videoId)) return
    sendCmd("dislike", [root.musicStatus.videoId])
  }

  function toggleMute() {
    var current = root.currentVolume
    if (current > 0) {
      root.lastVolume = current
      root.sendCmd("volume", ["0"])
    } else {
      root.sendCmd("volume", [String(root.lastVolume || 100)])
    }
  }

  function toggleLoop() {
    var current = (root.musicStatus && root.musicStatus.loop) || "no"
    root.sendCmd("loop", [current === "no" ? "inf" : "no"])
  }

  function selectIndex(index) {
    if (index < -1 || index >= root.activeList.length) return
    root.selectedIndex = index
    root.ensureSelectionVisible()
  }

  function ensureSelectionVisible() {
    if (root.selectedIndex < 0) return
    if (panelFlick.contentHeight <= panelFlick.height) return
    var repeater = root.activeListKind === "search" ? searchRepeater
      : (root.activeListKind === "playlist" ? trackRepeater : queueRepeater)
    if (!repeater) return
    var item = repeater.itemAt(root.selectedIndex)
    if (!item) return
    var pt = item.mapToItem(panelFlick, 0, 0)
    var maxY = Math.max(0, panelFlick.contentHeight - panelFlick.height)
    if (pt.y < 0)
      panelFlick.contentY = Math.max(0, panelFlick.contentY + pt.y)
    else if (pt.y + item.height > panelFlick.height)
      panelFlick.contentY = Math.min(maxY, panelFlick.contentY + pt.y + item.height - panelFlick.height)
  }

  // -------------------------------------------------------------- status refresh

  Process {
    id: statusProc
    command: [root.ctlPath, "status"]
    stdout: SplitParser {
      onRead: function(data) {
        root.appendProcessOutput("status", data)
      }
    }
    stderr: SplitParser {
      onRead: function(data) { root.appendProcessOutput("statusErr", data) }
    }
    onStarted: statusDeadline.start()
    onExited: function(exitCode) {
      statusDeadline.stop()
      var msg = root.processText("statusErr").trim()
      if (msg !== "") root.statusText = root.boundedString(msg.split("\n")[0], 256)
      if (hostWidget && hostWidget.reloadState) hostWidget.reloadState()
      root.refreshing = false
      if (!root.loggedIn && playlistsProc.state !== Process.Running)
        root.loadPlaylists()
    }
  }

  Process {
    id: playlistsProc
    command: [root.ctlPath, "playlists"]
    stdout: SplitParser {
      onRead: function(data) { root.appendProcessOutput("playlists", data) }
    }
    stderr: SplitParser {
      onRead: function(data) { root.appendProcessOutput("playlistsErr", data) }
    }
    onStarted: playlistsDeadline.start()
    onExited: function(exitCode) {
      playlistsDeadline.stop()
      var data = root.parseProcessJson(root.processText("playlists"))
      var msg = root.processText("playlistsErr").trim()
      if (data && data.ok) {
        root.loggedIn = true
        root.playlists = root.normalizePlaylists(data.playlists)
      } else if (msg.indexOf("Not logged in") !== -1) {
        root.loggedIn = false
        root.playlists = []
      }
      root.busy = false
      root.refreshing = false
    }
  }

  Process {
    id: tracksProc
    stdout: SplitParser {
      onRead: function(data) { root.appendProcessOutput("tracks", data) }
    }
    stderr: SplitParser {
      onRead: function(data) { root.appendProcessOutput("tracksErr", data) }
    }
    onStarted: tracksDeadline.start()
    onExited: function(exitCode) {
      tracksDeadline.stop()
      var data = root.parseProcessJson(root.processText("tracks"))
      var msg = root.processText("tracksErr").trim()
      if (data && data.ok) {
        root.playlistTracks = root.normalizeSongs(data.tracks, 500)
        root.activePlaylistTitle = root.boundedString(data.title || root.activePlaylistTitle, 256)
        if (root.playlistTracks.length === 0) root.statusText = "Playlist is empty"
      } else if (data && data.error) {
        root.statusText = root.boundedString(data.error, 256)
      }
      if (msg !== "") root.statusText = root.boundedString(msg.split("\n")[0], 256)
      root.busy = false
      if (exitCode !== 0 && root.statusText === "")
        root.statusText = "Could not load playlist"
    }
  }

  Process {
    id: searchProc
    stdout: SplitParser {
      onRead: function(data) { root.appendProcessOutput("search", data) }
    }
    stderr: SplitParser {
      onRead: function(data) { root.appendProcessOutput("searchErr", data) }
    }
    onStarted: searchDeadline.start()
    onExited: function(exitCode) {
      searchDeadline.stop()
      var data = root.parseProcessJson(root.processText("search"))
      if (data && data.ok && data.query === root.searchQuery)
        root.searchResults = root.normalizeSongs(data.songs, 100)
      root.busy = false
      root.searching = false
    }
  }

  Process {
    id: playNowProc
    stdout: SplitParser { onRead: function(data) { root.appendProcessOutput("play", data) } }
    stderr: SplitParser { onRead: function(data) { root.appendProcessOutput("playErr", data) } }
    onStarted: playDeadline.start()
    onExited: function(exitCode) {
      playDeadline.stop()
      root.busy = false
      if (exitCode === 0) {
        statusText = "Playing ✓"
        afterCommand.restart()
        root.refreshQueue()
      } else {
        statusText = "Play failed"
      }
    }
  }

  Process {
    id: mixProc
    stdout: SplitParser { onRead: function(data) { root.appendProcessOutput("mix", data) } }
    stderr: SplitParser { onRead: function(data) { root.appendProcessOutput("mixErr", data) } }
    onStarted: mixDeadline.start()
    onExited: function(exitCode) {
      mixDeadline.stop()
      root.busy = false
      if (exitCode === 0) {
        statusText = "Mix started ✓"
        root.refreshQueue()
      } else statusText = "Mix failed"
    }
  }

  Process {
    id: queueProc
    stdout: SplitParser {
      onRead: function(data) { root.appendProcessOutput("queue", data) }
    }
    stderr: SplitParser {
      onRead: function(data) { root.appendProcessOutput("queueErr", data) }
    }
    onStarted: queueDeadline.start()
    onExited: function(exitCode) {
      queueDeadline.stop()
      var data = root.parseProcessJson(root.processText("queue"))
      var msg = root.processText("queueErr").trim()
      if (data && data.ok)
        root.statusText = "Playing " + root.boundedString(data.title || root.activePlaylistTitle, 256) + " ✓"
      else if (data && data.error)
        root.statusText = root.boundedString(data.error, 256)
      if (msg !== "") root.statusText = root.boundedString(msg.split("\n")[0], 256)
      root.busy = false
      if (exitCode !== 0) root.statusText = "Could not play playlist"
      if (exitCode === 0) { afterCommand.restart(); root.refreshQueue() }
    }
  }

  Process {
    id: queueListProc
    command: [root.ctlPath, "queue-list"]
    stdout: SplitParser {
      onRead: function(data) { root.appendProcessOutput("queueList", data) }
    }
    stderr: SplitParser {
      onRead: function(data) { root.appendProcessOutput("queueListErr", data) }
    }
    onStarted: queueListDeadline.start()
    onExited: function(exitCode) {
      queueListDeadline.stop()
      var data = root.parseProcessJson(root.processText("queueList"))
      if (data && data.ok) {
        root.queuePosition = (typeof data.position === "number") ? data.position : -1
        var tracks = Array.isArray(data.tracks) ? data.tracks : []
        var rows = []
        for (var i = 0; i < Math.min(tracks.length, 100); i++) {
          var t = tracks[i] || {}
          rows.push({
            index: i,
            videoId: String(t.videoId || ""),
            title: root.boundedString(t.title, 256),
            artist: root.boundedString(t.artist, 256),
            duration: Math.max(0, Number(t.duration) || 0),
            current: !!t.current
          })
        }
        root.queueTracks = rows
      }
    }
  }

  Process {
    id: logoutProc
    command: [root.ctlPath, "logout"]
    stdout: SplitParser { onRead: function(data) { root.appendProcessOutput("logout", data) } }
    stderr: SplitParser { onRead: function(data) { root.appendProcessOutput("logoutErr", data) } }
    onStarted: logoutDeadline.start()
    onExited: function(exitCode) {
      logoutDeadline.stop()
      root.busy = false
      if (exitCode === 0) {
        root.loggedIn = false
        root.playlists = []
        root.activePlaylistId = ""
        root.activePlaylistTitle = ""
        root.playlistTracks = []
        root.close()
      }
    }
  }

  Process {
    id: createPlaylistProc
    stdout: SplitParser {
      onRead: function(data) { root.appendProcessOutput("create", data) }
    }
    stderr: SplitParser {
      onRead: function(data) { root.appendProcessOutput("createErr", data) }
    }
    onStarted: createDeadline.start()
    onExited: function(exitCode) {
      createDeadline.stop()
      var data = root.parseProcessJson(root.processText("create"))
      var msg = root.processText("createErr").trim()
      if (data && data.ok) {
        newPlaylistField.text = ""
        root.newPlaylistName = ""
        root.statusText = "Playlist created ✓"
        root.loadPlaylists()
      } else if (data && data.error) {
        root.statusText = root.boundedString(data.error, 256)
      }
      if (msg !== "") root.statusText = root.boundedString(msg.split("\n")[0], 256)
      root.busy = false
      if (exitCode !== 0) root.statusText = "Could not create playlist"
    }
  }

  Process {
    id: cmdProc
    stdout: SplitParser { onRead: function(data) { root.appendProcessOutput("cmd", data) } }
    stderr: SplitParser { onRead: function(data) { root.appendProcessOutput("cmdErr", data) } }
    onStarted: cmdDeadline.start()
    onExited: function(exitCode) {
      cmdDeadline.stop()
      root.busy = false
      root.refreshQueue()
      if (exitCode !== 0) {
        statusText = "Command failed"
        return
      }
      var action = String(cmdProc.command[1] || "Command")
      action = action.charAt(0).toUpperCase() + action.slice(1).replace(/-/g, " ")
      statusText = action + " ✓"
      if (action === "Remove" && root.activePlaylistId) {
        tracksProc.command = [root.ctlPath, "playlist", root.activePlaylistId]
        root.startProcess(tracksProc, "tracks")
      }
      afterCommand.restart()
    }
  }

  Timer { id: statusDeadline; interval: root.commandTimeout; onTriggered: { if (statusProc.running) { statusProc.running = false; root.statusText = "Status request timed out" } } }
  Timer { id: playlistsDeadline; interval: root.commandTimeout; onTriggered: { if (playlistsProc.running) { playlistsProc.running = false; root.statusText = "Library request timed out" } } }
  Timer { id: tracksDeadline; interval: root.commandTimeout; onTriggered: { if (tracksProc.running) { tracksProc.running = false; root.statusText = "Playlist request timed out" } } }
  Timer { id: searchDeadline; interval: root.commandTimeout; onTriggered: { if (searchProc.running) { searchProc.running = false; root.statusText = "Search timed out" } } }
  Timer { id: playDeadline; interval: root.commandTimeout; onTriggered: { if (playNowProc.running) playNowProc.running = false } }
  Timer { id: mixDeadline; interval: root.commandTimeout; onTriggered: { if (mixProc.running) mixProc.running = false } }
  Timer { id: queueDeadline; interval: root.commandTimeout; onTriggered: { if (queueProc.running) queueProc.running = false } }
  Timer { id: queueListDeadline; interval: root.commandTimeout; onTriggered: { if (queueListProc.running) queueListProc.running = false } }
  Timer { id: logoutDeadline; interval: root.commandTimeout; onTriggered: { if (logoutProc.running) logoutProc.running = false } }
  Timer { id: createDeadline; interval: root.commandTimeout; onTriggered: { if (createPlaylistProc.running) createPlaylistProc.running = false } }
  Timer { id: cmdDeadline; interval: root.commandTimeout; onTriggered: { if (cmdProc.running) cmdProc.running = false } }

  Process {
    id: thumbnailProc
    command: [root.ctlPath, "thumbnail", root.thumbnailVideoId]
    stdout: SplitParser { onRead: function(data) { root.appendProcessOutput("thumbnail", data) } }
    stderr: SplitParser { onRead: function(data) { root.appendProcessOutput("thumbnailErr", data) } }
    onStarted: thumbnailDeadline.start()
    onExited: function(exitCode) {
      thumbnailDeadline.stop()
      root.thumbnailSource = exitCode === 0 && root.isVideoId(root.thumbnailVideoId)
        ? "file://" + root.thumbnailPath(root.thumbnailVideoId)
        : ""
    }
  }

  Timer {
    id: thumbnailDeadline
    interval: root.commandTimeout
    onTriggered: { if (thumbnailProc.running) thumbnailProc.running = false }
  }

  function thumbnailPath(videoId) {
    return (Quickshell.env("XDG_CACHE_HOME") || (Quickshell.env("HOME") + "/.cache"))
      + "/yt-music/thumbs/" + videoId + ".jpg"
  }

  function loadThumbnail() {
    var id = root.musicStatus ? String(root.musicStatus.videoId || "") : ""
    id = root.isVideoId(id) ? id : ""
    if (id === root.thumbnailVideoId) return
    root.thumbnailVideoId = id
    root.thumbnailSource = ""
    if (root.thumbnailVideoId !== "") root.startProcess(thumbnailProc, "thumbnail")
  }

  onThumbnailVideoIdChanged: {
    if (root.thumbnailVideoId === "") root.thumbnailSource = ""
  }
  onMusicStatusChanged: {
    root.loadThumbnail()
    var key = String(root.musicStatus ? root.musicStatus.videoId : "")
      + ":" + String(root.musicStatus ? root.musicStatus.playlistPos : "")
    if (key !== root.queueKey) {
      root.queueKey = key
      root.refreshQueue()
    }
  }

  Timer {
    id: afterCommand
    interval: 1600
    onTriggered: { root.refresh(); root.refreshQueue() }
  }

  Timer {
    id: autoRefresh
    // Live updates come from the yt-music daemon via status.json; this is only a backstop.
    interval: 30000
    running: true
    repeat: true
    onTriggered: root.refresh()
  }

  Timer {
    id: searchDebounce
    interval: 450
    repeat: false
    onTriggered: root.search(searchField.text)
  }

  Timer {
    id: loginRefresh
    interval: 2500
    repeat: true
    running: false
    onTriggered: {
      if (root.loggedIn) {
        stop()
      } else {
        root.loadPlaylists()
      }
    }
  }

  Menu {
    id: contextMenu
  }

  Component {
    id: contextMenuItemComponent
    MenuItem {
      property var runAction: null
      onTriggered: { if (runAction) runAction() }
    }
  }

  Component {
    id: playlistMenuItemComponent
    MenuItem {
      property string playlistId: ""
      onTriggered: {
        if (playlistId !== "") root.sendCmd("playlist-add", [playlistId, root.contextVideoId])
      }
    }
  }

  Menu {
    id: playlistPickerMenu
  }

  Component.onCompleted: { root.loadThumbnail(); root.loadPlaylists() }

  // ---------------------------------------------------------------- surface

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
     contentWidth: 540
    contentHeight: panel.fittedContentHeight(contentColumn.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: searchField.activeFocus || newPlaylistField.activeFocus
      onCloseRequested: root.close()
      onMoveRequested: function(dx, dy) {
        if (dy !== 0) {
          var list = root.activeList
          if (list.length > 0) root.selectIndex(Math.max(-1, Math.min(list.length - 1, root.selectedIndex + dy)))
        } else if (dx !== 0) {
          root.sendCmd("seek", [dx > 0 ? "5" : "-5"])
        }
      }
      onActivateRequested: function() {
        if (root.activeListKind === "queue" && root.selectedIndex >= 0
            && root.selectedIndex < root.queueTracks.length) {
          root.queueJump(root.selectedIndex)
          return
        }
        var list = root.activeList
        if (root.selectedIndex >= 0 && root.selectedIndex < list.length) {
          var item = list[root.selectedIndex]
          if (item) root.playNow(item.videoId)
        } else {
          root.sendCmd("toggle", [])
        }
      }
      onDeleteRequested: function() {
        if (root.activeListKind === "queue" && root.selectedIndex >= 0
            && root.selectedIndex < root.queueTracks.length) {
          root.queueRemove(root.selectedIndex)
        } else if (root.playlistTracks.length > 0 && root.selectedIndex >= 0
            && root.selectedIndex < root.playlistTracks.length) {
          root.sendCmd("remove", [root.activePlaylistId, root.playlistTracks[root.selectedIndex].videoId])
        } else {
          root.clearSearch()
        }
      }
      onTextKey: function(t) {
        if (t === "c") root.close()
        else if (t === "s") root.sendCmd("stop", [])
        else if (t === "m") root.toggleMute()
        else if (t === "r") root.toggleLoop()
        else if (t === "f") root.sendCmd("shuffle", [])
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: contentColumn.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: contentColumn
          width: Math.min(panelFlick.width - Style.space(40), Style.space(540))
          x: Math.max(0, (panelFlick.width - width) / 2)
          spacing: Style.spacing.panelGap

          // ---- not logged in
          Rectangle {
            visible: !root.loggedIn
            width: parent.width
            height: Style.space(120)
            radius: Style.cornerRadius
            color: Style.normalFillFor(root.fg, Color.accent)

            Column {
              anchors.centerIn: parent
              width: parent.width - Style.space(64)
              spacing: Style.space(8)

              Text {
                textFormat: Text.PlainText
                anchors.horizontalCenter: parent.horizontalCenter
                text: Model.ICON.music
                color: root.fg
                font.family: root.fam
                font.pixelSize: Style.font.displayLarge
              }
              Text {
                textFormat: Text.PlainText
                anchors.horizontalCenter: parent.horizontalCenter
                text: root.loggedIn ? "" : "Not logged in"
                color: root.fg
                font.family: root.fam
                font.pixelSize: Style.font.heading
                font.bold: true
              }
              Text {
                textFormat: Text.PlainText
                anchors.horizontalCenter: parent.horizontalCenter
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                text: "Click below to open browser and log into YouTube Music.\nRun  yt-music-ctl login  in a terminal afterwards if needed."
                color: Qt.darker(root.fg, 1.4)
                font.family: root.fam
                font.pixelSize: Style.font.bodySmall
              }
              Button {
                anchors.horizontalCenter: parent.horizontalCenter
                height: Style.spacing.controlHeight
                iconText: Model.ICON.login
                text: "Login to YouTube Music"
                fontFamily: root.fam
                fontSize: Style.font.bodySmall
                foreground: root.fg
                onClicked: {
                  root.close()
                  loginRefresh.start()
                  if (root.bar) root.bar.run("omarchy-launch-terminal " + root.ctlPath + " login")
                }
              }
            }
          }

          // ---- now playing hero
            Rectangle {
              id: nowPlayingCard
              visible: Model.isActive(root.musicStatus)
              width: parent.width
              height: Style.space(136)
              radius: Style.cornerRadius
              color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.06)
              border.width: 1
              border.color: Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.45)

              Item {
              id: heroRow
              anchors.fill: parent

              MouseArea {
                anchors.fill: parent
                acceptedButtons: Qt.RightButton
                onClicked: function(mouse) {
                  if (!root.musicStatus || !root.isVideoId(root.musicStatus.videoId)) return
                  var point = heroRow.mapToItem(panelFlick, mouse.x, mouse.y)
                  root.openContextMenu(root.musicStatus.videoId, root.musicStatus.title, root.musicStatus.artist, "nowplaying", point.x, point.y)
                  mouse.accepted = true
                }
              }

              Rectangle {
                id: albumArt
                width: Style.space(96)
                height: Style.space(96)
                anchors.left: parent.left
                anchors.leftMargin: Style.space(8)
                anchors.verticalCenter: parent.verticalCenter
                radius: Style.cornerRadius
                color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.1)
                clip: true

                Image {
                  id: albumImage
                  anchors.fill: parent
                  source: root.thumbnailSource
                  fillMode: Image.PreserveAspectCrop
                  asynchronous: true
                  cache: true
                  sourceSize.width: 96
                  sourceSize.height: 96
                }

                Text {
                  anchors.centerIn: parent
                  visible: albumImage.status !== Image.Ready
                  text: Model.ICON.note
                  color: Color.accent
                  font.family: root.fam
                  font.pixelSize: Style.font.displayLarge
                }
              }

              Row {
                id: heroActions
                anchors.right: parent.right
                anchors.rightMargin: Style.space(14)
                anchors.bottom: parent.bottom
                anchors.bottomMargin: Style.space(20)
                spacing: Style.space(6)

                Item {
                  width: Style.space(28)
                  height: Style.space(32)
                  Text {
                    anchors.centerIn: parent
                    text: Model.ICON.prev
                    color: root.fg
                    font.family: root.fam
                    font.pixelSize: Style.font.body
                  }
                  MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.sendCmd("prev", [])
                  }
                }

                Item {
                  width: Style.space(28)
                  height: Style.space(32)
                  Text {
                    anchors.centerIn: parent
                    text: root.musicStatus && root.musicStatus.paused ? Model.ICON.play : Model.ICON.pause
                    color: root.fg
                    font.family: root.fam
                    font.pixelSize: Style.font.body
                  }
                  MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.sendCmd("toggle", [])
                  }
                }

                Item {
                  width: Style.space(28)
                  height: Style.space(32)
                  Text {
                    anchors.centerIn: parent
                    text: Model.ICON.next
                    color: root.fg
                    font.family: root.fam
                    font.pixelSize: Style.font.body
                  }
                  MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.sendCmd("next", [])
                  }
                }

                Item {
                  width: Style.space(28)
                  height: Style.space(32)
                  Text {
                    anchors.centerIn: parent
                    text: Model.ICON.like
                    color: Color.accent
                    font.family: root.fam
                    font.pixelSize: Style.font.body
                  }
                  MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.likeCurrent()
                  }
                }

                Item {
                  width: Style.space(28)
                  height: Style.space(32)
                  Text {
                    anchors.centerIn: parent
                    text: Model.ICON.dislike
                    color: root.fg
                    font.family: root.fam
                    font.pixelSize: Style.font.body
                  }
                  MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.dislikeCurrent()
                  }
                }

                Item {
                  width: Style.space(28)
                  height: Style.space(32)
                  Text {
                    anchors.centerIn: parent
                    text: Model.ICON.shuffle
                    color: root.fg
                    font.family: root.fam
                    font.pixelSize: Style.font.body
                  }
                  MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.sendCmd("shuffle", [])
                  }
                }

                Item {
                  width: Style.space(28)
                  height: Style.space(32)
                  Text {
                    anchors.centerIn: parent
                    text: Model.ICON.stop
                    color: root.fg
                    font.family: root.fam
                    font.pixelSize: Style.font.body
                  }
                  MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.sendCmd("stop", [])
                  }
                }

                Item {
                  width: Style.space(28)
                  height: Style.space(32)
                  Text {
                    anchors.centerIn: parent
                    text: Model.ICON.repeat
                    color: root.looping ? Color.accent : root.fg
                    font.family: root.fam
                    font.pixelSize: Style.font.body
                  }
                  MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.toggleLoop()
                  }
                }

                Item {
                  width: Style.space(28)
                  height: Style.space(32)
                  Text {
                    anchors.centerIn: parent
                    text: Model.ICON.volume
                    color: root.currentVolume === 0 ? Color.accent : Qt.darker(root.fg, 1.4)
                    font.family: root.fam
                    font.pixelSize: Style.font.body
                  }
                  MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.toggleMute()
                  }
                }
              }

              Text {
                anchors.left: albumArt.right
                anchors.leftMargin: Style.space(18)
                anchors.right: heroActions.left
                anchors.rightMargin: Style.space(12)
                anchors.verticalCenter: heroActions.verticalCenter
                textFormat: Text.PlainText
                elide: Text.ElideRight
                text: root.musicStatus
                  ? Model.fmtPosition(root.musicStatus.position || 0, root.musicStatus.duration || 0)
                  : ""
                color: Qt.darker(root.fg, 1.4)
                font.family: root.fam
                font.pixelSize: Style.font.caption
              }

              Column {
                anchors.left: albumArt.right
                anchors.leftMargin: Style.space(18)
                anchors.right: parent.right
                anchors.rightMargin: Style.space(18)
                anchors.top: parent.top
                anchors.topMargin: Style.space(18)
                anchors.bottom: heroActions.top
                anchors.bottomMargin: Style.space(4)
                spacing: Style.space(3)

                Text {
                  textFormat: Text.PlainText
                  text: "NOW PLAYING"
                  color: Color.accent
                  font.family: root.fam
                  font.pixelSize: Style.font.caption
                  font.bold: true
                }

                Text {
                  textFormat: Text.PlainText
                  width: parent.width
                  elide: Text.ElideRight
                  text: root.musicStatus ? (root.musicStatus.title || "") : ""
                  color: root.fg
                  font.family: root.fam
                  font.pixelSize: Style.font.heading
                  font.bold: true
                }
                Text {
                  textFormat: Text.PlainText
                  width: parent.width
                  elide: Text.ElideRight
                  text: root.musicStatus ? (root.musicStatus.artist || "") : ""
                  color: Qt.darker(root.fg, 1.4)
                  font.family: root.fam
                  font.pixelSize: Style.font.bodySmall
                }
              }

            }
          }

          // ---- seek
          Row {
            visible: Model.isActive(root.musicStatus)
            width: parent.width
            height: Style.spacing.controlHeight
            spacing: Style.spacing.lg

            Text {
              width: Style.space(56)
              height: parent.height
              verticalAlignment: Text.AlignVCenter
              textFormat: Text.PlainText
              text: seekSlider.dragging
                ? Model.fmtDuration(seekSlider.liveValue)
                : Model.fmtDuration(root.musicStatus ? (root.musicStatus.position || 0) : 0)
              color: root.fg
              font.family: root.fam
              font.pixelSize: Style.font.bodySmall
            }

            PanelSlider {
              id: seekSlider
              bar: root.bar
              anchors.verticalCenter: parent.verticalCenter
              width: parent.width - Style.space(56) - Style.space(64) - Style.spacing.lg * 2
              minimum: 0
              maximum: Math.max(1, Number(root.musicStatus && root.musicStatus.duration) || 1)
              value: Number(root.musicStatus && root.musicStatus.position) || 0
              step: 1
              integer: true
              tickCount: 0
              onReleased: function(v) {
                var duration = Number(root.musicStatus && root.musicStatus.duration) || 0
                if (duration > 0 && !root.busy)
                  root.sendCmd("seek-pct", [String(Math.round(v / duration * 100))])
              }
            }

            Text {
              width: Style.space(64)
              height: parent.height
              verticalAlignment: Text.AlignVCenter
              horizontalAlignment: Text.AlignRight
              textFormat: Text.PlainText
              text: Model.fmtDuration(Number(root.musicStatus && root.musicStatus.duration) || 0)
              color: root.fg
              font.family: root.fam
              font.pixelSize: Style.font.bodySmall
            }
          }

          // ---- volume
          Row {
            visible: Model.isActive(root.musicStatus)
            width: parent.width
            height: Style.spacing.controlHeight
            spacing: Style.spacing.lg

            Text {
              width: Style.space(120)
              height: parent.height
              verticalAlignment: Text.AlignVCenter
              text: Model.ICON.volume + "  Volume"
              color: root.fg
              font.family: root.fam
              font.pixelSize: Style.font.bodySmall
            }

            PanelSlider {
              id: volumeSlider
              bar: root.bar
              anchors.verticalCenter: parent.verticalCenter
              width: parent.width - Style.space(120) - Style.space(56) - Style.spacing.lg * 2
              minimum: 0
              maximum: 100
              integer: true
              step: 1
              tickCount: 11
              value: root.musicStatus && root.musicStatus.volume !== undefined
                ? root.musicStatus.volume
                : 100
              onReleased: function(v) {
                var target = Math.round(v)
                var current = root.musicStatus && root.musicStatus.volume !== undefined
                  ? root.musicStatus.volume
                  : 100
                if (target !== current && !root.busy) root.sendCmd("volume", [String(target)])
              }
            }

            Text {
              width: Style.space(56)
              height: parent.height
              verticalAlignment: Text.AlignVCenter
              horizontalAlignment: Text.AlignRight
              text: (volumeSlider.dragging ? Math.round(volumeSlider.liveValue) :
                (root.musicStatus && root.musicStatus.volume !== undefined
                  ? root.musicStatus.volume
                  : 100)) + "%"
              color: root.fg
              font.family: root.fam
              font.pixelSize: Style.font.body
            }
          }

          // ---- up next (queue)
          Column {
            visible: Model.isActive(root.musicStatus) && root.queueTracks.length > 0
            width: parent.width
            spacing: 0

            Item {
              width: parent.width
              height: Style.space(24)

              PanelSectionHeader {
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                text: "UP NEXT (" + root.queueTracks.length + ")"
                foreground: root.fg
                fontFamily: root.fam
              }

              Text {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                textFormat: Text.PlainText
                text: root.queueOpen ? "▾" : "▸"
                color: root.fg
                font.family: root.fam
                font.pixelSize: Style.font.bodySmall
              }

              MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: root.queueOpen = !root.queueOpen
              }
            }

            Column {
              visible: root.queueOpen
              width: parent.width
              spacing: 0

              Repeater {
                id: queueRepeater
                model: root.queueTracks
                delegate: Item {
                  id: queueRow
                  width: contentColumn.width
                  height: Style.space(32)

                  Rectangle {
                    anchors.fill: parent
                    color: index === root.selectedIndex
                      ? Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.18)
                      : "transparent"
                    radius: Style.cornerRadius
                  }

                  MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.selectIndex(index)
                  }

                  Row {
                    anchors.fill: parent
                    spacing: Style.spacing.sm

                    Text {
                      textFormat: Text.PlainText
                      width: Style.space(20)
                      text: (modelData.current || index === root.queuePosition)
                        ? Model.ICON.play
                        : String(index + 1)
                      color: (modelData.current || index === root.queuePosition)
                        ? Color.accent
                        : Qt.darker(root.fg, 1.4)
                      font.family: root.fam
                      font.pixelSize: Style.font.caption
                      verticalAlignment: Text.AlignVCenter
                    }

                    Column {
                      width: parent.width - Style.space(20) - Style.space(56)
                        - Style.space(44) - Style.spacing.sm * 3
                      spacing: 0

                      Text {
                        textFormat: Text.PlainText
                        width: parent.width
                        elide: Text.ElideRight
                        text: modelData.title || "Unknown"
                        color: root.fg
                        font.family: root.fam
                        font.pixelSize: Style.font.bodySmall
                      }
                      Text {
                        textFormat: Text.PlainText
                        width: parent.width
                        elide: Text.ElideRight
                        text: modelData.artist
                        color: Qt.darker(root.fg, 1.4)
                        font.family: root.fam
                        font.pixelSize: Style.font.caption
                      }
                    }

                    Text {
                      textFormat: Text.PlainText
                      width: Style.space(56)
                      text: modelData.duration > 0 ? Model.fmtDuration(modelData.duration) : ""
                      horizontalAlignment: Text.AlignRight
                      color: Qt.darker(root.fg, 1.4)
                      font.family: root.fam
                      font.pixelSize: Style.font.caption
                      verticalAlignment: Text.AlignVCenter
                    }

                    Button {
                      width: Style.space(44)
                      height: Style.space(28)
                      iconText: Model.ICON.play
                      tooltipText: "Jump to track"
                      fontFamily: root.fam
                      foreground: root.fg
                      enabled: !root.busy
                      onClicked: root.queueJump(index)
                    }
                  }

                  MouseArea {
                    anchors.fill: parent
                    acceptedButtons: Qt.RightButton
                    onClicked: function(mouse) {
                      var point = queueRow.mapToItem(panelFlick, mouse.x, mouse.y)
                      root.openContextMenu(modelData.videoId, modelData.title, modelData.artist, "queue", point.x, point.y, index)
                      mouse.accepted = true
                    }
                  }
                }
              }
            }
          }

            // ---- search
            Item {
              width: parent.width
              height: Style.spacing.controlHeight

              Row {
                width: parent.width - Style.space(40)
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: Style.spacing.sm

                TextField {
                  id: searchField
                  width: parent.width - Style.space(132) - Style.spacing.sm * 3
                  height: Style.spacing.controlHeight
                  placeholderText: "Lookup tunes..."
                  horizontalAlignment: Text.AlignHCenter
                  foreground: root.fg
                  hasCursor: false
                  onTextChanged: {
                    var query = text.trim()
                    root.searchQuery = query
                    root.searchResults = []
                    root.searching = query !== ""
                    if (query === "") {
                      searchDebounce.stop()
                    } else {
                      searchDebounce.restart()
                    }
                  }
                  onAccepted: root.search(text)
                }
                Button {
                  width: Style.space(44)
                  height: Style.spacing.controlHeight
                  iconText: Model.ICON.search
                  tooltipText: "Search"
                  fontFamily: root.fam
                  foreground: root.fg
                  enabled: !root.busy
                  onClicked: root.search(searchField.text)
                }
                Button {
                  width: Style.space(44)
                  height: Style.spacing.controlHeight
                  iconText: Model.ICON.close
                  tooltipText: "Clear lookup"
                  fontFamily: root.fam
                  foreground: root.fg
                  visible: searchField.text !== "" || root.searchQuery !== "" || root.searching || root.searchResults.length > 0
                  onClicked: root.clearSearch()
                }
                Button {
                  width: Style.space(44)
                  height: Style.spacing.controlHeight
                  iconText: Model.ICON.logout
                  tooltipText: "Log out"
                  fontFamily: root.fam
                  foreground: root.fg
                  visible: root.loggedIn
                  enabled: !root.busy
                  onClicked: root.logout()
                }
              }
            }

          // ---- search results
          Column {
            visible: root.searchResults.length > 0 || root.searching
            width: parent.width
            spacing: Style.spacing.panelGap

            PanelSectionHeader {
              text: "SEARCH RESULTS — " + root.searchQuery.toUpperCase()
              foreground: root.fg
              fontFamily: root.fam
            }

            Text {
              visible: root.searching
              textFormat: Text.PlainText
              text: "Searching…"
              color: Qt.darker(root.fg, 1.4)
              font.family: root.fam
              font.pixelSize: Style.font.bodySmall
            }

            Repeater {
              id: searchRepeater
              model: root.searchResults
              delegate: Item {
                id: searchRow
                width: contentColumn.width
                height: Style.space(40)

                Rectangle {
                  anchors.fill: parent
                  color: index === root.selectedIndex
                    ? Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.18)
                    : "transparent"
                  radius: Style.cornerRadius
                }

                MouseArea {
                  anchors.fill: parent
                  cursorShape: Qt.PointingHandCursor
                  onClicked: root.selectedIndex = index
                }

                Row {
                  anchors.fill: parent
                  spacing: Style.spacing.sm

                  Text {
                    textFormat: Text.PlainText
                    width: Style.space(24)
                    text: Model.ICON.note
                    color: Color.accent
                    font.family: root.fam
                    font.pixelSize: Style.font.bodySmall
                    verticalAlignment: Text.AlignVCenter
                  }

                  Column {
                    width: parent.width - Style.space(24) - Style.space(180)
                    spacing: 0

                    Text {
                      textFormat: Text.PlainText
                      width: parent.width
                      elide: Text.ElideRight
                      text: modelData.title
                      color: root.fg
                      font.family: root.fam
                      font.pixelSize: Style.font.bodySmall
                    }
                    Text {
                      textFormat: Text.PlainText
                      width: parent.width
                      elide: Text.ElideRight
                      text: modelData.artist
                      color: Qt.darker(root.fg, 1.4)
                      font.family: root.fam
                      font.pixelSize: Style.font.caption
                    }
                  }

                  Text {
                    textFormat: Text.PlainText
                    width: Style.space(80)
                    text: Model.fmtDuration(modelData.duration)
                    horizontalAlignment: Text.AlignRight
                    color: Qt.darker(root.fg, 1.4)
                    font.family: root.fam
                    font.pixelSize: Style.font.caption
                    verticalAlignment: Text.AlignVCenter
                  }

                  Button {
                    width: Style.space(52)
                    height: Style.space(28)
                    iconText: Model.ICON.play
                    tooltipText: "Play"
                    fontFamily: root.fam
                    foreground: root.fg
                    enabled: !root.busy
                    onClicked: root.playNow(modelData.videoId)
                  }

                  Button {
                    width: Style.space(44)
                    height: Style.space(28)
                    iconText: Model.ICON.shuffle
                    tooltipText: "Start mix"
                    fontFamily: root.fam
                    foreground: root.fg
                    enabled: !root.busy
                    onClicked: root.playMix(modelData.videoId)
                  }
                }

                MouseArea {
                  anchors.fill: parent
                  acceptedButtons: Qt.RightButton
                  onClicked: function(mouse) {
                    var point = searchRow.mapToItem(panelFlick, mouse.x, mouse.y)
                    root.openContextMenu(modelData.videoId, modelData.title, modelData.artist, "search", point.x, point.y)
                    mouse.accepted = true
                  }
                }
              }
            }
          }

          // ---- playlists
          Column {
            visible: root.loggedIn
            width: parent.width
            spacing: Style.spacing.panelGap

            Item {
              width: parent.width
              height: Style.space(24)

              PanelSectionHeader {
                anchors.centerIn: parent
                text: "PLAYLISTS"
                foreground: root.fg
                fontFamily: root.fam
              }
            }

            Row {
              width: parent.width - Style.space(40)
              height: Style.spacing.controlHeight
              anchors.horizontalCenter: parent.horizontalCenter
              spacing: Style.spacing.sm

              TextField {
                id: newPlaylistField
                width: parent.width - Style.space(116) - parent.spacing
                height: Style.spacing.controlHeight
                placeholderText: "New playlist name"
                foreground: root.fg
                hasCursor: false
                onTextChanged: root.newPlaylistName = text
                onAccepted: root.createPlaylist()
              }

              Button {
                width: Style.space(116)
                height: Style.spacing.controlHeight
                text: "Create"
                iconText: Model.ICON.plus
                fontFamily: root.fam
                fontSize: Style.font.bodySmall
                foreground: root.fg
                enabled: root.newPlaylistName.trim() !== "" && !root.busy
                onClicked: root.createPlaylist()
              }
            }

            Item {
              width: parent.width
              height: Style.spacing.controlHeight

              Dropdown {
                width: parent.width - Style.space(40)
                anchors.horizontalCenter: parent.horizontalCenter
                label: ""
                showLabel: false
                options: root.playlistOptions
                value: root.activePlaylistId
                foreground: root.fg
                fontFamily: root.fam
                onChanged: function(selectedValue) { root.selectPlaylist(selectedValue) }
              }
            }
           }

            // ---- playlist tracks view
          Column {
            visible: root.playlistTracks.length > 0
            width: parent.width
            spacing: Style.spacing.panelGap

            Item {
              width: parent.width
              height: Style.space(24)

              Text {
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                textFormat: Text.PlainText
                text: root.activePlaylistTitle.toUpperCase() + " (" + root.playlistTracks.length + ")"
                color: root.fg
                font.family: root.fam
                font.pixelSize: Style.font.caption
                font.bold: true
                verticalAlignment: Text.AlignVCenter
              }

              Button {
                id: playPlaylistButton
                anchors.right: closePlaylistButton.left
                anchors.rightMargin: Style.space(4)
                anchors.verticalCenter: parent.verticalCenter
                width: Style.space(32)
                height: Style.space(24)
                iconText: Model.ICON.play
                tooltipText: "Play playlist"
                fontFamily: root.fam
                foreground: Color.accent
                onClicked: root.playSelectedPlaylist()
              }

              Button {
                id: closePlaylistButton
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                width: Style.space(32)
                height: Style.space(24)
                iconText: Model.ICON.close
                tooltipText: "Close playlist"
                fontFamily: root.fam
                foreground: root.fg
                onClicked: root.closePlaylist()
              }
            }

            Repeater {
              id: trackRepeater
              model: root.playlistTracks
              delegate: Item {
                id: trackRow
                width: contentColumn.width
                height: Style.space(36)

                Rectangle {
                  anchors.fill: parent
                  color: index === root.selectedIndex
                    ? Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.18)
                    : "transparent"
                  radius: Style.cornerRadius
                }

                MouseArea {
                  anchors.fill: parent
                  cursorShape: Qt.PointingHandCursor
                  onClicked: root.selectedIndex = index
                }

                Row {
                  anchors.fill: parent
                  spacing: Style.spacing.sm

                  Text {
                    textFormat: Text.PlainText
                    width: Style.space(20)
                    text: index + 1
                    color: Qt.darker(root.fg, 1.4)
                    font.family: root.fam
                    font.pixelSize: Style.font.caption
                    verticalAlignment: Text.AlignVCenter
                  }

                  Column {
                    width: parent.width - Style.space(20) - Style.space(200)
                    spacing: 0

                    Text {
                      textFormat: Text.PlainText
                      width: parent.width
                      elide: Text.ElideRight
                      text: modelData.title
                      color: root.fg
                      font.family: root.fam
                      font.pixelSize: Style.font.bodySmall
                    }
                    Text {
                      textFormat: Text.PlainText
                      width: parent.width
                      elide: Text.ElideRight
                      text: modelData.artist
                      color: Qt.darker(root.fg, 1.4)
                      font.family: root.fam
                      font.pixelSize: Style.font.caption
                    }
                  }

                  Text {
                    textFormat: Text.PlainText
                    width: Style.space(64)
                    text: Model.fmtDuration(modelData.duration)
                    horizontalAlignment: Text.AlignRight
                    color: Qt.darker(root.fg, 1.4)
                    font.family: root.fam
                    font.pixelSize: Style.font.caption
                    verticalAlignment: Text.AlignVCenter
                  }

                  Button {
                    width: Style.space(52)
                    height: Style.space(28)
                    iconText: Model.ICON.play
                    tooltipText: "Play"
                    fontFamily: root.fam
                    foreground: root.fg
                    enabled: !root.busy
                    onClicked: root.playNow(modelData.videoId)
                  }

                  Button {
                    width: Style.space(44)
                    height: Style.space(28)
                    iconText: Model.ICON.shuffle
                    tooltipText: "Start mix"
                    fontFamily: root.fam
                    foreground: root.fg
                    enabled: !root.busy
                    onClicked: root.playMix(modelData.videoId)
                  }
                }

                MouseArea {
                  anchors.fill: parent
                  acceptedButtons: Qt.RightButton
                  onClicked: function(mouse) {
                    var point = trackRow.mapToItem(panelFlick, mouse.x, mouse.y)
                    root.openContextMenu(modelData.videoId, modelData.title, modelData.artist, "track", point.x, point.y)
                    mouse.accepted = true
                  }
                }
              }
            }
          }

        }
      }
    }
  }
}
