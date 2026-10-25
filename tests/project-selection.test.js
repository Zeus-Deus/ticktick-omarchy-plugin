const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const test = require('node:test')
const vm = require('node:vm')
const Model = require('../Model.js')
const service = fs.readFileSync(path.join(__dirname, '..', 'Service.qml'), 'utf8')
const panel = fs.readFileSync(path.join(__dirname, '..', 'Panel.qml'), 'utf8')
function body(source, name) {
  const match = source.match(new RegExp('  function ' + name + '\\([^)]*\\) \\{[\\s\\S]*?\\n  \\}'))
  assert.ok(match, name)
  return match[0]
}
function fixture(backend, project) {
  const ctx = {
    Model, cache: {backend, projects: [{id:'qa-list',name:'QA list'}]},
    settings: {defaultProject: project || ''}, actionError:'', pendingAdds:[],
    actionProc: {running:false,command:[]}, actionQueue:[], cli:'/plugin/bin/omarchy-ticktick',
  }
  ctx.root = ctx
  vm.createContext(ctx)
  for (const name of ['setting','runAction','submitQuickAdd']) vm.runInContext(body(service,name),ctx)
  return ctx
}

test('official quick add requires a selected accessible list before launching', () => {
  const ctx=fixture('openapi','')
  assert.equal(ctx.submitQuickAdd('QA task'),null)
  assert.equal(ctx.actionProc.running,false)
  assert.equal(ctx.pendingAdds.length,0)
  assert.match(ctx.actionError,/choose.*list/i)
  ctx.settings.defaultProject='another-account-list'
  assert.equal(ctx.submitQuickAdd('QA task'),null)
  assert.equal(ctx.actionProc.running,false)
})

test('official quick add passes the chosen project while legacy keeps inbox behavior', () => {
  const official=fixture('openapi','qa-list')
  assert.ok(official.submitQuickAdd('QA task'))
  const args=Array.from(official.actionProc.command)
  assert.equal(args[args.indexOf('--project')+1],'qa-list')
  const legacy=fixture('v2','')
  assert.ok(legacy.submitQuickAdd('QA task'))
  assert.equal(Array.from(legacy.actionProc.command).includes('--project'),false)
})

test('list selection persists without losing existing widget settings', () => {
  const saved=[]
  const ctx={moduleName:'plugin',settings:{id:'plugin',barLabel:'Next'},
    cache:{projects:[{id:'qa-list',name:'QA list'}]},
    hostWidget:{settings:{}},bar:{shell:{updateEntryInline:(id,entry)=>saved.push({id,entry})}}}
  ctx.root=ctx
  vm.createContext(ctx)
  vm.runInContext(body(panel,'chooseProject'),ctx)
  ctx.chooseProject('qa-list')
  assert.equal(ctx.settings.defaultProject,'qa-list')
  assert.equal(ctx.settings.barLabel,'Next')
  assert.equal(ctx.hostWidget.settings.defaultProject,'qa-list')
  assert.equal(saved.length,1)
  ctx.chooseProject('unknown-list')
  assert.equal(saved.length,1)
})
