const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const vm = require('node:vm')

const source = fs.readFileSync(path.join(__dirname, '..', 'Service.qml'), 'utf8')
function functionBody(name) {
  const match = source.match(new RegExp('  function ' + name + '\\([^)]*\\) \\{[\\s\\S]*?\\n  \\}'))
  assert.ok(match, name)
  return match[0]
}
function handler(name) {
  const action = source.slice(source.indexOf('    id: actionProc'), source.indexOf('  function runAction'))
  const match = action.match(new RegExp('    ' + name + ': \\{([\\s\\S]*?)\\n    \\}'))
  assert.ok(match, name)
  return '(function() {' + match[1] + '\n})()'
}
function fixture() {
  const writes = []
  const files = []
  const ctx = {
    String, Math,
    connecting: false, actionError: '', loginToken: '', actionQueue: [],
    cli: '/plugin/bin/omarchy-ticktick', statePath: '/private/state',
    tokenFile: { setText: text => files.push(text) },
    actionProc: { command: [], running: false },
    write: text => writes.push(text),
  }
  ctx.root = ctx
  Object.defineProperty(ctx, 'command', {get: () => ctx.actionProc.command})
  Object.defineProperty(ctx, 'running', {get: () => ctx.actionProc.running})
  vm.createContext(ctx)
  for (const name of ['connectWithToken', 'runAction', 'drainQueue']) vm.runInContext(functionBody(name), ctx)
  return {ctx, writes, files}
}

test('paste goes through stdin, not a file, argv, or the action queue', () => {
  const {ctx, files} = fixture()
  ctx.connectWithToken('  dummy-cookie  ')
  assert.deepEqual(files, [])
  assert.deepEqual(Array.from(ctx.actionProc.command), [ctx.cli, 'login', '--token-stdin'])
  assert.equal(ctx.loginToken, 'dummy-cookie')
  assert.equal(JSON.stringify(ctx.actionQueue).includes('dummy-cookie'), false)
})

test('queued paste cannot be replaced by a second connect', () => {
  const {ctx, files} = fixture()
  ctx.actionProc.running = true
  ctx.actionProc.command = [ctx.cli, 'sync']
  ctx.connectWithToken('dummy-first')
  ctx.connectWithToken('dummy-second')
  assert.equal(ctx.loginToken, 'dummy-first')
  assert.equal(ctx.actionQueue.length, 1)
  assert.equal(JSON.stringify(ctx.actionQueue).includes('dummy'), false)
  ctx.actionProc.running = false
  ctx.drainQueue()
  assert.deepEqual(Array.from(ctx.actionProc.command), [ctx.cli, 'login', '--token-stdin'])
  assert.deepEqual(files, [])
})

test('started login writes one line and clears the pending token', () => {
  const {ctx, writes} = fixture()
  ctx.connectWithToken('dummy-cookie')
  vm.runInContext(handler('onStarted'), ctx)
  assert.deepEqual(writes, ['dummy-cookie\n'])
  assert.equal(ctx.loginToken, '')
})

test('failed launch or login clears the token and connecting state', () => {
  const {ctx} = fixture()
  ctx.connectWithToken('dummy-cookie')
  ctx.actionProc.running = false
  vm.runInContext(handler('onRunningChanged'), ctx)
  assert.equal(ctx.loginToken, '')
  assert.equal(ctx.connecting, false)
})

test('login proceeds after the preceding non-login process fails to start', () => {
  const {ctx, writes} = fixture()
  ctx.actionProc.running = true
  ctx.actionProc.command = [ctx.cli, 'prior-action']
  ctx.connectWithToken('dummy-cookie')
  ctx.actionProc.running = false
  vm.runInContext(handler('onRunningChanged'), ctx)
  assert.equal(ctx.actionQueue.length, 0)
  assert.deepEqual(Array.from(ctx.actionProc.command), [ctx.cli, 'login', '--token-stdin'])
  vm.runInContext(handler('onStarted'), ctx)
  assert.deepEqual(writes, ['dummy-cookie\n'])
  assert.equal(ctx.loginToken, '')
})

test('an unrelated action completing does not discard a queued login', () => {
  const {ctx} = fixture()
  ctx.actionProc.running = true
  ctx.actionProc.command = [ctx.cli, 'sync']
  ctx.connectWithToken('dummy-cookie')
  ctx.actionProc.running = false
  vm.runInContext(handler('onRunningChanged'), ctx)
  assert.equal(ctx.loginToken, 'dummy-cookie')
  assert.equal(ctx.connecting, true)
})
