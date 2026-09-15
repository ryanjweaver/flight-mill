// Source-level regressions with DOM records and an in-memory API response.
// No browser, native window, real serial port, or visual acceptance is involved.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/app.js'), 'utf8');
const declarationsEnd = source.indexOf('\n  const labStop=');
assert.ok(declarationsEnd > 0, 'Load application declarations without startup or event wiring');

function harness() {
  const nodes = new Map();
  function node(id) {
    if (!nodes.has(id)) {
      let html = '';
      nodes.set(id, {
        value:'', textContent:'', hidden:false, disabled:false, dataset:{}, writes:0,
        parentElement:{classList:{toggle() {}}}, classList:{toggle() {}},
        checkValidity:() => true,
        get innerHTML() { return html; },
        set innerHTML(value) { this.writes++; html = value; }
      });
    }
    return nodes.get(id);
  }
  const requests = [];
  let response = null;
  const context = vm.createContext({
    document:{getElementById:node, querySelector:node, querySelectorAll:() => []},
    window:{matchMedia:() => ({matches:true}), FlightMillTrialDisplay:require('../static/js/trial-display.js')},
    setTimeout:() => 1, clearTimeout() {}, AbortController,
    fetch:async (url, options) => { requests.push(JSON.parse(options.body)); return response; }
  });
  vm.runInContext(source.slice(0, declarationsEnd) + `
    // Other views are outside these control-update tests; keep real receive/action.
    renderMetrics = drawChart = renderWarnings = renderArchive = renderDiagnostics = () => {};
    token = 'unit-test-token'; serviceAvailable = true;
    globalThis.controls = {receive, action};
  })();`, context);
  return {
    node, requests,
    receive:data => context.controls.receive(data),
    discover:async (data, ok = true) => {
      response = {ok, status:ok ? 200 : 500, json:async () => data};
      return context.controls.action('discover');
    }
  };
}

const com3 = {port:'COM3', candidate:true, description:'USB Serial Device'};
const disconnected = ports => ({state:'DISCONNECTED', source:'serial', connected:false, ports});

test('live snapshots preserve the existing suggestions and a manually entered port', () => {
  const ui = harness();
  ui.receive(disconnected([com3]));
  ui.node('serial-port').value = 'COM17';
  const writes = ui.node('serial-ports').writes;
  for (let i = 0; i < 50; i++) {
    ui.receive({...disconnected([{...com3}]), metrics:{elapsed_s:i}});
  }
  assert.equal(ui.node('serial-ports').writes, writes, 'Unchanged live updates must not replace datalist children');
  assert.equal(ui.node('serial-port').value, 'COM17');
});

test('port additions, removal and label changes refresh suggestions once per change', () => {
  const ui = harness();
  ui.receive(disconnected([com3]));
  let writes = ui.node('serial-ports').writes;
  for (const ports of [
    [com3, {port:'COM7', candidate:false, description:'Other adapter'}],
    [{...com3, description:'Renamed device'}],
    []
  ]) {
    ui.receive(disconnected(ports));
    assert.equal(ui.node('serial-ports').writes, ++writes);
  }
  assert.equal(ui.node('serial-ports').innerHTML, '');
});

test('port labels stay escaped and real connection changes still lock the input', () => {
  const ui = harness();
  ui.receive(disconnected([{port:'COM3"', description:'<USB & adapter>', candidate:false}]));
  const html = ui.node('serial-ports').innerHTML;
  assert.match(html, /COM3&quot;/);
  assert.match(html, /&lt;USB &amp; adapter&gt;/);
  ui.receive({...disconnected([com3]), connected:true, state:'IDLE', connection_state:'ready'});
  assert.equal(ui.node('serial-port').disabled, true);
  ui.receive(disconnected([com3]));
  assert.equal(ui.node('serial-port').disabled, false);
});

test('Find serial ports confirms unchanged results without opening a connection', async () => {
  const ui = harness();
  ui.receive(disconnected([com3]));
  const writes = ui.node('serial-ports').writes;
  await ui.discover(disconnected([com3]));
  assert.deepEqual(ui.requests, [{action:'discover'}]);
  assert.equal(ui.node('serial-ports').writes, writes);
  assert.equal(ui.node('toast').hidden, false);
  assert.equal(ui.node('toast-message').textContent, 'Found 1 serial port: COM3.');
});

test('Find serial ports reports multiple ports, empty results and scan failures', async () => {
  const ui = harness();
  ui.receive(disconnected([]));
  await ui.discover(disconnected([com3, {port:'COM7'}]));
  assert.equal(ui.node('toast-message').textContent, 'Found 2 serial ports: COM3, COM7.');
  await ui.discover(disconnected([]));
  assert.match(ui.node('toast-message').textContent, /^No serial ports found\./);
  await ui.discover({detail:'Enumeration failed'}, false);
  assert.equal(ui.node('toast-message').textContent, 'Enumeration failed');
});

test('guided USB reconnect locks editing and presents each phase with cancellation', () => {
  const ui = harness();
  for (const phase of ['waiting_for_unplug', 'waiting_for_replug', 'waiting_for_port']) {
    ui.receive({...disconnected([com3]), connection_state:phase});
    assert.equal(ui.node('serial-port').disabled, true);
    assert.equal(ui.node('connect-after-replug').disabled, true);
    assert.equal(ui.node('connect-button').dataset.action, 'disconnect');
    assert.equal(ui.node('connect-button').disabled, false);
    assert.equal(ui.node('connection-guidance').hidden, false);
    assert.match(ui.node('connection-guidance').textContent,
      phase === 'waiting_for_unplug' ? /Unplug USB/ : phase === 'waiting_for_port' ? /USB is back/ : /USB removal detected/);
  }
  ui.receive(disconnected([com3]));
  assert.equal(ui.node('serial-port').disabled, false);
  assert.equal(ui.node('connect-after-replug').disabled, false);
  assert.equal(ui.node('connection-guidance').hidden, true);
  assert.equal(ui.node('connect-button').dataset.action, 'connect');
  ui.receive({...disconnected([{...com3, port:'COM7'}]), connected:true,
    connection_state:'ready', state:'IDLE', selected_port:'COM7'});
  assert.equal(ui.node('serial-port').value, 'COM7');
  assert.equal(ui.node('connect-after-replug').disabled, true);
});
