const assert = require('node:assert/strict')
const test = require('node:test')
const Model = require('../Model.js')

test('official connection and capability warnings survive cache parsing', () => {
  const cache = Model.parseCache(JSON.stringify({
    backend: 'openapi', authMethod: 'oauth', warnings: ['Inbox coverage may be incomplete.'],
    capabilities: {recurringCompletion: false, focusPreferences: false},
  }))
  assert.equal(cache.backend, 'openapi')
  assert.equal(cache.authMethod, 'oauth')
  assert.deepEqual(cache.warnings, ['Inbox coverage may be incomplete.'])
  assert.equal(cache.capabilities.focusPreferences, false)
})

test('legacy and malformed cache metadata have safe defaults', () => {
  const empty = Model.parseCache('')
  assert.equal(empty.backend, 'v2')
  assert.deepEqual(empty.warnings, [])
  assert.deepEqual(empty.capabilities, {})
  const malformed = Model.parseCache('{"warnings":"not a list","capabilities":[]}')
  assert.deepEqual(malformed.warnings, [])
  assert.deepEqual(malformed.capabilities, {})
})
