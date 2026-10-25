const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const vm = require('node:vm')

const source = fs.readFileSync(process.env.TICKTICK_PANEL_SOURCE || path.join(__dirname, '..', 'Panel.qml'), 'utf8')

test('panel foreground resolves the theme when Qt also supplies a Color type', () => {
  const expression = source.match(/readonly property color fg:\s*([^\n]+)/)[1]
  const context = {Color: {}, Commons: {Color: {popups: {text: '#eeeeee'}}}}
  assert.equal(vm.runInNewContext(expression, context), '#eeeeee')
})

test('every panel palette access selects Commons rather than Qt Color', () => {
  let palette
  palette = new Proxy({}, {get: () => palette})
  const references = new Set(source.match(/\b(?:Commons\.)?Color\.\w+(?:\.\w+)*/g))
  assert.ok(references.size > 0)
  for (const reference of references)
    assert.notEqual(vm.runInNewContext(reference, {Color: {}, Commons: {Color: palette}}), undefined, reference)
})
