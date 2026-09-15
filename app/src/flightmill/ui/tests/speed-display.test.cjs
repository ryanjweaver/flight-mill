// Source-level view regression with DOM records; not visual/operator acceptance.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/js/app.js'), 'utf8');
const end = source.indexOf('\n  const labStop=');
assert.ok(end > 0);

function harness() {
  const nodes = new Map();
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {
      textContent:'', innerHTML:'', hidden:false,
      parentElement:{classList:{toggle() {}}}, classList:{toggle() {}}
    });
    return nodes.get(id);
  }
  const context = vm.createContext({
    document:{getElementById:node, querySelectorAll:() => []},
    window:{matchMedia:() => ({matches:true}), FlightMillTrialDisplay:require('../static/js/trial-display.js')}
  });
  vm.runInContext(source.slice(0, end) + `
    globalThis.render = (data) => {snapshot=data; renderMetrics();};
  })();`, context);
  return {node, render:context.render};
}

function snapshot(overrides={}) {
  return {state:'RECORDING', connected:true, ready:true, connection_state:'ready',
    trial:{id:'test-trial'}, metrics:{event_count:3, row_count:3, elapsed_s:12,
      speed_m_s:null, interval_speed_m_s:0.06283185, speed_after_gap:true,
      last_interval_s:10, speed_stale:true, last_pulse_age_s:0},
    chart:[{event_n:3, time_s:12, speed_m_s:null, interval_speed_m_s:0.06283185,
      speed_after_gap:true, cumulative_distance_m:1.88495559}], ...overrides};
}

test('restart pulse displays unavailable speed and a reason while keeping its event and distance', () => {
  const ui = harness();
  ui.render(snapshot());
  assert.equal(ui.node('speed').innerHTML, '— <small>m/s</small>');
  assert.match(ui.node('speed-detail').textContent, /Awaiting another revolution.*Long interval \(10.0 s\)/);
  assert.match(ui.node('event-table-body').innerHTML, /<td>3<\/td>.*— \(long gap\).*1.8850/);
  assert.doesNotMatch(ui.node('event-table-body').innerHTML, /0.0628/);
  assert.equal(ui.node('chart-empty').hidden, false);
  assert.equal(ui.node('empty-title').textContent, 'Awaiting another revolution.');
});

test('stopping after a gap shows unavailable historical speed without asking for future pulses', () => {
  const ui = harness();
  ui.render(snapshot({state:'COMPLETE'}));
  assert.match(ui.node('speed-detail').textContent, /^Speed unavailable.*Long interval/);
  assert.doesNotMatch(ui.node('speed-detail').textContent, /Awaiting/);
});

test('the next measured interval restores the number and first-ever pulse retains its explanation', () => {
  const ui = harness();
  const data = snapshot();
  data.metrics = {...data.metrics, speed_m_s:0.314159265, speed_after_gap:false, speed_stale:false};
  data.chart = [{event_n:4, time_s:14, speed_m_s:0.314159265, cumulative_distance_m:2.513274123}];
  ui.render(data);
  assert.equal(ui.node('speed').innerHTML, '0.314 <small>m/s</small>');
  assert.match(ui.node('speed-detail').textContent, /^Measured /);
  assert.equal(ui.node('chart-empty').hidden, true);
  data.metrics = {...data.metrics, speed_m_s:null, event_count:1};
  ui.render(data);
  assert.equal(ui.node('speed-detail').textContent, 'Awaiting two accepted pulses');
});
