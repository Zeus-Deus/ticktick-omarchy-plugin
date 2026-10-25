const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const vm = require('node:vm')
const source = fs.readFileSync(path.join(__dirname, '..', 'Service.qml'), 'utf8')
function body(name) {
  const match = source.match(new RegExp('  function ' + name + '\\([^)]*\\) \\{[\\s\\S]*?\\n  \\}'))
  assert.ok(match, name)
  return match[0]
}
function fixture() {
  const opened = [], signals = [], later = []
  const ctx = {
    authState: 'idle', authError: '', authUrl: '', actionError: '',
    oauthRequested: false, oauthHandled: false, oauthCancelled: false, oauthSucceeded: false, oauthGeneration: 0,
    connecting: false, loginToken: '', cli: '/plugin/bin/omarchy-ticktick', actionQueue: [],
    syncProc: {running: false}, actionProc: {running: false, command: []},
    oauthProc: {running: false, command: [], signal: n => signals.push(n)},
    launches: 0, launching: () => ctx.launches++,
    dataFile: {reload: () => ctx.reloads++}, reloads: 0,
    Model: {elide: (x, n) => String(x).slice(0, n)},
    Qt: {openUrlExternally: x => {opened.push(x); return true}, callLater: fn => later.push(fn)},
  }
  ctx.root = ctx
  Object.defineProperty(ctx, 'oauthConnecting', {get: () => ctx.oauthRequested || ctx.oauthProc.running})
  vm.createContext(ctx)
  for (const name of ['cliErrorMessage', 'runAction', 'connectWithToken', 'connectWithApiToken',
    'startOAuth', 'launchOAuthWhenIdle', 'handleOAuthEvent', 'reopenAuthPage', 'cancelOAuth', 'finishOAuth']) {
    vm.runInContext(body(name), ctx)
  }
  return {ctx, opened, signals, later}
}

test('official manual token uses stdin flag and cannot overlap OAuth', () => {
  const {ctx} = fixture()
  ctx.connectWithApiToken(' dummy-api-token ')
  assert.deepEqual(Array.from(ctx.actionProc.command), [ctx.cli, 'login', '--api-token-stdin'])
  assert.equal(ctx.loginToken, 'dummy-api-token')
  assert.equal(ctx.actionQueue.length, 0)
  assert.equal(ctx.authState, 'validating')
  const other = fixture().ctx
  other.oauthProc.running = true
  other.connectWithApiToken('dummy-token')
  assert.equal(other.loginToken, '')
  assert.equal(other.actionProc.running, false)
})

test('OAuth has its own process and waits for in-flight old sync', () => {
  const {ctx} = fixture()
  ctx.syncProc.running = true
  ctx.startOAuth()
  assert.equal(ctx.authState, 'preparing')
  assert.equal(ctx.oauthRequested, true)
  assert.equal(ctx.oauthProc.running, false)
  ctx.syncProc.running = false
  ctx.launchOAuthWhenIdle()
  assert.equal(ctx.oauthProc.running, true)
  assert.deepEqual(Array.from(ctx.oauthProc.command), [ctx.cli, 'login', '--oauth', '--json-events'])
  assert.equal(ctx.actionProc.running, false)
})

test('only a provider authorization URL can launch a browser', () => {
  const {ctx, opened, signals} = fixture()
  ctx.startOAuth()
  ctx.handleOAuthEvent(JSON.stringify({phase: 'waiting', url: 'https://other.invalid/?access_token=dummy'}))
  assert.deepEqual(opened, [])
  assert.equal(ctx.authState, 'failed')
  assert.deepEqual(signals, [15])
  const valid = fixture()
  valid.ctx.startOAuth()
  const url = 'https://ticktick.com/oauth/authorize?client_id=dummy&state=dummy'
  valid.ctx.handleOAuthEvent(JSON.stringify({phase: 'waiting', url}))
  assert.deepEqual(valid.opened, [])
  assert.equal(valid.ctx.launches, 1)
  valid.later.shift()()
  assert.deepEqual(valid.opened, [url])
  assert.equal(valid.ctx.authState, 'waiting')
})

test('cancel before the deferred browser handoff prevents launch', () => {
  const {ctx, opened, later} = fixture()
  ctx.startOAuth()
  ctx.handleOAuthEvent(JSON.stringify({phase: 'waiting', url: 'https://ticktick.com/oauth/authorize?state=dummy'}))
  ctx.cancelOAuth()
  later.shift()()
  assert.deepEqual(opened, [])
})

test('cancel clears pending flow and does not become a failed login', () => {
  const {ctx, signals} = fixture()
  ctx.startOAuth()
  ctx.authUrl = 'https://ticktick.com/oauth/authorize?state=dummy'
  ctx.cancelOAuth()
  ctx.oauthProc.running = false
  ctx.finishOAuth(-1)
  assert.deepEqual(signals, [15])
  assert.equal(ctx.authUrl, '')
  assert.equal(ctx.authError, '')
  assert.equal(ctx.authState, 'idle')
  assert.equal(ctx.reloads, 0)
})

test('success requires connected event and zero exit; failure is idempotent', () => {
  const {ctx} = fixture()
  ctx.startOAuth()
  ctx.handleOAuthEvent(JSON.stringify({phase: 'connected', ok: true}))
  ctx.oauthProc.running = false
  ctx.finishOAuth(0)
  ctx.finishOAuth(0)
  assert.equal(ctx.reloads, 1)
  assert.equal(ctx.authState, 'idle')
  const missing = fixture().ctx
  missing.startOAuth()
  missing.oauthProc.running = false
  missing.finishOAuth(0)
  assert.equal(missing.authState, 'failed')
  assert.equal(missing.reloads, 0)
})

test('failed-start fallback cannot finish a newer queued OAuth attempt', () => {
  const {ctx, later} = fixture()
  const start = source.indexOf('    id: oauthProc')
  const section = source.slice(start, source.indexOf('    stdout: SplitParser', start))
  const match = section.match(/    onRunningChanged: \{([\s\S]*?)\n    \}/)
  assert.ok(match)
  Object.defineProperty(ctx, 'running', {get: () => ctx.oauthProc.running})
  ctx.startOAuth()
  ctx.oauthProc.running = false
  vm.runInContext('(function() {' + match[1] + '\n})()', ctx)
  assert.equal(later.length, 1)
  ctx.syncProc.running = true
  ctx.startOAuth()
  later.shift()()
  assert.equal(ctx.authState, 'preparing')
  assert.equal(ctx.oauthRequested, true)
})

test('raw stderr JSON is humanized, not displayed as CLI instructions', () => {
  const {ctx} = fixture()
  assert.equal(ctx.cliErrorMessage('{"error":"A safe error"}'), 'A safe error')
  assert.equal(ctx.cliErrorMessage(''), '')
  assert.equal(ctx.cliErrorMessage('raw dummy-provider-secret'), 'TickTick could not finish this request. Please try again.')
})
