import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "Model.js" as Model

// TickTick popup: what is due, what habits are still open, and the two
// actions that matter on a bar — tick a task off, check a habit in.
//
// The panel never talks to TickTick itself. `bin/omarchy-ticktick` owns the
// session token and every request; this reads the JSON cache that CLI
// writes and shells back out for writes. That keeps a long-lived credential
// out of the shell process and makes every mutation a single auditable
// command.
Panel {
  id: root
  moduleName: "io.github.sotoaugusto.ticktick"
  ipcTarget: "io.github.sotoaugusto.ticktick"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root

  readonly property string pluginDir: Qt.resolvedUrl(".").toString().replace("file://", "")
  readonly property string cli: pluginDir + "bin/omarchy-ticktick"

  function openTickTick() {
    if (root.bar) root.bar.run("if command -v ticktick >/dev/null 2>&1; then ticktick; else xdg-open https://ticktick.com/webapp; fi")
  }

  // The popup is a layer-shell surface holding the keyboard. Anything that
  // puts another window in front goes out after it is down, or the window
  // opens behind a panel that still owns every key.
  function launchTickTick() {
    root.close()
    Qt.callLater(root.openTickTick)
  }

  // ---- the shared service ------------------------------------------------
  //
  // A bar exists per monitor, so this panel exists per monitor too. The cache,
  // the sync timer, the write queue, the undo window, and the focus clock are
  // all single-instance concerns and live in Service.qml; this reads them.
  // Without that split, two screens meant two focus clocks each logging the
  // same block.
  readonly property var svc: bar && bar.shell && typeof bar.shell.serviceFor === "function"
    ? bar.shell.serviceFor("io.github.sotoaugusto.ticktick")
    : null

  // The service has no settings of its own; every panel carries the same
  // inline entry, so whichever loads first hands them over.
  onSvcChanged: pushSettings()
  onSettingsChanged: pushSettings()
  Connections {
    target: root.svc
    ignoreUnknownSignals: true
    function onLaunching() { if (root.opened) root.close() }
  }
  function pushSettings() {
    if (svc && "settings" in svc) svc.settings = root.settings
  }

  // ---- cache (read through the service) ----------------------------------

  readonly property var cache: svc ? svc.cache : Model.parseCache("")
  readonly property date nowDate: svc ? svc.nowDate : new Date()

  readonly property bool signedIn: cache.syncedAt > 0 && !cache.authRequired
  readonly property string cacheError: cache.error ? String(cache.error) : ""
  readonly property string cacheWarning: cache.warnings && cache.warnings.length > 0 ? cache.warnings.join(" ") : ""
  readonly property string defaultProject: String(setting("defaultProject", ""))
  readonly property bool hasDefaultProject: (cache.projects || []).some(function(project) { return String(project.id) === root.defaultProject && !project.closed })
  readonly property bool needsProjectChoice: cache.backend === "openapi" && !hasDefaultProject
  readonly property var projectOptions: [{ value: "", label: "Choose list…" }].concat(
    (cache.projects || []).filter(function(project) { return project.id && !project.closed }).map(function(project) {
      return { value: String(project.id), label: String(project.name || "Untitled list") }
    }))

  function chooseProject(value) {
    var valid = (root.cache.projects || []).some(function(project) {
      return String(project.id) === value && !project.closed
    })
    if (!value || !valid) return
    var entry = { id: root.moduleName }
    for (var key in root.settings) if (key !== "id") entry[key] = root.settings[key]
    entry.defaultProject = value
    root.settings = entry
    if (root.hostWidget && "settings" in root.hostWidget) root.hostWidget.settings = entry
    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function")
      root.bar.shell.updateEntryInline(root.moduleName, entry)
  }
  // Writes made while TickTick was unreachable, waiting on the next sync.
  readonly property int queuedCount: cache.queued || 0

  readonly property int staleMinutes: Model.staleMinutes(cache.syncedAt, nowDate.getTime())

  readonly property int todayStamp: Model.dateStamp(nowDate)

  readonly property bool showTasks: setting("showTasks", true) !== false
  readonly property bool showHabits: setting("showHabits", true) !== false
  readonly property bool showPomo: setting("showPomo", true) !== false
  // The configured horizon is the default the panel opens at; `viewHorizon`
  // is what it is currently showing. Widening is a look, not a preference
  // change, so it resets when the panel closes.
  readonly property string horizon: setting("horizon", "Today")
  property string viewHorizon: horizon
  onHorizonChanged: viewHorizon = horizon

  function cycleView(delta) {
    viewHorizon = Model.cycleHorizon(viewHorizon, delta)
  }

  // Naming the destination beats naming the direction: the control wraps, so
  // "further ahead" is wrong exactly when you are at the widest view.
  readonly property string nextView: Model.cycleHorizon(viewHorizon, 1)
  readonly property string viewSwitchHint: (nextView === "Today" ? "Back to " : "Show ") + nextView
  readonly property bool includeOverdue: setting("includeOverdue", true) !== false
  readonly property int maxTasks: Math.max(3, parseInt(setting("maxTasks", 12), 10) || 12)

  readonly property var pendingIds: svc ? svc.pendingIds : ({})
  readonly property var pendingHabitIds: svc ? svc.pendingHabitIds : ({})
  readonly property var pendingAdds: svc ? svc.pendingAdds : []
  readonly property var pendingAction: svc ? svc.pendingAction : null
  readonly property int pendingCount: svc ? svc.pendingCount : 0
  readonly property int undoLeft: svc ? svc.undoLeft : 0
  readonly property int undoSeconds: svc ? svc.undoSeconds : 6
  readonly property string actionError: svc ? svc.actionError : ""
  readonly property bool connecting: svc ? svc.connecting : false

  // Browser sign-in, run and owned by the service so it outlives this
  // popup: the popup has to be down for the browser to be usable at all.
  readonly property string authState: svc && svc.authState ? String(svc.authState) : "idle"
  readonly property string authError: svc && svc.authError ? String(svc.authError) : ""
  readonly property string authUrl: svc && svc.authUrl ? String(svc.authUrl) : ""
  // The service reports a manual token check as "validating" too; that one
  // is `connecting`, and belongs to the token section rather than the browser.
  readonly property bool oauthActive: (svc ? svc.oauthConnecting === true : false)
    || authState === "preparing" || authState === "waiting"
    || (authState === "validating" && !connecting)
  readonly property bool canStartOAuth: !!svc && typeof svc.startOAuth === "function"
    && !oauthActive && !connecting

  // A rejected credential is only a reconnect if there was a sync before it.
  // The error cache keeps the last good payload, so syncedAt survives the
  // rejection; a first run never has one.
  readonly property bool reconnecting: cache.syncedAt > 0 && cache.authRequired

  // Which card the setup surface shows. A held manual token is not a stage
  // of its own: it lives in the token section that started it.
  readonly property string setupStage: oauthActive ? "signing-in"
    : authState === "failed" && !tokenAttempted ? "failed"
    : reconnecting ? "reconnect"
    : "connect"

  readonly property int refreshIntervalSec: svc ? svc.refreshIntervalSec : 300

  readonly property var pomoStats: svc ? svc.pomoStats : ({})
  readonly property var pomoPrefs: svc ? svc.pomoPrefs : ({})
  readonly property string pomoPhase: svc ? svc.pomoPhase : "idle"
  readonly property bool pomoRunning: svc ? svc.pomoRunning === true : false
  readonly property bool pomoPaused: svc ? svc.pomoPaused === true : false
  readonly property string pomoClock: svc ? svc.pomoClock : ""

  readonly property bool syncing: svc ? svc.syncing === true : false

  // Not while signing in: a sync that starts before the new session lands
  // stamps "signed out" over the cache the sign-in has just written.
  function refresh(force) { if (svc && !oauthActive) svc.refresh(force) }
  function runAction(args) { if (svc) svc.runAction(args) }
  function completeTask(task) { if (svc) svc.completeTask(task) }
  function checkInHabit(habit) { if (svc) svc.checkInHabit(habit) }
  function cancelPending() { if (svc) svc.cancelPending() }
  function flushPending() { if (svc) svc.flushPending() }
  function startPomo(phase) { if (svc) svc.startPomo(phase) }
  function pausePomo() { if (svc) svc.pausePomo() }
  function resumePomo() { if (svc) svc.resumePomo() }
  function stopPomo() { if (svc) svc.stopPomo() }
  function togglePomo() { if (svc) svc.togglePomo() }

  // ---- setup actions
  //
  // Sign-in and reopening the page hand off to the browser, so both close
  // the popup first and only then ask the service, on the next turn of the
  // event loop. Reopening the bar shows where it got to.
  function connectTickTick() {
    if (!canStartOAuth) return
    tokenField.text = ""
    tokenFallbackOpen = false
    tokenAttempted = false
    root.close()
    Qt.callLater(function() { if (root.svc) root.svc.startOAuth() })
  }

  function reopenSignIn() {
    if (!svc || typeof svc.reopenAuthPage !== "function" || authUrl === "") return
    root.close()
    Qt.callLater(function() { if (root.svc) root.svc.reopenAuthPage() })
  }

  // Cancel stays in the panel: nothing opens, and the card it returns to is
  // the one you want next.
  function cancelSignIn() {
    if (svc && typeof svc.cancelOAuth === "function") svc.cancelOAuth()
  }

  // The fallback is folded away by default: a token is a long-lived
  // credential, and the browser path never puts one on the clipboard.
  property bool tokenFallbackOpen: false
  // Set once a token has been sent, so a failure is reported where it was
  // typed instead of a first-run "not signed in" turning up as an error.
  property bool tokenAttempted: false
  // A token failure is a failed auth to the service; here it is the token's.
  readonly property string tokenError: tokenAttempted && !connecting && !signedIn
    ? (authState === "failed" && authError !== "" ? authError : actionError) : ""

  function toggleTokenFallback() {
    tokenFallbackOpen = !tokenFallbackOpen
    tokenField.text = ""
    if (tokenFallbackOpen) tokenField.forceActiveFocus()
    else keyCatcher.forceActiveFocus()
  }

  function connectWithApiToken() {
    var token = String(tokenField.text || "").trim()
    // The field is emptied before anything else, whatever happens next.
    tokenField.text = ""
    if (token === "" || connecting || oauthActive) return
    if (!svc || typeof svc.connectWithApiToken !== "function") return
    tokenAttempted = true
    svc.connectWithApiToken(token)
    keyCatcher.forceActiveFocus()
  }

  function leaveTokenField() {
    tokenField.text = ""
    keyCatcher.forceActiveFocus()
  }

  // The card disappears on its own; the receipt says why.
  onSignedInChanged: {
    if (!signedIn) return
    tokenAttempted = false
    tokenFallbackOpen = false
    if (root.opened) showCopyNote("Connected to TickTick")
  }

  // Editing reuses the add field rather than introducing an editor. The row
  // is rendered back into the same grammar you would have typed, so there is
  // one input and one syntax to know.
  property string editingTaskId: ""

  // The name the task had when the field was opened. The grammar can take a
  // trailing word that was part of the title, so the hint needs something to
  // compare against to say the edit is about to rename it.
  property string editingTitle: ""

  function beginEdit() {
    if (cursor < 0 || cursor >= navRows.length) return
    var row = navRows[cursor]
    if (row.section !== "task") return
    var task = displayTasks[row.index]
    if (!task || !task.id) return
    editingTaskId = String(task.id)
    editingTitle = String(task.title || "")
    quickAdd.text = Model.editLineFor(task)
    quickAdd.forceActiveFocus()
    quickAdd.selectAll()
  }

  function cancelEdit() {
    editingTaskId = ""
    editingTitle = ""
    quickAdd.text = ""
    keyCatcher.forceActiveFocus()
  }

  // What the line in the field would actually create, shown under it. The
  // quick-add grammar is narrow on purpose, and a clock it does not recognise
  // is not an error — the words stay in the title and the task lands at
  // midnight, where an all-day task is never announced. This is the receipt
  // that makes that visible before enter, which is what TickTick's own chip
  // does for the same reason.
  readonly property string quickAddHint: editingTaskId !== ""
    ? Model.quickAddPreview(Model.parseEdit(quickAdd.text, editingTitle), true, editingTitle)
    : Model.quickAddPreview(Model.parseQuickAdd(quickAdd.text), false, "")

  // The second line: what shift+enter would do instead, or "". It stays off the
  // first line so that line keeps all its room for what plain enter will do.
  readonly property string quickAddOfferHint: Model.quickAddOfferHint(quickAddOffer,
    editingTaskId !== "" ? Model.parseEdit(quickAdd.text, editingTitle) : Model.parseQuickAdd(quickAdd.text),
    editingTaskId !== "", editingTitle, nowDate)

  // What shift+enter would turn the line into: the range reading of a line
  // that ends in half a range ("gym 6 - 7am"), or null. See Model.halfRangeOffer.
  readonly property var quickAddOffer: Model.halfRangeOffer(quickAdd.text)

  // The keyboard help, grouped by where the keys act (see the help Column).
  readonly property var shortcutGroups: [
    {
      title: "Tasks & habits",
      entries: [
        { key: "\u2191 \u2193", what: "move \u2014 into an open task's subtasks too" },
        { key: "enter", what: "complete task / check in / flip subtask" },
        { key: "g / G", what: "first / last row" },
        { key: "o", what: "open details, or fold them and step back out" },
        { key: "e", what: "edit the selected task in the field" },
        { key: "c", what: "copy the selected task as markdown" },
        { key: "u", what: "undo the held action" }
      ]
    },
    {
      title: "Quick add field",
      entries: [
        { key: "a", what: "add a task" },
        { key: "#tag", what: "tag it — # is TickTick's own" },
        { key: "!1 !2 !3", what: "priority: high, medium, low" },
        { key: "tomorrow", what: "a trailing date or time sets when \u2014 \"tomorrow at 9pm\"" },
        { key: "\u21e7 enter", what: "take the offered range \u2014 \"gym 6 - 7am\" \u2192 06:00\u201307:00" }
      ]
    },
    {
      title: "Focus timer",
      entries: [
        { key: "p", what: "start or pause focus" },
        { key: "d / del", what: "discard the focus block" }
      ]
    },
    {
      title: "Panel",
      entries: [
        { key: "v", what: "cycle range: today \u2192 tomorrow \u2192 7 days \u21ba" },
        { key: "r", what: "sync now" },
        { key: "tab", what: "next bar panel" },
        { key: "?", what: "show or hide this list" },
        { key: "esc", what: "back out, then close" }
      ]
    }
  ]

  // Width of the rail the key chips stack into: the widest key as this font
  // actually draws it, plus the chip's padding. A fixed width clipped whichever
  // key ran long — "tomorrow" by 2px — and would clip more under a theme with a
  // larger caption size.
  FontMetrics {
    id: shortcutKeyMetrics
    font.family: Style.font.family
    font.pixelSize: Style.font.caption
    font.bold: true
  }
  readonly property real shortcutRailWidth: {
    var widest = 0
    for (var g = 0; g < shortcutGroups.length; g++) {
      var entries = shortcutGroups[g].entries
      for (var e = 0; e < entries.length; e++)
        widest = Math.max(widest, shortcutKeyMetrics.advanceWidth(entries[e].key))
    }
    return Math.max(Style.space(52), Math.ceil(widest) + Style.space(6))
  }

  // One task open at a time. `o` and the row's chevron both write here; a
  // task without details never takes the slot, so there is nothing to
  // open and nothing to collapse.
  property string expandedTaskId: ""

  function toggleDetails() {
    // `o` from inside an open task folds it and steps the cursor back onto
    // the task's own row — the arrows would otherwise start from a subtask
    // that no longer exists.
    if (cursor >= 0 && cursor < navRows.length && navRows[cursor].section === "subtask") {
      var openIndex = navRows[cursor].index
      expandedTaskId = ""
      syncCursorTo("task", openIndex)
      return
    }
    var task = cursorTask()
    if (!task || !Model.hasDetails(task)) return
    expandedTaskId = expandedTaskId === String(task.id) ? "" : String(task.id)
  }

  function toggleSubtaskAt(taskIndex, subIndex) {
    var task = displayTasks[taskIndex]
    var subList = Model.subtasks(task)
    if (subIndex < 0 || subIndex >= subList.length) return
    toggleSubtask(task, subList[subIndex])
  }

  function cursorTask() {
    if (cursor < 0 || cursor >= navRows.length) return null
    var row = navRows[cursor]
    if (row.section !== "task") return null
    return displayTasks[row.index] || null
  }

  // Checkbox flips are optimistic and panel-local: the row dims until the
  // cache comes back from the CLI carrying the new state.
  property var pendingItemIds: ({})

  // The cache always carries the post-click truth — a delivered write from
  // the server, or a queued one mirrored in by apply_locally — so a fresh
  // cache means every dimming has done its job.
  onCacheChanged: pendingItemIds = ({})

  function toggleSubtask(task, item) {
    if (!svc || !item || item.id === "") return
    var key = String(item.id)
    if (pendingItemIds[key]) return
    var next = {}
    for (var k in pendingItemIds) next[k] = pendingItemIds[k]
    next[key] = true
    pendingItemIds = next
    if (!svc.toggleSubtask(task, item)) {
      next = {}
      for (var j in pendingItemIds) if (j !== key) next[j] = pendingItemIds[j]
      pendingItemIds = next
    }
  }

  // ---- copy as markdown --------------------------------------------------

  property string copyPayload: ""
  property string copyNote: ""

  function showCopyNote(text) {
    copyNote = text
    copyNoteTimer.restart()
  }

  function copyCursorTask() {
    var task = cursorTask()
    if (!task) return
    // The text goes to wl-copy's stdin: a task's description can be pages
    // long, and an argv slot that big is not a promise every kernel makes.
    copyPayload = Model.taskMarkdown(task, cache.projects, cache.inboxId, nowDate)
    copyProc.running = true
  }

  Timer {
    id: copyNoteTimer
    interval: 2600
    onTriggered: root.copyNote = ""
  }

  Process {
    id: copyProc
    property bool launched: false
    command: ["wl-copy"]
    running: false
    stdinEnabled: true
    onStarted: {
      copyProc.launched = true
      copyProc.write(root.copyPayload)
      copyProc.stdinEnabled = false
    }
    onRunningChanged: if (!running && !copyProc.launched) root.showCopyNote("wl-copy is required to copy")
    onExited: function(code) {
      copyProc.stdinEnabled = true
      root.showCopyNote(code === 0 ? "Copied to clipboard" : "wl-copy is required to copy")
    }
  }

  // Shift+enter takes the offered range, then submits as usual. With nothing on
  // offer it is plain enter, so the key never silently does nothing.
  function submitQuickAddOffer() {
    if (quickAddOffer) quickAdd.text = quickAddOffer.line
    submitQuickAdd()
  }

  function quickAddReturn(event) {
    if (!(event.modifiers & Qt.ShiftModifier)) {
      event.accepted = false
      return
    }
    event.accepted = true
    submitQuickAddOffer()
  }

  function submitQuickAdd() {
    if (!svc) return
    var text = String(quickAdd.text || "").trim()
    if (text === "") return

    if (editingTaskId !== "") {
      var id = editingTaskId
      var wasTitled = editingTitle
      editingTaskId = ""
      editingTitle = ""
      quickAdd.text = ""
      svc.submitEdit(id, text, wasTitled)
      return
    }

    quickAdd.text = ""
    var parsed = svc.submitQuickAdd(text)
    // Adding something due later must not file it out of sight, so the view
    // widens to wherever the task landed. That stays here: the range being
    // shown is this screen's business, not the service's.
    if (parsed)
      viewHorizon = Model.widerHorizon(viewHorizon, Model.horizonForDue(parsed.due, nowDate))
  }


  readonly property var allDueTasks: showTasks
    ? Model.dueTasks(cache.tasks, { now: nowDate, horizon: viewHorizon, includeOverdue: includeOverdue })
    : []
  readonly property var visibleTasks: filterPending(allDueTasks)
  readonly property var listedTasks: visibleTasks.slice(0, maxTasks)

  readonly property var displayTasks: pendingAdds.concat(listedTasks)
  readonly property int hiddenTaskCount: Math.max(0, visibleTasks.length - listedTasks.length)
  readonly property int overdueCount: Model.overdueCount(visibleTasks, nowDate)

  // Whether the backlog rule renders below. Same condition as the
  // delegate's startsBacklog, over the same list: when maxTasks cuts the
  // list inside today's work, no rule draws and the header carries the
  // late count instead so it never goes invisible.
  readonly property bool backlogRuleVisible: {
    for (var i = 0; i < displayTasks.length; i++) {
      var t = displayTasks[i]
      if (!t || t.ghost === true || !Model.isOverdue(t, nowDate)) continue
      if (i === 0 || displayTasks[i - 1].ghost === true
          || !Model.isOverdue(displayTasks[i - 1], nowDate)) return true
    }
    return false
  }

  // Tag colours are the only colours TickTick actually stores for a task,
  // so they are the only ones taken literally. Due state is painted from the
  // theme instead — a hardcoded red would fight every Omarchy theme.
  readonly property var tagsById: Model.tagIndex(cache.tags)

  // ---- keyboard cursor. Same idiom as the first-party panels: the cursor
  //      only becomes visible once a key is pressed, and mouse hover keeps
  //      it in sync so the two input modes never disagree about "current".
  property bool cursorActive: false
  property int cursor: -1
  property bool helpVisible: false

  // An expanded task's subtasks are rows too: the arrows walk into them,
  // enter flips the one under the cursor, and `o` folds the task and steps
  // back out. Driven by expandedTaskId, so exactly one task is ever open.
  readonly property var navRows: {
    var rows = []
    // Signed out, the last cache is still loaded but none of it is shown.
    // A cursor over it would complete a task you cannot see.
    if (!signedIn) return rows
    if (showTasks) {
      for (var i = 0; i < displayTasks.length; i++) {
        rows.push({ section: "task", index: i })
        if (expandedTaskId === "" || String(displayTasks[i].id) !== expandedTaskId) continue
        var subs = Model.subtasks(displayTasks[i])
        for (var s = 0; s < subs.length; s++) rows.push({ section: "subtask", index: i, item: s })
      }
    }
    if (showHabits) {
      for (var j = 0; j < habits.length; j++) rows.push({ section: "habit", index: j })
    }
    return rows
  }

  // First and last are destinations, not big steps. Expressed as a delta they
  // went through moveCursor's "nothing selected yet" branch, where a negative
  // delta means "start from the end" — so `g` jumped to the last row whenever
  // the panel had just been opened.
  function cursorToFirst() {
    cursorActive = true
    cursor = navRows.length > 0 ? 0 : -1
  }

  function cursorToLast() {
    cursorActive = true
    cursor = navRows.length - 1
  }

  function moveCursor(delta) {
    cursorActive = true
    var count = navRows.length
    if (count === 0) { cursor = -1; return }
    if (cursor < 0) cursor = delta > 0 ? 0 : count - 1
    else cursor = Math.max(0, Math.min(count - 1, cursor + delta))
  }

  function syncCursorTo(section, index) {
    for (var i = 0; i < navRows.length; i++) {
      if (navRows[i].section === section && navRows[i].index === index) { cursor = i; return }
    }
  }

  function syncCursorToSubtask(taskIndex, itemIndex) {
    for (var i = 0; i < navRows.length; i++) {
      var row = navRows[i]
      if (row.section === "subtask" && row.index === taskIndex && row.item === itemIndex) {
        cursor = i
        return
      }
    }
  }

  function activateCursor() {
    // The setup card has one primary action, so enter is that action.
    if (!signedIn) {
      if (setupStage === "connect" || setupStage === "reconnect" || setupStage === "failed") connectTickTick()
      return
    }
    if (cursor < 0 || cursor >= navRows.length) return
    var row = navRows[cursor]
    // completeTask ignores an id-less ghost, so a pending row is inert
    // rather than silently completing the wrong task.
    if (row.section === "task") completeTask(displayTasks[row.index])
    else if (row.section === "subtask") toggleSubtaskAt(row.index, row.item)
    else checkInHabit(habits[row.index])
  }

  function isCursorOn(section, index) {
    if (!cursorActive || cursor < 0 || cursor >= navRows.length) return false
    var row = navRows[cursor]
    return row.section === section && row.index === index
  }

  function isCursorOnSubtask(taskIndex, itemIndex) {
    if (!cursorActive || cursor < 0 || cursor >= navRows.length) return false
    var row = navRows[cursor]
    return row.section === "subtask" && row.index === taskIndex && row.item === itemIndex
  }

  // The list shrinks as things get completed; a cursor left past the end
  // would silently act on the wrong row next time.
  onNavRowsChanged: if (cursor >= navRows.length) cursor = navRows.length - 1

  readonly property var habits: showHabits ? (cache.habits || []) : []
  readonly property int habitsRemaining: Model.habitsRemaining(habits, cache.checkins, todayStamp)

  // What the bar reads off this panel.
  readonly property string nextTitle: Model.nextTaskTitle(visibleTasks)
  readonly property string label: Model.barLabel(setting("barLabel", "Count"), visibleTasks, habitsRemaining, nowDate)
  readonly property bool hasWork: visibleTasks.length > 0 || habitsRemaining > 0

  function filterPending(tasks) {
    var result = []
    for (var i = 0; i < tasks.length; i++) {
      if (!pendingIds[tasks[i].id]) result.push(tasks[i])
    }
    return result
  }

  // ---- lifecycle --------------------------------------------------------

  function open() {
    root.controller.show()
    root.refresh()
  }

  function openFromHotkey() {
    root.controller.show()
    root.refresh()
  }

  function close() {
    // Closing is not a cancel. Anything still held is sent, so a click
    // followed by a close does what the click said it would.
    flushPending()
    if (projectPicker.popupOpen) projectPicker.close()
    viewHorizon = horizon
    // A stale cursor is worse than none: reopening and pressing enter would
    // act on whatever was selected in a previous session, which is not the
    // row the user is looking at.
    cursor = -1
    cursorActive = false
    editingTaskId = ""
    editingTitle = ""
    expandedTaskId = ""
    pendingItemIds = ({})
    quickAdd.text = ""
    quickAdd.text = ""
    quickAdd.focus = false
    // A pasted credential never outlives the popup it was pasted into. A
    // token already sent stays unfolded, so its result is there on reopen;
    // a settled token failure keeps its claim on the error, so it never
    // reappears as a browser sign-in that "didn't finish".
    tokenField.text = ""
    tokenField.focus = false
    if (!connecting) tokenFallbackOpen = false
    root.controller.hide()
  }

  function toggle() {
    if (root.opened) root.close()
    else root.openFromHotkey()
  }

  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.barIdentity, direction)
    return false
  }

  // `force` is an explicit user action — opening the panel, the sync button,
  // `r`. Timer ticks are not: they pass a max age so the second monitor's
  // instance skips work the first one just did.
  // Syncing is the service's job; this only asks.

  // ---- surface ----------------------------------------------------------

  readonly property color fg: Commons.Color.popups.text
  // Secondary text is the theme's own text colour at reduced strength, so it
  // composites over whatever popup background the theme paints. Darkening
  // the foreground only works on dark themes; on a light one it made
  // "secondary" text stronger than the primary.
  readonly property color muted: Qt.rgba(fg.r, fg.g, fg.b, 0.64)
  // The one quiet wash used for hover, the cursor row and the primary action.
  readonly property color accentWash: Qt.rgba(Commons.Color.accent.r, Commons.Color.accent.g, Commons.Color.accent.b, 0.14)

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(360))
    contentHeight: panel.fittedContentHeight(content.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: quickAdd.activeFocus || tokenField.activeFocus || projectPicker.popupOpen
      // Escape backs out one layer at a time — help, then a held action,
      // then the panel itself.
      onCloseRequested: {
        if (root.helpVisible) root.helpVisible = false
        else if (root.pendingAction) root.cancelPending()
        else root.close()
      }
      onMoveRequested: function(dx, dy) {
        if (dy !== 0) root.moveCursor(dy > 0 ? 1 : -1)
      }
      // Enter emits BOTH returnRequested and activateRequested. Binding
      // both to this acted on two rows per keypress; Space emits only
      // activate, which makes activate the right one to listen to.
      onActivateRequested: root.activateCursor()
      // Destructive actions answer to Delete in the first-party panels, so
      // discarding a focus block does too.
      onDeleteRequested: root.stopPomo()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(text) {
        if (text === "?") root.helpVisible = !root.helpVisible
        else if (text === "r") root.refresh()
        // The rest act on the list, the field and the timer, none of which
        // exist until there is an account behind them.
        else if (!root.signedIn) return
        else if (text === "a") {
          if (root.needsProjectChoice) projectPicker.open()
          else quickAdd.forceActiveFocus()
        }
        else if (text === "e") root.beginEdit()
        else if (text === "o") root.toggleDetails()
        else if (text === "c") root.copyCursorTask()
        else if (text === "u") root.cancelPending()
        else if (text === "p") {
          if (root.pomoRunning) root.pausePomo()
          else if (root.pomoPaused) root.resumePomo()
          else root.startPomo("focus")
        }
        else if (text === "d") root.stopPomo()
        else if (text === "v") root.cycleView(1)
        else if (text === "V") root.cycleView(-1)
        else if (text === "g") root.cursorToFirst()
        else if (text === "G") root.cursorToLast()
      }

      Flickable {
        id: scroll
        anchors.fill: parent
        contentWidth: width
        contentHeight: content.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height

        Column {
          id: content
          width: scroll.width
          spacing: Style.space(8)

          // ---- header
          Item {
            width: parent.width
            height: Math.max(headerText.implicitHeight, syncButton.height)

            // Identity without a logo: the same monochrome mark the bar shows
            // when signed out, painted in the theme's accent at the size of
            // the two header lines it sits beside.
            TickTickIcon {
              id: brandMark
              anchors.left: parent.left
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(20)
              height: width
              color: Commons.Color.accent
            }

            Column {
              id: headerText
              anchors.left: brandMark.right
              anchors.leftMargin: Style.space(10)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(1)

              // The title is the view switch. It already named the range, so
              // making it the control keeps one label instead of adding a
              // second one that says the same thing.
              // The hit area wraps the Row rather than sitting inside it:
              // a filling MouseArea is a horizontal anchor, and Row refuses
              // to lay out at all when one of its children uses those.
              Item {
                implicitWidth: viewRow.implicitWidth
                implicitHeight: viewRow.implicitHeight
                width: implicitWidth
                height: implicitHeight

                Row {
                  id: viewRow
                  spacing: Style.space(5)

                  // Signed out, an empty list is not news: "All clear" over a
                  // setup card reads as "you have nothing to do".
                  Text {
                    id: viewLabel
                    text: !root.signedIn ? "TickTick"
                      : root.visibleTasks.length === 0 && root.habitsRemaining === 0
                      ? "All clear"
                      : root.viewHorizon
                    color: viewSwitch.containsMouse ? Commons.Color.accent : root.fg
                    font.family: Style.font.family
                    font.pixelSize: Style.font.title
                    font.bold: true
                  }

                  // Position marks, not a chevron. A chevron promises a
                  // dropdown; this cycles. The marks also say how many ranges
                  // there are and which one is showing, which the chevron
                  // never did. They are drawn rather than typed — font dots
                  // sat on the baseline and changed weight with the theme's
                  // font — and the current range stretches into a short pill.
                  Row {
                    id: viewDots
                    anchors.verticalCenter: viewLabel.verticalCenter
                    spacing: Style.space(3)
                    visible: root.signedIn

                    Repeater {
                      model: Model.horizons()

                      Rectangle {
                        required property int index
                        readonly property bool current: index === Model.horizonIndex(root.viewHorizon)

                        anchors.verticalCenter: parent.verticalCenter
                        width: current ? Style.space(12) : Style.space(5)
                        height: Style.space(5)
                        radius: height / 2
                        color: current
                          ? (viewSwitch.containsMouse ? Commons.Color.accent : root.fg)
                          : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, viewSwitch.containsMouse ? 0.45 : 0.28)

                        Behavior on width { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }
                      }
                    }
                  }
                }

                MouseArea {
                  id: viewSwitch
                  anchors.fill: parent
                  enabled: root.signedIn
                  hoverEnabled: true
                  cursorShape: Qt.PointingHandCursor
                  onClicked: root.cycleView(1)

                  PanelToolTip {
                    text: root.viewSwitchHint + "  (v)"
                    visible: viewSwitch.containsMouse
                  }
                }
              }

              // The mark took some of the line, so a long status (queued,
              // stale) now stops short of the buttons instead of running
              // under them.
              Text {
                width: Math.min(implicitWidth,
                  (queuedBadge.visible ? queuedBadge.x : helpButton.x) - headerText.x - Style.space(6))
                elide: Text.ElideRight
                text: root.headerSubtitle()
                color: root.muted
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }
            }

            // Offline work should be visible without opening anything. It
            // sits next to the sync button because that is what clears it.
            Text {
              id: queuedBadge
              anchors.right: helpButton.left
              anchors.rightMargin: Style.space(6)
              anchors.verticalCenter: parent.verticalCenter
              // Signed out, the setup card says what is waiting and what
              // clears it; a sync from here would only fail again.
              visible: root.signedIn && root.queuedCount > 0
              text: " " + root.queuedCount
              color: Commons.Color.accent
              font.family: Style.font.family
              font.pixelSize: Style.font.caption

              PanelToolTip {
                text: root.queuedCount + " change(s) waiting for TickTick"
                visible: queuedHover.containsMouse
              }

              MouseArea {
                id: queuedHover
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.refresh()
              }
            }

            PanelActionButton {
              id: helpButton
              anchors.right: syncButton.visible ? syncButton.left : parent.right
              anchors.rightMargin: Style.space(2)
              anchors.verticalCenter: parent.verticalCenter
              iconText: ""
              tooltipText: "Keyboard shortcuts  (?)"
              foreground: root.helpVisible ? Commons.Color.accent : root.muted
              onClicked: root.helpVisible = !root.helpVisible
            }

            PanelActionButton {
              id: syncButton
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              visible: root.signedIn
              iconText: root.syncing ? "" : ""
              tooltipText: root.syncing ? "Syncing…" : "Sync now"
              foreground: root.fg
              onClicked: root.refresh()
            }
          }

          // ---- keyboard help. Reachable two ways on purpose: `?` for the
          //      keyboard, the header button for the mouse. A shortcut list
          //      only findable by shortcut helps whoever needs it least.
          //
          //      Grouped by where the keys act, because sixteen rows of
          //      undifferentiated key–prose pairs are scanned by reading
          //      every one. Each group answers "I want to touch the list /
          //      type a task / run the timer / drive the panel", which is
          //      how you look for a key you have forgotten.
          Column {
            width: parent.width
            spacing: Style.space(5)
            visible: root.helpVisible

            PanelSeparator { width: parent.width; foreground: root.fg }

            Repeater {
              model: root.shortcutGroups

              Column {
                id: shortcutGroup
                required property var modelData
                width: content.width
                spacing: Style.space(3)

                PanelSectionHeader {
                  text: shortcutGroup.modelData.title.toUpperCase()
                  foreground: root.fg
                }

                Repeater {
                  model: shortcutGroup.modelData.entries

                  Row {
                    id: shortcutRow
                    required property var modelData
                    width: content.width
                    spacing: Style.space(8)

                    // The key sits in a chip, not as bare colored text: at
                    // sixteen entries the eye needs anchors to walk back up
                    // and down the column, and a filled shape does that at
                    // any distance. Right-aligned in a shared column so the
                    // chips stack into one rail.
                    Item {
                      width: root.shortcutRailWidth
                      height: keycap.height

                      Rectangle {
                        id: keycap
                        anchors.right: parent.right
                        implicitWidth: keyText.implicitWidth + Style.space(6)
                        height: keyText.implicitHeight + Style.space(2)
                        radius: Style.space(3)
                        color: Qt.rgba(Commons.Color.accent.r, Commons.Color.accent.g, Commons.Color.accent.b, 0.13)

                        Text {
                          id: keyText
                          anchors.centerIn: parent
                          text: shortcutRow.modelData.key
                          color: Commons.Color.accent
                          font.family: Style.font.family
                          font.pixelSize: Style.font.caption
                          font.bold: true
                        }
                      }
                    }

                    Text {
                      anchors.verticalCenter: parent.verticalCenter
                      text: shortcutRow.modelData.what
                      color: root.muted
                      font.family: Style.font.family
                      font.pixelSize: Style.font.caption
                    }
                  }
                }
              }
            }
          }

          // ---- undo window
          Rectangle {
            width: parent.width
            height: root.pendingAction ? Style.space(28) : 0
            visible: root.pendingAction !== null
            radius: Style.space(4)
            color: Qt.rgba(Commons.Color.accent.r, Commons.Color.accent.g, Commons.Color.accent.b, 0.14)

            Row {
              anchors.fill: parent
              anchors.leftMargin: Style.space(8)
              anchors.rightMargin: Style.space(8)
              spacing: Style.space(8)

              Text {
                anchors.verticalCenter: parent.verticalCenter
                text: ""
                color: Commons.Color.accent
                font.family: Style.font.family
                font.pixelSize: Style.font.icon
              }

              Text {
                anchors.verticalCenter: parent.verticalCenter
                // Only the title elides; the countdown is the point of the
                // row and must never be the part that gets cut.
                // Only the title may be cut. The depth and the countdown are
                // the two facts a held action has to convey, so neither shares
                // an eliding label with it.
                width: parent.width - Style.space(30)
                  - undoCount.implicitWidth - undoDepth.implicitWidth
                elide: Text.ElideRight
                text: Model.undoLabel(root.pendingAction, root.undoLeft)
                textFormat: Text.PlainText
                color: root.fg
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
              }

              Text {
                id: undoDepth
                anchors.verticalCenter: parent.verticalCenter
                text: Model.heldSuffix(root.pendingCount)
                color: root.muted
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }

              Text {
                id: undoCount
                anchors.verticalCenter: parent.verticalCenter
                text: "undo " + root.undoLeft + "s"
                color: Commons.Color.accent
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
                font.bold: true
              }
            }

            MouseArea {
              anchors.fill: parent
              cursorShape: Qt.PointingHandCursor
              onClicked: root.cancelPending()
            }
          }

          // ---- copy note. Same surface the undo bar uses, on a timer
          //      instead of a countdown: copying needs a receipt, not a
          //      confirmation dialog.
          Rectangle {
            width: parent.width
            height: root.copyNote !== "" ? Style.space(28) : 0
            visible: root.copyNote !== ""
            radius: Style.space(4)
            color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.08)

            Text {
              anchors.fill: parent
              anchors.leftMargin: Style.space(8)
              anchors.rightMargin: Style.space(8)
              verticalAlignment: Text.AlignVCenter
              elide: Text.ElideRight
              text: root.copyNote
              textFormat: Text.PlainText
              color: root.fg
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }
          }

          // ---- setup. This is the only onboarding surface the plugin gets:
          //      `omarchy plugin add` never runs plugin code or install
          //      hooks, so there is no post-install script to lean on.
          //
          //      One action, one sentence of why it is safe. Sign-in happens
          //      on ticktick.com in the browser; the API token is folded
          //      underneath for machines where that is not an option.
          Column {
            id: setupCard
            width: parent.width
            spacing: Style.space(6)
            visible: !root.signedIn

            PanelSeparator { width: parent.width; foreground: root.fg }

            Text {
              id: setupLeadText
              width: parent.width
              topPadding: Style.space(2)
              wrapMode: Text.WordWrap
              lineHeight: 1.15
              text: root.setupLead()
              textFormat: Text.PlainText
              color: root.fg
              font.family: Style.font.family
              font.pixelSize: Style.font.body
            }

            Text {
              id: setupDetailText
              width: parent.width
              wrapMode: Text.WordWrap
              visible: text !== ""
              text: root.setupDetail()
              textFormat: Text.PlainText
              // A failure reads in the same colour the footer uses for one.
              color: root.setupStage === "failed" ? Commons.Color.accent : root.muted
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }

            // The card's one primary action, so it is the one filled surface
            // on it: the accent wash says "this is the way in" without a
            // brand colour the theme never chose.
            Button {
              id: connectButton
              width: parent.width
              visible: root.setupStage !== "signing-in"
              enabled: root.canStartOAuth
              bordered: true
              text: root.connectLabel()
              foreground: Commons.Color.accent
              background: root.accentWash
              fontSize: Style.font.body
              onClicked: root.connectTickTick()
            }

            // While the browser has it: a way back to the page if its tab got
            // lost, and a way out that frees the callback listener.
            Row {
              id: signInControls
              visible: root.setupStage === "signing-in"
              spacing: Style.space(6)

              Button {
                id: reopenButton
                visible: root.authState === "waiting" && root.authUrl !== ""
                text: "Open sign-in page again"
                foreground: root.fg
                fontSize: Style.font.bodySmall
                onClicked: root.reopenSignIn()
              }

              Button {
                id: cancelButton
                text: "Cancel"
                foreground: root.fg
                fontSize: Style.font.bodySmall
                onClicked: root.cancelSignIn()
              }
            }

            Item {
              id: tokenToggle
              width: parent.width
              height: tokenToggleText.implicitHeight
              visible: root.setupStage !== "signing-in"

              Text {
                id: tokenToggleText
                text: "Use an API token instead " + (root.tokenFallbackOpen ? "▾" : "▸")
                color: tokenToggleHover.containsMouse ? Commons.Color.accent : root.muted
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }

              MouseArea {
                id: tokenToggleHover
                anchors.fill: tokenToggleText
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.toggleTokenFallback()
              }
            }

            Column {
              id: tokenFallback
              width: parent.width
              spacing: Style.space(6)
              visible: root.tokenFallbackOpen && root.setupStage !== "signing-in"

              Text {
                width: parent.width
                wrapMode: Text.WordWrap
                text: "In TickTick, open Settings › Account › API Token, create a token and paste it here."
                textFormat: Text.PlainText
                color: root.muted
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }

              TextField {
                id: tokenField
                width: parent.width
                // The token is equivalent to a password, so it is masked and
                // never echoed back into the panel.
                password: true
                placeholderText: "Paste API token…"
                foreground: root.fg
                font.family: Style.font.family
                font.pixelSize: Style.font.bodySmall
                enabled: !root.connecting
                onAccepted: root.connectWithApiToken()
                Keys.onEscapePressed: root.leaveTokenField()
              }

              Button {
                id: tokenConnectButton
                bordered: true
                text: root.connecting ? "Connecting…" : "Connect with token"
                enabled: !root.connecting && String(tokenField.text || "").trim() !== ""
                foreground: root.fg
                fontSize: Style.font.bodySmall
                onClicked: root.connectWithApiToken()
              }

              Text {
                id: tokenErrorText
                width: parent.width
                wrapMode: Text.WordWrap
                visible: root.tokenError !== ""
                text: root.tokenError
                textFormat: Text.PlainText
                color: Commons.Color.accent
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }

              Text {
                id: tokenDisclosure
                width: parent.width
                wrapMode: Text.WordWrap
                text: "Anyone holding this token can read and change your tasks. "
                  + "It is kept only on this computer, in a private file. "
                  + "Revoke it in TickTick if it leaks."
                textFormat: Text.PlainText
                color: root.muted
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }
            }
          }

          // ---- quick add. One line: what to add, and where it lands. The
          //      official API requires a real destination list, so the list
          //      rides beside the field as its destination rather than
          //      stacking a labelled form above it. Without the official
          //      backend there is no picker and the field takes the row.
          Row {
            id: quickAddRow
            width: parent.width
            spacing: Style.space(6)
            // The field's own condition, not `quickAdd.visible`: a child
            // reports itself hidden while its parent is, which would latch.
            visible: root.signedIn && root.showTasks

            TextField {
              id: quickAdd
              anchors.verticalCenter: parent.verticalCenter
              width: parent.width - (projectPicker.visible ? projectPicker.width + parent.spacing : 0)
              visible: root.signedIn && root.showTasks
              enabled: root.editingTaskId !== "" || !root.needsProjectChoice
              placeholderText: root.editingTaskId !== ""
                ? "Enter saves · esc cancels"
                : (root.needsProjectChoice ? "Choose a list first →" : "Add a task…  #tag  !1")
              foreground: root.fg
              font.family: Style.font.family
              font.pixelSize: Style.font.body
              onAccepted: root.submitQuickAdd()
              Keys.onReturnPressed: function(event) { root.quickAddReturn(event) }
              Keys.onEnterPressed: function(event) { root.quickAddReturn(event) }
              Keys.onEscapePressed: root.cancelEdit()
            }

            Dropdown {
              id: projectPicker
              anchors.verticalCenter: parent.verticalCenter
              width: Style.space(124)
              visible: root.signedIn && root.showTasks && root.cache.backend === "openapi"
              label: "Quick add list"
              showLabel: false
              // One control height across the row, whatever the field's
              // padding token makes its own.
              rowHeight: Math.max(Style.spacing.controlHeight, Math.round(quickAdd.height))
              value: root.hasDefaultProject ? root.defaultProject : ""
              options: root.projectOptions
              foreground: root.fg
              enabled: root.projectOptions.length > 1
              onChanged: function(value) { root.chooseProject(value) }
            }
          }

          Text {
            width: parent.width
            visible: projectPicker.visible && root.projectOptions.length <= 1
            text: "Create a list in TickTick to use quick add here."
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
            color: root.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
          }

          Text {
            width: parent.width
            // The line is held while the field is in use rather than only while
            // there is something to say. Collapsing it with the hint made the
            // task list jump down on the first keystroke and back up after every
            // enter, which is the moment you are looking at that list to see
            // the task land.
            visible: quickAdd.visible && (quickAdd.activeFocus || quickAdd.text !== "")
            opacity: root.quickAddHint !== "" ? 1 : 0
            text: root.quickAddHint !== "" ? root.quickAddHint : " "
            textFormat: Text.PlainText
            elide: Text.ElideRight
            color: root.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
          }

          Text {
            width: parent.width
            // The offer's own line, held together with the one above: an offer
            // appears the moment "am" is typed, and the list must not move then
            // any more than it does on the first keystroke.
            visible: quickAdd.visible && (quickAdd.activeFocus || quickAdd.text !== "")
            opacity: root.quickAddOfferHint !== "" ? 1 : 0
            text: root.quickAddOfferHint !== "" ? root.quickAddOfferHint : " "
            textFormat: Text.PlainText
            elide: Text.ElideRight
            color: root.muted
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
          }

          // ---- tasks
          Column {
            width: parent.width
            spacing: Style.space(4)
            visible: root.signedIn && root.showTasks

            PanelSeparator { width: parent.width; foreground: root.fg }

            Item {
              width: parent.width
              height: sectionLabel_tasks.implicitHeight

              PanelSectionHeader {
                id: sectionLabel_tasks
                anchors.left: parent.left
                text: "TASKS"
                foreground: root.fg
              }

              // The late count belongs to the backlog, which has its own rule
              // further down — unless the rule itself is cut off by maxTasks,
              // when the header carries it so late work stays signalled.
              PanelSectionHeader {
                anchors.right: parent.right
                anchors.baseline: sectionLabel_tasks.baseline
                text: root.overdueCount > 0 && !root.backlogRuleVisible
                  ? root.visibleTasks.length + " · " + root.overdueCount + " LATE"
                  : String(root.visibleTasks.length)
                foreground: root.overdueCount > 0 && !root.backlogRuleVisible
                  ? Commons.Color.accent : root.fg
              }
            }

            Text {
              width: parent.width
              visible: root.displayTasks.length === 0
              text: "Nothing due."
              color: root.muted
              font.family: Style.font.family
              font.pixelSize: Style.font.bodySmall
            }

            Repeater {
              model: root.displayTasks

              // The delegate is a column so a rule can be drawn above the row
              // without disturbing it: the row below keeps its own geometry
              // and every taskRow.* binding inside it still resolves.
              Column {
                id: taskCell
                required property var modelData
                required property int index
                spacing: 0

                // The first late row -- including when late work opens the
                // list, because the rule carries the count and the backlog
                // should be named whether or not the day sits above it. A
                // dated ghost above never heads the backlog, so one below it
                // still starts the rule.
                readonly property bool startsBacklog: modelData.ghost !== true
                  && Model.isOverdue(modelData, root.nowDate)
                  && (index === 0
                    || root.displayTasks[index - 1].ghost === true
                    || !Model.isOverdue(root.displayTasks[index - 1], root.nowDate))

                Item {
                  width: content.width
                  height: taskCell.startsBacklog
                    ? Math.max(Style.space(16), backlogCount.implicitHeight) : 0
                  visible: taskCell.startsBacklog

                  PanelSectionHeader {
                    id: backlogCount
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    text: root.overdueCount + " LATE"
                    foreground: Commons.Color.accent
                  }

                  // The rule stops short of the count rather than running
                  // under it: one line, reading left to right, ending in
                  // what it is about.
                  Rectangle {
                    anchors.left: parent.left
                    anchors.right: backlogCount.left
                    anchors.rightMargin: Style.space(6)
                    anchors.verticalCenter: parent.verticalCenter
                    height: 1
                    color: Qt.rgba(Commons.Color.accent.r, Commons.Color.accent.g, Commons.Color.accent.b, 0.3)
                  }
                }

                Rectangle {
                  id: taskRow
                  readonly property var modelData: taskCell.modelData
                  readonly property int index: taskCell.index
                  readonly property bool selected: root.isCursorOn("task", index)
                  readonly property bool pending: modelData.ghost === true
                  readonly property bool late: !pending && Model.isOverdue(modelData, root.nowDate)
                  readonly property string tier: Model.dueTier(modelData, root.nowDate)
                  readonly property string tagHex: Model.tagColor(modelData, root.tagsById)
                  readonly property string tagName: Model.tagLabel(modelData, root.tagsById)
                  readonly property var taskSubtasks: Model.subtasks(modelData)
                  readonly property bool hasDetails: Model.hasDetails(modelData)
                  readonly property bool expanded: hasDetails
                    && root.expandedTaskId === String(modelData.id)

                  width: content.width
                  height: Style.space(26) + (expanded ? detailColumn.height : 0)
                  radius: Style.space(4)
                  color: (taskHover.containsMouse || taskRow.selected)
                    ? Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.08)
                    : "transparent"
                  // Hover tints, the keyboard cursor outlines. Two different
                  // states deserve two different marks, not two strengths of
                  // the same one.
                  border.width: taskRow.selected ? 1 : 0
                  border.color: Qt.rgba(Commons.Color.accent.r, Commons.Color.accent.g, Commons.Color.accent.b, 0.55)

                  // Row-wide hover for the highlight only. It deliberately
                  // accepts no buttons: completion is irreversible from here,
                  // so the row must not be a 300px-wide destructive target.
                  MouseArea {
                    id: taskHover
                    anchors.fill: parent
                    hoverEnabled: true
                    acceptedButtons: Qt.NoButton
                    // Hover takes the cursor so keyboard and mouse never
                    // disagree about which row is current. Over an open task,
                    // only the title band speaks for the task — the strips
                    // below it are the subtasks', and they sync themselves.
                    onContainsMouseChanged: taskHover.followHover()
                    onPositionChanged: taskHover.followHover()
                    function followHover() {
                      if (!containsMouse) return
                      if (taskRow.expanded && mouseY >= Style.space(26)) return
                      root.cursorActive = false
                      root.syncCursorTo("task", taskRow.index)
                    }
                  }

                  Row {
                    // The title band keeps the height it had before tasks could
                    // open: expansion adds a section below it, it does not
                    // stretch the line the checkbox lives on.
                    anchors.top: parent.top
                    anchors.left: parent.left
                    anchors.right: parent.right
                    height: Style.space(26)
                    anchors.leftMargin: Style.space(6)
                    anchors.rightMargin: Style.space(6)
                    spacing: Style.space(8)

                    // The only thing that completes a task. Small and
                    // deliberate, because there is no undo in the panel.
                    Item {
                      anchors.verticalCenter: parent.verticalCenter
                      width: Style.space(18)
                      height: Style.space(18)

                      Text {
                        anchors.centerIn: parent
                        opacity: taskRow.pending ? 0.45 : 1
                        text: circleHover.containsMouse ? "" : ""
                        color: taskRow.late ? Commons.Color.accent : root.muted
                        font.family: Style.font.family
                        font.pixelSize: Style.font.icon
                      }

                      MouseArea {
                        id: circleHover
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.completeTask(taskRow.modelData)
                      }
                    }

                    // The task's own tag colour, straight from TickTick.
                    //
                    // The column is reserved even when empty. A Row gives an
                    // invisible child no width, so hiding the dot used to slide
                    // every untagged title left and break the one vertical line
                    // the eye follows down the list.
                    Rectangle {
                      anchors.verticalCenter: parent.verticalCenter
                      opacity: taskRow.tagHex === "" ? 0 : 1
                      width: Style.space(6)
                      height: Style.space(6)
                      radius: width / 2
                      color: taskRow.tagHex === "" ? "transparent" : taskRow.tagHex

                      PanelToolTip {
                        // The tooltip's Text lives in the shell and defaults to
                        // AutoText, so the remote tag name is defanged here.
                        text: Model.plainText(taskRow.tagName)
                        visible: tagHover.containsMouse && taskRow.tagName !== ""
                      }

                      MouseArea {
                        id: tagHover
                        anchors.fill: parent
                        hoverEnabled: true
                      }
                    }

                    // A title longer than the row is truncated until you point
                    // at it, and then it scrolls. Only the row under the cursor
                    // moves: a list where every long row animates at once cannot
                    // be scanned, which is the whole job of this list.
                    Item {
                      id: titleClip
                      anchors.verticalCenter: parent.verticalCenter
                      width: parent.width - Style.space(30)
                        - Style.space(14)
                        - dueLabel.implicitWidth
                        - (taskRow.hasDetails ? Style.space(24) : 0)
                      height: Style.space(26)
                      clip: true

                      Text {
                        id: titleText
                        anchors.verticalCenter: parent.verticalCenter
                        text: String(taskRow.modelData.title || "")
                        // Titles come from the server. AutoText would promote
                        // anything HTML-shaped to rich text, so every Text that
                        // shows remote strings pins the format down.
                        textFormat: Text.PlainText
                        color: taskRow.tier === "upcoming" ? root.muted : root.fg
                        font.family: Style.font.family
                        font.pixelSize: Style.font.body
                        font.bold: Model.priorityRank(taskRow.modelData) === "high"

                        readonly property bool overflowing: implicitWidth > titleClip.width
                        readonly property bool scrolling: overflowing
                          && (taskRow.selected || taskHover.containsMouse)

                        width: scrolling ? implicitWidth : titleClip.width
                        elide: scrolling ? Text.ElideNone : Text.ElideRight

                        // Leaving mid-scroll would strand the text half off the
                        // row, so it returns home when it stops.
                        onScrollingChanged: if (!scrolling) x = 0

                        SequentialAnimation on x {
                          running: titleText.scrolling
                          loops: Animation.Infinite

                          PauseAnimation { duration: 700 }
                          NumberAnimation {
                            from: 0
                            to: Math.min(0, titleClip.width - titleText.implicitWidth)
                            // Constant reading speed, so a longer title takes
                            // longer rather than moving faster.
                            duration: Math.max(900, (titleText.implicitWidth - titleClip.width) * 28)
                            easing.type: Easing.Linear
                          }
                          PauseAnimation { duration: 1100 }
                          NumberAnimation { to: 0; duration: 350; easing.type: Easing.OutCubic }
                        }
                      }
                    }

                    Text {
                      id: dueLabel
                      anchors.verticalCenter: parent.verticalCenter
                      text: taskRow.pending ? "adding…" : Model.dueLabel(taskRow.modelData, root.nowDate)
                      color: taskRow.tier === "overdue"
                        ? Commons.Color.accent
                        : (taskRow.tier === "today" ? root.fg : root.muted)
                      font.family: Style.font.family
                      font.pixelSize: Style.font.caption
                    }

                    // The only sign a task has something behind it. Present
                    // whenever details exist — collapsed rows hide their
                    // description, so without this the feature would be
                    // invisible until you happened to press `o`. The hit area
                    // is the full title band, not the glyph: a chevron is a
                    // target for a pointer, and a 10px one is a target for
                    // nobody.
                    Item {
                      id: detailToggle
                      visible: taskRow.hasDetails
                      width: visible ? Style.space(24) : 0
                      height: Style.space(26)

                      MouseArea {
                        id: chevronHover
                        anchors.fill: parent
                        hoverEnabled: true
                        cursorShape: Qt.PointingHandCursor
                        onClicked: root.expandedTaskId = taskRow.expanded
                          ? ""
                          : String(taskRow.modelData.id)
                      }

                      Text {
                        anchors.centerIn: parent
                        text: taskRow.expanded ? "\u25BE" : "\u25B8"
                        color: chevronHover.containsMouse || taskRow.expanded
                          ? root.fg
                          : root.muted
                        font.family: Style.font.family
                        font.pixelSize: Style.font.bodySmall
                      }

                      PanelToolTip {
                        text: taskRow.expanded ? "Hide details (o)" : "Show details (o)"
                        visible: chevronHover.containsMouse
                      }
                    }
                  }

                  // ---- expanded details: description and subtasks -----------
                  //
                  // Sits below the title band inside the same row rectangle, so
                  // the cursor outline, hover tint, and selection geometry all
                  // keep working while a task is open.
                  Column {
                    id: detailColumn
                    visible: taskRow.expanded
                    height: visible ? implicitHeight : 0
                    width: parent.width - Style.space(36)
                    x: Style.space(30)
                    y: Style.space(26)
                    spacing: Style.space(6)

                    Text {
                      visible: String(taskRow.modelData.content || "").trim() !== ""
                      width: parent.width
                      text: String(taskRow.modelData.content || "").trim()
                      // Descriptions come from the server and often read as
                      // markdown; AutoText would promote anything HTML-shaped
                      // in them to rich text.
                      textFormat: Text.PlainText
                      wrapMode: Text.Wrap
                      // The whole text, not a preview: the panel's own scroll
                      // is how a long one is read, and a description that
                      // stops mid-sentence is not a description.
                      color: root.muted
                      font.family: Style.font.family
                      font.pixelSize: Style.font.bodySmall
                      lineHeight: 1.15
                    }

                    Repeater {
                      model: taskRow.taskSubtasks

                      // A subtask behaves like a row, not like a label beside a
                      // tiny checkbox: the whole strip is the hit area, hover
                      // takes the keyboard cursor, and the cursor highlight is
                      // the same tint the task rows use.
                      Rectangle {
                        id: subtaskRow
                        required property var modelData
                        required property int index
                        readonly property bool itemPending: root.pendingItemIds[modelData.id] === true
                        readonly property bool selected: root.isCursorOnSubtask(taskRow.index, index)

                        width: detailColumn.width
                        height: Style.space(22)
                        radius: Style.space(3)
                        color: selected
                          ? Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.08)
                          : "transparent"

                        opacity: itemPending ? 0.45 : 1

                        MouseArea {
                          id: subtaskHover
                          anchors.fill: parent
                          hoverEnabled: true
                          cursorShape: Qt.PointingHandCursor
                          // Hover takes the cursor so `enter` always acts on
                          // the row being pointed at, exactly as task rows do.
                          onContainsMouseChanged: if (containsMouse) {
                            root.cursorActive = false
                            root.syncCursorToSubtask(taskRow.index, subtaskRow.index)
                          }
                          onClicked: root.toggleSubtask(taskRow.modelData, subtaskRow.modelData)
                        }

                        Row {
                          anchors.fill: parent
                          anchors.leftMargin: Style.space(4)
                          anchors.rightMargin: Style.space(4)
                          spacing: Style.space(8)

                          Item {
                            anchors.verticalCenter: parent.verticalCenter
                            width: Style.space(18)
                            height: Style.space(18)

                            Text {
                              anchors.centerIn: parent
                              text: subtaskRow.modelData.done ? "\u2611" : "\u2610"
                              color: subtaskRow.modelData.done ? Commons.Color.accent : root.muted
                              font.family: Style.font.family
                              font.pixelSize: Style.font.bodySmall
                            }
                          }

                          Text {
                            anchors.verticalCenter: parent.verticalCenter
                            width: parent.width - Style.space(26)
                            elide: Text.ElideRight
                            text: subtaskRow.modelData.title
                            // Subtask titles come from the server; same rule as
                            // every other remote string here.
                            textFormat: Text.PlainText
                            color: subtaskRow.modelData.done ? root.muted : root.fg
                            font.family: Style.font.family
                            font.pixelSize: Style.font.body
                            font.strikeout: subtaskRow.modelData.done
                          }
                        }
                      }
                    }
                  }
                }
              }
            }

            Text {
              width: parent.width
              visible: root.hiddenTaskCount > 0
              text: "+" + root.hiddenTaskCount + " more"
              color: root.muted
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }
          }

          // ---- habits
          Column {
            width: parent.width
            spacing: Style.space(4)
            visible: root.signedIn && root.showHabits && root.habits.length > 0

            // One rule divides the field from the lists, above TASKS. Below
            // it the sections are told apart by their headers and the air
            // above them; a rule over every header boxed each list in.
            Item { width: parent.width; height: Style.space(6) }

            Item {
              width: parent.width
              height: sectionLabel_habits.implicitHeight

              PanelSectionHeader {
                id: sectionLabel_habits
                anchors.left: parent.left
                text: "HABITS"
                foreground: root.fg
              }

              PanelSectionHeader {
                anchors.right: parent.right
                anchors.baseline: sectionLabel_habits.baseline
                text: root.habitsRemaining > 0 ? root.habitsRemaining + " LEFT" : "DONE"
                foreground: root.fg
              }
            }

            Repeater {
              model: root.habits

              Rectangle {
                id: habitRow
                required property var modelData
                required property int index
                readonly property bool selected: root.isCursorOn("habit", index)
                readonly property var rawProgress: Model.habitProgress(modelData, root.cache.checkins, root.todayStamp)
                // A held check-in shows as done immediately; undo puts it back.
                readonly property var progress: root.pendingHabitIds[String(modelData.id)]
                  ? { value: rawProgress.goal, goal: rawProgress.goal, ratio: 1, done: true, quantified: rawProgress.quantified }
                  : rawProgress
                readonly property int streak: Model.habitStreak(root.cache.checkins, modelData.id, root.todayStamp)

                width: content.width
                height: Style.space(26)
                radius: Style.space(4)
                color: (habitHover.containsMouse || habitRow.selected)
                  ? Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.08)
                  : "transparent"
                border.width: habitRow.selected ? 1 : 0
                border.color: Qt.rgba(Commons.Color.accent.r, Commons.Color.accent.g, Commons.Color.accent.b, 0.55)

                // Parity with tasks: the row highlights, the circle acts.
                // Clicking a habit's name used to check it in, which is the
                // same misclick trap the task rows already had removed.
                MouseArea {
                  id: habitHover
                  anchors.fill: parent
                  hoverEnabled: true
                  acceptedButtons: Qt.NoButton
                  onContainsMouseChanged: if (containsMouse) {
                    root.cursorActive = false
                    root.syncCursorTo("habit", habitRow.index)
                  }
                }

                Row {
                  anchors.fill: parent
                  anchors.leftMargin: Style.space(6)
                  anchors.rightMargin: Style.space(6)
                  spacing: Style.space(8)

                  Item {
                    anchors.verticalCenter: parent.verticalCenter
                    width: Style.space(18)
                    height: Style.space(18)

                    Text {
                      anchors.centerIn: parent
                      text: habitRow.progress.done ? "" : ""
                      color: habitRow.progress.done ? Commons.Color.accent : root.muted
                      font.family: Style.font.family
                      font.pixelSize: Style.font.icon
                    }

                    MouseArea {
                      id: habitCircleHover
                      anchors.fill: parent
                      hoverEnabled: true
                      cursorShape: Qt.PointingHandCursor
                      onClicked: root.checkInHabit(habitRow.modelData)
                    }
                  }

                  // Habits have no tag colour, but they keep the same empty
                  // column so their titles line up with the tasks above.
                  Item {
                    anchors.verticalCenter: parent.verticalCenter
                    width: Style.space(6)
                    height: Style.space(6)
                  }

                  Text {
                    anchors.verticalCenter: parent.verticalCenter
                    width: parent.width - Style.space(44) - streakLabel.implicitWidth
                    elide: Text.ElideRight
                    text: Model.habitLabel(habitRow.modelData, habitRow.progress)
                    textFormat: Text.PlainText
                    color: habitRow.progress.done ? root.muted : root.fg
                    font.family: Style.font.family
                    font.pixelSize: Style.font.body
                  }

                  Text {
                    id: streakLabel
                    anchors.verticalCenter: parent.verticalCenter
                    text: habitRow.streak > 1 ? habitRow.streak + " " : ""
                    color: root.muted
                    font.family: Style.font.family
                    font.pixelSize: Style.font.caption
                  }
                }
              }
            }
          }

          // ---- pomodoro
          Column {
            width: parent.width
            spacing: Style.space(4)
            visible: root.signedIn && root.showPomo

            Item { width: parent.width; height: Style.space(6) }

            Item {
              width: parent.width
              height: sectionLabel_focus.implicitHeight

              PanelSectionHeader {
                id: sectionLabel_focus
                anchors.left: parent.left
                text: "FOCUS"
                foreground: root.fg
              }

              PanelSectionHeader {
                anchors.right: parent.right
                anchors.baseline: sectionLabel_focus.baseline
                text: Model.pomoTodayLabel(root.pomoStats, root.pomoPrefs).toUpperCase()
                foreground: root.fg
              }
            }

            Item {
              width: parent.width
              height: Style.space(28)

              Row {
                anchors.left: parent.left
                anchors.leftMargin: Style.space(6)
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(8)

                Text {
                  anchors.verticalCenter: parent.verticalCenter
                  text: ""
                  color: root.pomoPhase === "focus" ? Commons.Color.accent : root.muted
                  font.family: Style.font.family
                  font.pixelSize: Style.font.icon
                }

                Text {
                  anchors.verticalCenter: parent.verticalCenter
                  text: root.pomoPhase === "idle"
                    ? Model.formatClock(Model.pomoPhaseSeconds("focus", root.pomoPrefs))
                    : root.pomoClock
                  color: root.pomoPhase === "idle" ? root.muted : root.fg
                  font.family: Style.font.family
                  font.pixelSize: Style.font.subtitle
                  font.bold: root.pomoRunning
                }

                Text {
                  anchors.verticalCenter: parent.verticalCenter
                  text: root.pomoPhase === "idle"
                    ? "ready"
                    : (root.pomoPaused ? "paused" : Model.pomoPhaseLabel(root.pomoPhase).toLowerCase())
                  color: root.muted
                  font.family: Style.font.family
                  font.pixelSize: Style.font.caption
                }
              }

              Row {
                anchors.right: parent.right
                anchors.rightMargin: Style.space(6)
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(4)

                PanelActionButton {
                  iconText: root.pomoRunning ? "" : ""
                  tooltipText: root.pomoRunning ? "Pause" : (root.pomoPaused ? "Resume" : "Start focus")
                  foreground: root.fg
                  onClicked: {
                    if (root.pomoRunning) root.pausePomo()
                    else if (root.pomoPaused) root.resumePomo()
                    else root.startPomo("focus")
                  }
                }

                PanelActionButton {
                  visible: root.pomoPhase !== "idle"
                  iconText: ""
                  tooltipText: "Discard this block"
                  foreground: root.muted
                  onClicked: root.stopPomo()
                }
              }
            }
          }

          // ---- out to the source of truth. A panel that only shows a
          //      slice of your tasks should say where the rest live.
          //      Signed out it is still the way to the app, worded and
          //      placed apart from the card so it never reads as a way to
          //      connect: the app's own login does not reach this plugin.
          //      Hidden mid sign-in, where the browser is the window to use.
          Item {
            id: escapeRow
            width: parent.width
            height: Style.space(22)
            visible: root.signedIn || root.setupStage !== "signing-in"

            Row {
              anchors.centerIn: parent
              spacing: Style.space(4)

              Text {
                id: escapeLabel
                text: root.signedIn ? "Open in TickTick" : "Open TickTick"
                color: escapeHover.containsMouse ? Commons.Color.accent : root.muted
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }

              Text {
                anchors.verticalCenter: escapeLabel.verticalCenter
                text: ""
                color: escapeHover.containsMouse ? Commons.Color.accent : root.muted
                font.family: Style.font.family
                font.pixelSize: Style.font.caption
              }
            }

            MouseArea {
              id: escapeHover
              anchors.fill: parent
              hoverEnabled: true
              cursorShape: Qt.PointingHandCursor
              onClicked: root.launchTickTick()

              PanelToolTip {
                text: "Opens TickTick itself. Signing in there does not connect this panel."
                visible: !root.signedIn && escapeHover.containsMouse
              }
            }
          }

          // ---- footer
          Text {
            id: footerError
            width: parent.width
            // While the setup card is up it already explains the situation in
            // the plugin's own terms. Repeating the CLI's "run this command"
            // underneath it contradicts the card, so nothing shows here then;
            // sign-in and token failures are reported inside the card.
            visible: root.signedIn && (root.actionError !== "" || root.cacheError !== "" || root.cacheWarning !== "")
            wrapMode: Text.WordWrap
            text: root.actionError !== "" ? root.actionError : (root.cacheError !== "" ? root.cacheError : root.cacheWarning)
            textFormat: Text.PlainText
            color: Commons.Color.accent
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
          }
        }
      }
    }
  }

  function headerSubtitle() {
    if (!signedIn) {
      if (setupStage === "signing-in") {
        if (authState === "preparing") return "Starting sign-in…"
        if (authState === "validating") return "Checking your account…"
        return "Waiting for browser sign-in…"
      }
      if (connecting) return "Connecting…"
      if (setupStage === "failed") return "Sign-in didn't finish"
      if (setupStage === "reconnect") return "Signed out"
      return "Not connected"
    }
    if (root.syncing) return "Syncing…"

    var parts = []
    if (showTasks) parts.push(visibleTasks.length + (visibleTasks.length === 1 ? " task" : " tasks"))
    // "2 of 2 habits" read as two done; the count is of what is still open.
    if (showHabits && habits.length > 0) parts.push(habitsRemaining + " of " + habits.length + " habits left")

    if (queuedCount > 0) parts.push(queuedCount + " waiting to send")

    var age = staleMinutes
    if (age > 10) parts.push("synced " + age + "m ago")
    return parts.join(" · ")
  }

  // ---- setup card copy. What it says follows setupStage; the service's
  //      authError is already a sentence meant for people, never CLI output.

  function setupLead() {
    if (setupStage === "signing-in") {
      if (authState === "preparing") return "Starting sign-in…"
      if (authState === "validating") return "Checking your TickTick account…"
      return "Finish signing in to TickTick in your browser."
    }
    if (setupStage === "failed") return "Sign-in didn't finish."
    if (setupStage === "reconnect") {
      var lead = "TickTick signed this plugin out. Reconnect to keep syncing."
      if (queuedCount > 0)
        lead += " " + queuedCount + (queuedCount === 1 ? " change is" : " changes are") + " waiting to send."
      return lead
    }
    return "Connect your TickTick account to see your tasks, habits and focus here."
  }

  function setupDetail() {
    if (setupStage === "signing-in")
      return authState === "waiting" ? "This panel updates by itself once you allow access." : ""
    if (setupStage === "failed") return authError !== "" ? authError : "TickTick didn't confirm the sign-in."
    return "Sign-in opens ticktick.com in your browser. This plugin never sees your password."
  }

  function connectLabel() {
    if (setupStage === "failed") return "Try again"
    if (setupStage === "reconnect") return "Reconnect TickTick"
    return "Connect TickTick"
  }
}
