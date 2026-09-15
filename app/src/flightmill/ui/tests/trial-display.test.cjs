const {test} = require('node:test');
const assert = require('node:assert/strict');
const display = require('../static/js/trial-display.js');
const ready = {ready:true,state:'IDLE',sensor_state:'clear',validation:{valid:true}};
const local = {available:true,busy:false,valid:true,dirty:false};
test('ARM follows sensor, authoritative lifecycle, local metadata and collision state', () => {
  assert.equal(display.canArm(ready,local),true);
  for (const change of [{ready:false},{state:'ARMED'},{state:'STOPPING'},{pending:{action:'arm'}},
    {sensor_state:'blocked'},{sensor_state:'unknown'},{validation:{valid:false}}]) {
    assert.equal(display.canArm({...ready,...change},local),false);
  }
  for (const change of [{available:false},{busy:true},{valid:false}]) {
    assert.equal(display.canArm(ready,{...local,...change}),false);
  }
  assert.equal(display.canArm({...ready,validation:{valid:false}},{...local,dirty:true}),true);
});
test('active observations remain live, including zero while armed', () => {
  for (const state of ['ARMED','RECORDING','STOPPING','SAVING']) {
    const shown=display.observation({duration_s:4,event_count:0,row_count:0},
      {active:true,elapsed_s:4,event_count:0,row_count:0});
    assert.equal(shown.duration,4,state);
    assert.equal(shown.durationCaption,'Active elapsed time',state);
    assert.equal(shown.events,0,state);
    assert.equal(shown.eventCaption,'Live device events',state);
    assert.equal(shown.rowCaption,'Rows written',state);
  }
});
test('complete and zero-event terminal observations retain exact values', () => {
  for (const count of [0,17]) {
    const shown=display.observation({duration_s:0,stop_reason:'host_stop',event_count:count,row_count:count});
    assert.equal(shown.duration,0);
    assert.equal(shown.durationCaption,'Confirmed final duration');
    assert.equal(shown.events,count);
    assert.equal(shown.eventCaption,'Confirmed terminal count');
    assert.equal(shown.rowCaption,'Raw rows retained');
  }
});
test('interruption retains elapsed and observed count without inventing a final summary', () => {
  const shown=display.observation({duration_s:null,incomplete:true,event_count:23,row_count:22},
    {current:true,elapsed_s:12.345,event_count:23,row_count:22});
  assert.equal(shown.duration,12.345);
  assert.equal(shown.durationCaption,'Retained elapsed · final duration unconfirmed');
  assert.equal(shown.events,23);
  assert.equal(shown.eventCaption,'Last observed device events');
  assert.equal(shown.rowCaption,'Rows reported written');
});
test('storage failure STOP confirmation alone does not confirm stored duration/count', () => {
  const shown=display.observation({duration_s:null,incomplete:true,stop_reason:'storage_failure'},
    {current:true,elapsed_s:494.953,event_count:9873,row_count:9872,device_stop_confirmed:true,durable_row_count:9872});
  assert.equal(shown.duration,494.953);
  assert.equal(shown.durationCaption,'Retained elapsed · final duration unconfirmed');
  assert.equal(shown.events,9873);
  assert.equal(shown.eventCaption,'Last observed device events');
  assert.equal(shown.rows,9872);
  assert.equal(shown.durableRows,9872);
  assert.equal(shown.stopCaption,'Device STOP confirmed; persistence is separate');
});
test('incomplete persistence may still retain a terminal observation', () => {
  const shown=display.observation({duration_s:7.125,stop_reason:'host_stop',incomplete:true,
    event_count:9,row_count:8,metadata:{accepted_event_count:9}});
  assert.equal(shown.durationCaption,'Confirmed final duration');
  assert.equal(shown.eventCaption,'Confirmed terminal count');
  assert.equal(shown.rows,8);
});
test('a disagreeing terminal count does not relabel or replace the higher observed count', () => {
  const shown=display.observation({duration_s:7,stop_reason:'host_stop',event_count:10,
    metadata:{accepted_event_count:9}});
  assert.equal(shown.events,10);
  assert.equal(shown.eventCaption,'Last observed device events');
  assert.equal(shown.terminalEvents,9);
});
test('recovered unknown values remain unknown independent of current connection', () => {
  const shown=display.observation({duration_s:null,event_count:null,row_count:3,incomplete:true});
  assert.equal(shown.duration,null);
  assert.equal(shown.durationCaption,'Final duration unconfirmed');
  assert.equal(shown.events,null);
  assert.equal(shown.eventCaption,'Final device count unconfirmed');
  assert.equal(shown.rows,3);
  assert.equal(shown.durableRows,null);
});
test('recovered unknown duration and source are never inferred from current selection', () => {
  assert.equal(display.sourceLabel({}), 'UNKNOWN');
  assert.equal(display.sourceLabel({acquisition_mode:'serial'}), 'SERIAL');
  assert.equal(display.sourceLabel({acquisition_mode:'simulation'}), 'SIMULATION');
  assert.equal(display.durationLabel({duration_s:null},String),'Unconfirmed duration');
  assert.equal(display.durationLabel({duration_s:0},String),'0');
});
