/* Presentation rules shared by the retained UI and its state regressions. */
(function (root) {
  "use strict";
  const rules = {
    canArm(snapshot, local) {
      return Boolean(snapshot?.ready && snapshot.state === "IDLE" && !snapshot.pending &&
        snapshot.sensor_state === "clear" && local.available && !local.busy && local.valid &&
        (local.dirty || snapshot.validation?.valid !== false));
    },
    sourceLabel(trial) { return String(trial?.acquisition_mode || "unknown").toUpperCase(); },
    observation(trial = {}, context = {}) {
      const active = Boolean(context.active);
      // A STOP acknowledgement alone has no stored duration/count. A retained
      // terminal observation can exist even when persistence is incomplete.
      const terminal = !active && trial.duration_s != null && Boolean(trial.stop_reason);
      const duration = active ? context.elapsed_s ?? trial.duration_s ?? null : trial.duration_s ?? context.elapsed_s ?? null;
      const events = context.event_count ?? trial.event_count ?? null;
      const terminalEvents = terminal ? trial.metadata?.accepted_event_count ?? trial.event_count ?? null : null;
      return {
        duration,
        durationCaption: active ? "Active elapsed time" : terminal ? "Confirmed final duration" :
          duration == null ? "Final duration unconfirmed" : "Retained elapsed · final duration unconfirmed",
        events, terminalEvents,
        eventCaption: active ? "Live device events" : events == null ? "Final device count unconfirmed" :
          terminalEvents != null && terminalEvents === events ? "Confirmed terminal count" : "Last observed device events",
        rows: context.row_count ?? trial.row_count ?? null,
        rowCaption: active ? "Rows written" : context.current ? "Rows reported written" : "Raw rows retained",
        durableRows: context.durable_row_count ?? null,
        stopCaption: context.device_stop_confirmed === true ? "Device STOP confirmed; persistence is separate" : ""
      };
    },
    durationLabel(trial, format) { return trial.duration_s == null ? "Unconfirmed duration" : format(trial.duration_s); }
  };
  if (typeof module !== "undefined" && module.exports) module.exports = rules;
  else root.FlightMillTrialDisplay = rules;
})(typeof window !== "undefined" ? window : globalThis);
