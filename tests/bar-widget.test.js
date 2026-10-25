const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const vm = require('node:vm')

const source = fs.readFileSync(path.join(__dirname, '..', 'BarWidget.qml'), 'utf8')
const button = source.slice(source.indexOf('  WidgetButton {'), source.indexOf('    // Scrolling label.'))

function binding(text, name, indent, property = false) {
  const lines = text.split('\n')
  const prefix = property ? 'readonly property \\w+ ' : ''
  const pattern = new RegExp('^ {' + indent + '}' + prefix + name + ':\\s*(.*)$')
  const start = lines.findIndex(line => pattern.test(line))
  assert.notEqual(start, -1, name)
  const parts = [lines[start].match(pattern)[1]]
  for (let i = start + 1; i < lines.length; i++) {
    if (!new RegExp('^ {' + (indent + 1) + ',}\\S').test(lines[i])) break
    parts.push(lines[i].trim())
  }
  return parts.join(' ')
}

function state(overrides = {}) {
  const ctx = {
    signedIn: false, pomoActive: false, pomoClock: '24:00', panelLabel: '3  2♦',
    vertical: false, marquee: false, marqueeWidth: 150, hasWork: true,
    barLabelMode: 'Count', Style: {bar: {iconSlot: 32}, font: {body: 12}, space: x => x},
    Model: {barLabelDescription: () => 'next task', cycleBarLabel: () => 'Next'},
    ...overrides,
  }
  ctx.root = ctx
  vm.createContext(ctx)
  for (const name of ['icon', 'iconSize', 'iconSlot', 'setupIcon', 'activeIcon', 'activeLabel', 'displayText', 'verticalLines']) {
    if (!new RegExp('readonly property \\w+ ' + name + ':').test(source)) continue
    Object.defineProperty(ctx, name, {get: () => vm.runInContext(binding(source, name, 2, true), ctx)})
  }
  Object.defineProperty(ctx, 'text', {get: () => vm.runInContext(binding(button, 'text', 4), ctx)})
  return {ctx, read: name => vm.runInContext(binding(button, name, 4), ctx)}
}

test('disconnected bar has no setup text in either orientation', () => {
  for (const vertical of [false, true]) {
    const {ctx, read} = state({vertical})
    assert.equal(ctx.displayText, '')
    assert.deepEqual(Array.from(ctx.verticalLines), [])
    assert.equal(read('labelVisible'), false)
    assert.equal(read('hasVisualContent'), true)
    assert.equal(read('dimmed'), true)
  }
})

test('disconnected brand mark occupies the standard icon slot', () => {
  const {ctx, read} = state()
  assert.equal(read('fixedWidth'), ctx.Style.bar.iconSlot)
  assert.match(source, /TickTickIcon\s*\{[\s\S]*?visible: !root\.signedIn/)
  assert.match(source, /TickTickIcon\s*\{[\s\S]*?color: button\.foreground/)
})

test('brand size follows the bar token with an older Omarchy fallback', () => {
  const {ctx} = state()
  assert.equal(ctx.iconSize, 12)
  ctx.Style.bar.iconSize = 18
  assert.equal(ctx.iconSize, 18)
})

test('setup instructions stay in the disconnected tooltip', () => {
  const {read} = state()
  assert.equal(read('tooltipText'), 'TickTick — not connected; click to set up')
})

test('connected counts and focus timer remain unchanged', () => {
  const regular = state({signedIn: true})
  assert.equal(regular.ctx.displayText, regular.ctx.icon + '  3  2♦')
  assert.deepEqual(Array.from(regular.ctx.verticalLines), [regular.ctx.icon, '3  2♦'])
  assert.equal(regular.read('dimmed'), false)
  const focus = state({signedIn: true, pomoActive: true})
  assert.equal(focus.ctx.activeLabel, '24:00')
  assert.equal(focus.ctx.displayText, '  24:00')
})
