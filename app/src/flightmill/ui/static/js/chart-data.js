/* Pure display sampling helpers, shared with the focused regression check. */
(function(root,factory) {
  const helpers=factory();
  if (typeof module === "object" && module.exports) module.exports=helpers;
  else root.FlightMillChartData=helpers;
})(typeof globalThis !== "undefined" ? globalThis : this,function() {
  "use strict";
  function samplePoints(points,limit=1600) {
    const stride=Math.max(1,Math.ceil(points.length/limit));
    return points.map((point,index) => ({...point,sourceIndex:index}))
      .filter((point,index) => index % stride === 0 || index === points.length-1);
  }
  function isContinuous(previous,next) {
    return Boolean(previous && next &&
      Number(next.event_n)-Number(previous.event_n) === next.sourceIndex-previous.sourceIndex);
  }
  return {samplePoints,isContinuous};
});
