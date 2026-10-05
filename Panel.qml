import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model
import "AsyncState.js" as AsyncState

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
  // True while the shared mpv pipeline is playing an internet radio stream
  // (status.source === "radio"). The Hero and transport branch on it.
  readonly property bool radioLive: !!(root.musicStatus && root.musicStatus.live)

  property bool openedFromHotkey: false
  property bool busy: false
  property bool refreshing: false
  property string statusText: ""

  // Audio visualizer: cava's raw ASCII frames drive a flat bar strip below the
  // now-playing Hero. `visualizerOn` is the user's toggle and is persisted in
  // ui-state.json; `visualizerBars` holds the latest frame's 0..100 heights.
  property var visualizerBars: []
  property bool visualizerOn: true
  property string visualizerStyle: "bars"
  // Display-only cava settings for every visualizer style: mirrored stereo vs
  // left-to-right mono, and linear vs decibel scaling.
  property string visualizerChannels: "stereo"
  property string visualizerScaling: "linear"
  // True once the runtime cava config has been written, so cavaProc does not
  // start against a missing file.
  property bool cavaReady: false
  property bool cavaWritePending: false
  // Latest frame's peak (0..100) and its decaying peak-hold, driving the VU
  // ladders.
  property real visualizerLevel: 0
  property real visualizerPeak: 0
  property var visualizerBandPeaks: []
  property var visualizerSpectrumBars: []
  // Single display gain applied on top of the volume scale, so a full-volume
  // frame fills the meters instead of topping out around half.
  readonly property real visualizerGain: 1.25
  readonly property int visualizerBarCount: 24
  readonly property int visualizerLadderSegments: 28
  readonly property int visualizerVerticalSegments: 20
  readonly property int visualizerSpectrumSegments: 10
  readonly property string cavaRuntimePath: Quickshell.env("HOME") + "/.local/state/yt-music/cava-runtime.conf"
  onVisualizerChannelsChanged: root.writeCavaConfig()

  readonly property string ctlPath: Quickshell.env("HOME") + "/.local/bin/yt-music-ctl"
  readonly property color fg: root.barForeground
  readonly property string fam: root.bar ? root.bar.fontFamily : Style.font.family

  property bool loggedIn: false
  property int loginPollCount: 0
  property var playlists: []
  readonly property var playlistOptions: root.playlists.map(function(playlist) {
    return { value: playlist.id, label: playlist.title }
  })
  property var playlistTracks: []
  property string activePlaylistTitle: ""
  property string activePlaylistId: ""
  readonly property var tracksTokens: AsyncState.makeTokenSource()
  property var pendingPlaylistOpen: null
  property var searchResults: []
  property string searchQuery: ""
  property string searchFilter: "songs"
  property bool searching: false
  readonly property var pendingSearch: AsyncState.makePendingQueue()
  property int selectedIndex: -1
  // Multi-select over the active list (search results / up-next queue): keys
  // are Model.rowKey(row) values and the value is the row itself, so the
  // count/token helpers stay pure JS.
  property bool selectMode: false
  property var selectedKeys: ({})
  property int selectionAnchor: -1
  property var playlistAddTokens: []
  readonly property int selectedCount: root.selectedRows.length
  readonly property var selectedRows: {
    var out = []
    for (var i = 0; i < root.activeList.length; i++) {
      var row = root.activeList[i]
      if (row && root.selectedKeys[Model.rowKey(row)] !== undefined) out.push(row)
    }
    return out
  }
  readonly property bool selectionAllSongs: Model.allSongs(root.selectedRows)
  // Playlist management (H3): inline rename row, queue-save row, the delete
  // confirmation dialog, the tokens staged between create + add-items, and the
  // playlist index behind the current right-click menu.
  property bool renameOpen: false
  property bool queueSaveOpen: false
  property bool deleteConfirmOpen: false
  property var pendingPlaylistTokens: []
  // What the staged playlist-add selection could not contribute, so the
  // result message can explain it: rows with nothing to add (local files)
  // and repeated selections of the same track.
  property int playlistAddUnaddable: 0
  property int playlistAddDuplicateSelections: 0
  // videoId -> title for the staged rows, so the result message can name the
  // tracks YouTube would not add instead of only counting them.
  property var playlistAddLabels: ({})
  property int contextTrackIndex: -1
  property int lastVolume: 100
  // Coalesced volume target while dragging or wheel-scrolling; volumeApply
  // sends it once the input settles.
  property real volumePending: 100
  readonly property int currentVolume: root.musicStatus && root.musicStatus.volume !== undefined
    ? Math.round(Number(root.musicStatus.volume))
    : 100
  property var queueTracks: []
  // Local play history (newest first) from `yt-music-ctl last-played`.
  property var lastPlayed: []
  // Internet radio: a bundled curated catalog, a locally saved favorites list
  // and a directory search, all filled from the backend's `station-*`
  // commands. `stationRows` is the list the Stations tab renders for the
  // current section.
  property string stationSection: "featured"
  property string stationQuery: ""
  property string stationSearchMode: "name"
  property var stationResults: []
  property var stationFavorites: []
  property var stationCatalog: []
  readonly property var stationRows: root.stationSection === "search"
    ? root.stationResults
    : (root.stationSection === "featured" ? root.stationCatalog : root.stationFavorites)
  property bool stationBusy: false
  readonly property var pendingStationSearch: AsyncState.makePendingQueue()
  // True while a station search is in flight or debouncing; drives the
  // Searching…/empty states so a replaced query never flashes "No stations
  // found." in the gap before the next request starts.
  property bool stationSearching: false
  // Set when a favourites refresh is skipped because one is already running,
  // so the in-flight fetch replays it on exit instead of dropping the update.
  readonly property var stationFavoritesDirty: AsyncState.makeDirtyFlag()
  // Same coalescing for the featured catalog: an add/remove that lands mid-fetch
  // is replayed when the in-flight catalog load exits.
  readonly property var stationCatalogDirty: AsyncState.makeDirtyFlag()
  // Optimistic favourite overrides (id -> bool) so the star flips on click.
  // Cleared when the authoritative favourites list reloads.
  property var stationFavOverrides: ({})
  property int queuePosition: -1
  property string contextQueueKey: ""
  property string queueKey: ""
  property string libraryKind: ""
  property string libraryTitle: ""
  property string librarySubtitle: ""
  property var libraryRows: []
  property string libraryRefId: ""
  readonly property var libraryTokens: AsyncState.makeTokenSource()
  // Browse-level stack: opening an album/artist from a library list (or an
  // artist's similar-artists list) remembers the level you came from, so Back
  // returns there instead of dropping straight to the Home/Recent/… pills.
  property var libraryParents: []
  // A detail (album/artist/playlist) opens in place under the tab it was
  // opened from, so the tab bar always reflects what the user is browsing.
  // detailTab is that tab; empty means no detail is open.
  property string detailTab: ""
  readonly property bool libraryDetail: root.libraryKind === "album" || root.libraryKind === "artist"
  readonly property bool playlistDetail: root.activePlaylistId !== ""
  readonly property bool detailActive: root.detailTab !== ""
  // Top-level content tab: "search" | "playlists" | "library" | "queue".
  // "" means every section is collapsed (clicking the active tab again hides
  // it). Collapsing is deliberately NOT persisted: saveUiState() maps "" back
  // to "search". Replaces the old libraryExpanded / playlistsExpanded flags.
  property string activeTab: "search"
  property bool libraryStale: false
  property string libraryThumbUrl: ""
  property string libraryImageSource: ""
  property string libraryMeta: ""
  property string libraryDescription: ""
  property bool libraryInfoOpen: false
  property bool albumInLibrary: false
  property bool libraryDirty: false
  property string albumStatusRefId: ""
  property var albumCmdQueue: []
  property bool albumCmdRunning: false
  property string albumCmdCurrentId: ""
  property string albumCmdCurrentAction: ""
  readonly property bool libraryRichHeader: root.libraryDetail
    && (root.libraryKind === "album" || root.libraryKind === "artist")
    && (root.libraryImageSource !== "" || root.libraryThumbUrl !== ""
      || root.libraryMeta !== "" || root.libraryDescription !== "")
  readonly property var libraryList: root.libraryRows
  property bool queueSaved: false
  // Max height available to the active tab's scroll viewport: the panel's
  // usable height minus the fixed chrome above it (now-playing card, lyrics
  // section and tab strip). tabFlick.y comes from the Column layout and does
  // not depend on the Flickable's own height, so this is loop-free.
  readonly property real tabBodyMaxHeight: {
    var avail = panel.availableCardHeight - panel.verticalContentInset
      - (tabFlick ? tabFlick.y : 0)
    if (!isFinite(avail) || avail < Style.space(96)) avail = Style.space(96)
    return avail
  }
  // Track shown in the hero card: the playing track, or the most recent
  // last-played one when nothing is playing.
  readonly property var heroTrack: Model.isActive(root.musicStatus)
    ? ({ videoId: root.musicStatus ? String(root.musicStatus.videoId || "") : "",
         title: root.musicStatus ? String(root.musicStatus.title || "") : "",
         artist: root.musicStatus ? String(root.musicStatus.artist || "") : "" })
    : (root.lastPlayed.length > 0 ? root.lastPlayed[0] : null)
  readonly property var tabItems: {
    var items = []
    items.push({ key: "queue", label: root.queueTracks.length > 0
        ? "Next (" + root.queueTracks.length + ")" : "Next" })
    items.push({ key: "search", label: "Search" })
    items.push({ key: "last", label: "History" })
    if (root.loggedIn) {
      items.push({ key: "playlists", label: "Playlists" })
      items.push({ key: "library", label: "Library" })
    }
    // Radio needs no YouTube login, so Stations is always visible and last.
    items.push({ key: "stations", label: "Stations" })
    return items
  }
  readonly property string activeListKind: root.libraryDetail
      ? (root.libraryList.length > 0 ? "library" : "")
    : root.playlistDetail
      ? (root.playlistTracks.length > 0 ? "playlist" : "")
    : root.activeTab === "" ? ""
    : root.activeTab === "queue" ? "queue"
    : root.activeTab === "search" ? (root.searchResults.length > 0 ? "search" : "")
    : root.activeTab === "last" ? (root.lastPlayed.length > 0 ? "last" : "")
    : root.activeTab === "playlists" ? (root.playlistTracks.length > 0 ? "playlist" : "")
    : root.activeTab === "stations" ? ""
    : (root.libraryList.length > 0 ? "library" : "")
  readonly property var activeList: root.activeListKind === "search" ? root.searchResults
    : (root.activeListKind === "last" ? root.lastPlayed
    : (root.activeListKind === "playlist" ? root.playlistTracks
    : (root.activeListKind === "library" ? root.libraryList
    : (root.activeListKind === "queue" ? root.queueTracks : []))))
  readonly property bool looping: !!(root.musicStatus && root.musicStatus.loop
    && root.musicStatus.loop !== "no")
  readonly property bool shuffling: !!(root.musicStatus && root.musicStatus.shuffle)
  onSearchResultsChanged: {
    var rows = root.searchResults
    if (rows.length === 0) {
      root.selectedIndex = -1
      root.clearSelection()
      return
    }
    // A refreshed result list (stale-while-revalidate, repeat search) must not
    // discard an in-progress selection: keep only rows that are still present.
    var keep = {}
    for (var i = 0; i < rows.length; i++) {
      var k = Model.rowKey(rows[i])
      if (root.selectedKeys[k] !== undefined) keep[k] = rows[i]
    }
    root.selectedKeys = keep
    if (Object.keys(keep).length > 0) {
      if (root.selectedIndex >= rows.length) root.selectedIndex = rows.length - 1
    } else {
      root.selectMode = false
      root.selectionAnchor = -1
      root.selectedIndex = -1
    }
  }
  onActiveTabChanged: {
    root.clearSelection()
    // A detail belongs to the tab it was opened from; leaving that tab shows
    // the tab's own content instead of a stale detail.
    if (root.detailActive && root.activeTab !== root.detailTab) root.closeDetail()
    // Opening Up Next should land on the playing row, not the top of the queue.
    if (root.activeTab === "queue") queueScrollTimer.restart()
    if (root.activeTab === "stations") {
      root.refreshStationFavorites()
      root.refreshStationCatalog()
      if (root.stationSection === "search" && root.stationQuery !== "")
        root.searchStations(root.stationQuery)
    }
  }
  onActiveListKindChanged: root.selectedIndex = -1
  function hasTab(key) {
    for (var i = 0; i < root.tabItems.length; i++)
      if (root.tabItems[i].key === key) return true
    return false
  }
  // Never let the active tab point at a hidden section, and never allow the
  // all-collapsed state when only one tab exists (its strip would be hidden,
  // leaving no way to reopen anything).
  function clampActiveTab() {
    if (root.tabItems.length <= 1) {
      if (root.tabItems.length === 1 && root.activeTab !== root.tabItems[0].key)
        root.activeTab = root.tabItems[0].key
      return
    }
    if (root.activeTab !== "" && !root.hasTab(root.activeTab)) root.activeTab = "search"
  }
  onTabItemsChanged: root.clampActiveTab()
  property var likedVideoIds: ({})
  property string newPlaylistName: ""
  readonly property int maxProcessOutput: 65536
  readonly property int commandTimeout: 15000
  property var processOutput: ({})
  property string thumbnailSource: ""
  property string thumbnailVideoId: ""
  property int thumbnailRetries: 0
  property string loadingText: ""

  property bool stateRestored: false

  property bool lyricsOpen: false
  property bool lyricsLoading: false
  property bool lyricsHas: false
  property string lyricsText: ""
  property string lyricsVideoId: ""
  property var lyricsSynced: null

  property string contextVideoId: ""
  property string contextTitle: ""
  property string contextArtist: ""
  property string contextSource: ""
  property real contextX: 0
  property real contextY: 0
  property var contextRow: null
  // The station row whose right-click menu is open.
  property var contextStation: null

  function open() {
    statusText = ""
    root.controller.show()
    root.refresh()
    root.refreshQueue()
    root.refreshLastPlayed()
    root.refreshStationFavorites()
    root.refreshStationCatalog()
    root.restoreSession()
  }

  function openFromHotkey() {
    root.openedFromHotkey = true
    root.open()
  }

  function close() {
    loginRefresh.stop()
    root.controller.hide()
    root.saveUiState()
  }

  function visualizerStyleNext() {
    return root.visualizerStyle === "bars" ? "mirror"
      : (root.visualizerStyle === "mirror" ? "gradient"
      : (root.visualizerStyle === "gradient" ? "meter"
      : (root.visualizerStyle === "meter" ? "vu"
      : (root.visualizerStyle === "vu" ? "vuv"
      : (root.visualizerStyle === "vuv" ? "vus"
      : (root.visualizerStyle === "vus" ? "dots" : "bars"))))))
  }

  function visualizerStyleLabel() {
    return root.visualizerStyle === "bars" ? "Bars"
      : (root.visualizerStyle === "mirror" ? "Mirror"
      : (root.visualizerStyle === "gradient" ? "Gradient"
      : (root.visualizerStyle === "meter" ? "Meter"
      : (root.visualizerStyle === "vu" ? "VU"
      : (root.visualizerStyle === "vuv" ? "VU Vertical"
      : (root.visualizerStyle === "vus" ? "VU Spectrum"
      : (root.visualizerStyle === "dots" ? "VU Dots" : "Bars")))))))
  }

  function visualizerBarColor(level) {
    var start = Color.accent
    var end = Color.urgent
    var t = Math.max(0, Math.min(100, Number(level) || 0)) / 100
    return Qt.rgba(start.r + (end.r - start.r) * t,
                   start.g + (end.g - start.g) * t,
                   start.b + (end.b - start.b) * t, 1)
  }

  function visualizerZoneColor(level) {
    var z = Model.cavaZone(level)
    return z === "red" ? "#e05555" : (z === "amber" ? "#e8c34a" : "#3bd66b")
  }

  function writeCavaConfig() {
    if (cavaWriteProc.running) {
      root.cavaWritePending = true
      return
    }
    cavaWriteProc.running = true
  }

  function uiStateScript(mode) {
    if (mode === "save") {
      return "import json,sys,os\n" +
        "try:\n" +
        " p=os.path.expanduser('~/.local/state/yt-music/ui-state.json')\n" +
        " os.makedirs(os.path.dirname(p),exist_ok=True)\n" +
        " d={'libraryKind':sys.argv[1],'libraryRefId':sys.argv[2],"
        + "'activeTab':sys.argv[3],'searchFilter':sys.argv[4],"
        + "'stationSection':sys.argv[5],'visualizerOn':sys.argv[6],"
        + "'visualizerStyle':sys.argv[7],'visualizerChannels':sys.argv[8],"
        + "'visualizerScaling':sys.argv[9]}\n" +
        " t=p+'.tmp'\n" +
        " f=open(t,'w')\n" +
        " f.write(json.dumps(d))\n" +
        " f.close()\n" +
        " os.chmod(t,0o600)\n" +
        " os.replace(t,p)\n" +
        "except Exception:\n" +
        " pass"
    }
    return "import json,os,sys\n" +
      "try:\n" +
      " f=open(os.path.expanduser('~/.local/state/yt-music/ui-state.json'))\n" +
      " d=json.load(f)\n" +
      " f.close()\n" +
      " sys.stdout.write(json.dumps(d if isinstance(d,dict) else {}))\n" +
      "except Exception:\n" +
      " sys.stdout.write('{}')"
  }

  function saveUiState() {
    if (uiSaveProc.running) return
    uiSaveProc.command = ["python3", "-c", root.uiStateScript("save"),
      root.libraryKind, root.libraryRefId,
      (root.activeTab === "") ? "search" : root.activeTab,
      root.searchFilter, root.stationSection,
      root.visualizerOn ? "1" : "0", root.visualizerStyle,
      root.visualizerChannels, root.visualizerScaling]
    root.startProcess(uiSaveProc, "uiSave")
  }

  function restoreUiState() {
    if (root.stateRestored || uiLoadProc.running) return
    uiLoadProc.command = ["python3", "-c", root.uiStateScript("load")]
    root.startProcess(uiLoadProc, "uiLoad")
  }

  function applyUiState(data) {
    if (root.stateRestored) return
    root.stateRestored = true
    root.libraryParents = []
    if (!data || typeof data !== "object") return
    var filter = String(data.searchFilter || "")
    if (filter === "songs" || filter === "albums" || filter === "artists"
        || filter === "playlists")
      root.searchFilter = filter
    var stationSection = String(data.stationSection || "")
    if (stationSection === "featured" || stationSection === "favorites"
        || stationSection === "search")
      root.stationSection = stationSection
    if (data.visualizerOn !== undefined)
      root.visualizerOn = data.visualizerOn === true || data.visualizerOn === 1
        || data.visualizerOn === "1"
    var visualizerStyle = String(data.visualizerStyle || "")
    if (visualizerStyle === "bars" || visualizerStyle === "mirror"
        || visualizerStyle === "gradient" || visualizerStyle === "meter"
        || visualizerStyle === "vu" || visualizerStyle === "vuv"
        || visualizerStyle === "vus" || visualizerStyle === "dots")
      root.visualizerStyle = visualizerStyle
    var visualizerChannels = String(data.visualizerChannels || "")
    if (visualizerChannels === "mono" || visualizerChannels === "stereo")
      root.visualizerChannels = visualizerChannels
    var visualizerScaling = String(data.visualizerScaling || "")
    if (visualizerScaling === "linear" || visualizerScaling === "decibel")
      root.visualizerScaling = visualizerScaling
    var kind = String(data.libraryKind || "")
    var refId = String(data.libraryRefId || "")
    var restored = false
    if (kind === "album" || kind === "artist") {
      if (refId !== "") {
        if (kind === "album") root.openAlbum(refId, "")
        else root.openArtist(refId, "")
        restored = true
      }
    } else if (kind === "home" || kind === "history" || kind === "liked"
               || kind === "songs" || kind === "albums" || kind === "artists") {
      root.loadLibrary(kind)
      restored = true
    }
    var tab = String(data.activeTab || "")
    // Back-compat: pre-tab state files only stored a libraryExpanded boolean.
    if (tab !== "search" && tab !== "last" && tab !== "playlists"
        && tab !== "library" && tab !== "queue" && tab !== "stations")
      tab = (data.libraryExpanded === true && restored) ? "library" : "search"
    // A restored detail must be hosted by the restored tab, otherwise
    // onActiveTabChanged would close it right away.
    if (kind === "album" || kind === "artist") root.detailTab = tab
    root.activeTab = tab
  }

  function toggle() {
    root.opened ? root.close() : root.openFromHotkey()
  }

  function refresh() {
    if (statusProc.running) return
    root.refreshing = true
    root.startProcess(statusProc, "status")
  }

  function refreshQueue() {
    if (!root.opened) return
    if (queueListProc.running) return
    root.startProcess(queueListProc, "queueList")
  }

  function refreshLastPlayed() {
    if (!root.opened) return
    if (lastPlayedProc.running) return
    root.startProcess(lastPlayedProc, "lastPlayed")
  }

  function clearLastPlayed() {
    if (lastPlayedClearProc.running) return
    root.startProcess(lastPlayedClearProc, "lastPlayedClear")
  }

  function clearQueue() {
    if (queueClearProc.running) return
    root.startProcess(queueClearProc, "queueClear")
  }

  function queueUpcomingCount() {
    return Model.queueUpcomingCount(root.queueTracks, root.queuePosition)
  }

  // Index of the currently-playing/queued row, or -1. Prefers mpv's reported
  // position, falling back to the row flagged `current` from the backend so the
  // saved-queue (nothing playing) view works too.
  function queueCurrentIndex() {
    return Model.queueCurrentIndex(root.queueTracks, root.queuePosition)
  }

  // Bring the current queue row into view so the top of the Up Next tab is not
  // mistaken for the next track (the list holds the whole queue, played rows
  // included). When the row is off screen it is placed with one row of context
  // above it, so the track after it - the real "next" - is visible too. The
  // view is left alone when the current row is already on screen.
  function scrollQueueToCurrent() {
    if (root.activeTab !== "queue") return
    var index = root.queueCurrentIndex()
    if (index < 0) return
    var item = queueRepeater.itemAt(index)
    if (!item) return
    var flick = queueListFlick
    var pt = item.mapToItem(flick, 0, 0)
    if (pt.y >= 0 && pt.y + item.height <= flick.height) return
    var maxY = Math.max(0, flick.contentHeight - flick.height)
    flick.contentY = Math.max(0, Math.min(maxY,
      flick.contentY + pt.y - item.height))
  }

  function restoreSession() {
    if (root.busy) return
    if (restoreProc.running) return
    root.startProcess(restoreProc, "restore")
  }

  function startProcess(proc, key) {
    root.processOutput[key] = ""
    var deadline = root.deadlineFor(key)
    if (deadline) deadline.restart()
    proc.running = true
  }

  function deadlineFor(key) {
    if (key === "status") return statusDeadline
    if (key === "playlists") return playlistsDeadline
    if (key === "tracks") return tracksDeadline
    if (key === "search") return searchDeadline
    if (key === "play") return playDeadline
    if (key === "mix") return mixDeadline
    if (key === "queue") return queueDeadline
    if (key === "queueList") return queueListDeadline
    if (key === "library") return libraryDeadline
    if (key === "albumStatus") return albumStatusDeadline
    if (key === "albumCmd") return albumCmdDeadline
    if (key === "logout") return logoutDeadline
    if (key === "create") return createDeadline
    if (key === "cmd") return cmdDeadline
    if (key === "lyrics") return lyricsDeadline
    if (key === "thumbnail") return thumbnailDeadline
    if (key === "cover") return coverDeadline
    if (key === "lastPlayed") return lastPlayedDeadline
    if (key === "lastPlayedClear") return lastPlayedClearDeadline
    if (key === "likedSet") return likedSetDeadline
    if (key === "queueClear") return queueClearDeadline
    if (key === "restore") return restoreDeadline
    if (key === "stationFavorites") return stationFavoritesDeadline
    if (key === "stationCatalog") return stationCatalogDeadline
    if (key === "stationSearch") return stationSearchDeadline
    if (key === "stationPlay") return stationPlayDeadline
    if (key === "stationFav") return stationFavDeadline
    if (key === "stationFeatured") return stationFeaturedDeadline
    return null
  }

  // Called when a request's deadline fires. It runs whether or not the process
  // is still `running`: a missing `yt-music-ctl` fails to start, so `running`
  // is already false and a `if (running)` guard would leave the panel's flags
  // stuck forever. Every flag a request can set is cleared here.
  function commandTimeoutHit(key) {
    root.statusText = "Backend unavailable — is yt-music-ctl installed?"
    if (key === "status") root.refreshing = false
    else if (key === "search") root.searching = false
    else if (key === "stationSearch") root.stationSearching = false
    else if (key === "stationPlay") root.stationBusy = false
    else if (key === "tracks" || key === "library") root.loadingText = ""
    else if (key === "lyrics") root.lyricsLoading = false
    else if (key === "albumCmd") { root.albumCmdRunning = false; root.pumpAlbumCmdQueue() }
    else if (key === "play" || key === "mix" || key === "queue" || key === "logout" || key === "create" || key === "cmd") root.busy = false
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

  // Pure row-shaping lives in Model.js so it is unit-tested; these thin
  // wrappers keep the call sites in this file unchanged.
  function boundedString(value, limit) { return Model.boundedString(value, limit) }

  function isVideoId(value) { return Model.isVideoId(value) }

  function normalizeSong(song) { return Model.normalizeSong(song) }

  function normalizeSongs(items, limit) { return Model.normalizeSongs(items, limit) }

  function normalizeMixedRows(items, limit) { return Model.normalizeMixedRows(items, limit) }

  function normalizePlaylists(items) { return Model.normalizePlaylists(items) }

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
    root.closeLibrary()
    root.activePlaylistId = id
    root.activePlaylistTitle = title
    root.playlistTracks = []
    root.detailTab = root.activeTab
    root.selectedIndex = -1
    root.renameOpen = false
    root.statusText = ""
    root.loadingText = "Loading…"
    if (tracksProc.running) {
      // Drop the in-flight response for the playlist we are leaving, then
      // load this one once it exits.
      root.tracksTokens.invalidate()
      root.pendingPlaylistOpen = { id: id, title: title }
      return
    }
    root.startTracksRequest(id)
  }

  function startTracksRequest(id) {
    tracksProc.requestToken = root.tracksTokens.next()
    tracksProc.command = [root.ctlPath, "playlist", id]
    root.loadingText = "Loading…"
    root.startProcess(tracksProc, "tracks")
  }

  function playSelectedPlaylist() {
    if (!root.activePlaylistId || root.busy) return
    root.busy = true
    queueProc.command = [root.ctlPath, "queue", root.activePlaylistId]
    root.startProcess(queueProc, "queue")
  }

  function closePlaylist() {
    root.tracksTokens.invalidate()
    root.pendingPlaylistOpen = null
    root.activePlaylistId = ""
    root.activePlaylistTitle = ""
    root.playlistTracks = []
    root.selectedIndex = -1
    root.renameOpen = false
    root.queueSaveOpen = false
    if (!root.libraryDetail) root.detailTab = ""
  }

  function logout() {
    if (root.busy) return
    loginRefresh.stop()
    root.loginPollCount = 0
    root.busy = true
    root.startProcess(logoutProc, "logout")
  }

  function createPlaylist() {
    root.createPlaylistNamed(root.newPlaylistName.trim(), [])
  }

  // Creates a playlist; `tokens` are staged in root.pendingPlaylistTokens so
  // createPlaylistProc.onExited can chain a playlist-add-items right after.
  function createPlaylistNamed(title, tokens) {
    var name = String(title || "").trim()
    if (!name || root.busy) return
    root.pendingPlaylistTokens = (tokens || []).slice()
    root.busy = true
    root.statusText = "Creating playlist…"
    createPlaylistProc.command = [root.ctlPath, "create-playlist", name]
    root.startProcess(createPlaylistProc, "create")
  }

  function renameActivePlaylist() {
    if (!root.activePlaylistId || root.busy) return
    var title = renameField.text.trim()
    if (!title) return
    root.sendCmd("playlist-edit", [root.activePlaylistId, "--title", title])
    root.renameOpen = false
  }

  function editActivePlaylistPrivacy(privacy) {
    if (!root.activePlaylistId || root.busy) return
    root.sendCmd("playlist-edit", [root.activePlaylistId, "--privacy", privacy])
  }

  function deleteActivePlaylist() {
    if (!root.activePlaylistId || root.busy) return
    root.deleteConfirmOpen = false
    root.sendCmd("playlist-delete", [root.activePlaylistId])
  }

  function movePlaylistTrack(from, to) {
    if (!root.activePlaylistId || root.busy) return
    root.sendCmd("playlist-move", [root.activePlaylistId, from, to])
  }

  function saveQueueAsPlaylist() {
    var tokens = Model.queueTokens(root.queueTracks)
    if (tokens.length === 0) return
    var title = queueSaveField.text.trim() || "Queue"
    queueSaveField.text = ""
    root.queueSaveOpen = false
    root.createPlaylistNamed(title, tokens)
  }

  function openPlaylistOptions(anchorItem) {
    clearMenu(playlistOptionsMenu)
    playlistOptionsMenu.addItem("Rename playlist", function() { root.renameOpen = true })
    playlistOptionsMenu.addItem("Make public", function() { root.editActivePlaylistPrivacy("PUBLIC") })
    playlistOptionsMenu.addItem("Make private", function() { root.editActivePlaylistPrivacy("PRIVATE") })
    playlistOptionsMenu.addItem("Make unlisted", function() { root.editActivePlaylistPrivacy("UNLISTED") })
    playlistOptionsMenu.addItem("Delete playlist…", function() { root.deleteConfirmOpen = true })
    var p = anchorItem.mapToItem(panelFlick, 0, anchorItem.height)
    playlistOptionsMenu.popupAt(panelFlick, p.x, p.y)
  }

  function search(query) {
    if (query === undefined || query.trim() === "") return
    var q = query.trim()
    if (searchProc.running) {
      root.pendingSearch.offer(q, "")
      return
    }
    root.startSearchRequest(q)
  }

  function startSearchRequest(q) {
    root.searchQuery = q
    root.searchResults = []
    root.searching = true
    searchProc.command = [root.ctlPath, "search", "-f", root.searchFilter, q]
    root.startProcess(searchProc, "search")
  }

  function clearSearch() {
    searchField.text = ""
    root.searchQuery = ""
    root.searchFilter = "songs"
    root.searchResults = []
    root.searching = false
    root.pendingSearch.clear()
    root.selectedIndex = -1
  }

  function openRow(row, fromSearch) {
    if (!row) return
    if (row.kind === "song") { root.playNow(row.videoId); return }
    if (row.kind === "album") root.openAlbum(row.browseId, row.title)
    else if (row.kind === "artist") root.openArtist(row.browseId, row.title)
    else if (row.kind === "playlist") root.openPlaylist(row.browseId, row.title)
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

  function playArtistRadio(browseId) {
    var id = String(browseId || "")
    if (root.busy || mixProc.running || !id) return
    root.busy = true
    mixProc.command = [root.ctlPath, "radio", id]
    root.startProcess(mixProc, "mix")
  }

  // ---- internet radio (Stations tab): favorites + directory search
  function normalizeStations(items) { return Model.normalizeStations(items) }

  function stationSubtitle(row) { return Model.stationSubtitle(row) }

  function refreshStationFavorites() {
    if (stationFavoritesProc.running) {
      // The fetch in flight will replay this once it exits.
      root.stationFavoritesDirty.mark()
      return
    }
    root.startProcess(stationFavoritesProc, "stationFavorites")
  }

  function refreshStationCatalog() {
    if (stationCatalogProc.running) {
      // The fetch in flight will replay this once it exits.
      root.stationCatalogDirty.mark()
      return
    }
    root.startProcess(stationCatalogProc, "stationCatalog")
  }

  function searchStations(query) {
    var q = root.boundedString(query, 256).trim()
    if (q === "") {
      root.stationResults = []
      root.stationSearching = false
      root.pendingStationSearch.clear()
      return
    }
    if (stationSearchProc.running) {
      // Let the in-flight lookup finish; replay only a genuinely newer query
      // (an identical one is already on its way and needs no second run).
      // Always overwrite: returning to the in-flight query must clear a
      // pending one, or the replay would wedge `stationSearching` true.
      root.pendingStationSearch.offer(q, (stationSearchProc.command || [])[2] || "")
      return
    }
    var command = [root.ctlPath, "station-search", q]
    if (root.stationSearchMode === "tag") command.push("--tag")
    stationSearchProc.command = command
    root.stationSearching = true
    root.startProcess(stationSearchProc, "stationSearch")
  }

  function isStationFavorite(id) {
    var target = String(id || "")
    // An in-flight toggle is reflected immediately so the star flips on click
    // rather than after the round trip + refetch. The override is cleared once
    // the authoritative list reloads.
    if (root.stationFavOverrides[target] !== undefined)
      return root.stationFavOverrides[target]
    for (var i = 0; i < root.stationFavorites.length; i++) {
      if (String(root.stationFavorites[i].id) === target) return true
    }
    return false
  }

  function toggleStationFavorite(row) {
    if (!row || !row.url || stationFavProc.running) return
    var adding = !root.isStationFavorite(row.id)
    // Optimistic flip; reverted on failure below.
    var next = {}
    for (var k in root.stationFavOverrides) next[k] = root.stationFavOverrides[k]
    next[String(row.id)] = adding
    root.stationFavOverrides = next
    if (adding)
      stationFavProc.command = [root.ctlPath, "station-fav-add", JSON.stringify(row)]
    else
      stationFavProc.command = [root.ctlPath, "station-fav-remove", String(row.id)]
    root.startProcess(stationFavProc, "stationFav")
  }

  function isStationFeatured(id) {
    var target = String(id || "")
    for (var i = 0; i < root.stationCatalog.length; i++) {
      if (String(root.stationCatalog[i].id) === target) return true
    }
    return false
  }

  function toggleStationFeatured(row) {
    if (!row || !row.id || stationFeaturedProc.running) return
    if (root.isStationFeatured(row.id))
      stationFeaturedProc.command = [root.ctlPath, "station-featured-remove", String(row.id)]
    else
      stationFeaturedProc.command = [root.ctlPath, "station-featured-add", JSON.stringify(row)]
    root.startProcess(stationFeaturedProc, "stationFeatured")
  }

  function playStation(row) {
    if (!row || !row.url || root.stationBusy || root.busy) return
    root.stationBusy = true
    stationPlayProc.command = [root.ctlPath, "station-play", JSON.stringify(row)]
    root.startProcess(stationPlayProc, "stationPlay")
  }

  // Commands whose effect is already visible in the UI (transport, queue
  // shuffling); their generic "<Command> ✓" acknowledgment would only flash a
  // pointless toast.
  readonly property var quietCommands: ["toggle", "pause", "resume", "next", "prev",
    "seek", "seek-pct", "volume", "stop", "loop", "shuffle", "queue-jump",
    "queue-remove", "queue-remove-keys", "queue-move", "queue-clear", "enqueue", "enqueue-files",
    "precache", "thumbnail", "image", "status",
    "station-play", "station-search", "station-favorites", "station-catalog",
    "station-fav-add", "station-fav-remove"]
  function isQuietCommand(name) { return root.quietCommands.indexOf(name) !== -1 }

  function sendCmd(command, args) {
    if (root.busy) {
      root.statusText = "Still finishing the last action — try again"
      return false
    }
    root.busy = true
    cmdProc.command = [root.ctlPath, command].concat((args || []).map(String))
    root.startProcess(cmdProc, "cmd")
    return true
  }

  function queueJump(index) {
    if (root.busy) return
    root.sendCmd("queue-jump", [String(index)])
  }

  function queueIndexForKey(key) {
    if (!key) return -1
    for (var i = 0; i < root.queueTracks.length; i++) {
      if (Model.queueKeyAt(root.queueTracks, i) === key) return i
    }
    return -1
  }

  function queueRemoveKey(key) {
    if (root.busy || !key) return
    root.sendCmd("queue-remove-keys", [key])
  }

  function queueMove(from, to) {
    if (root.busy) return
    root.sendCmd("queue-move", [String(from), String(to)])
  }

  function libraryCommand(kind) {
    if (kind === "liked") return [root.ctlPath, "liked", "200"]
    if (kind === "songs" || kind === "albums" || kind === "artists")
      return [root.ctlPath, "library", kind, "200"]
    if (kind === "home") return [root.ctlPath, "home", "4"]
    if (kind === "history") return [root.ctlPath, "history", "200"]
    return null
  }

  function invalidateLibraryRequest() {
    root.libraryTokens.invalidate()
  }

  function startLibraryRequest(command) {
    libraryProc.requestToken = root.libraryTokens.next()
    libraryProc.command = command
    root.startProcess(libraryProc, "library")
  }

  function loadLibrary(kind) {
    if (libraryProc.running) return
    var command = root.libraryCommand(kind)
    if (!command) return
    var sameScreen = (root.libraryKind === kind) && root.libraryRows.length > 0
    root.libraryKind = kind
    // Browse mode: no detail is open under this tab.
    root.detailTab = ""
    root.libraryParents = []
    if (!sameScreen) {
      root.libraryTitle = ""
      root.librarySubtitle = ""
      root.libraryRows = []
      root.libraryRefId = ""
      root.resetLibraryInfo()
    }
    root.activeTab = "library"
    root.selectedIndex = -1
    root.statusText = ""
    root.libraryDirty = false
    root.loadingText = "Loading…"
    root.startLibraryRequest(command)
  }

  function refetchLibrary() {
    if (libraryProc.running) return
    if (!root.libraryKind) return
    var command = null
    if (root.libraryKind === "album")
      command = [root.ctlPath, "album", root.libraryRefId, "-r"]
    else if (root.libraryKind === "artist")
      command = [root.ctlPath, "artist", root.libraryRefId, "-r"]
    else {
      var base = root.libraryCommand(root.libraryKind)
      if (!base) return
      command = base.concat(["-r"])
    }
    root.loadingText = "Loading…"
    root.startLibraryRequest(command)
  }

  function resetLibraryInfo() {
    root.libraryThumbUrl = ""
    root.libraryImageSource = ""
    root.libraryMeta = ""
    root.libraryDescription = ""
    root.libraryInfoOpen = false
  }

  function openAlbum(browseId, title) {
    if (!browseId || libraryProc.running) return
    root.closePlaylist()
    var sameScreen = (root.libraryKind === "album")
      && root.libraryRefId === browseId && root.libraryRows.length > 0
    if (!sameScreen) root.pushLibraryParent()
    root.libraryKind = "album"
    if (!sameScreen) {
      root.libraryTitle = title || "Album"
      root.librarySubtitle = ""
      root.libraryRows = []
      root.resetLibraryInfo()
    }
    root.libraryRefId = browseId
    root.detailTab = root.activeTab
    root.selectedIndex = -1
    root.loadingText = "Loading…"
    root.startLibraryRequest([root.ctlPath, "album", browseId])
    if (root.loggedIn) {
      root.albumInLibrary = false
      root.albumStatusRefId = browseId
      albumStatusProc.command = [root.ctlPath, "album-status", browseId]
      root.startProcess(albumStatusProc, "albumStatus")
    }
  }

  function openArtist(browseId, name) {
    if (!browseId || libraryProc.running) return
    root.closePlaylist()
    var sameScreen = (root.libraryKind === "artist")
      && root.libraryRefId === browseId && root.libraryRows.length > 0
    if (!sameScreen) root.pushLibraryParent()
    root.libraryKind = "artist"
    if (!sameScreen) {
      root.libraryTitle = name || "Artist"
      root.librarySubtitle = ""
      root.libraryRows = []
      root.resetLibraryInfo()
    }
    root.libraryRefId = browseId
    root.detailTab = root.activeTab
    root.selectedIndex = -1
    root.loadingText = "Loading…"
    root.startLibraryRequest([root.ctlPath, "artist", browseId])
  }

  function snapshotLibrary() {
    return {
      kind: root.libraryKind,
      refId: root.libraryRefId,
      title: root.libraryTitle,
      subtitle: root.librarySubtitle,
      rows: root.libraryRows,
      meta: root.libraryMeta,
      description: root.libraryDescription,
      thumbUrl: root.libraryThumbUrl,
      imageSource: root.libraryImageSource,
      stale: root.libraryStale,
      infoOpen: root.libraryInfoOpen
    }
  }

  // Remember the current library level before drilling into an album/artist.
  function pushLibraryParent() {
    if (root.libraryKind === "" || root.libraryRows.length === 0) return
    var stack = root.libraryParents.slice()
    stack.push(root.snapshotLibrary())
    if (stack.length > 8) stack = stack.slice(stack.length - 8)
    root.libraryParents = stack
  }

  function applyLibrarySnapshot(level) {
    if (!level) {
      root.closeLibrary()
      return
    }
    root.libraryKind = String(level.kind || "")
    root.libraryRefId = String(level.refId || "")
    root.libraryTitle = String(level.title || "")
    root.librarySubtitle = String(level.subtitle || "")
    root.libraryRows = level.rows || []
    root.libraryMeta = String(level.meta || "")
    root.libraryDescription = String(level.description || "")
    root.libraryThumbUrl = String(level.thumbUrl || "")
    root.libraryImageSource = String(level.imageSource || "")
    root.libraryStale = level.stale === true
    root.libraryInfoOpen = level.infoOpen === true
    root.selectedIndex = -1
    // A restored detail keeps its host tab; a restored list is plain browse.
    root.detailTab = root.libraryDetail ? (root.detailTab || root.activeTab) : ""
  }

  // Back / tab pop for the library: return to the browse level you came from,
  // and only close the whole section from the top level.
  function libraryBack() {
    root.invalidateLibraryRequest()
    if (root.libraryParents.length > 0) {
      var stack = root.libraryParents.slice()
      var parent = stack.pop()
      root.libraryParents = stack
      root.applyLibrarySnapshot(parent)
      if (root.libraryDirty && !root.libraryDetail) {
        root.libraryDirty = false
        root.refetchLibrary()
      }
      return
    }
    root.closeLibrary()
  }

  function closeLibrary() {
    root.invalidateLibraryRequest()
    root.libraryParents = []
    root.libraryKind = ""
    root.libraryTitle = ""
    root.librarySubtitle = ""
    root.libraryRows = []
    root.libraryRefId = ""
    root.resetLibraryInfo()
    root.selectedIndex = -1
    if (!root.playlistDetail) root.detailTab = ""
  }

  // Album save/remove is serialised here rather than through sendCmd(): a
  // burst of "Save to library" clicks must all apply, and the backend calls
  // are idempotent so replaying one is harmless.
  function queueAlbumLibrary(action, browseId) {
    if (!browseId) return
    var q = root.albumCmdQueue.slice()
    q.push({ action: action, browseId: browseId })
    if (q.length > 20) q = q.slice(q.length - 20)
    root.albumCmdQueue = q
    root.statusText = (action === "album-save" ? "Saving to library…" : "Removing from library…")
    root.pumpAlbumCmdQueue()
  }
  function pumpAlbumCmdQueue() {
    if (root.albumCmdRunning || root.albumCmdQueue.length === 0) return
    var q = root.albumCmdQueue.slice()
    var spec = q.shift()
    root.albumCmdQueue = q
    root.albumCmdRunning = true
    root.albumCmdCurrentId = spec.browseId
    root.albumCmdCurrentAction = spec.action
    albumCmdProc.command = [root.ctlPath, spec.action, spec.browseId]
    root.startProcess(albumCmdProc, "albumCmd")
  }

  function closeDetail() {
    root.closeLibrary()
    root.closePlaylist()
    root.detailTab = ""
  }

  // Clicking the active content tab pops one level out of it first: out of an
  // open playlist or library list back to that tab's list/pills, and only the
  // click at the tab's top level collapses the tab.
  function toggleTab(key) {
    if (root.activeTab !== key) {
      root.activeTab = key
      return
    }
    if (key === "playlists" && root.playlistDetail) {
      root.closePlaylist()
      return
    }
    if (key === "library" && root.libraryKind !== "") {
      root.libraryBack()
      return
    }
    root.activeTab = ""
  }

  function songCount(rows) { return Model.songCount(rows) }

  function librarySongCount() {
    return root.songCount(root.libraryRows)
  }

  function songSubtitle(row) { return Model.songSubtitle(row) }

  function enqueueNav(mode, kind, id) {
    if (!id || root.busy) return
    if (kind !== "album" && kind !== "artist" && kind !== "playlist") return
    root.sendCmd("enqueue", [mode, kind, id])
  }

  function enqueueFiles(mode) {
    if (root.busy) return
    var ids = []
    for (var i = 0; i < root.searchResults.length; i++) {
      var r = root.searchResults[i]
      if (r && r.kind === "song" && r.videoId) ids.push(String(r.videoId))
    }
    if (ids.length === 0) return
    root.sendCmd("enqueue-files", [mode].concat(ids))
  }

  // ---- search multi-select (Ctrl/Shift clicks, action bar, batch add)
  function clearSelection() {
    root.selectMode = false
    root.selectedKeys = ({})
    root.selectionAnchor = -1
  }

  function isRowSelected(row) {
    return !!root.selectedKeys[Model.rowKey(row)]
  }

  function toggleRowAt(index, setAnchor) {
    if (index < 0 || index >= root.activeList.length) return
    var row = root.activeList[index]
    if (!row) return
    root.selectedKeys = Model.toggleSelected(root.selectedKeys, row)
    if (setAnchor !== false) root.selectionAnchor = index
    root.selectedIndex = index
    if (!root.selectMode) root.selectMode = true
  }

  function selectRangeAt(index) {
    if (root.selectionAnchor < 0) {
      root.toggleRowAt(index, true)
      return
    }
    root.selectedKeys = Model.selectedRange(root.selectedKeys, root.activeList, root.selectionAnchor, index)
    root.selectedIndex = index
    if (!root.selectMode) root.selectMode = true
  }

  // Batch queue removal: send every selected row key in one command. The
  // backend re-reads the playlist and removes each matching entry itself, so a
  // queue advance or an inserted precache entry cannot shift an index onto the
  // wrong track (which the old one-remove-per-round-trip pump could).
  function removeSelectedFromQueue() {
    var keys = []
    for (var i = 0; i < root.queueTracks.length; i++) {
      var row = root.queueTracks[i]
      if (row && row.key && root.isRowSelected(row)) keys.push(String(row.key))
    }
    if (keys.length === 0) return
    root.sendCmd("queue-remove-keys", keys)
  }

  // Returns true when the click was consumed as a selection gesture, i.e. the
  // caller should skip the normal "select + open row" behaviour.
  function handleRowClick(index, modifiers) {
    var ctrl = (modifiers & Qt.ControlModifier) !== 0
    var shift = (modifiers & Qt.ShiftModifier) !== 0
    if (root.selectMode || ctrl || shift) {
      if (shift) root.selectRangeAt(index)
      else root.toggleRowAt(index, true)
      return true
    }
    return false
  }

  function openContextMenu(videoId, title, artist, source, x, y, listIndex) {
    root.contextRow = null
    if (!root.isVideoId(videoId) && source !== "queue") return
    root.contextVideoId = String(videoId || "")
    root.contextTitle = title || ""
    root.contextArtist = artist || ""
    root.contextSource = source || ""
    root.contextTrackIndex = (source === "track") ? Number(listIndex) : -1
    root.contextQueueKey = (source === "queue") ? Model.queueKeyAt(root.queueTracks, Number(listIndex)) : ""
    root.contextX = x; root.contextY = y
    root.rebuildContextMenu()
    contextMenu.popupAt(panelFlick, x, y)
  }

  function openRowMenu(row, source, x, y) {
    if (!row) return
    if (row.kind === "song") {
      root.openContextMenu(row.videoId, row.title, row.artist, source, x, y)
      return
    }
    root.contextRow = row
    root.contextVideoId = ""
    root.contextTitle = row.title || ""
    root.contextArtist = row.artist || ""
    root.contextSource = source || ""
    root.contextX = x; root.contextY = y
    root.rebuildContextMenu()
    contextMenu.popupAt(panelFlick, x, y)
  }

  // Stage the tokens the playlist picker will add and remember what the
  // selection could not contribute.
  function stagePlaylistTokens(rows) {
    var tokens = Model.rowsToTokens(rows)
    var addable = 0
    for (var i = 0; i < rows.length; i++)
      if (Model.rowAddable(rows[i])) addable++
    root.playlistAddUnaddable = rows.length - addable
    root.playlistAddDuplicateSelections = addable - tokens.length
    var labels = ({})
    for (var k = 0; k < rows.length; k++) {
      var row = rows[k]
      var vid = row && row.videoId ? String(row.videoId) : ""
      if (vid !== "" && row.title) labels[vid] = String(row.title)
    }
    root.playlistAddLabels = labels
    root.playlistAddTokens = tokens
    return tokens
  }

  // Both entry points just stage `playlistAddTokens`; showPlaylistPicker()
  // is the single place that validates and pops the menu.
  function openPlaylistPicker() {
    var rows = root.isVideoId(root.contextVideoId)
      ? [{ kind: "song", videoId: root.contextVideoId, browseId: "" }] : []
    root.stagePlaylistTokens(rows)
    root.showPlaylistPicker(root.contextX, root.contextY)
  }

  function openPlaylistPickerForSelection(anchorItem) {
    var tokens = root.stagePlaylistTokens(root.selectedRows)
    if (tokens.length === 0) {
      root.statusText = root.playlistAddUnaddable > 0
        ? "Nothing to add: local files cannot go in a YouTube playlist"
        : "Nothing to add to a playlist"
      return
    }
    // Anchor under the button so the menu doesn't jump to the last right-click.
    var p = anchorItem ? anchorItem.mapToItem(panelFlick, 0, anchorItem.height) : { x: 0, y: 0 }
    root.showPlaylistPicker(p.x, p.y)
  }

  function showPlaylistPicker(x, y) {
    if (!root.loggedIn || root.playlistAddTokens.length === 0) return
    rebuildPlaylistPicker()
    Qt.callLater(function() {
      playlistPickerMenu.popupAt(panelFlick, x, y)
    })
  }

  function clearMenu(menu) {
    // Menu.clear() does not exist in Qt 6.11; the themed MenuPopup keeps its
    // rows in a JS array model, so just swap in an empty one.
    menu.menuItems = []
  }

  function addContextItem(text, handler) {
    contextMenu.addItem(text, handler)
  }

  function rebuildContextMenu() {
    clearMenu(contextMenu)
    if (root.contextRow) {
      var navRow = root.contextRow
      if (navRow.kind === "album") {
        addContextItem("Album info", function() {
          root.openRow(navRow, root.contextSource === "search")
          root.libraryInfoOpen = true
        })
        if (root.loggedIn)
          addContextItem("Save to library", function() { root.queueAlbumLibrary("album-save", navRow.browseId) })
      } else if (navRow.kind === "artist") {
        addContextItem("Artist info", function() {
          root.openRow(navRow, root.contextSource === "search")
          root.libraryInfoOpen = true
        })
        addContextItem("Start radio", function() { root.playArtistRadio(navRow.browseId) })
      }
      addContextItem("Play all", function() { root.enqueueNav("play", navRow.kind, navRow.browseId) })
      addContextItem("Add all to queue", function() { root.enqueueNav("queue", navRow.kind, navRow.browseId) })
      if (root.loggedIn)
        addContextItem("Add all to playlist…", function() {
          if (root.stagePlaylistTokens([navRow]).length > 0)
            root.showPlaylistPicker(root.contextX, root.contextY)
        })
      addContextItem("Open", function() { root.openRow(navRow, root.contextSource === "search") })
      return
    }
    if (root.isVideoId(root.contextVideoId)) {
      addContextItem("Play now", function() { root.sendCmd("play", [root.contextVideoId]) })
      addContextItem("Play next", function() { root.sendCmd("play-next", [root.contextVideoId, root.contextTitle, root.contextArtist]) })
      addContextItem("Add to queue", function() { root.sendCmd("queue-add", [root.contextVideoId, root.contextTitle, root.contextArtist]) })
      addContextItem("Start mix", function() { root.playMix(root.contextVideoId) })
      if (root.loggedIn)
        addContextItem("Like", function() { root.sendCmd("like", [root.contextVideoId]) })
      if (root.loggedIn && root.contextSource === "nowplaying")
        addContextItem("Dislike", function() { root.sendCmd("dislike", [root.contextVideoId]) })
      if (root.loggedIn)
        addContextItem("Add to playlist…", function() { root.openPlaylistPicker() })
      if (root.contextSource === "track" && root.activePlaylistId !== "")
        addContextItem("Remove from playlist", function() { root.sendCmd("remove", [root.activePlaylistId, root.contextVideoId]) })
      if (root.contextSource === "track" && root.activePlaylistId !== "") {
        if (root.contextTrackIndex > 0)
          addContextItem("Move up", function() { root.movePlaylistTrack(root.contextTrackIndex, root.contextTrackIndex - 1) })
        if (root.contextTrackIndex >= 0 && root.contextTrackIndex < root.playlistTracks.length - 1)
          addContextItem("Move down", function() { root.movePlaylistTrack(root.contextTrackIndex, root.contextTrackIndex + 1) })
      }
    }
    if (root.contextSource === "queue" && root.contextQueueKey !== "") {
      var qidx = root.queueIndexForKey(root.contextQueueKey)
      if (qidx > 0)
        addContextItem("Move up", function() {
          var idx = root.queueIndexForKey(root.contextQueueKey)
          if (idx > 0) root.queueMove(idx, idx - 1)
        })
      if (qidx >= 0 && qidx < root.queueTracks.length - 1)
        addContextItem("Move down", function() {
          var idx = root.queueIndexForKey(root.contextQueueKey)
          if (idx >= 0 && idx < root.queueTracks.length - 1) root.queueMove(idx, idx + 1)
        })
      addContextItem("Remove from queue", function() { root.queueRemoveKey(root.contextQueueKey) })
    }
  }

  function openStationMenu(row, x, y) {
    if (!row) return
    root.contextStation = row
    root.contextX = x; root.contextY = y
    root.rebuildStationMenu()
    stationMenu.popupAt(panelFlick, x, y)
  }

  function rebuildStationMenu() {
    clearMenu(stationMenu)
    var row = root.contextStation
    if (!row) return
    stationMenu.addItem("Play station", function() { root.playStation(row) })
    stationMenu.addItem(root.isStationFeatured(row.id) ? "Remove from Featured"
                                                        : "Add to Featured",
      function() { root.toggleStationFeatured(row) })
    stationMenu.addItem(root.isStationFavorite(row.id) ? "Remove from Favorites"
                                                       : "Add to Favorites",
      function() { root.toggleStationFavorite(row) })
  }

  // A discoverability cheat sheet for the keyboard and the right-click menus.
  // Rows are informational; selecting one just closes the menu.
  function openHelp(anchorItem) {
    clearMenu(helpMenu)
    var rows = [
      ["Space / Enter", "Play or pause (or open the selected row)"],
      ["Up / Down", "Move the list cursor"],
      ["Left / Right", "Seek 5 seconds"],
      ["Delete", "Remove the selected queue or playlist row"],
      ["Esc", "Close a menu, or the panel"],
      ["Right-click a track", "Play, queue, playlist, like, mix"],
      ["Right-click a station", "Play, Featured, Favorites"],
      ["Right-click the bar icon", "Transport menu"],
      ["Middle-click the bar icon", "Play / pause"],
      ["Click a tab twice", "Collapse it"]
    ]
    for (var i = 0; i < rows.length; i++) {
      helpMenu.addItem(rows[i][0] + "  —  " + rows[i][1], function() {})
    }
    var p = anchorItem ? anchorItem.mapToItem(panelFlick, 0, anchorItem.height) : { x: 0, y: 0 }
    helpMenu.popupAt(panelFlick, p.x, p.y)
  }

  function rebuildPlaylistPicker() {
    clearMenu(playlistPickerMenu)
    for (var i = 0; i < root.playlists.length; i++) {
      var pl = root.playlists[i]
      if (!pl || !pl.id) continue
      playlistPickerMenu.addItem(pl.title, root.playlistAddHandler(pl.id))
    }
    // Inline create: the typed name becomes a new playlist that receives the
    // staged tokens right after creation.
    playlistPickerMenu.createHandler = function(query) { root.createPlaylistWithTokens(query) }
  }

  function playlistAddHandler(playlistId) {
    // The returned closure captures the parameter, so every row keeps its own
    // playlist id instead of the loop variable.
    return function() {
      var tokens = root.playlistAddTokens.slice()
      if (tokens.length === 0) return
      if (!root.sendCmd("playlist-add-items", [playlistId].concat(tokens))) {
        root.statusText = "Still busy — try again"
        return
      }
      root.clearSelection()
    }
  }

  // "Create" row in the playlist picker: stage the tokens, clear the
  // selection, then let createPlaylistNamed() chain the add-items.
  function createPlaylistWithTokens(title) {
    var name = String(title || "").trim()
    if (name === "" || root.playlistAddTokens.length === 0 || root.busy) return
    var tokens = root.playlistAddTokens.slice()
    root.clearSelection()
    root.createPlaylistNamed(name, tokens)
  }

  // Fetch the liked ids once so the hero heart can show the real rating. The
  // `liked` command is metadata-cached (15 min) and serves stale instantly, so
  // this costs one cheap lookup rather than a request per track.
  function refreshLikedSet() {
    if (likedSetProc.running) return
    likedSetProc.command = [root.ctlPath, "liked", "200"]
    root.startProcess(likedSetProc, "likedSet")
  }

  function isCurrentLiked() {
    var vid = (root.musicStatus && root.musicStatus.videoId) ? String(root.musicStatus.videoId) : ""
    return vid !== "" && root.likedVideoIds[vid] === true
  }

  function likeCurrent() {
    if (!root.musicStatus || !root.isVideoId(root.musicStatus.videoId)) return
    var vid = String(root.musicStatus.videoId)
    if (root.likedVideoIds[vid] === true) sendCmd("unlike", [vid])
    else sendCmd("like", [vid])
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

  // Viewport height for a tab's list: whatever room is left in the tab body
  // below the fixed header rows above `item`, capped by the list's content.
  // `item` is the list's Flickable and its y is set by the layout, so it does
  // not depend on the Flickable's own height and this cannot loop.
  function listViewportHeight(item, wanted) {
    // Walk the ancestor chain reading `y` in JS (rather than mapToItem) so the
    // binding records those properties and re-runs when the header above the
    // list changes height or the tab is laid out.
    var top = 0
    var it = item
    while (it && it !== tabBody) { top += it.y || 0; it = it.parent }
    var avail = root.tabBodyMaxHeight - top
    if (!isFinite(avail) || avail < 0) avail = 0
    if (!isFinite(wanted)) return avail
    if (wanted <= avail) return wanted
    return Math.max(Style.space(96), avail)
  }

  // Scroll `index` of a list `repeater` into view inside `view` (defaults to
  // the active tab's scroll viewport; each list passes its own inner viewport).
  // Kept separate from the selection helper so the queue can scroll to the
  // currently-playing row without moving the keyboard cursor.
  function scrollListToRow(repeater, index, view) {
    var flick = view || tabFlick
    if (!repeater || index < 0) return
    if (flick.contentHeight <= flick.height) return
    var item = repeater.itemAt(index)
    if (!item) return
    var pt = item.mapToItem(flick, 0, 0)
    var maxY = Math.max(0, flick.contentHeight - flick.height)
    if (pt.y < 0)
      flick.contentY = Math.max(0, flick.contentY + pt.y)
    else if (pt.y + item.height > flick.height)
      flick.contentY = Math.min(maxY, flick.contentY + pt.y + item.height - flick.height)
  }

  function ensureSelectionVisible() {
    if (root.selectedIndex < 0) return
    var repeater = root.activeListKind === "search" ? searchRepeater
      : (root.activeListKind === "last" ? lastRepeater
      : (root.activeListKind === "playlist" ? trackRepeater
      : (root.activeListKind === "library" ? libraryRepeater : queueRepeater)))
    var view = root.activeListKind === "queue" ? queueListFlick
      : root.activeListKind === "last" ? lastList
      : root.activeListKind === "search" ? searchList
      : root.activeListKind === "playlist" ? trackList
      : root.activeListKind === "library" ? libraryView
      : tabFlick
    root.scrollListToRow(repeater, root.selectedIndex, view)
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

  // Writes the full cava config into the runtime path cava reads. cava's
  // `live-config` reloads it in place when the file is rewritten, so changing
  // an option never restarts the process.
  Process {
    id: cavaWriteProc
    command: ["python3", "-c",
      "import os,sys\n"
      + "p=sys.argv[1]\n"
      + "os.makedirs(os.path.dirname(p),exist_ok=True)\n"
      + "tmp=p+'.tmp'\n"
      + "open(tmp,'w').write(sys.argv[2])\n"
      + "os.replace(tmp,p)\n",
      root.cavaRuntimePath,
      Model.cavaConfig(root.visualizerChannels)]
    onExited: function(exitCode) {
      if (exitCode === 0) root.cavaReady = true
      if (root.cavaWritePending) {
        root.cavaWritePending = false
        root.writeCavaConfig()
      }
    }
  }

  // Audio visualizer source: cava prints one raw ASCII bar frame per line.
  // It runs only while the panel is open, the toggle is on, and audio is
  // actually playing (radio or YouTube). A missing cava just exits quietly.
  Process {
    id: cavaProc
    command: ["cava", "-p", root.cavaRuntimePath]
    running: root.cavaReady && root.opened && root.visualizerOn && Model.isPlaying(root.musicStatus)
    stdout: SplitParser {
      onRead: function(line) {
        var bars = Model.cavaApplyScaling(Model.parseCavaFrame(line, root.visualizerBarCount), root.visualizerScaling)
        var vol = root.musicStatus ? root.musicStatus.volume : 100
        var disp = Model.cavaVolumeScale(vol) * root.visualizerGain

        // Spectrum/flat bars track the volume and the display gain.
        root.visualizerBars = Model.cavaScaleBars(bars, disp)

        // Single VU/Peak level: damped, then volume+gain.
        var level0 = Model.cavaLevel(bars)
        root.visualizerLevel = Math.max(0, Math.min(100, level0 * disp))
        root.visualizerPeak = Math.max(root.visualizerLevel,
                                       Math.max(0, root.visualizerPeak - 2))

        // VU spectrum keeps its headroom (tallest column = level0) and tracks
        // volume+gain.
        var bandMax = Model.cavaPeak(bars)
        var head = Model.cavaScaleBars(bars, bandMax > 0 ? level0 / bandMax : 0)
        root.visualizerSpectrumBars = Model.cavaScaleBars(head, disp)
        root.visualizerBandPeaks = Model.cavaBandPeaks(
          root.visualizerSpectrumBars, root.visualizerBandPeaks, 4)
      }
    }
    onExited: {
      root.visualizerBars = []
      root.visualizerLevel = 0
      root.visualizerPeak = 0
      root.visualizerBandPeaks = []
      root.visualizerSpectrumBars = []
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
    }
  }

  Process {
    id: tracksProc
    property int requestToken: 0
    stdout: SplitParser {
      onRead: function(data) { root.appendProcessOutput("tracks", data) }
    }
    stderr: SplitParser {
      onRead: function(data) { root.appendProcessOutput("tracksErr", data) }
    }
    onStarted: tracksDeadline.start()
    onExited: function(exitCode) {
      tracksDeadline.stop()
      root.loadingText = ""
      if (root.tracksTokens.isCurrent(tracksProc.requestToken)) {
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
        if (exitCode !== 0 && root.statusText === "")
          root.statusText = "Could not load playlist"
      }
      if (root.pendingPlaylistOpen !== null) {
        var pending = root.pendingPlaylistOpen
        root.pendingPlaylistOpen = null
        root.startTracksRequest(pending.id)
      }
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
        root.searchResults = root.normalizeMixedRows(data.items, 100)
      root.searching = false
      if (root.pendingSearch.hasPending()) {
        var q = root.pendingSearch.take()
        root.startSearchRequest(q)
      }
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
      var data = root.parseProcessJson(root.processText("play"))
      if (exitCode === 0 && (!data || data.ok !== false)) {
        statusText = "Playing ✓"
        afterCommand.restart()
        root.refreshQueue()
      } else {
        statusText = root.boundedString((data && data.error) || "Play failed", 256)
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
      var data = root.parseProcessJson(root.processText("mix"))
      if (exitCode === 0 && (!data || data.ok !== false)) {
        statusText = "Mix started ✓"
        root.refreshQueue()
      } else {
        statusText = root.boundedString((data && data.error) || "Mix failed", 256)
      }
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
        root.queueSaved = !!data.saved
        root.queuePosition = (typeof data.position === "number") ? data.position : -1
        var tracks = Array.isArray(data.tracks) ? data.tracks : []
        var rows = []
        var seen = {}
        for (var i = 0; i < Math.min(tracks.length, 100); i++) {
          var t = tracks[i] || {}
          var vid = String(t.videoId || "")
          var occ = 0
          if (vid !== "") { occ = seen[vid] || 0; seen[vid] = occ + 1 }
          rows.push({
            index: i,
            kind: "song",
            browseId: "",
            key: vid !== "" ? ("v:" + vid + "#" + occ) : ("q:" + i),
            videoId: vid,
            title: root.boundedString(t.title, 256),
            artist: root.boundedString(t.artist, 256),
            album: root.boundedString(t.album, 256),
            duration: Math.max(0, Number(t.duration) || 0),
            number: Math.max(0, Number(t.number) || 0),
            stream: !!t.stream,
            current: !!t.current
          })
        }
        // Reassign only when the queue actually changed. A brand-new array
        // makes the queue Repeater destroy and rebuild every delegate, which
        // flashes the row play icons whenever a command that does not touch
        // the queue (volume, seek, pause, ...) triggers a refresh.
        if (JSON.stringify(rows) !== JSON.stringify(root.queueTracks))
          root.queueTracks = rows
        queueScrollTimer.restart()
      } else {
        root.queueSaved = false
      }
    }
  }

  Process {
    id: lastPlayedProc
    command: [root.ctlPath, "last-played", "100"]
    stdout: SplitParser {
      onRead: function(data) { root.appendProcessOutput("lastPlayed", data) }
    }
    stderr: SplitParser {
      onRead: function(data) { root.appendProcessOutput("lastPlayedErr", data) }
    }
    onStarted: lastPlayedDeadline.start()
    onExited: function(exitCode) {
      lastPlayedDeadline.stop()
      var data = root.parseProcessJson(root.processText("lastPlayed"))
      if (data && data.ok)
        root.lastPlayed = root.normalizeSongs(data.items, 100)
    }
  }

  Process {
    id: lastPlayedClearProc
    command: [root.ctlPath, "last-played", "clear"]
    stdout: SplitParser {
      onRead: function(data) { root.appendProcessOutput("lastPlayedClear", data) }
    }
    onStarted: lastPlayedClearDeadline.start()
    onExited: function(exitCode) {
      lastPlayedClearDeadline.stop()
      if (exitCode === 0) root.lastPlayed = []
    }
  }

  Process {
    id: likedSetProc
    command: [root.ctlPath, "liked", "200"]
    stdout: SplitParser {
      onRead: function(data) { root.appendProcessOutput("likedSet", data) }
    }
    stderr: SplitParser {
      onRead: function(data) { root.appendProcessOutput("likedSetErr", data) }
    }
    onStarted: likedSetDeadline.start()
    onExited: function(exitCode) {
      likedSetDeadline.stop()
      var data = root.parseProcessJson(root.processText("likedSet"))
      if (data && data.ok)
        root.likedVideoIds = Model.likedSet(data.items)
    }
  }

  Process {
    id: queueClearProc
    command: [root.ctlPath, "queue-clear"]
    stdout: SplitParser {
      onRead: function(data) { root.appendProcessOutput("queueClear", data) }
    }
    onStarted: queueClearDeadline.start()
    onExited: function(exitCode) {
      queueClearDeadline.stop()
      root.refresh()
      root.refreshQueue()
    }
  }

  Process {
    id: restoreProc
    command: [root.ctlPath, "restore"]
    stdout: SplitParser {
      onRead: function(data) { root.appendProcessOutput("restore", data) }
    }
    onStarted: restoreDeadline.start()
    onExited: function(exitCode) {
      restoreDeadline.stop()
      root.refresh()
      root.refreshQueue()
      root.refreshLastPlayed()
    }
  }

  Process {
    id: stationFavoritesProc
    command: [root.ctlPath, "station-favorites"]
    stdout: SplitParser {
      onRead: function(data) { root.appendProcessOutput("stationFavorites", data) }
    }
    stderr: SplitParser {
      onRead: function(data) { root.appendProcessOutput("stationFavoritesErr", data) }
    }
    onStarted: stationFavoritesDeadline.start()
    onExited: function(exitCode) {
      stationFavoritesDeadline.stop()
      var data = root.parseProcessJson(root.processText("stationFavorites"))
      if (data && data.ok && Array.isArray(data.items)) {
        root.stationFavorites = root.normalizeStations(data.items)
        // The authoritative list is in: the optimistic overrides are spent.
        if (Object.keys(root.stationFavOverrides).length > 0)
          root.stationFavOverrides = ({})
      }
      // Replay a refresh that arrived while this fetch was running (e.g. a
      // favourite was toggled) so the list can never be left stale.
      if (root.stationFavoritesDirty.take())
        root.refreshStationFavorites()
    }
  }

  Process {
    id: stationCatalogProc
    command: [root.ctlPath, "station-catalog"]
    stdout: SplitParser {
      onRead: function(data) { root.appendProcessOutput("stationCatalog", data) }
    }
    stderr: SplitParser {
      onRead: function(data) { root.appendProcessOutput("stationCatalogErr", data) }
    }
    onStarted: stationCatalogDeadline.start()
    onExited: function(exitCode) {
      stationCatalogDeadline.stop()
      var data = root.parseProcessJson(root.processText("stationCatalog"))
      if (data && data.ok && Array.isArray(data.items))
        root.stationCatalog = root.normalizeStations(data.items)
      // Replay a refresh that arrived while this fetch was running (e.g. a
      // featured add/remove) so the list can never be left stale.
      if (root.stationCatalogDirty.take())
        root.refreshStationCatalog()
    }
  }

  Process {
    id: stationFeaturedProc
    stdout: SplitParser {
      onRead: function(data) { root.appendProcessOutput("stationFeatured", data) }
    }
    stderr: SplitParser {
      onRead: function(data) { root.appendProcessOutput("stationFeaturedErr", data) }
    }
    onStarted: stationFeaturedDeadline.start()
    onExited: function(exitCode) {
      stationFeaturedDeadline.stop()
      var data = root.parseProcessJson(root.processText("stationFeatured"))
      if (data && data.ok === false)
        root.statusText = root.boundedString(data.error || "Station update failed", 256)
      root.refreshStationCatalog()
    }
  }

  Process {
    id: stationSearchProc
    stdout: SplitParser {
      onRead: function(data) { root.appendProcessOutput("stationSearch", data) }
    }
    stderr: SplitParser {
      onRead: function(data) { root.appendProcessOutput("stationSearchErr", data) }
    }
    onStarted: stationSearchDeadline.start()
    onExited: function(exitCode) {
      stationSearchDeadline.stop()
      var data = root.parseProcessJson(root.processText("stationSearch"))
      // `station-search` does not echo the query, so compare the one we sent:
      // a late response for a replaced query must not overwrite newer results.
      var sent = String((stationSearchProc.command || [])[2] || "")
      if (sent === root.stationQuery) {
        // Only this response owns the current query; a replaced query keeps the
        // searching flag until its own request starts (and exits).
        root.stationSearching = false
        if (data && data.ok === false)
          root.statusText = root.boundedString(data.error || "Station search failed", 256)
        else if (data && data.ok)
          root.stationResults = root.normalizeStations(data.items)
        else
          root.statusText = root.boundedString("Station search failed", 256)
      }
      if (root.pendingStationSearch.hasPending()) {
        var pending = root.pendingStationSearch.take()
        root.searchStations(pending)
      }
    }
  }

  Process {
    id: stationPlayProc
    stdout: SplitParser {
      onRead: function(data) { root.appendProcessOutput("stationPlay", data) }
    }
    stderr: SplitParser {
      onRead: function(data) { root.appendProcessOutput("stationPlayErr", data) }
    }
    onStarted: stationPlayDeadline.start()
    onExited: function(exitCode) {
      stationPlayDeadline.stop()
      root.stationBusy = false
      var data = root.parseProcessJson(root.processText("stationPlay"))
      if (exitCode === 0 && (!data || data.ok !== false)) {
        root.statusText = "Playing station ✓"
        root.refresh()
        // Two live stations share the queue key `videoId:playlistPos`, so
        // onMusicStatusChanged will not fire refreshQueue for a station->station
        // switch; refresh it here so Up Next never keeps the old station's rows.
        root.refreshQueue()
      } else {
        root.statusText = root.boundedString((data && data.error) || "Station failed", 256)
      }
    }
  }

  Process {
    id: stationFavProc
    stdout: SplitParser {
      onRead: function(data) { root.appendProcessOutput("stationFav", data) }
    }
    stderr: SplitParser {
      onRead: function(data) { root.appendProcessOutput("stationFavErr", data) }
    }
    onStarted: stationFavDeadline.start()
    onExited: function(exitCode) {
      stationFavDeadline.stop()
      var data = root.parseProcessJson(root.processText("stationFav"))
      if (data && data.ok === false) {
        // The optimistic flip was wrong: drop every override so the stars snap
        // back to the authoritative list on the refresh below.
        root.stationFavOverrides = ({})
        root.statusText = root.boundedString(data.error || "Station update failed", 256)
      }
      root.refreshStationFavorites()
    }
  }

  Timer { id: stationFavoritesDeadline; interval: root.commandTimeout; onTriggered: { if (stationFavoritesProc.running) stationFavoritesProc.running = false } }
  Timer { id: stationCatalogDeadline; interval: root.commandTimeout; onTriggered: { if (stationCatalogProc.running) stationCatalogProc.running = false } }
  Timer { id: stationSearchDeadline; interval: root.commandTimeout; onTriggered: { if (stationSearchProc.running) stationSearchProc.running = false; root.commandTimeoutHit("stationSearch") } }
  Timer { id: stationPlayDeadline; interval: root.commandTimeout; onTriggered: { if (stationPlayProc.running) stationPlayProc.running = false; root.commandTimeoutHit("stationPlay") } }
  Timer { id: stationFavDeadline; interval: root.commandTimeout; onTriggered: { if (stationFavProc.running) stationFavProc.running = false } }
  Timer { id: stationFeaturedDeadline; interval: root.commandTimeout; onTriggered: { if (stationFeaturedProc.running) stationFeaturedProc.running = false } }

  Timer {
    id: stationSearchDebounce
    interval: 350
    repeat: false
    onTriggered: root.searchStations(root.stationQuery)
  }

  // Only search when the user actually asks for it; empty queries reset.
  // A changed non-empty query clears the old list and flags a search straight
  // away, so the previous query's results and the "No stations found." empty
  // state never flash while the debounce waits to fire.
  onStationQueryChanged: {
    if (String(root.stationQuery || "").trim() === "") {
      stationSearchDebounce.stop()
      root.stationResults = []
      root.stationSearching = false
      root.pendingStationSearch.clear()
      return
    }
    root.stationResults = []
    root.stationSearching = true
    stationSearchDebounce.restart()
  }

  Timer {
    id: lastPlayedDeadline
    interval: root.commandTimeout
    onTriggered: { if (lastPlayedProc.running) lastPlayedProc.running = false }
  }

  Timer { id: likedSetDeadline; interval: root.commandTimeout; onTriggered: { if (likedSetProc.running) likedSetProc.running = false } }

  Timer {
    id: lastPlayedClearDeadline
    interval: root.commandTimeout
    onTriggered: { if (lastPlayedClearProc.running) lastPlayedClearProc.running = false }
  }

  Timer {
    id: queueClearDeadline
    interval: root.commandTimeout
    onTriggered: { if (queueClearProc.running) queueClearProc.running = false }
  }

  Timer {
    id: restoreDeadline
    interval: root.commandTimeout
    onTriggered: { if (restoreProc.running) restoreProc.running = false }
  }

  Process {
    id: libraryProc
    property int requestToken: 0
    stdout: SplitParser {
      onRead: function(data) { root.appendProcessOutput("library", data) }
    }
    stderr: SplitParser {
      onRead: function(data) { root.appendProcessOutput("libraryErr", data) }
    }
    onStarted: libraryDeadline.start()
    onExited: function(exitCode) {
      libraryDeadline.stop()
      root.loadingText = ""
      if (!root.libraryTokens.isCurrent(libraryProc.requestToken)) return
      var data = root.parseProcessJson(root.processText("library"))
      if (!data || !data.ok) {
        var libMsg = root.processText("libraryErr").trim()
        root.statusText = root.boundedString(
          (data && data.error) || (libMsg !== "" ? libMsg.split("\n")[0] : "Could not load library"),
          256)
        return
      }
      root.libraryStale = (data.stale === true)
      if (root.libraryStale) libraryRefreshTimer.restart()
      root.libraryRows = root.normalizeMixedRows(data.items, 500)
      var kind = root.libraryKind
      if (kind === "liked") {
        root.libraryTitle = root.boundedString(data.title || "Liked Music", 128)
      } else if (kind === "home") {
        root.libraryTitle = root.boundedString("Home", 128)
      } else if (kind === "history") {
        root.libraryTitle = root.boundedString("Recently played", 128)
      } else if (kind === "songs") {
        root.libraryTitle = root.boundedString("Songs", 128)
      } else if (kind === "albums") {
        root.libraryTitle = root.boundedString("Albums", 128)
      } else if (kind === "artists") {
        root.libraryTitle = root.boundedString("Artists", 128)
      } else if (kind === "album") {
        root.libraryTitle = root.boundedString(data.title || root.libraryTitle, 128)
        root.librarySubtitle = root.boundedString(
          (data.artist || "") + (data.year ? "  ·  " + data.year : ""), 256)
        root.libraryThumbUrl = root.boundedString(data.thumbnail || "", 512)
        var albumMeta = []
        if (data.year) albumMeta.push(root.boundedString(data.year, 64))
        var albumTracks = Number(data.trackCount) || 0
        if (albumTracks > 0)
          albumMeta.push(albumTracks + (albumTracks === 1 ? " track" : " tracks"))
        if (data.duration) albumMeta.push(root.boundedString(data.duration, 64))
        root.libraryMeta = root.boundedString(albumMeta.join("  ·  "), 256)
        root.libraryDescription = root.boundedString(data.description || "", 4096)
        root.fetchCover()
      } else if (kind === "artist") {
        root.libraryTitle = root.boundedString(data.name || root.libraryTitle, 128)
        root.librarySubtitle = root.boundedString(data.subscribers || "", 128)
        root.libraryThumbUrl = root.boundedString(data.thumbnail || "", 512)
        var artistMeta = []
        if (data.subscribers) artistMeta.push(root.boundedString(data.subscribers, 64))
        if (data.monthlyListeners)
          artistMeta.push(root.boundedString(data.monthlyListeners, 64) + " monthly listeners")
        root.libraryMeta = root.boundedString(artistMeta.join("  ·  "), 256)
        root.libraryDescription = root.boundedString(data.description || "", 4096)
        root.fetchCover()
        var sims = root.normalizeMixedRows(data.similar || [], 100)
        if (sims.length > 0) root.libraryRows = root.libraryRows.concat(sims)
      }
    }
  }

  Process {
    id: albumStatusProc
    stdout: SplitParser {
      onRead: function(data) { root.appendProcessOutput("albumStatus", data) }
    }
    stderr: SplitParser {
      onRead: function(data) { root.appendProcessOutput("albumStatusErr", data) }
    }
    onStarted: albumStatusDeadline.start()
    onExited: function(exitCode) {
      albumStatusDeadline.stop()
      var data = root.parseProcessJson(root.processText("albumStatus"))
      if (data && data.ok && root.albumStatusRefId === root.libraryRefId)
        root.albumInLibrary = (data.inLibrary === true)
    }
  }

  Process {
    id: albumCmdProc
    stdout: SplitParser {
      onRead: function(data) { root.appendProcessOutput("albumCmd", data) }
    }
    stderr: SplitParser {
      onRead: function(data) { root.appendProcessOutput("albumCmdErr", data) }
    }
    onStarted: albumCmdDeadline.start()
    onExited: function(exitCode) {
      albumCmdDeadline.stop()
      root.albumCmdRunning = false
      var data = root.parseProcessJson(root.processText("albumCmd"))
      var action = root.albumCmdCurrentAction
      if (data && data.ok) {
        if (root.albumCmdCurrentId === root.libraryRefId)
          root.albumInLibrary = (action === "album-save")
        root.libraryDirty = true
        statusText = (action === "album-save" ? "Saved to library ✓" : "Removed from library ✓")
        if (root.activeTab === "library" && root.libraryKind === "albums") {
          root.libraryDirty = false
          root.refetchLibrary()
        }
      } else {
        statusText = root.boundedString((data && data.error) || "Library update failed", 256)
      }
      root.pumpAlbumCmdQueue()
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
      var chained = false
      if (data && data.ok) {
        newPlaylistField.text = ""
        root.newPlaylistName = ""
        root.loadPlaylists()
        var pendingTokens = root.pendingPlaylistTokens
        var newPlaylistId = String(data.id || "")
        if (pendingTokens.length > 0 && newPlaylistId !== "") {
          // Add the staged queue tracks to the playlist we just created; this
          // hands busy/status back to cmdProc, so don't clear them below.
          root.pendingPlaylistTokens = []
          root.busy = false
          if (root.sendCmd("playlist-add-items", [newPlaylistId].concat(pendingTokens))) {
            root.statusText = "Playlist created · adding tracks…"
          } else {
            root.statusText = "Playlist created · could not add tracks (busy)"
          }
          chained = true
        } else {
          root.statusText = "Playlist created ✓"
        }
      } else if (data && data.error) {
        root.statusText = root.boundedString(data.error, 256)
      }
      if (msg !== "") root.statusText = root.boundedString(msg.split("\n")[0], 256)
      if (!chained) {
        // Drop any staged tokens: a failed create must not leak them into the
        // next (unrelated) create from the Playlists tab.
        root.busy = false
        root.pendingPlaylistTokens = []
      }
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
      var cmdName = String(cmdProc.command[1] || "")
      var action = String(cmdProc.command[1] || "Command")
      action = action.charAt(0).toUpperCase() + action.slice(1).replace(/-/g, " ")
      if (cmdName === "playlist-add" || cmdName === "playlist-add-items") {
        // Batch/single add report {ok, added, duplicates, ...}: summarise the
        // counts instead of the generic "<Name> ✓".
        var d = root.parseProcessJson(root.processText("cmd"))
        if (d && d.ok) {
          var addN = Number(d.added) || 0
          var dupN = Number(d.duplicates) || 0
          var addMsg = addN > 0 ? "Added " + addN + (addN === 1 ? " track" : " tracks")
                                : "Nothing added"
          var dupNames = Model.idsToNames(d.duplicateVideoIds, root.playlistAddLabels, 2)
          if (dupN > 0) {
            if (dupNames !== "") addMsg += " · already in playlist: " + dupNames
            else addMsg += " · " + dupN + " already in playlist"
          }
          var skipN = Number(d.skipped) || 0
          var skipNames = Model.idsToNames(d.skippedVideoIds, root.playlistAddLabels, 2)
          if (skipN > 0) {
            if (skipNames !== "") addMsg += " · YouTube skipped: " + skipNames
            else addMsg += " · " + skipN + " skipped by YouTube"
          }
          if (root.playlistAddDuplicateSelections > 0)
            addMsg += " · " + root.playlistAddDuplicateSelections
              + (root.playlistAddDuplicateSelections === 1 ? " duplicate selection" : " duplicate selections")
          if (root.playlistAddUnaddable > 0)
            addMsg += " · " + root.playlistAddUnaddable + " not addable"
          root.playlistAddDuplicateSelections = 0
          root.playlistAddUnaddable = 0
          statusText = addMsg + " ✓"
          root.playlistAddLabels = ({})
          var dest = String(cmdProc.command[2] || "")
          if (dest !== "" && dest === root.activePlaylistId) {
            root.startTracksRequest(root.activePlaylistId)
          }
        } else {
          statusText = root.boundedString((d && d.error) || "Add failed", 256)
        }
        afterCommand.restart()
        return
      }
      if (cmdName === "playlist-edit") {
        var ed = root.parseProcessJson(root.processText("cmd"))
        if (ed && ed.ok) {
          var titleFlag = cmdProc.command.indexOf("--title")
          if (titleFlag !== -1)
            root.activePlaylistTitle = root.boundedString(cmdProc.command[titleFlag + 1] || "", 256)
          statusText = "Playlist updated ✓"
          root.loadPlaylists()
        } else {
          statusText = root.boundedString((ed && ed.error) || "Update failed", 256)
        }
        afterCommand.restart()
        return
      }
      if (cmdName === "playlist-delete") {
        // The backend reports failures (e.g. "Cannot delete Liked Music") as
        // {ok:false} with exit 0, so parse instead of trusting the exit code.
        var del = root.parseProcessJson(root.processText("cmd"))
        if (del && del.ok) {
          statusText = "Playlist deleted ✓"
          root.closePlaylist()
          root.loadPlaylists()
        } else {
          statusText = root.boundedString((del && del.error) || "Delete failed", 256)
        }
        afterCommand.restart()
        return
      }
      if (cmdName === "playlist-move") {
        var mv = root.parseProcessJson(root.processText("cmd"))
        if (mv && mv.ok) {
          statusText = "Track moved ✓"
          if (root.activePlaylistId) {
            root.startTracksRequest(root.activePlaylistId)
          }
        } else {
          statusText = root.boundedString((mv && mv.error) || "Move failed", 256)
        }
        afterCommand.restart()
        return
      }
      // Every remaining command reports {ok:false, error} with exit 0 on a
      // handled failure, so trust the payload rather than the exit code.
      var generic = root.parseProcessJson(root.processText("cmd"))
      if (generic && generic.ok === false) {
        statusText = root.boundedString(generic.error || (action + " failed"), 256)
        afterCommand.restart()
        return
      }
      if (cmdName === "queue-remove-keys") {
        var removedN = (generic && Array.isArray(generic.removed)) ? generic.removed.length : 0
        root.clearSelection()
        if (removedN > 0)
          statusText = "Removed " + removedN + (removedN === 1 ? " track" : " tracks") + " ✓"
        afterCommand.restart()
        return
      }
      if (cmdName === "like" || cmdName === "unlike" || cmdName === "dislike") {
        // Reflect the rating immediately. The backend invalidates its `liked`
        // cache, so the next panel open re-reads the authoritative list.
        var ratedVid = String((cmdProc.command && cmdProc.command[2]) || "")
        if (ratedVid !== "") {
          var nextLiked = {}
          for (var likedKey in root.likedVideoIds) nextLiked[likedKey] = root.likedVideoIds[likedKey]
          nextLiked[ratedVid] = (cmdName === "like")
          root.likedVideoIds = nextLiked
        }
        if (cmdName === "like") statusText = "Liked ✓"
        else if (cmdName === "unlike") statusText = "Unliked ✓"
        else statusText = "Disliked ✓"
        afterCommand.restart()
        return
      }
      if (!root.isQuietCommand(cmdName))
        statusText = action + " ✓"
      if (action === "Remove" && root.activePlaylistId) {
        root.startTracksRequest(root.activePlaylistId)
      }
      afterCommand.restart()
    }
  }

  Timer { id: statusDeadline; interval: root.commandTimeout; onTriggered: { if (statusProc.running) { statusProc.running = false; root.statusText = "Status request timed out" } else root.commandTimeoutHit("status") } }
  Timer { id: playlistsDeadline; interval: root.commandTimeout; onTriggered: { if (playlistsProc.running) { playlistsProc.running = false; root.statusText = "Library request timed out" } } }
  Timer { id: tracksDeadline; interval: root.commandTimeout; onTriggered: { if (tracksProc.running) tracksProc.running = false; root.commandTimeoutHit("tracks") } }
  Timer { id: searchDeadline; interval: root.commandTimeout; onTriggered: { if (searchProc.running) { searchProc.running = false; root.statusText = "Search timed out" } else root.commandTimeoutHit("search") } }
  Timer { id: playDeadline; interval: root.commandTimeout; onTriggered: { if (playNowProc.running) playNowProc.running = false; else root.commandTimeoutHit("play") } }
  Timer { id: mixDeadline; interval: root.commandTimeout; onTriggered: { if (mixProc.running) mixProc.running = false; else root.commandTimeoutHit("mix") } }
  Timer { id: queueDeadline; interval: root.commandTimeout; onTriggered: { if (queueProc.running) queueProc.running = false; else root.commandTimeoutHit("queue") } }
  Timer { id: queueListDeadline; interval: root.commandTimeout; onTriggered: { if (queueListProc.running) queueListProc.running = false } }
  Timer { id: libraryDeadline; interval: root.commandTimeout; onTriggered: { if (libraryProc.running) libraryProc.running = false; root.commandTimeoutHit("library") } }
  Timer { id: albumStatusDeadline; interval: root.commandTimeout; onTriggered: { if (albumStatusProc.running) albumStatusProc.running = false } }
  Timer { id: albumCmdDeadline; interval: root.commandTimeout; onTriggered: { if (albumCmdProc.running) albumCmdProc.running = false; root.albumCmdRunning = false; root.pumpAlbumCmdQueue() } }
  Timer { id: logoutDeadline; interval: root.commandTimeout; onTriggered: { if (logoutProc.running) logoutProc.running = false; else root.commandTimeoutHit("logout") } }
  Timer { id: createDeadline; interval: root.commandTimeout; onTriggered: { if (createPlaylistProc.running) createPlaylistProc.running = false; else root.commandTimeoutHit("create") } }
  Timer { id: cmdDeadline; interval: root.commandTimeout; onTriggered: { if (cmdProc.running) cmdProc.running = false; else root.commandTimeoutHit("cmd") } }
  Timer {
    id: libraryRefreshTimer
    interval: 6000
    repeat: false
    onTriggered: root.refetchLibrary()
  }

  Process {
    id: uiSaveProc
    stdout: SplitParser { onRead: function(data) { root.appendProcessOutput("uiSave", data) } }
    stderr: SplitParser { onRead: function(data) { root.appendProcessOutput("uiSaveErr", data) } }
  }

  Process {
    id: uiLoadProc
    stdout: SplitParser { onRead: function(data) { root.appendProcessOutput("uiLoad", data) } }
    stderr: SplitParser { onRead: function(data) { root.appendProcessOutput("uiLoadErr", data) } }
    onExited: function(exitCode) {
      var raw = root.processText("uiLoad").trim()
      var data = raw === "" ? null : root.parseProcessJson(raw)
      if (data) root.applyUiState(data)
      else root.stateRestored = true
    }
  }

  Process {
    id: lyricsProc
    stdout: SplitParser { onRead: function(data) { root.appendProcessOutput("lyrics", data) } }
    stderr: SplitParser { onRead: function(data) { root.appendProcessOutput("lyricsErr", data) } }
    onStarted: lyricsDeadline.start()
    onExited: function(exitCode) {
      lyricsDeadline.stop()
      var cmd = lyricsProc.command || []
      var sent = String(cmd.length > 2 ? cmd[2] : "")
      if (sent !== root.lyricsVideoId) {
        if (root.lyricsOpen && root.isVideoId(root.lyricsVideoId)) {
          lyricsProc.command = [root.ctlPath, "lyrics", root.lyricsVideoId]
          root.startProcess(lyricsProc, "lyrics")
        } else {
          root.lyricsLoading = false
        }
        return
      }
      var data = root.parseProcessJson(root.processText("lyrics"))
      if (data && data.ok) {
        root.lyricsHas = data.hasLyrics === true
        root.lyricsText = root.lyricsHas && Array.isArray(data.lines)
          ? data.lines.join("\n") : ""
        root.lyricsSynced = Array.isArray(data.synced) ? data.synced : null
      } else {
        root.lyricsHas = false
        root.lyricsText = ""
        root.lyricsSynced = null
      }
      root.lyricsLoading = false
    }
  }

  Timer { id: lyricsDeadline; interval: root.commandTimeout; onTriggered: { if (lyricsProc.running) { lyricsProc.running = false; root.lyricsLoading = false } else root.commandTimeoutHit("lyrics") } }

  Process {
    id: thumbnailProc
    command: [root.ctlPath, "thumbnail", root.thumbnailVideoId]
    stdout: SplitParser { onRead: function(data) { root.appendProcessOutput("thumbnail", data) } }
    stderr: SplitParser { onRead: function(data) { root.appendProcessOutput("thumbnailErr", data) } }
    onStarted: thumbnailDeadline.start()
    onExited: function(exitCode) {
      thumbnailDeadline.stop()
      var ok = exitCode === 0 && root.isVideoId(root.thumbnailVideoId)
      root.thumbnailSource = ok ? "file://" + root.thumbnailPath(root.thumbnailVideoId) : ""
      if (!ok && root.thumbnailRetries < 2 && root.isVideoId(root.thumbnailVideoId)) {
        root.thumbnailRetries++
        thumbnailRetryTimer.restart()
      }
    }
  }

  Timer {
    id: thumbnailDeadline
    interval: root.commandTimeout
    onTriggered: { if (thumbnailProc.running) thumbnailProc.running = false }
  }

  Timer {
    id: thumbnailRetryTimer
    interval: 1500
    onTriggered: {
      if (root.thumbnailSource === "" && root.isVideoId(root.thumbnailVideoId))
        root.startProcess(thumbnailProc, "thumbnail")
    }
  }

  Process {
    id: coverProc
    stdout: SplitParser { onRead: function(data) { root.appendProcessOutput("cover", data) } }
    stderr: SplitParser { onRead: function(data) { root.appendProcessOutput("coverErr", data) } }
    onStarted: coverDeadline.start()
    onExited: function(exitCode) {
      // `image <url>` prints nothing on success; the cached path is keyed by a
      // sha256 we cannot rebuild from QML, so libraryImageSource stays on the
      // remote URL (set by fetchCover) while this warms the on-disk cache.
      coverDeadline.stop()
    }
  }

  Timer {
    id: coverDeadline
    interval: root.commandTimeout
    onTriggered: { if (coverProc.running) coverProc.running = false }
  }

  function fetchCover() {
    root.libraryImageSource = root.libraryThumbUrl
    if (root.libraryThumbUrl === "" || coverProc.running) return
    coverProc.command = [root.ctlPath, "image", root.libraryThumbUrl]
    root.startProcess(coverProc, "cover")
  }

  function thumbnailPath(videoId) {
    return (Quickshell.env("XDG_CACHE_HOME") || (Quickshell.env("HOME") + "/.cache"))
      + "/yt-music/thumbs/" + videoId + ".jpg"
  }

  function loadThumbnail() {
    var id = root.musicStatus ? String(root.musicStatus.videoId || "") : ""
    if (!root.isVideoId(id) && root.heroTrack) id = String(root.heroTrack.videoId || "")
    id = root.isVideoId(id) ? id : ""
    if (id === root.thumbnailVideoId) return
    root.thumbnailVideoId = id
    root.thumbnailRetries = 0
    root.thumbnailSource = ""
    if (root.thumbnailVideoId !== "") root.startProcess(thumbnailProc, "thumbnail")
  }

  function loadLyrics(videoId) {
    var id = root.isVideoId(videoId) ? String(videoId) : ""
    root.lyricsVideoId = id
    root.lyricsText = ""
    root.lyricsHas = false
    root.lyricsSynced = null
    root.lyricsLoading = id !== ""
    if (id === "" || lyricsProc.running) return
    lyricsProc.command = [root.ctlPath, "lyrics", id]
    root.startProcess(lyricsProc, "lyrics")
  }

  function toggleLyrics() {
    root.lyricsOpen = !root.lyricsOpen
    if (root.lyricsOpen)
      root.loadLyrics(root.musicStatus && root.musicStatus.videoId)
  }

  onThumbnailVideoIdChanged: {
    if (root.thumbnailVideoId === "") root.thumbnailSource = ""
  }
  onLastPlayedChanged: root.loadThumbnail()
  onMusicStatusChanged: {
    root.loadThumbnail()
    // Radio has no track lyrics and hides the Lyrics button; close the panel
    // so a stream cannot leave the lyrics view stranded behind the UI.
    if (root.radioLive && root.lyricsOpen) root.lyricsOpen = false
    var currentVideoId = String(root.musicStatus ? root.musicStatus.videoId : "")
    if (root.lyricsOpen && currentVideoId !== root.lyricsVideoId)
      root.loadLyrics(currentVideoId)
    var key = currentVideoId
      + ":" + String(root.musicStatus ? root.musicStatus.playlistPos : "")
    if (key !== root.queueKey) {
      root.queueKey = key
      root.refreshQueue()
    }
  }

  Timer {
    id: afterCommand
    interval: 1600
    onTriggered: { root.refresh(); root.refreshQueue(); root.refreshLastPlayed() }
  }

  // Waits a beat after the queue model changes so the Repeater has created the
  // delegate before scrollQueueToCurrent looks it up.
  Timer {
    id: queueScrollTimer
    interval: 120
    repeat: false
    onTriggered: root.scrollQueueToCurrent()
  }

  Timer {
    id: statusClear
    // Messages are transient by design: long enough to read, gone before the
    // next interaction.
    interval: 4000
    onTriggered: root.statusText = ""
  }

  onStatusTextChanged: if (statusText !== "") statusClear.restart()



  Timer {
    id: autoRefresh
    // Live updates come from the yt-music daemon via status.json; this is only a backstop.
    interval: 30000
    running: true
    repeat: true
    onTriggered: { root.refresh(); root.refreshLastPlayed() }
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
        root.loginPollCount += 1
        // Bounded: do not poll the network forever if login is abandoned.
        if (root.loginPollCount > 40) {
          stop()
          root.statusText = "Login not detected — run yt-music-ctl login or reopen the panel"
        } else {
          root.loadPlaylists()
        }
      }
    }
  }

  // Theme-driven row highlight mirroring qs.Ui.CursorSurface's visuals. The
  // panel keeps its own hover/cursor model (per-row MouseArea + selectedIndex),
  // so this only centralizes the Style/Border tokens instead of the CursorSurface
  // hasCursor contract.
  component RowHighlight: BorderSurface {
    id: hl

    property bool hasCursor: false      // panel keyboard cursor (selectedIndex)
    property bool hovered: false        // pointer hover
    property bool current: false        // active/playing row
    property bool multi: false          // multi-select (search results)
    property color foreground: Color.foreground
    property color accent: Color.accent

    readonly property bool _hot: hl.hasCursor || hl.hovered

    anchors.fill: parent
    radius: Style.cornerRadius
    color: hl.multi ? Style.selectionFillFor(hl.foreground, hl.accent)
         : hl._hot ? Style.hoverFillFor(hl.foreground, hl.accent)
         : hl.current ? Style.selectedFillFor(hl.foreground, hl.accent)
         : "transparent"
    borderSpec: hl.multi ? Border.none()
         : hl._hot ? Border.controlSpec("hover-cursor", hl.foreground, hl.accent)
         : hl.current ? Border.controlSpec("selected", hl.foreground, hl.accent)
         : Border.none()
    Behavior on color { ColorAnimation { duration: 60 } }
  }

  // Themed context menus. Qt's Menu/MenuItem render with the platform-native
  // look, so both menus are Popups that reuse the Omarchy popup surface
  // vocabulary from qs.Ui/Dropdown (Color.popups tokens + BorderSurface) and
  // drive their rows from a JS array model instead of Qt menu items.
  component MenuPopup: Popup {
    id: menu

    property var menuItems: []
    property Item menuParent: null

    // Searchable pickers (playlist add) layer a filter field over the rows
    // and can offer an inline "create" row driven by `createHandler`.
    property bool searchable: false
    property string searchPlaceholder: "Search…"
    property string emptyText: ""
    property var createHandler: null
    property string searchQuery: ""
    // A single "active row" cursor shared by the mouse and the arrow keys, so
    // hovering one row can never leave a second row looking selected. -1 means
    // nothing is current until the pointer or a key picks a row.
    property int highlightIndex: -1

    readonly property var shownItems: menu.searchable ? Model.filterByTitle(menu.menuItems, menu.searchQuery) : menu.menuItems

    focus: true
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    padding: Style.spacing.hairline
    leftPadding: Border.left(menu.borderSpec) + Style.spacing.hairline
    rightPadding: Border.right(menu.borderSpec) + Style.spacing.hairline
    topPadding: Border.top(menu.borderSpec) + Style.spacing.hairline
    bottomPadding: Border.bottom(menu.borderSpec) + Style.spacing.hairline

    readonly property var borderSpec: Border.localOrSurfaceSpec("popups", "border", Color.popups.border, Color.popups.border, Style.normalBorderWidth)

    background: BorderSurface {
      color: Color.popups.background
      borderSpec: menu.borderSpec
      radius: Style.cornerRadius
    }

    contentItem: ColumnLayout {
      id: menuColumn
      spacing: Style.spacing.labelGap
      focus: !menu.searchable

      Keys.onUpPressed: menu.moveHighlight(-1)
      Keys.onDownPressed: menu.moveHighlight(1)
      Keys.onReturnPressed: menu.triggerHighlighted()
      Keys.onEnterPressed: menu.triggerHighlighted()

      TextField {
        id: menuSearchField
        visible: menu.searchable
        Layout.fillWidth: true
        Layout.preferredWidth: Style.space(220)
        Layout.preferredHeight: Style.spacing.popupRowHeight
        placeholderText: menu.searchPlaceholder
        foreground: Color.popups.text
        hasCursor: false
        onTextChanged: menu.searchQuery = text
        onAccepted: menu.triggerFirstOrCreate()
      }

      PanelSeparator {
        visible: menu.searchable
        Layout.fillWidth: true
        foreground: Color.popups.text
      }

      Repeater {
        model: menu.shownItems

        delegate: Rectangle {
          id: row
          required property var modelData
          // Declared alongside modelData: once a delegate has any required
          // property it must declare every injected one it reads, and this
          // row uses `index` for the highlight. Without it the binding throws
          // and the row is never themed.
          required property int index
          Layout.fillWidth: true
          implicitWidth: rowLabel.implicitWidth + 2 * Style.spacing.controlPaddingX
          implicitHeight: Style.spacing.popupRowHeight
          color: index === menu.highlightIndex
            ? Style.hoverFillFor(Color.popups.text, Color.accent) : "transparent"

          Text {
            id: rowLabel
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.leftMargin: Style.spacing.controlPaddingX
            anchors.rightMargin: Style.spacing.controlPaddingX
            textFormat: Text.PlainText
            text: String(modelData.text)
            color: index === menu.highlightIndex
              ? Style.hoverStateColor(Color.popups.text, Color.accent) : Color.popups.text
            font.family: root.fam
            font.pixelSize: Style.font.body
            elide: Text.ElideRight
          }

          MouseArea {
            id: rowMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            // Hover and the arrow keys drive the same cursor, so only one row
            // is ever highlighted.
            onEntered: menu.highlightIndex = index
            onClicked: menu.trigger(modelData)
          }
        }
      }

      Text {
        visible: menu.searchable && menu.shownItems.length === 0 && menu.searchQuery.trim() === "" && menu.emptyText !== ""
        Layout.fillWidth: true
        Layout.leftMargin: Style.spacing.controlPaddingX
        Layout.rightMargin: Style.spacing.controlPaddingX
        text: menu.emptyText
        color: Qt.darker(Color.popups.text, 1.5)
        font.family: root.fam
        font.pixelSize: Style.font.body
      }

      Rectangle {
        id: createRow
        visible: menu.searchable && menu.createHandler !== null && menu.searchQuery.trim() !== ""
        Layout.fillWidth: true
        implicitHeight: Style.spacing.popupRowHeight
        color: createRowMouse.containsMouse ? Style.hoverFillFor(Color.popups.text, Color.accent) : "transparent"

        Text {
          id: createRowLabel
          anchors.left: parent.left
          anchors.right: parent.right
          anchors.verticalCenter: parent.verticalCenter
          anchors.leftMargin: Style.spacing.controlPaddingX
          anchors.rightMargin: Style.spacing.controlPaddingX
          textFormat: Text.PlainText
          text: 'Create "' + menu.searchQuery.trim() + '"'
          color: createRowMouse.containsMouse ? Style.hoverStateColor(Color.popups.text, Color.accent) : Color.popups.text
          font.family: root.fam
          font.pixelSize: Style.font.body
          elide: Text.ElideRight
        }

        MouseArea {
          id: createRowMouse
          anchors.fill: parent
          hoverEnabled: true
          cursorShape: Qt.PointingHandCursor
          onClicked: menu.triggerCreate()
        }
      }
    }

    function addItem(text, runAction) {
      // `title` is what Model.filterByTitle() matches on; `text` stays the
      // label the row delegate renders.
      menuItems = menuItems.concat([{ title: text, text: text, runAction: runAction }])
    }

    function trigger(item) {
      if (item && typeof item.runAction === "function") item.runAction()
      close()
    }

    // Enter with a query: run the first visible row, else the create row.
    function triggerFirstOrCreate() {
      if (menu.shownItems.length > 0) menu.trigger(menu.shownItems[0])
      else menu.triggerCreate()
    }

    function triggerCreate() {
      if (typeof menu.createHandler === "function" && menu.searchQuery.trim() !== "")
        menu.createHandler(menu.searchQuery.trim())
      close()
    }

    function moveHighlight(delta) {
      var n = menu.shownItems.length
      if (n === 0) return
      if (menu.highlightIndex < 0) {
        menu.highlightIndex = delta > 0 ? 0 : n - 1
        return
      }
      var i = (menu.highlightIndex + delta) % n
      if (i < 0) i += n
      menu.highlightIndex = i
    }

    function triggerHighlighted() {
      var n = menu.shownItems.length
      if (n === 0) { menu.triggerFirstOrCreate(); return }
      var i = menu.highlightIndex < 0 ? 0 : Math.max(0, Math.min(menu.highlightIndex, n - 1))
      menu.trigger(menu.shownItems[i])
    }

    // Menu.popup(parent, x, y) replacement: x/y stay relative to parentItem.
    function popupAt(parentItem, px, py) {
      menuParent = parentItem
      parent = parentItem
      x = px
      y = py
      open()
      fitToParent()
    }

    // Native Menu flipped/clamped itself; keep the popup inside the panel so
    // right-clicks near the bottom edge don't push rows past the card.
    function fitToParent() {
      if (!opened || !menuParent) return
      if (x > menuParent.width - width) x = Math.max(0, menuParent.width - width)
      if (y > menuParent.height - height) y = Math.max(0, menuParent.height - height)
    }

    // Reset the filter on every open and hand the field the keys, so typed
    // characters land in the search box instead of the panel behind it.
    onOpened: {
      menuSearchField.text = ""
      menu.searchQuery = ""
      menu.highlightIndex = -1
      if (menu.searchable) Qt.callLater(function() { menuSearchField.forceActiveFocus() })
      else Qt.callLater(function() { menuColumn.forceActiveFocus() })
    }

    onImplicitWidthChanged: fitToParent()
    onImplicitHeightChanged: fitToParent()
  }

  MenuPopup { id: contextMenu }
  MenuPopup { id: stationMenu }
  MenuPopup { id: helpMenu }

  MenuPopup {
    id: playlistPickerMenu
    searchable: true
    searchPlaceholder: "Search or create…"
    emptyText: "No playlists yet"
  }

  MenuPopup { id: playlistOptionsMenu }

  Component.onCompleted: { root.loadThumbnail(); root.loadPlaylists(); root.restoreUiState(); root.refreshLikedSet(); root.writeCavaConfig() }

  // ---------------------------------------------------------------- surface

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
     contentWidth: Style.space(540)
    contentHeight: panel.fittedContentHeight(contentColumn.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: searchField.activeFocus || newPlaylistField.activeFocus
        || contextMenu.opened || playlistPickerMenu.opened
        || stationMenu.opened || helpMenu.opened
        || renameField.activeFocus || queueSaveField.activeFocus
        || stationField.activeFocus
      onCloseRequested: {
        if (root.deleteConfirmOpen) { root.deleteConfirmOpen = false; return }
        root.close()
      }
      onMoveRequested: function(dx, dy) {
        if (root.deleteConfirmOpen) {
          if (dx !== 0) deleteConfirm.selectedIndex = deleteConfirm.selectedIndex === 0 ? 1 : 0
          return
        }
        if (dy !== 0) {
          var list = root.activeList
          if (list.length > 0) root.selectIndex(Math.max(-1, Math.min(list.length - 1, root.selectedIndex + dy)))
        } else if (dx !== 0) {
          root.sendCmd("seek", [dx > 0 ? "5" : "-5"])
        }
      }
      onActivateRequested: function() {
        if (root.deleteConfirmOpen) { root.deleteActivePlaylist(); return }
        // The Stations tab has no keyboard list (activeListKind is ""), so
        // Enter/Space must not fall through to the transport toggle.
        if (root.activeTab === "stations") return
        if (root.selectMode && root.activeListKind === "search") {
          root.toggleRowAt(root.selectedIndex, true)
          return
        }
        if (root.activeListKind === "queue" && root.selectedIndex >= 0
            && root.selectedIndex < root.queueTracks.length) {
          root.queueJump(root.selectedIndex)
          return
        }
        if (root.activeListKind === "library" && root.selectedIndex >= 0
            && root.selectedIndex < root.libraryList.length) {
          root.openRow(root.libraryList[root.selectedIndex], false)
          return
        }
        if (root.activeListKind === "search" && root.selectedIndex >= 0
            && root.selectedIndex < root.searchResults.length) {
          root.openRow(root.searchResults[root.selectedIndex], true)
          return
        }
        if (root.activeListKind === "last" && root.selectedIndex >= 0
            && root.selectedIndex < root.lastPlayed.length) {
          root.playNow(root.lastPlayed[root.selectedIndex].videoId)
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
        if (root.deleteConfirmOpen) return
        if (root.activeListKind === "") return
        if (root.activeListKind === "library") return
        if (root.activeListKind === "last") return
        if (root.activeListKind === "queue" && root.selectedIndex >= 0
            && root.selectedIndex < root.queueTracks.length) {
          root.queueRemoveKey(Model.queueKeyAt(root.queueTracks, root.selectedIndex))
        } else if (root.playlistTracks.length > 0 && root.selectedIndex >= 0
            && root.selectedIndex < root.playlistTracks.length) {
          root.sendCmd("remove", [root.activePlaylistId, root.playlistTracks[root.selectedIndex].videoId])
        }
      }
      onTextKey: function(t) {
        if (root.deleteConfirmOpen) return
        if (t === "c") root.close()
        else if (t === "s") root.sendCmd("stop", [])
        else if (t === "m") root.toggleMute()
        else if (t === "r") root.toggleLoop()
        else if (t === "f") root.sendCmd("shuffle", [])
        else if (t === "/") { root.activeTab = "search"; searchField.forceActiveFocus() }
        else if (t === "x") {
          if (root.activeTab === "search") {
            if (root.selectMode) root.clearSelection()
            else root.selectMode = true
          }
        }
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

          // ---- not logged in (radio needs no login, so hide this while live)
          Rectangle {
            visible: !root.loggedIn && !root.radioLive
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
                  root.loginPollCount = 0
                  loginRefresh.start()
                  if (root.bar) root.bar.run("omarchy-launch-terminal " + root.ctlPath + " login")
                }
              }
            }
          }

          // ---- now playing hero
            BorderSurface {
              id: nowPlayingCard
              visible: Model.isActive(root.musicStatus) || root.lastPlayed.length > 0
              width: parent.width
              height: Style.space(136)
              radius: Style.cornerRadius
              color: Style.normalFillFor(root.fg, Color.accent)
              borderSpec: Border.flat(
                Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.45),
                Style.normalBorderWidth)

              Item {
              id: heroRow
              anchors.fill: parent

              MouseArea {
                anchors.fill: parent
                acceptedButtons: Qt.RightButton
                onClicked: function(mouse) {
                  if (!root.heroTrack || !root.isVideoId(root.heroTrack.videoId)) return
                  var point = heroRow.mapToItem(panelFlick, mouse.x, mouse.y)
                  root.openContextMenu(root.heroTrack.videoId, root.heroTrack.title, root.heroTrack.artist,
                    Model.isActive(root.musicStatus) ? "nowplaying" : "last", point.x, point.y)
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
                  source: (root.radioLive && root.musicStatus
                           && root.musicStatus.stationFavicon)
                          ? root.musicStatus.stationFavicon
                          : root.thumbnailSource
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
                anchors.left: albumArt.right
                // Each button is a 28px cell with the glyph centred, so the
                // glyph's visual left edge is inset ~9px. Pull the row left by
                // that much so the icons align with the track text above.
                anchors.leftMargin: Style.space(9)
                anchors.bottom: parent.bottom
                anchors.bottomMargin: Style.space(20)
                spacing: Style.space(6)

                Item {
                  visible: !Model.isActive(root.musicStatus) && root.heroTrack !== null
                  width: Style.space(28)
                  height: Style.space(32)
                  Text {
                    anchors.centerIn: parent
                    text: Model.ICON.play
                    color: Color.accent
                    font.family: root.fam
                    font.pixelSize: Style.font.body
                  }
                  MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: if (root.heroTrack) root.playNow(root.heroTrack.videoId)
                  }
                }

                Item {
                  visible: Model.isActive(root.musicStatus) && !root.radioLive
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
                  visible: Model.isActive(root.musicStatus)
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
                  visible: Model.isActive(root.musicStatus) && !root.radioLive
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
                  visible: Model.isActive(root.musicStatus) && !root.radioLive
                  width: Style.space(28)
                  height: Style.space(32)
                  Text {
                    anchors.centerIn: parent
                    text: Model.ICON.like
                    color: root.isCurrentLiked() ? Color.accent : root.fg
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
                  visible: Model.isActive(root.musicStatus) && !root.radioLive
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
                  visible: Model.isActive(root.musicStatus) && !root.radioLive
                  width: Style.space(28)
                  height: Style.space(32)
                  Text {
                    anchors.centerIn: parent
                    text: Model.ICON.shuffle
                    color: root.shuffling ? Color.accent : root.fg
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
                  visible: Model.isActive(root.musicStatus)
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
                  visible: Model.isActive(root.musicStatus) && !root.radioLive
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
                  visible: Model.isActive(root.musicStatus)
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
                  text: root.radioLive ? "LIVE RADIO"
                    : (Model.isActive(root.musicStatus) ? "NOW PLAYING" : "HISTORY")
                  color: Color.accent
                  font.family: root.fam
                  font.pixelSize: Style.font.caption
                  font.bold: true
                }

                Text {
                  textFormat: Text.PlainText
                  width: parent.width
                  elide: Text.ElideRight
                  text: root.radioLive
                    ? (root.musicStatus.stationName || root.musicStatus.title || "")
                    : (root.heroTrack ? (root.heroTrack.title || "") : "")
                  color: root.fg
                  font.family: root.fam
                  font.pixelSize: Style.font.heading
                  font.bold: true
                }
                Text {
                  textFormat: Text.PlainText
                  width: parent.width
                  elide: Text.ElideRight
                  // Live radio has no album line: "LIVE" plus the ICY track.
                  text: root.radioLive
                    ? ("LIVE" + (root.musicStatus.nowPlaying ? " · " + root.musicStatus.nowPlaying : ""))
                    : (root.heroTrack ? (root.heroTrack.artist || "") : "")
                  color: Qt.darker(root.fg, 1.4)
                  font.family: root.fam
                  font.pixelSize: Style.font.bodySmall
                }
              }

            }
          }

          // ---- audio visualizer (a Hero strip: cava bars)
          Item {
            id: visualizerStrip
            visible: root.visualizerOn && Model.isActive(root.musicStatus)
            width: parent.width
            height: root.visualizerStyle === "vuv" || root.visualizerStyle === "vus"
              || root.visualizerStyle === "dots" ? Style.space(64) : Style.space(22)

            // 1. Spectrum bars (bars / mirror / gradient / meter)
            Item {
              id: spectrumRenderer
              anchors.fill: parent
              visible: root.visualizerStyle === "bars" || root.visualizerStyle === "mirror"
                || root.visualizerStyle === "gradient" || root.visualizerStyle === "meter"

              Row {
                id: visualizerRow
                anchors.fill: parent
                spacing: Style.spacing.sm

                Repeater {
                  model: root.visualizerBarCount

                  delegate: Item {
                    width: (visualizerRow.width - visualizerRow.spacing * (root.visualizerBarCount - 1)) / root.visualizerBarCount
                    height: visualizerRow.height

                    Rectangle {
                      width: parent.width
                      height: Math.max(2, parent.height * (root.visualizerBars[index] || 0) / 100)
                      y: root.visualizerStyle === "mirror"
                         ? (parent.height - height) / 2
                         : parent.height - height
                      radius: width / 2
                      color: root.visualizerStyle === "gradient"
                        ? root.visualizerBarColor(root.visualizerBars[index] || 0)
                        : (root.visualizerStyle === "meter"
                          ? root.visualizerZoneColor(root.visualizerBars[index] || 0)
                          : Color.accent)
                      Behavior on height { NumberAnimation { duration: 55; easing.type: Easing.OutQuad } }
                    }
                  }
                }
              }
            }

            // 2. VU ladder (vu): equal-width LED segments filling to
            // visualizerLevel, with the peak-hold segment drawn so it bounces
            // and sags as its value decays.
            Row {
              id: vuRenderer
              anchors.fill: parent
              visible: root.visualizerStyle === "vu"
              spacing: 1

              Repeater {
                model: root.visualizerLadderSegments

                delegate: Rectangle {
                  readonly property real fraction: index / (root.visualizerLadderSegments - 1)
                  readonly property bool lit: root.visualizerLevel > 0
                    && fraction * 100 <= root.visualizerLevel
                  readonly property bool peakLit: root.visualizerPeak > 0
                    && (fraction * 100 <= root.visualizerPeak
                      && (index === root.visualizerLadderSegments - 1
                        || (index + 1) / (root.visualizerLadderSegments - 1) * 100 > root.visualizerPeak))
                  width: (vuRenderer.width - vuRenderer.spacing * (root.visualizerLadderSegments - 1)) / root.visualizerLadderSegments
                  height: vuRenderer.height
                  color: (lit || peakLit)
                    ? root.visualizerZoneColor(fraction * 100)
                    : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)
                }
              }
            }

            // 4. Vertical VU ladder (vuv): the LED ladder reversed so segment 0 is the
            // bottom, with the same green/amber/red zones and peak-hold as the
            // horizontal VU.
            Column {
              id: vuVerticalRenderer
              anchors.fill: parent
              visible: root.visualizerStyle === "vuv"
              spacing: 1

              Repeater {
                model: root.visualizerVerticalSegments

                delegate: Rectangle {
                  // The Column lays out top-to-bottom; flip the index so fraction 0 is
                  // the bottom segment.
                  readonly property int seg: root.visualizerVerticalSegments - 1 - index
                  readonly property real fraction: seg / (root.visualizerVerticalSegments - 1)
                  readonly property bool lit: root.visualizerLevel > 0
                    && fraction * 100 <= root.visualizerLevel
                  readonly property bool peakLit: root.visualizerPeak > 0
                    && (fraction * 100 <= root.visualizerPeak
                      && (seg === root.visualizerVerticalSegments - 1
                        || (seg + 1) / (root.visualizerVerticalSegments - 1) * 100 > root.visualizerPeak))
                  width: vuVerticalRenderer.width
                  height: (vuVerticalRenderer.height - vuVerticalRenderer.spacing * (root.visualizerVerticalSegments - 1)) / root.visualizerVerticalSegments
                  color: (lit || peakLit)
                    ? root.visualizerZoneColor(fraction * 100)
                    : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)
                }
              }
            }

            // 5. VU spectrum (vus): one short vertical LED ladder per frequency band.
            Item {
              id: vuSpectrumRenderer
              anchors.fill: parent
              visible: root.visualizerStyle === "vus"

              Row {
                anchors.fill: parent
                spacing: 2

                Repeater {
                  model: root.visualizerBarCount

                  delegate: Column {
                    id: bandColumn
                    readonly property int band: index
                    width: (parent.width - parent.spacing * (root.visualizerBarCount - 1)) / root.visualizerBarCount
                    height: parent.height
                    spacing: 1

                    Repeater {
                      model: root.visualizerSpectrumSegments

                      delegate: Rectangle {
                        readonly property int segFromBottom: root.visualizerSpectrumSegments - 1 - index
                        readonly property real fraction: segFromBottom / (root.visualizerSpectrumSegments - 1)
                        readonly property real value: Number(root.visualizerSpectrumBars[bandColumn.band]) || 0
                        readonly property real peak: Number(root.visualizerBandPeaks[bandColumn.band]) || 0
                        readonly property bool lit: value > 0 && fraction * 100 <= value
                        readonly property bool peakLit: peak > 0 && fraction * 100 <= peak
                          && (segFromBottom === root.visualizerSpectrumSegments - 1
                            || (segFromBottom + 1) / (root.visualizerSpectrumSegments - 1) * 100 > peak)
                        width: bandColumn.width
                        height: (bandColumn.height - bandColumn.spacing * (root.visualizerSpectrumSegments - 1)) / root.visualizerSpectrumSegments
                        color: (lit || peakLit)
                          ? root.visualizerZoneColor(fraction * 100)
                          : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)
                      }
                    }
                  }
                }
              }
            }

            // 6. VU dots (dots): one LED per band at its level (classic dot mode),
            // with a peak-hold dot above it.
            Item {
              id: vuDotsRenderer
              anchors.fill: parent
              visible: root.visualizerStyle === "dots"

              Row {
                anchors.fill: parent
                spacing: 2

                Repeater {
                  model: root.visualizerBarCount

                  delegate: Column {
                    id: dotColumn
                    readonly property int band: index
                    readonly property real value: Number(root.visualizerSpectrumBars[band]) || 0
                    readonly property real peak: Number(root.visualizerBandPeaks[band]) || 0
                    readonly property int levelLed: Math.min(root.visualizerSpectrumSegments - 1,
                      Math.floor(value * root.visualizerSpectrumSegments / 100))
                    readonly property int peakLed: Math.min(root.visualizerSpectrumSegments - 1,
                      Math.floor(peak * root.visualizerSpectrumSegments / 100))
                    width: (parent.width - parent.spacing * (root.visualizerBarCount - 1)) / root.visualizerBarCount
                    height: parent.height
                    spacing: 1

                    Repeater {
                      model: root.visualizerSpectrumSegments

                      delegate: Rectangle {
                        readonly property int segFromBottom: root.visualizerSpectrumSegments - 1 - index
                        readonly property real fraction: segFromBottom / (root.visualizerSpectrumSegments - 1)
                        readonly property bool dot: dotColumn.value > 0 && segFromBottom === dotColumn.levelLed
                        readonly property bool peakDot: dotColumn.peak > 0 && segFromBottom === dotColumn.peakLed
                        width: dotColumn.width
                        height: (dotColumn.height - dotColumn.spacing * (root.visualizerSpectrumSegments - 1)) / root.visualizerSpectrumSegments
                        radius: width / 2
                        color: (dot || peakDot)
                          ? root.visualizerZoneColor(fraction * 100)
                          : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.12)
                      }
                    }
                  }
                }
              }
            }
          }

          // ---- seek (song position only; a live stream has no seek/duration)
          Row {
            visible: Model.isActive(root.musicStatus) && !root.radioLive
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
              onMoved: function(v) {
                // PanelSlider emits moved() on every wheel notch, so coalesce
                // the target and send once the gesture settles. Sending one
                // command per notch toggled the global busy lock (which flashed
                // the row action icons) and dropped intermediate values.
                root.volumePending = v
                volumeApply.restart()
              }
            }

            Text {
              width: Style.space(56)
              height: parent.height
              verticalAlignment: Text.AlignVCenter
              horizontalAlignment: Text.AlignRight
              text: (volumeSlider.dragging || volumeApply.running
                ? Math.round(root.volumePending)
                : (root.musicStatus && root.musicStatus.volume !== undefined
                  ? root.musicStatus.volume
                  : 100)) + "%"
              color: root.fg
              font.family: root.fam
              font.pixelSize: Style.font.body
            }
          }

          Timer {
            id: volumeApply
            interval: 130
            repeat: false
            onTriggered: {
              // Wait out an in-flight command so the settled value is applied
              // rather than dropped by the single shared cmd process.
              if (root.busy) { volumeApply.restart(); return }
              var target = Math.round(root.volumePending)
              var current = root.musicStatus && root.musicStatus.volume !== undefined
                ? root.musicStatus.volume
                : 100
              if (target !== current) root.sendCmd("volume", [String(target)])
            }
          }

          // ---- lyrics toggle (a live stream has no track lyrics) + visualizer controls
          Row {
            visible: Model.isActive(root.musicStatus)
            width: parent.width
            height: Style.spacing.controlHeight
            spacing: Style.spacing.sm

            Button {
              visible: !root.radioLive
              width: Style.space(96)
              height: Style.spacing.controlHeight
              text: "Lyrics"
              fontFamily: root.fam
              fontSize: Style.font.bodySmall
              foreground: root.lyricsOpen ? Color.accent : root.fg
              onClicked: root.toggleLyrics()
            }

            Button {
              width: Style.space(36)
              height: Style.spacing.controlHeight
              iconText: Model.ICON.equalizer
              tooltipText: root.visualizerOn ? "Hide visualizer" : "Show visualizer"
              fontFamily: root.fam
              fontSize: Style.font.bodySmall
              foreground: root.visualizerOn ? Color.accent : root.fg
              onClicked: root.visualizerOn = !root.visualizerOn
            }

            Button {
              visible: root.visualizerOn
              width: Style.space(36)
              height: Style.spacing.controlHeight
              iconText: Model.ICON.sliders
              tooltipText: "Visualizer style: " + root.visualizerStyleLabel()
              fontFamily: root.fam
              fontSize: Style.font.bodySmall
              foreground: root.fg
              onClicked: root.visualizerStyle = root.visualizerStyleNext()
            }

            Button {
              visible: root.visualizerOn
              width: Style.space(56)
              height: Style.spacing.controlHeight
              text: root.visualizerChannels === "mono" ? "Mono" : "Stereo"
              tooltipText: "Visualiser channels (display only)"
              fontFamily: root.fam
              fontSize: Style.font.bodySmall
              foreground: root.fg
              onClicked: root.visualizerChannels = root.visualizerChannels === "mono"
                ? "stereo" : "mono"
            }

            Button {
              visible: root.visualizerOn
              width: Style.space(40)
              height: Style.spacing.controlHeight
              text: root.visualizerScaling === "decibel" ? "dB" : "LIN"
              tooltipText: "Visualiser scaling (linear / decibel)"
              fontFamily: root.fam
              fontSize: Style.font.bodySmall
              foreground: root.fg
              onClicked: root.visualizerScaling = root.visualizerScaling === "decibel"
                ? "linear" : "decibel"
            }
          }

          // ---- lyrics
          Column {
            visible: root.lyricsOpen
            width: parent.width
            spacing: Style.spacing.sm

            Item {
              width: parent.width
              height: Style.space(24)

              PanelSectionHeader {
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                text: "LYRICS"
                foreground: root.fg
                fontFamily: root.fam
              }

              Button {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                width: Style.space(32)
                height: Style.space(24)
                iconText: Model.ICON.close
                tooltipText: "Close lyrics"
                fontFamily: root.fam
                foreground: root.fg
                onClicked: root.lyricsOpen = false
              }
            }

            Text {
              visible: root.lyricsLoading
              textFormat: Text.PlainText
              text: "Loading…"
              color: Qt.darker(root.fg, 1.4)
              font.family: root.fam
              font.pixelSize: Style.font.bodySmall
            }

            Text {
              visible: !root.lyricsLoading && !root.lyricsHas
              textFormat: Text.PlainText
              text: "No lyrics available."
              color: Qt.darker(root.fg, 1.4)
              font.family: root.fam
              font.pixelSize: Style.font.bodySmall
            }

            Flickable {
              visible: !root.lyricsLoading && root.lyricsHas && root.lyricsText !== ""
              width: parent.width
              height: Math.min(contentHeight, Style.space(220))
              contentWidth: width
              contentHeight: lyricsBody.implicitHeight
              clip: true
              boundsBehavior: Flickable.StopAtBounds
              flickableDirection: Flickable.VerticalFlick
              interactive: contentHeight > height
              ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

              Text {
                id: lyricsBody
                width: parent.width
                textFormat: Text.PlainText
                text: root.lyricsText
                wrapMode: Text.WordWrap
                color: root.fg
                font.family: root.fam
                font.pixelSize: Style.font.bodySmall
              }
            }
          }

          // ---- content tabs (Up Next / Search / Playlists / Library)
          Row {
            id: tabStrip
            visible: root.tabItems.length > 1
            width: parent.width - Style.space(40)
            height: Style.spacing.controlHeight
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Style.spacing.sm

            Repeater {
              model: root.tabItems
              delegate: Button {
                // Leave room for the help button on the right.
                width: (tabStrip.width - Style.spacing.sm * root.tabItems.length
                        - Style.space(28)) / root.tabItems.length
                height: Style.spacing.controlHeight
                text: modelData.label
                fontFamily: root.fam
                fontSize: Style.font.bodySmall
                selected: root.activeTab === modelData.key
                bordered: true
                foreground: root.fg
                onClicked: root.toggleTab(modelData.key)
              }
            }

            Button {
              id: helpButton
              width: Style.space(28)
              height: Style.spacing.controlHeight
              text: "?"
              tooltipText: "Keyboard & mouse help"
              fontFamily: root.fam
              fontSize: Style.font.bodySmall
              bordered: true
              foreground: root.fg
              onClicked: root.openHelp(helpButton)
            }
          }

          // Active tab content scrolls in its own viewport under the tab
          // strip, so the now-playing card and tabs stay fixed on every tab.
          Flickable {
            id: tabFlick
            width: parent.width
            height: {
              var wanted = tabBody.implicitHeight
              if (!isFinite(wanted)) wanted = root.tabBodyMaxHeight
              return Math.min(Math.max(0, wanted), root.tabBodyMaxHeight)
            }
            contentWidth: width
            contentHeight: tabBody.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            flickableDirection: Flickable.VerticalFlick
            interactive: contentHeight > height
            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

            Column {
              id: tabBody
              width: tabFlick.width
              spacing: Style.spacing.panelGap
            // ---- up next (queue)
            Column {
              id: queueSection
              visible: root.activeTab === "queue"
              width: parent.width
              spacing: Style.space(6)

              PanelSeparator {
                foreground: root.fg
              }

              Row {
                width: parent.width
                height: Style.spacing.controlHeight
                spacing: Style.spacing.sm

                PanelSectionHeader {
                  id: queueHeaderTitle
                  text: "UP NEXT"
                  foreground: root.fg
                  fontFamily: root.fam
                  height: parent.height
                  verticalAlignment: Text.AlignVCenter
                }

                Item {
                  width: Math.max(0, parent.width - queueHeaderTitle.implicitWidth
                    - queueHeaderActions.implicitWidth - Style.spacing.sm * 2)
                  height: Style.spacing.hairline
                }

                Row {
                  id: queueHeaderActions
                  spacing: Style.spacing.sm

                  Button {
                    width: Style.space(60)
                    height: Style.spacing.controlHeight
                    text: "Resume"
                    fontFamily: root.fam
                    fontSize: Style.font.bodySmall
                    foreground: root.fg
                    visible: root.queueSaved && root.queueTracks.length > 0 && !root.selectMode
                    enabled: !root.busy
                    onClicked: root.restoreSession()
                  }

                  Button {
                    width: Style.space(52)
                    height: Style.spacing.controlHeight
                    text: root.selectMode ? "Done" : "Select"
                    iconText: root.selectMode ? Model.ICON.check : ""
                    fontFamily: root.fam
                    fontSize: Style.font.bodySmall
                    foreground: root.fg
                    visible: root.queueTracks.length > 0
                    onClicked: {
                      if (root.selectMode) root.clearSelection()
                      else root.selectMode = true
                    }
                  }

                  Button {
                    width: Style.space(52)
                    height: Style.spacing.controlHeight
                    text: "Save"
                    fontFamily: root.fam
                    fontSize: Style.font.bodySmall
                    foreground: root.fg
                    visible: root.queueTracks.length > 0 && !root.selectMode
                    enabled: !root.busy
                    onClicked: root.queueSaveOpen = true
                  }

                  Button {
                    width: Style.space(52)
                    height: Style.spacing.controlHeight
                    text: "Clear"
                    fontFamily: root.fam
                    fontSize: Style.font.bodySmall
                    foreground: root.fg
                    visible: !root.selectMode
                    enabled: (root.queueSaved ? root.queueTracks.length > 0
                                              : root.queueUpcomingCount() > 0) && !root.busy
                    onClicked: root.clearQueue()
                  }
                }
              }

              Row {
                visible: root.selectMode
                width: parent.width - Style.space(40)
                height: Style.spacing.controlHeight
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: Style.spacing.sm

                Text {
                  textFormat: Text.PlainText
                  height: Style.spacing.controlHeight
                  verticalAlignment: Text.AlignVCenter
                  text: root.selectedCount + " selected"
                  color: root.fg
                  font.family: root.fam
                  font.pixelSize: Style.font.caption
                }

                Button {
                  id: queueAddSelectedButton
                  width: Style.space(120)
                  height: Style.spacing.controlHeight
                  text: "Add to playlist…"
                  iconText: Model.ICON.plus
                  fontFamily: root.fam
                  fontSize: Style.font.bodySmall
                  foreground: root.fg
                  enabled: root.selectedCount > 0 && root.loggedIn && !root.busy
                  onClicked: root.openPlaylistPickerForSelection(queueAddSelectedButton)
                }

                Button {
                  width: Style.space(72)
                  height: Style.spacing.controlHeight
                  text: "Remove"
                  fontFamily: root.fam
                  fontSize: Style.font.bodySmall
                  foreground: root.fg
                  enabled: root.selectedCount > 0 && !root.busy
                  onClicked: root.removeSelectedFromQueue()
                }
              }

              Row {
                visible: root.queueSaveOpen
                width: parent.width - Style.space(40)
                height: Style.spacing.controlHeight
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: Style.spacing.sm

                TextField {
                  id: queueSaveField
                  width: parent.width - Style.space(52) - Style.space(80) - parent.spacing * 2
                  height: Style.spacing.controlHeight
                  placeholderText: "New playlist name"
                  foreground: root.fg
                  hasCursor: false
                  onAccepted: root.saveQueueAsPlaylist()
                }

                Button {
                  width: Style.space(52)
                  height: Style.spacing.controlHeight
                  text: "Save"
                  fontFamily: root.fam
                  fontSize: Style.font.bodySmall
                  foreground: root.fg
                  enabled: !root.busy
                  onClicked: root.saveQueueAsPlaylist()
                }

                Button {
                  width: Style.space(80)
                  height: Style.spacing.controlHeight
                  text: "Cancel"
                  fontFamily: root.fam
                  fontSize: Style.font.bodySmall
                  foreground: root.fg
                  onClicked: root.queueSaveOpen = false
                }
              }

              // The list scrolls on its own so the panel chrome above it -
              // now-playing card, tabs and the UP NEXT header - stays fixed.
              Flickable {
                id: queueListFlick
                width: parent.width
                // Bound the viewport to the space left under the fixed chrome.
                // The positioner coordinates can be momentarily undefined while
                // the section lays out, so guard against a non-finite result: a
                // NaN here would collapse the whole panel.
                height: {
                  if (root.queueTracks.length === 0) return 0
                  var top = (queueSection.y || 0) + (queueListFlick.y || 0)
                  var avail = root.tabBodyMaxHeight - top
                  if (!isFinite(avail) || avail < Style.space(96)) avail = Style.space(96)
                  var wanted = queueListContent.implicitHeight
                  if (!isFinite(wanted)) return avail
                  return Math.max(Style.space(96), Math.min(wanted, avail))
                }
                contentWidth: width
                contentHeight: queueListContent.implicitHeight
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                flickableDirection: Flickable.VerticalFlick
                interactive: contentHeight > height
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                Column {
                  id: queueListContent
                  width: parent.width
                  spacing: 0

                  Repeater {
                    id: queueRepeater
                    model: root.queueTracks
                    delegate: Item {
                      id: queueRow
                      width: contentColumn.width
                      height: Style.space(32)

                      RowHighlight {
                        id: queueRowBg
                        foreground: root.fg
                        multi: root.isRowSelected(modelData)
                        hasCursor: index === root.selectedIndex
                        hovered: queueRowClick.containsMouse
                        current: modelData.current || index === root.queuePosition
                      }

                      MouseArea {
                        id: queueRowClick
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: function(mouse) {
                          if (root.handleRowClick(index, mouse.modifiers)) return
                          root.selectIndex(index)
                        }
                      }

                      Row {
                        anchors.fill: parent
                        spacing: Style.spacing.sm

                        Item {
                          width: Style.space(20)
                          height: parent.height

                          Text {
                            visible: !root.selectMode
                            anchors.left: parent.left
                            width: Style.space(20)
                            textFormat: Text.PlainText
                            text: modelData.stream
                              ? Model.ICON.globe
                              : (modelData.current
                                 || index === root.queuePosition)
                                ? Model.ICON.play
                                : String(modelData.number || (index + 1))
                            color: (modelData.stream || modelData.current
                                    || index === root.queuePosition)
                              ? Color.accent
                              : Qt.darker(root.fg, 1.4)
                            font.family: root.fam
                            font.pixelSize: Style.font.caption
                            verticalAlignment: Text.AlignVCenter
                          }

                          Rectangle {
                            visible: root.selectMode && !root.isRowSelected(modelData)
                            anchors.centerIn: parent
                            width: Style.space(12)
                            height: Style.space(12)
                            radius: width / 2
                            color: "transparent"
                            border.width: Style.normalBorderWidth
                            border.color: Qt.darker(root.fg, 1.3)
                          }

                          Text {
                            visible: root.isRowSelected(modelData)
                            anchors.centerIn: parent
                            textFormat: Text.PlainText
                            text: Model.ICON.check
                            color: Color.accent
                            font.family: root.fam
                            font.pixelSize: Style.font.caption
                          }
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
                            text: root.songSubtitle(modelData)
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

                        PanelActionButton {
                          width: Style.space(44)
                          height: Style.space(28)
                          iconText: Model.ICON.play
                          tooltipText: "Jump to track"
                          fontFamily: root.fam
                          foreground: root.fg
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

              Text {
                visible: root.queueTracks.length === 0 && !queueListProc.running
                width: parent.width
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                text: "Queue is empty. Right-click any track and choose “Add to queue”."
                color: Qt.darker(root.fg, 1.4)
                font.family: root.fam
                font.pixelSize: Style.font.bodySmall
              }
            }

              // ---- last played (local history)
              Column {
                visible: root.activeTab === "last"
                width: parent.width
                spacing: Style.space(6)

                PanelSeparator {
                  foreground: root.fg
                }

                Row {
                  width: parent.width
                  height: Style.spacing.controlHeight
                  spacing: Style.spacing.sm

                  PanelSectionHeader {
                    text: "HISTORY"
                    foreground: root.fg
                    fontFamily: root.fam
                    height: parent.height
                    verticalAlignment: Text.AlignVCenter
                  }

                  Item {
                    width: parent.width - Style.space(150)
                    height: Style.spacing.hairline
                    visible: root.lastPlayed.length > 0
                  }

                  Button {
                    width: Style.space(52)
                    height: Style.spacing.controlHeight
                    text: "Clear"
                    fontFamily: root.fam
                    fontSize: Style.font.bodySmall
                    foreground: root.fg
                    enabled: root.lastPlayed.length > 0 && !root.busy
                    onClicked: root.clearLastPlayed()
                  }
                }

                Flickable {
                  id: lastList
                  width: parent.width
                  height: root.listViewportHeight(lastList, lastListContent.implicitHeight)
                  contentWidth: width
                  contentHeight: lastListContent.implicitHeight
                  clip: true
                  boundsBehavior: Flickable.StopAtBounds
                  flickableDirection: Flickable.VerticalFlick
                  interactive: contentHeight > height
                  ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                  Column {
                    id: lastListContent
                    width: parent.width
                    spacing: 0

                    Column {
                      width: parent.width
                      spacing: 0

                      Repeater {
                        id: lastRepeater
                        model: root.lastPlayed
                        delegate: Item {
                          id: lastRow
                          width: contentColumn.width
                          height: Style.space(32)

                          RowHighlight {
                            id: lastRowBg
                            foreground: root.fg
                            hasCursor: index === root.selectedIndex
                            hovered: lastRowClick.containsMouse
                          }

                          MouseArea {
                            id: lastRowClick
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: { root.selectIndex(index); root.playNow(modelData.videoId) }
                          }

                          Row {
                            anchors.fill: parent
                            spacing: Style.spacing.sm

                            Text {
                              textFormat: Text.PlainText
                              width: Style.space(20)
                              text: Model.ICON.play
                              color: Qt.darker(root.fg, 1.4)
                              font.family: root.fam
                              font.pixelSize: Style.font.caption
                              verticalAlignment: Text.AlignVCenter
                            }

                            Column {
                              width: parent.width - Style.space(20) - Style.space(56)
                                - Style.spacing.sm * 2
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
                                text: root.songSubtitle(modelData)
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
                          }

                          MouseArea {
                            anchors.fill: parent
                            acceptedButtons: Qt.RightButton
                            onClicked: function(mouse) {
                              var point = lastRow.mapToItem(panelFlick, mouse.x, mouse.y)
                              root.openContextMenu(modelData.videoId, modelData.title, modelData.artist, "last",
                                point.x, point.y)
                              mouse.accepted = true
                            }
                          }
                        }
                      }
                    }
                  }
                }


                Text {
                  visible: root.lastPlayed.length === 0
                  width: parent.width
                  topPadding: Style.space(8)
                  text: "Nothing played yet."
                  color: Qt.darker(root.fg, 1.4)
                  font.family: root.fam
                  font.pixelSize: Style.font.bodySmall
                }
              }

              // ---- search
              Column {
                visible: root.activeTab === "search" && !root.detailActive
                width: parent.width
                spacing: Style.spacing.sm

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

                Row {
                  width: parent.width - Style.space(40)
                  anchors.horizontalCenter: parent.horizontalCenter
                  spacing: Style.spacing.sm
                  visible: searchField.text !== "" || root.searchQuery !== "" || root.searching || root.searchResults.length > 0

                  Repeater {
                    model: [
                      { key: "songs", label: "Songs" },
                      { key: "albums", label: "Albums" },
                      { key: "artists", label: "Artists" },
                      { key: "playlists", label: "Playlists" }
                    ]
                    delegate: Button {
                      width: (parent.width - Style.spacing.sm * 3) / 4
                      height: Style.spacing.controlHeight
                      text: modelData.label
                      fontFamily: root.fam
                      fontSize: Style.font.bodySmall
                      selected: root.searchFilter === modelData.key
                      active: root.searchFilter === modelData.key
                      bordered: true
                      foreground: root.fg
                      enabled: !root.searching
                      onClicked: {
                        if (root.searchFilter !== modelData.key) {
                          root.searchFilter = modelData.key
                          if (searchField.text.trim() !== "") root.search(searchField.text)
                        }
                      }
                    }
                  }
                }
              }

            // ---- search results
            Column {
              visible: root.activeTab === "search" && !root.detailActive
                && (root.searchResults.length > 0 || root.searching || root.searchQuery !== "")
              width: parent.width
              spacing: Style.spacing.panelGap

              Text {
                width: parent.width
                visible: !root.searching && root.searchResults.length === 0
                  && root.searchQuery !== ""
                textFormat: Text.PlainText
                text: "No results for \"" + root.searchQuery + "\""
                color: Qt.darker(root.fg, 1.4)
                font.family: root.fam
                font.pixelSize: Style.font.bodySmall
                horizontalAlignment: Text.AlignHCenter
                elide: Text.ElideRight
              }

              Item {
                width: parent.width
                height: Style.space(28)
                visible: root.searchResults.length > 0

                Row {
                  anchors.left: parent.left
                  spacing: Style.spacing.sm
                  visible: !root.selectMode

                  Button {
                    width: Style.space(72)
                    height: Style.space(28)
                    text: "Play all"
                    fontFamily: root.fam
                    fontSize: Style.font.bodySmall
                    foreground: root.fg
                    visible: root.searchFilter === "songs" && root.songCount(root.searchResults) > 0
                    enabled: !root.busy && root.songCount(root.searchResults) > 0
                    onClicked: root.enqueueFiles("play")
                  }

                  Button {
                    width: Style.space(72)
                    height: Style.space(28)
                    text: "Queue all"
                    fontFamily: root.fam
                    fontSize: Style.font.bodySmall
                    foreground: root.fg
                    visible: root.searchFilter === "songs" && root.songCount(root.searchResults) > 0
                    enabled: !root.busy && root.songCount(root.searchResults) > 0
                    onClicked: root.enqueueFiles("queue")
                  }
                }

                Row {
                  anchors.left: parent.left
                  spacing: Style.spacing.sm
                  visible: root.selectMode

                  Text {
                    textFormat: Text.PlainText
                    height: Style.space(28)
                    verticalAlignment: Text.AlignVCenter
                    text: root.selectedCount + " selected"
                    color: root.fg
                    font.family: root.fam
                    font.pixelSize: Style.font.caption
                  }

                  Button {
                    id: addSelectedButton
                    width: Style.space(120)
                    height: Style.space(28)
                    text: "Add to playlist…"
                    iconText: Model.ICON.plus
                    fontFamily: root.fam
                    fontSize: Style.font.bodySmall
                    foreground: root.fg
                    enabled: root.selectedCount > 0 && root.loggedIn && !root.busy
                    onClicked: root.openPlaylistPickerForSelection(addSelectedButton)
                  }

                  Button {
                    width: Style.space(72)
                    height: Style.space(28)
                    text: "Queue"
                    visible: root.selectionAllSongs
                    fontFamily: root.fam
                    fontSize: Style.font.bodySmall
                    foreground: root.fg
                    enabled: !root.busy
                    onClicked: {
                      var ids = Model.videoIds(root.selectedRows)
                      if (ids.length > 0) root.sendCmd("enqueue-files", ["queue"].concat(ids))
                      root.clearSelection()
                    }
                  }
                }

                Button {
                  id: selectToggleButton
                  anchors.right: parent.right
                  width: Style.space(72)
                  height: Style.space(28)
                  text: root.selectMode ? "Done" : "Select"
                  iconText: root.selectMode ? Model.ICON.check : ""
                  fontFamily: root.fam
                  fontSize: Style.font.bodySmall
                  foreground: root.fg
                  bordered: true
                  onClicked: {
                    if (root.selectMode) root.clearSelection()
                    else root.selectMode = true
                  }
                }
              }

              Text {
                visible: root.selectMode
                width: parent.width
                wrapMode: Text.WordWrap
                textFormat: Text.PlainText
                text: "Click rows to select. Shift-click for a range, Ctrl-click to toggle."
                color: Qt.darker(root.fg, 1.4)
                font.family: root.fam
                font.pixelSize: Style.font.caption
              }

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

              Flickable {
                id: searchList
                width: parent.width
                height: root.listViewportHeight(searchList, searchListContent.implicitHeight)
                contentWidth: width
                contentHeight: searchListContent.implicitHeight
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                flickableDirection: Flickable.VerticalFlick
                interactive: contentHeight > height
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                Column {
                  id: searchListContent
                  width: parent.width
                  spacing: 0

                  Repeater {
                    id: searchRepeater
                    model: root.searchResults
                    delegate: Item {
                      id: searchRow
                      width: contentColumn.width
                      height: Style.space(40)

                      RowHighlight {
                        id: searchRowBg
                        foreground: root.fg
                        multi: root.isRowSelected(modelData)
                        hasCursor: index === root.selectedIndex
                        hovered: searchRowClick.containsMouse
                      }

                      MouseArea {
                        id: searchRowClick
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: function(mouse) {
                          if (root.handleRowClick(index, mouse.modifiers)) return
                          root.selectIndex(index)
                          if (modelData.kind !== "song") root.openRow(modelData, true)
                        }
                      }

                      Row {
                        anchors.fill: parent
                        spacing: Style.spacing.sm

                        Item {
                          width: Style.space(24)
                          height: parent.height

                          Text {
                            visible: !root.selectMode
                            anchors.left: parent.left
                            anchors.verticalCenter: parent.verticalCenter
                            textFormat: Text.PlainText
                            width: Style.space(24)
                            text: modelData.kind === "song" ? Model.ICON.note
                              : (modelData.kind === "playlist" ? Model.ICON.playlist : Model.ICON.music)
                            color: Color.accent
                            font.family: root.fam
                            font.pixelSize: Style.font.bodySmall
                            verticalAlignment: Text.AlignVCenter
                          }

                          Rectangle {
                            visible: root.selectMode && !root.isRowSelected(modelData)
                            anchors.centerIn: parent
                            width: Style.space(14)
                            height: Style.space(14)
                            radius: width / 2
                            color: "transparent"
                            border.width: Style.normalBorderWidth
                            border.color: Qt.darker(root.fg, 1.3)
                          }

                          Text {
                            visible: root.isRowSelected(modelData)
                            anchors.centerIn: parent
                            textFormat: Text.PlainText
                            text: Model.ICON.check
                            color: Color.accent
                            font.family: root.fam
                            font.pixelSize: Style.font.bodySmall
                          }
                        }

                        Column {
                          width: modelData.kind === "song"
                            ? parent.width - Style.space(24) - Style.space(176)
                              - Style.spacing.sm * 4
                            : parent.width - Style.space(24) - Style.space(44)
                              - Style.spacing.sm * 2
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
                            text: root.songSubtitle(modelData)
                            color: Qt.darker(root.fg, 1.4)
                            font.family: root.fam
                            font.pixelSize: Style.font.caption
                          }
                        }

                        Text {
                          visible: modelData.kind === "song"
                          textFormat: Text.PlainText
                          width: Style.space(80)
                          text: Model.fmtDuration(modelData.duration)
                          horizontalAlignment: Text.AlignRight
                          color: Qt.darker(root.fg, 1.4)
                          font.family: root.fam
                          font.pixelSize: Style.font.caption
                          verticalAlignment: Text.AlignVCenter
                        }

                        PanelActionButton {
                          visible: modelData.kind === "song"
                          width: Style.space(52)
                          height: Style.space(28)
                          iconText: Model.ICON.play
                          tooltipText: "Play"
                          fontFamily: root.fam
                          foreground: root.fg
                          onClicked: root.playNow(modelData.videoId)
                        }

                        PanelActionButton {
                          visible: modelData.kind === "song"
                          width: Style.space(44)
                          height: Style.space(28)
                          iconText: Model.ICON.shuffle
                          tooltipText: "Start mix"
                          fontFamily: root.fam
                          foreground: root.fg
                          onClicked: root.playMix(modelData.videoId)
                        }

                        Text {
                          visible: modelData.kind !== "song"
                          textFormat: Text.PlainText
                          width: Style.space(44)
                          text: "›"
                          horizontalAlignment: Text.AlignRight
                          color: Color.accent
                          font.family: root.fam
                          font.pixelSize: Style.font.body
                          verticalAlignment: Text.AlignVCenter
                        }
                      }

                      MouseArea {
                        anchors.fill: parent
                        acceptedButtons: Qt.RightButton
                        onClicked: function(mouse) {
                          var point = searchRow.mapToItem(panelFlick, mouse.x, mouse.y)
                          root.openRowMenu(modelData, "search", point.x, point.y)
                          mouse.accepted = true
                        }
                      }
                    }
                  }
                }
              }

            }

            // ---- playlists
            Column {
              visible: root.loggedIn && root.activeTab === "playlists" && !root.detailActive
              width: parent.width
              spacing: Style.spacing.panelGap

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

              Text {
                visible: root.playlists.length === 0 && !playlistsProc.running
                textFormat: Text.PlainText
                text: "No playlists yet."
                color: Qt.darker(root.fg, 1.4)
                font.family: root.fam
                font.pixelSize: Style.font.bodySmall
              }

              Flickable {
                id: playlistsList
                width: parent.width
                height: root.listViewportHeight(playlistsList, playlistsListContent.implicitHeight)
                contentWidth: width
                contentHeight: playlistsListContent.implicitHeight
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                flickableDirection: Flickable.VerticalFlick
                interactive: contentHeight > height
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                Column {
                  id: playlistsListContent
                  width: parent.width
                  spacing: 0

                  Repeater {
                    model: root.playlists
                    delegate: Item {
                      id: playlistRow
                      width: contentColumn.width
                      height: Style.space(40)

                      RowHighlight {
                        id: playlistRowBg
                        foreground: root.fg
                        hasCursor: index === root.selectedIndex
                        hovered: playlistRowClick.containsMouse
                      }

                      MouseArea {
                        id: playlistRowClick
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.openPlaylist(modelData.id, modelData.title)
                      }

                      Row {
                        anchors.fill: parent
                        spacing: Style.spacing.sm

                        Text {
                          textFormat: Text.PlainText
                          width: Style.space(24)
                          text: Model.ICON.playlist
                          color: Color.accent
                          font.family: root.fam
                          font.pixelSize: Style.font.bodySmall
                          verticalAlignment: Text.AlignVCenter
                        }

                        Column {
                          width: parent.width - Style.space(24) - Style.space(56)
                            - Style.space(40) - Style.spacing.sm * 3
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
                            visible: modelData.description !== ""
                            textFormat: Text.PlainText
                            width: parent.width
                            elide: Text.ElideRight
                            text: modelData.description
                            color: Qt.darker(root.fg, 1.4)
                            font.family: root.fam
                            font.pixelSize: Style.font.caption
                          }
                        }

                        Text {
                          textFormat: Text.PlainText
                          width: Style.space(40)
                          text: "›"
                          horizontalAlignment: Text.AlignRight
                          color: Color.accent
                          font.family: root.fam
                          font.pixelSize: Style.font.body
                          verticalAlignment: Text.AlignVCenter
                        }
                      }
                    }
                  }
                }
              }

             }

              // ---- playlist tracks view
            Column {
              visible: root.playlistDetail && root.playlistTracks.length > 0
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
                  id: playlistOptionsButton
                  anchors.right: playPlaylistButton.left
                  anchors.rightMargin: Style.space(4)
                  anchors.verticalCenter: parent.verticalCenter
                  width: Style.space(32)
                  height: Style.space(24)
                  iconText: Model.ICON.more
                  tooltipText: "Playlist options"
                  fontFamily: root.fam
                  foreground: root.fg
                  onClicked: root.openPlaylistOptions(playlistOptionsButton)
                }

                Button {
                  id: playPlaylistButton
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  width: Style.space(32)
                  height: Style.space(24)
                  iconText: Model.ICON.play
                  tooltipText: "Play playlist"
                  fontFamily: root.fam
                  foreground: Color.accent
                  onClicked: root.playSelectedPlaylist()
                }

              }

              Row {
                visible: root.renameOpen
                width: parent.width - Style.space(40)
                height: Style.spacing.controlHeight
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: Style.spacing.sm

                TextField {
                  id: renameField
                  width: parent.width - Style.space(52) - Style.space(80) - parent.spacing * 2
                  height: Style.spacing.controlHeight
                  placeholderText: "Playlist name"
                  foreground: root.fg
                  hasCursor: false
                  onAccepted: root.renameActivePlaylist()
                  onVisibleChanged: if (visible) {
                    text = root.activePlaylistTitle
                    forceActiveFocus()
                  }
                }

                Button {
                  width: Style.space(52)
                  height: Style.spacing.controlHeight
                  text: "Save"
                  fontFamily: root.fam
                  fontSize: Style.font.bodySmall
                  foreground: root.fg
                  enabled: !root.busy
                  onClicked: root.renameActivePlaylist()
                }

                Button {
                  width: Style.space(80)
                  height: Style.spacing.controlHeight
                  text: "Cancel"
                  fontFamily: root.fam
                  fontSize: Style.font.bodySmall
                  foreground: root.fg
                  onClicked: root.renameOpen = false
                }
              }

              Flickable {
                id: trackList
                width: parent.width
                height: root.listViewportHeight(trackList, trackListContent.implicitHeight)
                contentWidth: width
                contentHeight: trackListContent.implicitHeight
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                flickableDirection: Flickable.VerticalFlick
                interactive: contentHeight > height
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                Column {
                  id: trackListContent
                  width: parent.width
                  spacing: 0

                  Repeater {
                    id: trackRepeater
                    model: root.playlistTracks
                    delegate: Item {
                      id: trackRow
                      width: contentColumn.width
                      height: Style.space(36)

                      RowHighlight {
                        id: trackRowBg
                        foreground: root.fg
                        hasCursor: index === root.selectedIndex
                        hovered: trackRowClick.containsMouse
                      }

                      MouseArea {
                        id: trackRowClick
                        anchors.fill: parent
                        hoverEnabled: true
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
                            text: root.songSubtitle(modelData)
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

                        PanelActionButton {
                          width: Style.space(52)
                          height: Style.space(28)
                          iconText: Model.ICON.play
                          tooltipText: "Play"
                          fontFamily: root.fam
                          foreground: root.fg
                          onClicked: root.playNow(modelData.videoId)
                        }

                        PanelActionButton {
                          width: Style.space(44)
                          height: Style.space(28)
                          iconText: Model.ICON.shuffle
                          tooltipText: "Start mix"
                          fontFamily: root.fam
                          foreground: root.fg
                          onClicked: root.playMix(modelData.videoId)
                        }
                      }

                      MouseArea {
                        anchors.fill: parent
                        acceptedButtons: Qt.RightButton
                        onClicked: function(mouse) {
                          var point = trackRow.mapToItem(panelFlick, mouse.x, mouse.y)
                          root.openContextMenu(modelData.videoId, modelData.title, modelData.artist, "track", point.x, point.y, index)
                          mouse.accepted = true
                        }
                      }
                    }
                  }
                }
              }

            }

            Text {
              visible: root.playlistDetail
                && (root.loadingText !== ""
                  || (root.playlistTracks.length === 0 && !tracksProc.running))
              textFormat: Text.PlainText
              text: root.loadingText !== "" ? root.loadingText : "Playlist is empty."
              color: Qt.darker(root.fg, 1.4)
              font.family: root.fam
              font.pixelSize: Style.font.bodySmall
            }

            // ---- stations (internet radio; no YouTube login required)
            Column {
              id: stationsSection
              visible: root.activeTab === "stations"
              width: parent.width
              spacing: Style.space(6)

              PanelSeparator {
                foreground: root.fg
              }

              Row {
                width: parent.width
                height: Style.spacing.controlHeight
                spacing: Style.spacing.sm

                PanelSectionHeader {
                  text: "STATIONS"
                  foreground: root.fg
                  fontFamily: root.fam
                  height: parent.height
                  verticalAlignment: Text.AlignVCenter
                }
              }

              Row {
                width: parent.width - Style.space(40)
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: Style.spacing.sm

                Repeater {
                  model: [
                    { key: "featured", label: "Featured" },
                    { key: "favorites", label: "Favorites" },
                    { key: "search", label: "Search" }
                  ]
                  delegate: Button {
                    width: (parent.width - Style.spacing.sm * 2) / 3
                    height: Style.spacing.controlHeight
                    text: modelData.label
                    fontFamily: root.fam
                    fontSize: Style.font.bodySmall
                    selected: root.stationSection === modelData.key
                    active: root.stationSection === modelData.key
                    bordered: true
                    foreground: root.fg
                    onClicked: root.stationSection = modelData.key
                  }
                }
              }

              Row {
                visible: root.stationSection === "search"
                width: parent.width - Style.space(40)
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: Style.spacing.sm

                TextField {
                  id: stationField
                  width: parent.width - Style.space(44) - Style.spacing.sm
                  height: Style.spacing.controlHeight
                  placeholderText: root.stationSearchMode === "tag"
                    ? "Search stations by genre..."
                    : "Search stations by name..."
                  horizontalAlignment: Text.AlignHCenter
                  foreground: root.fg
                  hasCursor: false
                  onTextChanged: root.stationQuery = text.trim()
                  onAccepted: {
                    // Enter searches now; cancel the pending debounce so it
                    // cannot fire a second, duplicate lookup.
                    stationSearchDebounce.stop()
                    root.searchStations(text)
                  }
                }

                Button {
                  width: Style.space(44)
                  height: Style.spacing.controlHeight
                  iconText: Model.ICON.close
                  tooltipText: "Clear station search"
                  fontFamily: root.fam
                  foreground: root.fg
                  visible: stationField.text !== "" || root.stationResults.length > 0
                  onClicked: {
                    stationField.text = ""
                    root.stationQuery = ""
                    root.stationResults = []
                  }
                }
              }

              Row {
                visible: root.stationSection === "search"
                width: parent.width - Style.space(40)
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: Style.spacing.sm

                Repeater {
                  model: [
                    { key: "name", label: "Name" },
                    { key: "tag", label: "Genre" }
                  ]
                  delegate: Button {
                    width: (parent.width - Style.spacing.sm) / 2
                    height: Style.spacing.controlHeight
                    text: modelData.label
                    fontFamily: root.fam
                    fontSize: Style.font.bodySmall
                    selected: root.stationSearchMode === modelData.key
                    active: root.stationSearchMode === modelData.key
                    bordered: true
                    foreground: root.fg
                    enabled: !stationSearchProc.running
                    onClicked: {
                      if (root.stationSearchMode !== modelData.key) {
                        root.stationSearchMode = modelData.key
                        if (root.stationQuery !== "") {
                          root.stationResults = []
                          root.searchStations(root.stationQuery)
                        }
                      }
                    }
                  }
                }
              }

              Flickable {
                id: stationList
                width: parent.width
                height: root.listViewportHeight(stationList, stationListContent.implicitHeight)
                contentWidth: width
                contentHeight: stationListContent.implicitHeight
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                flickableDirection: Flickable.VerticalFlick
                interactive: contentHeight > height
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                Column {
                  id: stationListContent
                  width: parent.width
                  spacing: 0

                  Repeater {
                    id: stationRepeater
                    model: root.stationRows
                    delegate: Item {
                      id: stationRow
                      width: contentColumn.width
                      height: Style.space(40)

                      RowHighlight {
                        id: stationRowBg
                        foreground: root.fg
                        hasCursor: index === root.selectedIndex
                        hovered: stationRowClick.containsMouse
                        current: root.radioLive
                          && String(modelData.id) === String(root.musicStatus.stationId)
                      }

                      MouseArea {
                        id: stationRowClick
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.playStation(modelData)
                      }

                      Row {
                        anchors.fill: parent
                        spacing: Style.spacing.sm

                        Rectangle {
                          width: Style.space(24)
                          height: Style.space(24)
                          anchors.verticalCenter: parent.verticalCenter
                          radius: Style.space(3)
                          color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.1)
                          clip: true

                          Image {
                            id: stationLogo
                            anchors.fill: parent
                            source: modelData.favicon ? modelData.favicon : ""
                            asynchronous: true
                            cache: true
                            fillMode: Image.PreserveAspectFit
                            onStatusChanged: if (status === Image.Error) visible = false
                          }

                          Text {
                            anchors.centerIn: parent
                            visible: stationLogo.status !== Image.Ready
                            text: Model.ICON.globe
                            color: Color.accent
                            font.family: root.fam
                            font.pixelSize: Style.font.bodySmall
                          }
                        }

                        Column {
                          width: parent.width - Style.space(128) - Style.spacing.sm * 4
                          spacing: 0

                          Text {
                            textFormat: Text.PlainText
                            width: parent.width
                            elide: Text.ElideRight
                            text: modelData.name || modelData.url || "Unknown"
                            color: root.fg
                            font.family: root.fam
                            font.pixelSize: Style.font.bodySmall
                          }
                          Text {
                            visible: root.stationSubtitle(modelData) !== ""
                            textFormat: Text.PlainText
                            width: parent.width
                            elide: Text.ElideRight
                            text: root.stationSubtitle(modelData)
                            color: Qt.darker(root.fg, 1.4)
                            font.family: root.fam
                            font.pixelSize: Style.font.caption
                          }
                        }

                        Text {
                          visible: root.radioLive
                            && String(modelData.id) === String(root.musicStatus.stationId)
                          textFormat: Text.PlainText
                          width: Style.space(36)
                          text: "LIVE"
                          color: Color.accent
                          font.family: root.fam
                          font.pixelSize: Style.font.caption
                          font.bold: true
                          verticalAlignment: Text.AlignVCenter
                        }

                        PanelActionButton {
                          width: Style.space(32)
                          height: Style.space(28)
                          iconText: root.isStationFavorite(modelData.id)
                            ? Model.ICON.star : Model.ICON.starOutline
                          tooltipText: root.isStationFavorite(modelData.id)
                            ? "Remove from favorites" : "Add to favorites"
                          fontFamily: root.fam
                          foreground: root.isStationFavorite(modelData.id) ? Color.accent : root.fg
                          enabled: !stationFavProc.running
                          onClicked: root.toggleStationFavorite(modelData)
                        }

                        PanelActionButton {
                          width: Style.space(36)
                          height: Style.space(28)
                          iconText: Model.ICON.play
                          tooltipText: "Play station"
                          fontFamily: root.fam
                          foreground: root.fg
                          enabled: !root.stationBusy
                          onClicked: root.playStation(modelData)
                        }
                      }

                      MouseArea {
                        anchors.fill: parent
                        acceptedButtons: Qt.RightButton
                        onClicked: function(mouse) {
                          var point = stationRow.mapToItem(panelFlick, mouse.x, mouse.y)
                          root.openStationMenu(modelData, point.x, point.y)
                        }
                      }
                    }
                  }
                }
              }

              Text {
                visible: root.stationSection === "search" && root.stationSearching
                width: parent.width
                textFormat: Text.PlainText
                text: "Searching…"
                color: Qt.darker(root.fg, 1.4)
                font.family: root.fam
                font.pixelSize: Style.font.bodySmall
              }

              Text {
                visible: root.stationSection === "search" && !root.stationSearching
                  && root.stationQuery !== "" && root.stationResults.length === 0
                width: parent.width
                textFormat: Text.PlainText
                text: "No stations found."
                color: Qt.darker(root.fg, 1.4)
                font.family: root.fam
                font.pixelSize: Style.font.bodySmall
              }

              Text {
                visible: root.stationSection === "featured"
                  && !stationCatalogProc.running && root.stationCatalog.length === 0
                width: parent.width
                wrapMode: Text.WordWrap
                textFormat: Text.PlainText
                text: "No featured stations available. Search the directory, then right-click a station to add it."
                color: Qt.darker(root.fg, 1.4)
                font.family: root.fam
                font.pixelSize: Style.font.bodySmall
              }

              Text {
                visible: root.stationSection === "favorites"
                  && !stationFavoritesProc.running && root.stationFavorites.length === 0
                width: parent.width
                wrapMode: Text.WordWrap
                textFormat: Text.PlainText
                text: "No favorite stations yet. Search the directory and tap the star."
                color: Qt.darker(root.fg, 1.4)
                font.family: root.fam
                font.pixelSize: Style.font.bodySmall
              }
            }

            // ---- library
            Column {
              visible: root.loggedIn && (root.activeTab === "library" || root.libraryDetail)
              width: parent.width
              spacing: Style.spacing.panelGap

              Row {
                width: parent.width - Style.space(40)
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: Style.spacing.sm
                visible: !root.libraryDetail

                Repeater {
                  model: [
                    { key: "home", label: "Home" },
                    { key: "history", label: "Recent" },
                    { key: "liked", label: "Liked" },
                    { key: "songs", label: "Songs" },
                    { key: "albums", label: "Albums" },
                    { key: "artists", label: "Artists" }
                  ]
                  delegate: Button {
                    width: (parent.width - Style.spacing.sm * 5) / 6
                    height: Style.spacing.controlHeight
                    text: modelData.label
                    fontFamily: root.fam
                    fontSize: Style.font.bodySmall
                    selected: root.libraryKind === modelData.key
                    active: root.libraryKind === modelData.key
                    bordered: true
                    foreground: root.fg
                    enabled: !libraryProc.running
                    onClicked: root.loadLibrary(modelData.key)
                  }
                }
              }

              Row {
                width: parent.width
                height: Style.space(28)
                visible: (root.activeTab === "library" || root.libraryDetail)
                  && root.libraryKind !== ""
                spacing: Style.spacing.sm

                Button {
                  width: Style.space(72)
                  height: Style.space(28)
                  text: "Back"
                  iconText: Model.ICON.arrowLeft
                  fontFamily: root.fam
                  fontSize: Style.font.bodySmall
                  foreground: root.fg
                  onClicked: root.libraryBack()
                }

                Button {
                  visible: root.libraryKind === "album" || root.libraryKind === "artist"
                  enabled: !root.busy && root.librarySongCount() > 0
                  width: Style.space(72)
                  height: Style.space(28)
                  text: "Play all"
                  fontFamily: root.fam
                  fontSize: Style.font.bodySmall
                  foreground: root.fg
                  onClicked: root.enqueueNav("play", root.libraryKind, root.libraryRefId)
                }

                Button {
                  visible: root.libraryKind === "album" || root.libraryKind === "artist"
                  enabled: !root.busy && root.librarySongCount() > 0
                  width: Style.space(72)
                  height: Style.space(28)
                  text: "Queue all"
                  fontFamily: root.fam
                  fontSize: Style.font.bodySmall
                  foreground: root.fg
                  onClicked: root.enqueueNav("queue", root.libraryKind, root.libraryRefId)
                }

                Button {
                  visible: root.libraryKind === "album" && root.loggedIn
                  enabled: root.libraryRefId !== ""
                  width: Style.space(72)
                  height: Style.space(28)
                  text: root.albumInLibrary ? "Remove" : "Save"
                  fontFamily: root.fam
                  fontSize: Style.font.bodySmall
                  foreground: root.fg
                  onClicked: root.queueAlbumLibrary(root.albumInLibrary ? "album-remove" : "album-save", root.libraryRefId)
                }

                Text {
                  visible: !root.libraryRichHeader
                  width: parent.width - Style.space(72)
                    - ((root.libraryKind === "album" || root.libraryKind === "artist")
                      ? Style.space(144) + Style.spacing.sm * 2 : 0)
                    - (root.libraryKind === "album" ? Style.space(72) + Style.spacing.sm : 0)
                    - Style.spacing.sm
                  height: Style.space(28)
                  elide: Text.ElideRight
                  verticalAlignment: Text.AlignVCenter
                  textFormat: Text.PlainText
                  text: root.libraryTitle
                    + (root.librarySubtitle !== "" ? "  ·  " + root.librarySubtitle : "")
                  color: root.fg
                  font.family: root.fam
                  font.pixelSize: Style.font.bodySmall
                  font.bold: true
                }
              }

              Row {
                width: parent.width - Style.space(40)
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: Style.space(12)
                visible: root.libraryRichHeader

                Rectangle {
                  id: libraryCover
                  visible: root.libraryImageSource !== "" || root.libraryThumbUrl !== ""
                  width: Style.space(96)
                  height: Style.space(96)
                  radius: Style.cornerRadius
                  color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.1)
                  clip: true

                  Image {
                    id: libraryCoverImage
                    anchors.fill: parent
                    source: root.libraryImageSource
                    fillMode: Image.PreserveAspectCrop
                    asynchronous: true
                    cache: true
                    sourceSize.width: 96
                    sourceSize.height: 96
                  }

                  Text {
                    anchors.centerIn: parent
                    visible: libraryCoverImage.status !== Image.Ready
                    text: Model.ICON.note
                    color: Color.accent
                    font.family: root.fam
                    font.pixelSize: Style.font.displayLarge
                  }
                }

                Column {
                  width: parent.width
                    - (libraryCover.visible ? libraryCover.width + Style.space(12) : 0)
                  spacing: Style.space(3)

                  Text {
                    textFormat: Text.PlainText
                    width: parent.width
                    elide: Text.ElideRight
                    text: root.libraryTitle
                      + (root.librarySubtitle !== "" ? "  ·  " + root.librarySubtitle : "")
                    color: root.fg
                    font.family: root.fam
                    font.pixelSize: Style.font.bodySmall
                    font.bold: true
                  }

                  Text {
                    visible: root.libraryMeta !== ""
                    textFormat: Text.PlainText
                    width: parent.width
                    elide: Text.ElideRight
                    text: root.libraryMeta
                    color: Qt.darker(root.fg, 1.4)
                    font.family: root.fam
                    font.pixelSize: Style.font.caption
                  }

                  Text {
                    visible: root.libraryDescription !== ""
                    textFormat: Text.PlainText
                    width: parent.width
                    text: root.libraryDescription
                    color: Qt.darker(root.fg, 1.4)
                    font.family: root.fam
                    font.pixelSize: Style.font.caption
                    wrapMode: Text.Wrap
                    elide: Text.ElideRight
                    maximumLineCount: root.libraryInfoOpen ? 400 : 3

                    MouseArea {
                      anchors.fill: parent
                      cursorShape: Qt.PointingHandCursor
                      onClicked: root.libraryInfoOpen = !root.libraryInfoOpen
                    }
                  }
                }
              }

              Text {
                visible: (root.activeTab === "library" || root.libraryDetail)
                  && root.libraryKind !== ""
                  && (root.loadingText !== ""
                    || (root.libraryList.length === 0 && !libraryProc.running))
                textFormat: Text.PlainText
                text: root.loadingText !== "" ? root.loadingText : "Nothing here."
                color: Qt.darker(root.fg, 1.4)
                font.family: root.fam
                font.pixelSize: Style.font.bodySmall
              }

              Flickable {
                id: libraryView
                width: parent.width
                height: root.listViewportHeight(libraryView, libraryViewContent.implicitHeight)
                contentWidth: width
                contentHeight: libraryViewContent.implicitHeight
                clip: true
                boundsBehavior: Flickable.StopAtBounds
                flickableDirection: Flickable.VerticalFlick
                interactive: contentHeight > height
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                Column {
                  id: libraryViewContent
                  width: parent.width
                  spacing: 0

                  Repeater {
                    id: libraryRepeater
                    visible: root.activeTab === "library" || root.libraryDetail
                    model: root.libraryList
                    delegate: Item {
                      id: libraryRow
                      width: contentColumn.width
                      height: Style.space(40)

                      RowHighlight {
                        id: libraryRowBg
                        foreground: root.fg
                        hasCursor: index === root.selectedIndex
                        hovered: libraryRowClick.containsMouse
                      }

                      MouseArea {
                        id: libraryRowClick
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: {
                          root.selectIndex(index)
                          if (modelData.kind !== "song") root.openRow(modelData, false)
                        }
                      }

                      Row {
                        anchors.fill: parent
                        spacing: Style.spacing.sm

                        Text {
                          textFormat: Text.PlainText
                          width: Style.space(24)
                          text: modelData.kind === "song" ? Model.ICON.note
                            : ((modelData.kind === "album" || modelData.kind === "artist")
                              ? Model.ICON.music : Model.ICON.playlist)
                          color: Color.accent
                          font.family: root.fam
                          font.pixelSize: Style.font.bodySmall
                          verticalAlignment: Text.AlignVCenter
                        }

                        Column {
                          width: parent.width - Style.space(24) - Style.space(56)
                            - Style.space(40) - Style.spacing.sm * 3
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
                            visible: root.songSubtitle(modelData) !== ""
                            textFormat: Text.PlainText
                            width: parent.width
                            elide: Text.ElideRight
                            text: root.songSubtitle(modelData)
                            color: Qt.darker(root.fg, 1.4)
                            font.family: root.fam
                            font.pixelSize: Style.font.caption
                          }
                        }

                        Text {
                          visible: modelData.kind === "song"
                          textFormat: Text.PlainText
                          width: Style.space(56)
                          text: modelData.duration > 0 ? Model.fmtDuration(modelData.duration) : ""
                          horizontalAlignment: Text.AlignRight
                          color: Qt.darker(root.fg, 1.4)
                          font.family: root.fam
                          font.pixelSize: Style.font.caption
                          verticalAlignment: Text.AlignVCenter
                        }

                        PanelActionButton {
                          width: Style.space(40)
                          height: Style.space(28)
                          visible: modelData.kind === "song"
                          iconText: Model.ICON.play
                          tooltipText: "Play"
                          fontFamily: root.fam
                          foreground: root.fg
                          onClicked: root.playNow(modelData.videoId)
                        }

                        Text {
                          visible: modelData.kind !== "song"
                          textFormat: Text.PlainText
                          width: Style.space(40)
                          text: "›"
                          horizontalAlignment: Text.AlignRight
                          color: Color.accent
                          font.family: root.fam
                          font.pixelSize: Style.font.body
                          verticalAlignment: Text.AlignVCenter
                        }
                      }

                      MouseArea {
                        anchors.fill: parent
                        acceptedButtons: Qt.RightButton
                        onClicked: function(mouse) {
                          var point = libraryRow.mapToItem(panelFlick, mouse.x, mouse.y)
                          root.openRowMenu(modelData, "library", point.x, point.y)
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
      }

      // The panel's single feedback channel. statusText is written all over
      // this file; without an element bound to it nothing the app said
      // ("Added 1 track · already in playlist: Schism", "Play failed") was
      // ever visible.
      Rectangle {
        id: statusToast
        z: 10
        visible: root.statusText !== ""
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottom: parent.bottom
        anchors.bottomMargin: Style.space(14)
        width: Math.min(panelFlick.width - Style.space(40), Style.space(540))
        height: statusToastLabel.implicitHeight + Style.space(20)
        radius: Style.cornerRadius
        color: Color.notifications.background
        border.width: 1
        border.color: Color.notifications.border

        Text {
          id: statusToastLabel
          anchors.fill: parent
          anchors.margins: Style.space(10)
          text: root.statusText
          textFormat: Text.PlainText
          wrapMode: Text.WordWrap
          maximumLineCount: 3
          elide: Text.ElideRight
          color: Color.notifications.text
          font.family: root.fam
          font.pixelSize: Style.font.body
          verticalAlignment: Text.AlignVCenter
        }
      }

      ConfirmDialog {
        id: deleteConfirm
        anchors.fill: parent
        z: 20
        opened: root.deleteConfirmOpen
        message: 'Delete "' + root.activePlaylistTitle + '"?'
        confirmText: "Delete"
        foreground: root.fg
        selectedText: Color.accent
        fontFamily: root.fam
        onCanceled: root.deleteConfirmOpen = false
        onConfirmed: root.deleteActivePlaylist()
      }
    }
  }
}
