const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const vm = require('node:vm')

// Runs the setup card's real handlers and bindings out of Panel.qml against a
// fake service, so the order of "hide the popup, then hand off" and what the
// card says in each state are checked as behaviour, not as text in a file.
const source = fs.readFileSync(path.join(__dirname, '..', 'Panel.qml'), 'utf8')
const lines = source.split('\n')
const indentOf = line => line.match(/^ */)[0].length

function block(start) {
  const closing = ' '.repeat(indentOf(lines[start])) + '}'
  for (let i = start + 1; i < lines.length; i++) {
    if (lines[i] === closing) return lines.slice(start, i + 1).join('\n')
  }
  throw new Error('unterminated block at line ' + (start + 1))
}

function continued(start, first, indent) {
  const parts = [first]
  for (let i = start + 1; i < lines.length; i++) {
    if (lines[i].trim() === '' || indentOf(lines[i]) <= indent) break
    parts.push(lines[i].trim())
  }
  return parts.join(' ')
}

function functionSource(name) {
  const start = lines.findIndex(line => line.startsWith('  function ' + name + '('))
  assert.notEqual(start, -1, name)
  return block(start)
}

function propertyExpression(name) {
  const pattern = new RegExp('^  readonly property \\w+ ' + name + ':\\s*(.*)$')
  const start = lines.findIndex(line => pattern.test(line))
  assert.notEqual(start, -1, name)
  const first = lines[start].match(pattern)[1]
  if (first.trim() === '{') return '(function() ' + block(start).slice(block(start).indexOf('{')) + ')()'
  return continued(start, first, 2)
}

// A binding or one-line handler on the element carrying `id: <id>`.
function element(id, name) {
  const at = lines.findIndex(line => line.trim() === 'id: ' + id)
  assert.notEqual(at, -1, id)
  const indent = indentOf(lines[at])
  for (let i = at; i < lines.length; i++) {
    if (lines[i].trim() !== '' && indentOf(lines[i]) < indent) break
    const match = indentOf(lines[i]) === indent && lines[i].trim().match(new RegExp('^' + name + ':\\s*(.*)$'))
    if (match) return continued(i, match[1], indent)
  }
  assert.fail(id + '.' + name)
}

function handlerBlock(name) {
  const start = lines.findIndex(line => line.trim().startsWith(name + ':'))
  assert.notEqual(start, -1, name)
  const body = block(start)
  return body.slice(body.indexOf(':') + 1).trim()
}

const BINDINGS = ['signedIn', 'cacheError', 'cacheWarning', 'defaultProject', 'hasDefaultProject', 'needsProjectChoice', 'projectOptions', 'queuedCount', 'actionError', 'connecting', 'authState',
  'authError', 'authUrl', 'oauthActive', 'canStartOAuth', 'reconnecting', 'setupStage', 'tokenError', 'navRows']
const FUNCTIONS = ['openTickTick', 'launchTickTick', 'refresh', 'connectTickTick', 'reopenSignIn', 'cancelSignIn',
  'toggleTokenFallback', 'connectWithApiToken', 'leaveTokenField', 'activateCursor', 'close',
  'headerSubtitle', 'setupLead', 'setupDetail', 'connectLabel']

const FIRST_RUN = {syncedAt: 0, authRequired: true, queued: 0, error: 'Not signed in to TickTick.'}
const REJECTED = {syncedAt: 1700000000000, authRequired: true, queued: 0, error: 'Session expired'}
const CONNECTED = {syncedAt: 1700000000000, authRequired: false, queued: 0, error: null}

function panel({cache = FIRST_RUN, service = {}, withService = true} = {}) {
  const events = []
  const later = []
  const field = (name, extra = {}) => ({
    text: '', focus: false, activeFocus: false,
    forceActiveFocus() { events.push('focus:' + name) },
    ...extra,
  })
  const svc = withService ? {
    authState: 'idle', authError: '', authUrl: '', oauthConnecting: false, connecting: false, actionError: '',
    startOAuth: () => events.push('startOAuth'),
    cancelOAuth: () => events.push('cancelOAuth'),
    reopenAuthPage: () => events.push('reopenAuthPage'),
    connectWithApiToken: token => events.push('apiToken:' + token),
    refresh: () => events.push('refresh'),
    completeTask: task => events.push('complete:' + task.id),
    checkInHabit: () => events.push('checkIn'),
    startPomo: () => events.push('startPomo'),
    // close() sends held writes first; that is covered elsewhere and kept
    // out of the launch-order log here.
    flushPending: () => {},
    ...service,
  } : null
  const ctx = {
    String, Math, JSON, Number,
    Qt: {callLater: fn => later.push(fn)},
    Model: {subtasks: () => [], cycleHorizon: h => h},
    svc, cache: {...cache}, opened: true,
    settings: {},
    setting(name, fallback) { const value = ctx.settings[name]; return value === undefined || value === null ? fallback : value },
    projectPicker: {popupOpen: false, close() {}, open() { events.push('projectPicker') }},
    bar: {run: command => events.push('run:' + command)},
    controller: {hide: () => events.push('hide')},
    quickAdd: field('quickAdd'), tokenField: field('tokenField'), keyCatcher: field('keyCatcher'),
    horizon: 'Today', viewHorizon: 'Today', showTasks: true, showHabits: true,
    displayTasks: [{id: 'hidden-task', title: 'stale'}], habits: [], expandedTaskId: '',
    cursor: -1, cursorActive: false, helpVisible: false, editingTaskId: '', editingTitle: '',
    pendingItemIds: {}, tokenFallbackOpen: false, tokenAttempted: false, syncing: false,
    copyNote: '', staleMinutes: 0, visibleTasks: [], habitsRemaining: 0,
    flushPending() { if (svc) svc.flushPending() },
    completeTask(task) { if (svc) svc.completeTask(task) },
    checkInHabit(habit) { if (svc) svc.checkInHabit(habit) },
    startPomo(phase) { if (svc) svc.startPomo(phase) },
    showCopyNote(text) { ctx.copyNote = text },
  }
  ctx.root = ctx
  vm.createContext(ctx)
  for (const name of BINDINGS) {
    const expression = propertyExpression(name)
    Object.defineProperty(ctx, name, {get: () => vm.runInContext(expression, ctx), configurable: true})
  }
  for (const name of FUNCTIONS) vm.runInContext(functionSource(name), ctx)
  const read = (id, name) => vm.runInContext(element(id, name), ctx)
  const run = code => vm.runInContext(code, ctx)
  const flushLater = () => { while (later.length) later.shift()() }
  return {ctx, svc, events, later, read, run, flushLater}
}

test('first run is a connect card, not an empty list or a reconnect', () => {
  const {ctx, read} = panel()
  assert.equal(ctx.signedIn, false)
  assert.equal(ctx.reconnecting, false)
  assert.equal(ctx.setupStage, 'connect')
  assert.equal(read('viewLabel', 'text'), 'TickTick')
  assert.equal(ctx.headerSubtitle(), 'Not connected')
  assert.equal(ctx.connectLabel(), 'Connect TickTick')
  assert.match(ctx.setupLead(), /^Connect your TickTick account/)
  assert.doesNotMatch(ctx.setupLead() + ctx.setupDetail(), /ended|session|reconnect|F12|cookie/i)
  assert.match(ctx.setupDetail(), /browser.*never sees your password/)
  assert.equal(read('setupCard', 'visible'), true)
  assert.equal(read('connectButton', 'visible'), true)
  assert.equal(read('connectButton', 'enabled'), true)
  assert.equal(read('signInControls', 'visible'), false)
})

test('signed out hides range dots, sync, queued badge and the raw error footer', () => {
  const {ctx, read} = panel({service: {actionError: '{"ok": false, "error": "Not signed in"}'}})
  assert.equal(read('viewDots', 'visible'), false)
  assert.equal(read('viewSwitch', 'enabled'), false)
  assert.equal(read('syncButton', 'visible'), false)
  ctx.cache.queued = 3
  assert.equal(read('queuedBadge', 'visible'), false)
  assert.equal(read('footerError', 'visible'), false)
  // No token was sent, so the first-run "not signed in" is not a token error.
  assert.equal(ctx.tokenError, '')
})

test('only a credential rejected after a real sync reads as reconnect', () => {
  const {ctx} = panel({cache: {...REJECTED, queued: 2}})
  assert.equal(ctx.setupStage, 'reconnect')
  assert.equal(ctx.connectLabel(), 'Reconnect TickTick')
  assert.equal(ctx.headerSubtitle(), 'Signed out')
  assert.match(ctx.setupLead(), /signed this plugin out.*2 changes are waiting to send/)
  ctx.cache.queued = 1
  assert.match(ctx.setupLead(), /1 change is waiting/)
})

test('Connect hides the popup before the service opens the browser', () => {
  const {ctx, events, later, flushLater} = panel()
  ctx.tokenFallbackOpen = true
  ctx.tokenField.text = 'dummy-half-typed'
  run(ctx, element('connectButton', 'onClicked'))
  assert.equal(ctx.tokenField.text, '')
  assert.equal(ctx.tokenFallbackOpen, false)
  assert.ok(events.includes('hide'))
  assert.equal(events.includes('startOAuth'), false, 'browser must not launch while popup is up')
  assert.equal(later.length, 1)
  flushLater()
  assert.ok(events.indexOf('hide') < events.indexOf('startOAuth'))
})

test('enter on the signed-out card connects; it never completes a hidden stale task', () => {
  const {ctx, events, flushLater} = panel({cache: REJECTED})
  ctx.cursor = 0
  ctx.cursorActive = true
  assert.deepEqual(Array.from(ctx.navRows), [])
  ctx.activateCursor()
  flushLater()
  assert.equal(events.some(e => e.startsWith('complete:')), false)
  assert.deepEqual(events.filter(e => e === 'hide' || e === 'startOAuth'), ['hide', 'startOAuth'])
})

test('waiting state: reopen hides first, cancel stays, connect and token are hidden', () => {
  const {ctx, svc, events, later, flushLater, read} = panel({
    service: {authState: 'waiting', authUrl: 'https://ticktick.com/oauth/authorize?x=1', oauthConnecting: true},
  })
  assert.equal(ctx.setupStage, 'signing-in')
  assert.equal(ctx.headerSubtitle(), 'Waiting for browser sign-in…')
  assert.match(ctx.setupLead(), /Finish signing in to TickTick in your browser/)
  assert.equal(read('connectButton', 'visible'), false)
  assert.equal(read('signInControls', 'visible'), true)
  assert.equal(read('reopenButton', 'visible'), true)
  assert.equal(read('tokenToggle', 'visible'), false)
  ctx.tokenFallbackOpen = true
  assert.equal(read('tokenFallback', 'visible'), false)
  assert.equal(read('escapeRow', 'visible'), false)

  // A second start cannot be launched from the card or from enter.
  assert.equal(ctx.canStartOAuth, false)
  ctx.activateCursor()
  ctx.connectTickTick()
  assert.equal(later.length, 0)

  run(ctx, element('reopenButton', 'onClicked'))
  assert.deepEqual(events, ['hide'])
  flushLater()
  assert.deepEqual(events, ['hide', 'reopenAuthPage'])

  events.length = 0
  run(ctx, element('cancelButton', 'onClicked'))
  assert.deepEqual(events, ['cancelOAuth'])

  // Nor does a sync race the sign-in into stamping the new cache signed out.
  events.length = 0
  ctx.refresh(true)
  assert.deepEqual(events, [])
  svc.authUrl = ''
  assert.equal(read('reopenButton', 'visible'), false)
})

test('preparing and validating say what is happening without a reopen link', () => {
  for (const [state, subtitle] of [['preparing', 'Starting sign-in…'], ['validating', 'Checking your account…']]) {
    const {ctx, read} = panel({service: {authState: state, oauthConnecting: true}})
    assert.equal(ctx.setupStage, 'signing-in')
    assert.equal(ctx.headerSubtitle(), subtitle)
    assert.equal(ctx.setupDetail(), '')
    assert.equal(read('reopenButton', 'visible'), false)
  }
})

test('failure shows the service message and offers retry plus the token path', () => {
  const {ctx, events, flushLater, read} = panel({
    service: {authState: 'failed', authError: 'You declined access in TickTick.'},
  })
  assert.equal(ctx.setupStage, 'failed')
  assert.equal(ctx.headerSubtitle(), "Sign-in didn't finish")
  assert.equal(ctx.setupDetail(), 'You declined access in TickTick.')
  assert.equal(ctx.connectLabel(), 'Try again')
  assert.equal(read('tokenToggle', 'visible'), true)
  ctx.svc.authError = ''
  assert.equal(ctx.setupDetail(), "TickTick didn't confirm the sign-in.")
  ctx.connectTickTick()
  flushLater()
  assert.deepEqual(events, ['hide', 'startOAuth'])
})

test('token fallback is folded, masked, disclosed and never mentions cookies', () => {
  const {ctx, read} = panel()
  assert.equal(read('tokenFallback', 'visible'), false)
  run(ctx, element('tokenToggleHover', 'onClicked'))
  assert.equal(ctx.tokenFallbackOpen, true)
  assert.equal(read('tokenFallback', 'visible'), true)
  assert.equal(read('tokenField', 'password'), true)
  assert.match(read('tokenDisclosure', 'text'), /read and change your tasks.*only on this computer.*Revoke/)
  const card = block(lines.findIndex(line => line.trim() === 'id: setupCard') - 1)
  assert.match(card, /Settings › Account › API Token/)
  assert.doesNotMatch(card, /F12|cookie|password: false/i)
})

test('a sent token leaves the field at once and goes to the official token API', () => {
  const {ctx, events} = panel()
  ctx.tokenFallbackOpen = true
  ctx.tokenField.text = '  dummy-api-token  '
  ctx.connectWithApiToken()
  assert.equal(ctx.tokenField.text, '')
  assert.ok(events.includes('apiToken:dummy-api-token'))
  assert.equal(events.includes('hide'), false, 'nothing external opens, so the panel stays')
  assert.equal(ctx.tokenAttempted, true)

  // Blank or mid-sign-in submits still clear the field and send nothing.
  for (const service of [{}, {oauthConnecting: true, authState: 'waiting'}, {connecting: true}]) {
    const other = panel({service})
    other.ctx.tokenField.text = service.oauthConnecting || service.connecting ? 'dummy-other' : '   '
    other.ctx.connectWithApiToken()
    assert.equal(other.ctx.tokenField.text, '')
    assert.equal(other.events.some(e => e.startsWith('apiToken:')), false)
  }
})

// Service.connectWithToken(token, true) marks the check authState
// "validating" with connecting set, then "failed" + authError on a non-zero
// exit. Neither may turn into the browser card.
test('a token check stays in the token section, never the browser card', () => {
  const {ctx, svc, events, read} = panel()
  ctx.tokenFallbackOpen = true
  ctx.tokenField.text = 'dummy-bad'
  ctx.connectWithApiToken()
  Object.assign(svc, {connecting: true, authState: 'validating'})
  assert.equal(ctx.oauthActive, false)
  assert.equal(ctx.setupStage, 'connect')
  assert.equal(read('signInControls', 'visible'), false, 'no Cancel that cannot stop a token check')
  assert.equal(read('tokenFallback', 'visible'), true)
  assert.equal(read('tokenConnectButton', 'text'), 'Connecting…')
  assert.equal(ctx.tokenError, '')
  assert.equal(ctx.headerSubtitle(), 'Connecting…')
  assert.equal(read('connectButton', 'enabled'), false, 'no browser sign-in while a token is in flight')

  Object.assign(svc, {connecting: false, authState: 'failed',
    authError: 'TickTick rejected that API token.', actionError: 'TickTick rejected that API token.'})
  assert.equal(ctx.setupStage, 'connect')
  assert.equal(ctx.connectLabel(), 'Connect TickTick')
  assert.equal(ctx.setupDetail().includes('rejected'), false, 'reported once, by the token field')
  assert.equal(ctx.tokenError, 'TickTick rejected that API token.')
  assert.equal(read('tokenErrorText', 'visible'), true)
  assert.equal(read('footerError', 'visible'), false)

  // Closing folds it; reopening is a plain connect card, not "sign-in didn't finish".
  ctx.close()
  assert.equal(ctx.tokenFallbackOpen, false)
  assert.equal(ctx.setupStage, 'connect')
  assert.equal(ctx.headerSubtitle(), 'Not connected')

  // A browser attempt after that owns any failure it produces.
  events.length = 0
  ctx.connectTickTick()
  assert.equal(ctx.tokenAttempted, false)
  assert.equal(ctx.setupStage, 'failed')
})

test('closing clears any paste; a pending token keeps its section for the result', () => {
  const {ctx, svc, events} = panel()
  ctx.tokenFallbackOpen = true
  ctx.tokenAttempted = true
  ctx.tokenField.text = 'dummy-unsent'
  ctx.close()
  assert.equal(ctx.tokenField.text, '')
  assert.equal(ctx.tokenField.focus, false)
  assert.equal(ctx.tokenFallbackOpen, false)
  assert.equal(events.at(-1), 'hide')

  svc.connecting = true
  ctx.tokenFallbackOpen = true
  ctx.tokenAttempted = true
  ctx.tokenField.text = 'dummy-second'
  ctx.close()
  assert.equal(ctx.tokenField.text, '')
  assert.equal(ctx.tokenFallbackOpen, true)
})

test('Escape in the token field clears it and hands keys back to the panel', () => {
  const {ctx, events} = panel()
  ctx.tokenField.text = 'dummy-typed'
  run(ctx, element('tokenField', 'Keys.onEscapePressed'))
  assert.equal(ctx.tokenField.text, '')
  assert.ok(events.includes('focus:keyCatcher'))
  // While it has focus, the panel's single-key shortcuts must not eat j/k/x/enter.
  ctx.tokenField.activeFocus = true
  assert.equal(read(ctx, 'keyCatcher', 'blocked'), true)
})

test('Open TickTick is apart from the card, honest, and launches after hiding', () => {
  const {ctx, events, later, flushLater, read} = panel()
  assert.equal(read('escapeRow', 'visible'), true)
  assert.equal(read('escapeLabel', 'text'), 'Open TickTick')
  const tip = lines.slice(lines.findIndex(l => l.trim() === 'id: escapeHover')).join('\n')
  assert.match(tip, /does not connect this panel/)
  run(ctx, element('escapeHover', 'onClicked'))
  assert.deepEqual(events, ['hide'])
  assert.equal(later.length, 1)
  flushLater()
  assert.equal(events.length, 2)
  assert.match(events[1], /^run:.*ticktick/)
  const card = block(lines.findIndex(line => line.trim() === 'id: setupCard') - 1)
  assert.doesNotMatch(card, /openTickTick|launchTickTick/, 'the card itself never offers the app as a way in')
})

test('signed-in panel keeps its header, controls, footer and shortcuts', () => {
  const {ctx, events, read} = panel({cache: CONNECTED, service: {actionError: 'Network down'}})
  assert.equal(ctx.signedIn, true)
  assert.equal(read('setupCard', 'visible'), false)
  assert.equal(read('viewLabel', 'text'), 'All clear')
  ctx.visibleTasks = [{id: 't'}]
  assert.equal(read('viewLabel', 'text'), 'Today')
  assert.equal(read('viewDots', 'visible'), true)
  assert.equal(read('syncButton', 'visible'), true)
  assert.equal(read('footerError', 'visible'), true)
  assert.equal(read('escapeLabel', 'text'), 'Open in TickTick')
  assert.deepEqual(Array.from(ctx.navRows, row => row.section), ['task'])

  const onTextKey = vm.runInContext('(' + handlerBlock('onTextKey') + ')', ctx)
  onTextKey('p')
  assert.ok(events.includes('startPomo'))
  ctx.cursor = 0
  ctx.cursorActive = true
  ctx.activateCursor()
  assert.ok(events.includes('complete:hidden-task'))
  assert.equal(events.includes('startOAuth'), false)
})

test('signed out, list and timer keys do nothing; ? and r still answer', () => {
  const {ctx, events} = panel()
  const onTextKey = vm.runInContext('(' + handlerBlock('onTextKey') + ')', ctx)
  for (const key of ['a', 'p', 'd', 'v', 'e', 'o', 'c']) onTextKey(key)
  assert.deepEqual(events, [])
  onTextKey('?')
  assert.equal(ctx.helpVisible, true)
  onTextKey('r')
  assert.deepEqual(events, ['refresh'])
})

test('the card going away on success leaves a receipt and resets the fallback', () => {
  const {ctx, run} = panel({cache: CONNECTED})
  ctx.tokenFallbackOpen = true
  ctx.tokenAttempted = true
  run('(function() ' + handlerBlock('onSignedInChanged') + ')()')
  assert.equal(ctx.copyNote, 'Connected to TickTick')
  assert.equal(ctx.tokenFallbackOpen, false)
  assert.equal(ctx.tokenAttempted, false)
})

test('an older service without OAuth leaves the button inert instead of throwing', () => {
  const {ctx, events, later} = panel({service: {startOAuth: undefined}})
  assert.equal(ctx.canStartOAuth, false)
  ctx.connectTickTick()
  ctx.activateCursor()
  assert.equal(later.length, 0)
  assert.deepEqual(events, [])
  const none = panel({withService: false})
  none.ctx.connectTickTick()
  none.ctx.cancelSignIn()
  none.ctx.reopenSignIn()
  assert.equal(none.later.length, 0)
})

test('official quick add asks for a real list and preserves editing access', () => {
  const {ctx, events, read} = panel({cache: {...CONNECTED, backend: 'openapi', projects: [{id: 'p1', name: 'Work'}, {id: 'closed', name: 'Old', closed: true}]}})
  assert.equal(read('projectPicker', 'visible'), true)
  assert.equal(read('quickAdd', 'enabled'), false)
  assert.equal(ctx.projectOptions.length, 2)
  const onTextKey = vm.runInContext('(' + handlerBlock('onTextKey') + ')', ctx)
  onTextKey('a')
  assert.ok(events.includes('projectPicker'))
  assert.equal(events.includes('focusQuickAdd'), false)
  ctx.editingTaskId = 't1'
  assert.equal(read('quickAdd', 'enabled'), true)
  ctx.editingTaskId = ''
  ctx.settings.defaultProject = 'p1'
  assert.equal(read('quickAdd', 'enabled'), true)
  assert.equal(read('projectPicker', 'value'), 'p1')
  ctx.settings.defaultProject = 'closed'
  assert.equal(read('quickAdd', 'enabled'), false)
})

test('official capability warnings are visible without replacing action errors', () => {
  const {ctx, read} = panel({cache: {...CONNECTED, backend: 'openapi', warnings: ['Focus preferences unavailable.']}})
  assert.equal(read('footerError', 'visible'), true)
  assert.equal(read('footerError', 'text'), 'Focus preferences unavailable.')
  ctx.svc.actionError = 'Write failed'
  assert.equal(read('footerError', 'text'), 'Write failed')
})

function run(ctx, code) {
  return vm.runInContext(code, ctx)
}

function read(ctx, id, name) {
  return vm.runInContext(element(id, name), ctx)
}
