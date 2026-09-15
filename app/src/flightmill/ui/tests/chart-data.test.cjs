const assert=require("node:assert/strict");
const test=require("node:test");
const {samplePoints,isContinuous}=require("../static/js/chart-data.js");

test("a sampled final short segment remains connected when events are consecutive",() => {
  const points=samplePoints(Array.from({length:1602},(_,index) => ({event_n:index+1})));
  assert.equal(points.at(-1).sourceIndex-points.at(-2).sourceIndex,1);
  assert.equal(isContinuous(points.at(-2),points.at(-1)),true);
});
test("a missing event in the forced final point does not become a connecting segment",() => {
  const rows=Array.from({length:1602},(_,index) => ({event_n:index+1}));
  rows.at(-1).event_n=1603;
  const points=samplePoints(rows);
  assert.equal(isContinuous(points.at(-2),points.at(-1)),false);
});
test("sampling preserves evidence of a missing event inside a skipped interval",() => {
  const rows=Array.from({length:3201},(_,index) => ({event_n:index+1+(index>=2?1:0)}));
  const points=samplePoints(rows);
  assert.equal(isContinuous(points[0],points[1]),false);
  assert.equal(isContinuous(points[1],points[2]),true);
});
test("unthinned accepted rows use their actual sequence adjacency",() => {
  const points=samplePoints([{event_n:1},{event_n:2},{event_n:4}]);
  assert.equal(isContinuous(points[0],points[1]),true);
  assert.equal(isContinuous(points[1],points[2]),false);
});
test("omitting a long-gap speed breaks the line even when display sampling skips that interval",() => {
  const rows=Array.from({length:3202},(_,index) => ({event_n:index+1,speed_m_s:index===2?null:0.4}));
  const visible=rows.filter(point => point.speed_m_s != null);
  const points=samplePoints(visible);
  assert.equal(isContinuous(points[0],points[1]),false);
  assert.equal(isContinuous(points[1],points[2]),true);
  const distancePoints=samplePoints(rows);
  assert.equal(isContinuous(distancePoints[0],distancePoints[1]),true);
});
