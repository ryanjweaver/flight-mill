// Execute the production delegated callback with DOM records and in-memory HTTP.
// These are source-level regressions, not browser or operator acceptance checks.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/app.js'), 'utf8');
const declarationsEnd = source.indexOf('\n  const labStop=');
const clickStart = source.indexOf('  document.addEventListener("click",async (event) => {');
const clickEnd = source.indexOf('\n  function markSetupChanged()', clickStart);
assert.ok(declarationsEnd > 0 && clickStart > declarationsEnd && clickEnd > clickStart);

function harness(initial = {}) {
  const nodes = new Map();
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {
      value:'', textContent:'', hidden:false, disabled:false, dataset:{}, innerHTML:'',
      parentElement:{classList:{toggle() {}}}, classList:{toggle() {}},
      checkValidity:() => true
    });
    return nodes.get(id);
  }
  const requests = [];
  let callback;
  let selected = {state:'DISCONNECTED', source:'serial', connected:false,
    selected_port:'COM3', ports:[], ...initial};
  let selectError = null;
  let selectWait = null;
  const context = vm.createContext({
    document:{getElementById:node, querySelector:node, querySelectorAll:() => [],
      addEventListener:(name, listener) => { assert.equal(name, 'click'); callback = listener; }},
    window:{matchMedia:() => ({matches:true}), FlightMillTrialDisplay:require('../static/js/trial-display.js')},
    setTimeout:() => 1, clearTimeout() {}, AbortController,
    fetch:async (url, options) => {
      assert.equal(url, '/api/action');
      assert.equal(options.method, 'POST');
      const body = JSON.parse(options.body);
      requests.push(body);
      if (body.action === 'select_source') {
        if (selectWait) await selectWait;
        if (selectError) return {ok:false, status:409, json:async () => ({detail:selectError})};
        selected = {...selected, source:body.source,
          selected_port:body.source === 'serial' ? body.port || 'COM7' : null};
      } else if (body.action === 'connect') {
        selected = {...selected, connected:true, state:'IDLE', connection_state:'ready'};
      } else if (body.action === 'connect_after_replug') {
        selected = {...selected, connection_state:'waiting_for_unplug'};
      } else if (body.action === 'disconnect') {
        selected = {...selected, connected:false, state:'DISCONNECTED', connection_state:'disconnected'};
      }
      return {ok:true, status:200, json:async () => ({...selected})};
    }
  });
  vm.runInContext(source.slice(0, declarationsEnd) + `
    renderMetrics = drawChart = renderWarnings = renderArchive = renderDiagnostics = () => {};
    token = 'unit-test-token'; serviceAvailable = true;
    globalThis.receiveState = receive;
  ` + source.slice(clickStart, clickEnd) + '\n})();', context);
  context.receiveState(selected);
  node('source-select').value = selected.source;
  node('serial-port').value = selected.selected_port || '';
  node('connect-after-replug').dataset.action = 'connect_after_replug';
  return {
    node, requests,
    click:(id = 'connect-button') => callback({target:{closest:() => node(id)}}),
    failSelection:message => { selectError = message; },
    holdSelection:promise => { selectWait = promise; }
  };
}

test('ordinary Connect applies the chosen source and trimmed port before exactly one connect', async () => {
  const ui = harness({source:'simulation', selected_port:null});
  ui.node('source-select').value = 'serial';
  ui.node('serial-port').value = '  COM3  ';
  await ui.click();
  assert.deepEqual(ui.requests, [
    {action:'select_source', source:'serial', port:'COM3'}, {action:'connect'}
  ]);
});

test('ordinary Connect reuses an unchanged selection and the inert USB default', async () => {
  for (const selected_port of ['COM3', '']) {
    const ui = harness({selected_port});
    await ui.click();
    assert.deepEqual(ui.requests, [{action:'connect'}]);
  }
});

test('ordinary Connect applies an edited COM number before connecting', async () => {
  const ui = harness();
  ui.node('serial-port').value = 'COM7';
  await ui.click();
  assert.deepEqual(ui.requests, [
    {action:'select_source', source:'serial', port:'COM7'}, {action:'connect'}
  ]);
});

test('clearing the port requests automatic selection instead of reusing the previous port', async () => {
  const ui = harness();
  ui.node('serial-port').value = '   ';
  await ui.click();
  assert.deepEqual(ui.requests, [
    {action:'select_source', source:'serial', port:null}, {action:'connect'}
  ]);
  assert.equal(ui.node('serial-port').value, 'COM7');
});

test('guided reconnect applies the selected USB port and dispatches only connect_after_replug', async () => {
  const ui = harness({source:'simulation', selected_port:null});
  ui.node('source-select').value = 'serial';
  ui.node('serial-port').value = 'COM7';
  await ui.click('connect-after-replug');
  assert.deepEqual(ui.requests, [
    {action:'select_source', source:'serial', port:'COM7'}, {action:'connect_after_replug'}
  ]);
  assert.equal(ui.node('connect-button').dataset.action, 'disconnect');
  assert.equal(ui.node('serial-port').disabled, true);
});

test('guided reconnect reuses an unchanged USB selection without ordinary connect', async () => {
  const ui = harness();
  await ui.click('connect-after-replug');
  assert.deepEqual(ui.requests, [{action:'connect_after_replug'}]);
});

test('a failed source selection prevents either connection action', async () => {
  for (const id of ['connect-button', 'connect-after-replug']) {
    const ui = harness();
    ui.node('serial-port').value = 'COM7';
    ui.failSelection('Select a port manually: no unique USB candidate was found');
    await ui.click(id);
    assert.deepEqual(ui.requests, [{action:'select_source', source:'serial', port:'COM7'}]);
    assert.match(ui.node('toast-message').textContent, /no unique USB candidate/);
  }
});

test('a second click while selection is pending cannot dispatch an extra connect', async () => {
  const ui = harness();
  ui.node('serial-port').value = 'COM7';
  let release;
  ui.holdSelection(new Promise(resolve => { release = resolve; }));
  const first = ui.click();
  assert.equal(ui.node('connect-button').disabled, true);
  await ui.click();
  release();
  await first;
  assert.deepEqual(ui.requests, [
    {action:'select_source', source:'serial', port:'COM7'}, {action:'connect'}
  ]);
});

test('cancel guided reconnect sends one disconnect and no new selection or connection', async () => {
  const ui = harness({connection_state:'waiting_for_replug'});
  await ui.click();
  assert.deepEqual(ui.requests, [{action:'disconnect'}]);
  assert.equal(ui.node('connect-button').dataset.action, 'connect');
  assert.equal(ui.node('serial-port').disabled, false);
});

test('guided reconnect rejects Simulation in the source selector without sending any action', async () => {
  const ui = harness({source:'simulation', selected_port:null});
  await ui.click('connect-after-replug');
  assert.deepEqual(ui.requests, []);
  assert.match(ui.node('toast-message').textContent, /Select USB serial device/);
});
