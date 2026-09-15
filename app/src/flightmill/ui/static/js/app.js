/* Flight Mill: snapshots are authoritative; visual rounding never changes recordings. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const profileNames = {steady:"Steady flight",ramp:"Rise & fall",flight_rest:"Flight & rest",zero:"No flight",stress:"Stress test",manual:"Manual pulses"};
  const setupForm = $("setup-form");
  let snapshot = null, token = null, socket = null, busy = false, serviceAvailable = false;
  let setupDirty = false, setupHydrated = false, lastSnapshot = 0, lastCount = 0;
  let sourceHydrated = false;
  let serialPortsMarkup = null;
  let selectedTrial = null, lastTrialId = null, archiveSignature = "", diagnosticsSignature = "", warningsSignature = "";
  let chartSeries = "speed", chartWindow = 60, chartPoints = [], chartGeometry = null;
  let toastTimer = null, reconnectTimer = null, previousState = null, activeView = "trial";
  let animate = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  $("animate-pulses").checked = animate;

  const number = (value, digits = 3) => value === null || value === undefined || !Number.isFinite(Number(value)) ? "—" : Number(value).toLocaleString(undefined, {minimumFractionDigits:digits,maximumFractionDigits:digits});
  const elapsed = (seconds) => {
    if (!Number.isFinite(Number(seconds))) return "00:00:00";
    const total = Math.max(0, Math.floor(Number(seconds)));
    return [Math.floor(total / 3600), Math.floor(total / 60) % 60, total % 60].map((n) => String(n).padStart(2,"0")).join(":");
  };
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
  const messageText = (value) => typeof value === "string" ? value : Array.isArray(value) ? value.map((entry) => typeof entry === "string" ? entry : entry.msg || entry.message || "Invalid request").join("; ") : value?.message || "The request could not be completed.";
  const text = (id, value) => { if ($(id).textContent !== String(value)) $(id).textContent = value; };
  const stateName = () => String(snapshot?.state || "DISCONNECTED").toUpperCase();
  const activeTrial = () => ["ARMED","RECORDING","STOPPING","SAVING"].includes(stateName());
  const isConnected = () => Boolean(snapshot?.connected);

  function toast(message, error = false) {
    text("toast-message", message);
    $("toast").classList.toggle("error", error);
    $("toast").hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { $("toast").hidden = true; }, error ? 12000 : 5500);
  }
  function setServiceStatus(available, message = "") {
    serviceAvailable = available;
    $("service-banner").hidden = available;
    text("service-banner", message || "Local service unavailable. The display may be stale; reconnecting automatically.");
    renderControls();
  }
  async function responseJSON(response) {
    let value;
    try { value = await response.json(); }
    catch { throw new Error("The local service returned an unreadable response."); }
    if (!response.ok) {
      const error = new Error(messageText(value.detail || value.message || ("Request failed (" + response.status + ").")));
      error.status = response.status;
      throw error;
    }
    return value;
  }
  async function action(name, payload = {}, successMessage = null) {
    if (busy || !token) return null;
    $("toast").hidden = true;
    busy = true;
    renderControls();
    const request = new AbortController();
    const timeout = setTimeout(() => request.abort(),10000);
    try {
      const response = await fetch("/api/action", {
        method:"POST", credentials:"same-origin", cache:"no-store",
        headers:{"Content-Type":"application/json","X-Flightmill-Token":token},
        body:JSON.stringify({action:name,...payload}), signal:request.signal
      });
      const result = await responseJSON(response);
      serviceAvailable = true;
      receive(result);
      if (name === "discover") {
        const ports = result.ports || [];
        toast(ports.length ? "Found " + ports.length + " serial port" + (ports.length === 1 ? "" : "s") + ": " + ports.map(port => port.port).join(", ") + "." : "No serial ports found. Check the USB cable and power, then scan again.");
      } else if (successMessage) toast(successMessage);
      return result;
    } catch(error) {
      toast(error.name === "AbortError" ? "The request timed out. Its outcome is unconfirmed; check the restored live state before trying again." : error.message || "The action failed.", true);
      if ([401,403].includes(error.status)) { token=null;void initialize(); }
      return null;
    } finally {
      clearTimeout(timeout);
      busy = false;
      renderControls();
    }
  }
  async function initialize() {
    try {
      const session = await responseJSON(await fetch("/api/session",{cache:"no-store",credentials:"same-origin"}));
      token = session.token;
      receive(await responseJSON(await fetch("/api/state",{cache:"no-store",credentials:"same-origin"})));
      connectSocket();
    } catch(error) {
      setServiceStatus(false, "Waiting for the local acquisition service. Reconnecting automatically.");
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(initialize, 2500);
    }
  }
  function connectSocket() {
    if (!token || socket?.readyState === WebSocket.OPEN || socket?.readyState === WebSocket.CONNECTING) return;
    const scheme = location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(scheme + "//" + location.host + "/ws?token=" + encodeURIComponent(token));
    socket.onmessage = (event) => {
      try { receive(JSON.parse(event.data)); }
      catch { setServiceStatus(false, "A display update could not be read. Reconnecting to the live stream."); }
    };
    socket.onclose = () => {
      socket = null;
      clearTimeout(reconnectTimer);
      reconnectTimer = setTimeout(connectSocket, 2000);
    };
    socket.onerror = () => { /* onclose and the state fallback restore the display. */ };
  }
  async function pollFallback() {
    if (!token || Date.now() - lastSnapshot < 1800) return;
    try {
      const result = await responseJSON(await fetch("/api/state",{cache:"no-store",credentials:"same-origin"}));
      receive(result);
      if (!socket) connectSocket();
    } catch(error) {
      setServiceStatus(false, "Connection to the local service was lost. Recording may continue; the display is stale.");
      if ([401,403].includes(error.status)) { token=null;void initialize(); }
    }
  }

  function setupPayload() {
    const result = Object.fromEntries(new FormData(setupForm));
    for (const key of ["trial_number","attempt","arm_radius_cm"]) result[key] = Number(result[key]);
    result.planned_duration_s = result.planned_duration_s === "" ? null : Number(result.planned_duration_s);
    if (!result.output_dir?.trim()) delete result.output_dir;
    return result;
  }
  function updateGeometry() {
    const radius = Number(setupForm.elements.arm_radius_cm.value);
    $("circumference").innerHTML = (radius > 0 ? number(2 * Math.PI * radius / 100,4) : "—") + " <small>m</small>";
  }
  function hydrateSetup() {
    if (!snapshot?.setup) return;
    if (!sourceHydrated) {
      $("source-select").value = snapshot.source || "serial";
      $("serial-port").value = snapshot.selected_port || "";
      sourceHydrated = true;
    }
    if (setupDirty && !activeTrial()) return;
    for (const [key,value] of Object.entries(snapshot.setup)) {
      const field = setupForm.elements.namedItem(key);
      if (field && value !== undefined && value !== null && field.value !== String(value)) field.value = value;
    }
    if (snapshot.output_dir) setupForm.elements.output_dir.placeholder = snapshot.output_dir;
    setupHydrated = true;
    updateGeometry();
  }
  function renderValidation() {
    if (setupDirty && !activeTrial()) return;
    const validation = snapshot?.validation;
    const filename = snapshot?.filename || validation?.filename;
    if (filename) text("filename-preview", filename);
    const box = $("filename-preview").parentElement;
    box.classList.toggle("valid", Boolean(validation?.valid));
    box.classList.toggle("invalid", Boolean(validation && !validation.valid && validation.errors?.length));
    if (validation?.errors?.length) {
      text("validation-status", validation.errors.map(messageText).join(" · "));
    } else if (validation?.valid) {
      const changed = Object.entries(validation.sanitized || {}).map(([key,value]) => key + " → " + value);
      text("validation-status", changed.length ? "Filename adjusted: " + changed.join(", ") : "Setup valid · one accepted pulse per revolution.");
    }
  }
  function renderControls() {
    const state = stateName();
    const connected = isConnected();
    const recording = state === "RECORDING";
    const armed = state === "ARMED";
    const terminal = ["COMPLETE","ERROR"].includes(state) || (state === "DISCONNECTED" && snapshot?.trial);
    const idle = state === "IDLE" || (!connected && !activeTrial() && !terminal);
    const waiting = busy || Boolean(snapshot?.pending);
    const unavailable = !serviceAvailable || !token;
    $("setup-fields").disabled = activeTrial() || busy;
    $("trial-identity-fields").disabled = activeTrial() || busy;
    text("setup-lock", activeTrial() ? "Locked for trial" : "Editable");
    $("validate-button").disabled = !idle || waiting || unavailable;
    $("arm-button").disabled = !window.FlightMillTrialDisplay.canArm(snapshot, {
      available:!unavailable, busy:waiting, valid:setupForm.checkValidity(), dirty:setupDirty
    });
    $("arm-button").hidden = terminal;
    $("start-button").disabled = !armed || waiting || unavailable;
    $("start-button").hidden = terminal;
    $("stop-button").disabled = !recording || waiting || unavailable;
    $("stop-button").hidden = terminal;
    if ($("lab-stop-button")) $("lab-stop-button").disabled = !recording || waiting || unavailable;
    $("disarm-button").hidden = !armed;
    $("disarm-button").disabled = waiting || unavailable;
    $("new-trial-button").hidden = !terminal;
    $("new-trial-button").disabled = waiting || unavailable;
    const replugWaiting = ["waiting_for_unplug", "waiting_for_replug", "waiting_for_port"].includes(snapshot?.connection_state);
    $("connect-button").dataset.action = connected || replugWaiting ? "disconnect" : "connect";
    $("connect-button").innerHTML = replugWaiting ? 'Cancel USB reconnect' : connected ? 'Disconnect <span aria-hidden="true">↗</span>' : 'Connect selected source <span aria-hidden="true">↗</span>';
    $("connect-button").disabled = waiting || unavailable || activeTrial();
    const opening = snapshot?.connection_state === "opening";
    $("connect-button").disabled ||= opening;
    for (const id of ["source-select", "serial-port", "discover-ports", "connect-after-replug"]) $(id).disabled = connected || activeTrial() || waiting || opening || replugWaiting || unavailable;
    $("empty-connect").dataset.action = replugWaiting ? "disconnect" : "connect";
    text("empty-connect", replugWaiting ? "Cancel USB reconnect" : "Connect selected source →");
    $("connection-guidance").hidden = !replugWaiting;
    text("connection-guidance", snapshot?.connection_state === "waiting_for_unplug" ? "Unplug USB from the idle prototype, wait five seconds, then plug it back in. The app will connect automatically. Cancel or complete this step within 60 seconds." : snapshot?.connection_state === "waiting_for_port" ? "USB is back. Waiting briefly for Windows to make its port available; no Connect click is needed." : "USB removal detected. Plug the same prototype back in now; no Connect click is needed.");
    const serial = (snapshot?.source || "serial") === "serial";
    if (connected && serial && snapshot.selected_port && $("serial-port").value !== snapshot.selected_port) {
      $("serial-port").value = snapshot.selected_port;
    }
    text("connection-detail", "Selected: " + (snapshot?.source || "serial") + " · " + (snapshot?.connection_state || "disconnected") + (snapshot?.selected_port ? " · " + snapshot.selected_port : "") + (snapshot?.device?.device_id ? " · " + snapshot.device.device_id + " · Firmware " + snapshot.device.firmware_version + " · Protocol " + snapshot.device.protocol_version : ""));
    text("output-directory", "Current output folder: " + (snapshot?.output_dir || "Awaiting service"));
    $("sensor-warning").hidden = !connected || snapshot.sensor_state === "clear";
    text("sensor-warning", snapshot?.sensor_state === "blocked" ? "Beam blocked — clear the sensor before arming." : "Sensor state unconfirmed — wait for a current device status before arming.");
    const config = snapshot?.configuration || {};
    text("configuration-detail", "Requested: " + number(config.requested_interval_us / 1000, 0) + " ms · Confirmed: " + (config.confirmed_interval_us == null ? "pending ARM echo" : number(config.confirmed_interval_us / 1000, 0) + " ms") + " · Saved radius: " + number(config.radius_cm, 2) + " cm (host-defined)." + (setupDirty && !activeTrial() ? " Form edits are pending validation or ARM." : ""));
    const portsMarkup = (snapshot?.ports || []).map(p => '<option value="' + escape(p.port) + '">' + escape((p.candidate ? "USB candidate · " : "Manual selection · ") + (p.description || "Unknown")) + '</option>').join("");
    // Replacing datalist children during live updates dismisses its native popup.
    if (portsMarkup !== serialPortsMarkup) {
      $("serial-ports").innerHTML = portsMarkup;
      serialPortsMarkup = portsMarkup;
    }
    document.querySelector(".header-status .simulation-badge").textContent = serial ? "USB SERIAL" : "◇ SIMULATION";
    $("empty-connect").disabled = waiting || unavailable;
    $("sensor-check").disabled = !connected || state !== "IDLE" || waiting || unavailable;
    $("note-button").disabled = !["ARMED","RECORDING"].includes(state) || waiting || unavailable;
    $("note-input").disabled = !["ARMED","RECORDING"].includes(state) || unavailable;
    $("manual-pulse").disabled = !connected || !["IDLE","ARMED","RECORDING"].includes(state) || waiting || unavailable;
    $("toggle-sensor").disabled = !connected || waiting || unavailable;
    text("toggle-sensor", snapshot?.sensor_blocked ? "Clear sensor" : "Block sensor");
    $("short-button").disabled = !connected || !["ARMED","RECORDING"].includes(state) || waiting || unavailable;
    $("long-button").disabled = !connected || !["ARMED","RECORDING"].includes(state) || waiting || unavailable;
    $("apply-simulation").disabled = !connected || waiting || unavailable || ["STOPPING","SAVING"].includes(state);
    $$("[data-fault]").forEach((button) => {
      button.disabled = !connected || waiting || unavailable;
      if (snapshot?.simulation?.faults?.length) button.disabled ||= !snapshot.simulation.faults.includes(button.dataset.fault);
    });
    if ($("choose-output-dir")) $("choose-output-dir").disabled = activeTrial() || busy;
    $("open-lab").disabled = serial;
    const descriptions = {
      IDLE:["Ready for a flight","Check your setup, then arm the trial.","○"],
      ARMED:["Trial armed","Files reserved. Start when you are ready.","◉"],
      RECORDING:["Recording in progress","Accepted events are being written to disk.","●"],
      STOPPING:["Stopping recording","Waiting for the device summary and file finalization.","◌"],
      SAVING:["Saving trial","Finalizing and verifying the recorded files.","◌"],
      COMPLETE:["Trial saved","Your recording is ready in the trial archive.","✓"],
      ERROR:["Trial needs attention","Inspect the findings and retained files in the archive.","!"],
      DISCONNECTED:["Instrument offline","Select a source and connect to get started.","○"]
    };
    const info = descriptions[state] || [state,"Waiting for instrument state.","○"];
    if (terminal && snapshot?.trial?.incomplete) {
      info[0] = "Incomplete trial preserved";
      info[1] = "Review the stop reason and retained files in the archive.";
      info[2] = "!";
    }
    text("trial-state", info[0]);
    text("state-description", unavailable ? "Display disconnected · acquisition state is unconfirmed." : waiting ? "Applying " + (snapshot?.pending?.action || "your request") + "…" : info[1]);
    text("state-glyph",info[2]);
    const stateSummary = $("state-glyph").parentElement;
    stateSummary.className = "state-summary " + (recording ? "recording" : terminal ? snapshot?.trial?.incomplete ? "error" : "complete" : "");
    const step = terminal ? 3 : recording || state === "STOPPING" ? 2 : armed ? 1 : connected ? 1 : 0;
    ["connect","arm","record","save"].forEach((name,i) => {
      const node = $("step-" + name);
      node.classList.toggle("done", i < step || (i === 3 && terminal));
      node.classList.toggle("current", i === step);
    });
  }
  function receive(data) {
    if (!data || typeof data !== "object" || !("state" in data)) return;
    snapshot = data;
    if (data.trial?.id && data.trial.id !== lastTrialId) { selectedTrial=data.trial.id;lastTrialId=data.trial.id; }
    lastSnapshot = Date.now();
    setServiceStatus(true);
    hydrateSetup();
    renderValidation();
    renderMetrics();
    renderWarnings();
    renderControls();
    drawChart();
    renderArchive();
    renderDiagnostics();
    const state = stateName();
    if (previousState && previousState !== state && ["COMPLETE","ERROR"].includes(state)) {
      toast(data.trial?.incomplete ? "Trial preserved as incomplete. See the archive for details." : "Trial saved. Your files are ready in the archive.", Boolean(data.trial?.incomplete));
    }
    previousState = state;
  }
  function renderMetrics() {
    if (!snapshot) return;
    const metrics = snapshot?.metrics || {};
    const state = stateName();
    const hasTrial = Boolean(snapshot?.trial) || ["ARMED","RECORDING","STOPPING","COMPLETE"].includes(state);
    const shown = trialObservation(allTrials().find((trial) => trial.id === snapshot.trial?.id) || snapshot.trial || {});
    text("elapsed",shown.duration == null ? "—" : elapsed(shown.duration));
    const historical = Boolean(snapshot.trial) && !activeTrial();
    text("elapsed-detail",hasTrial ? shown.durationCaption : "Waiting to start");
    text("revolutions",hasTrial ? number(shown.events,0) : "—");
    text("event-count-label",hasTrial ? shown.eventCaption : "Revolutions");
    text("row-count",hasTrial ? number(shown.rows,0) + " " + shown.rowCaption.toLowerCase() + (metrics.dropped_events ? " · " + metrics.dropped_events + " device drops" : "") : "No accepted events");
    $("distance").innerHTML = (hasTrial ? number(metrics.distance_m ?? 0) : "—") + " <small>m</small>";
    $("speed").innerHTML = number(metrics.speed_m_s) + " <small>m/s</small>";
    $("speed").parentElement.classList.toggle("stale",Boolean(metrics.speed_stale));
    text("speed-detail",metrics.speed_after_gap
      ? (historical ? "Speed unavailable" : "Awaiting another revolution") + " · Long interval (" + number(metrics.last_interval_s,1) + " s)"
      : metrics.speed_m_s == null ? historical ? "No interval speed recorded" : "Awaiting two accepted pulses" : historical ? "Final interval · historical value" : metrics.speed_stale ? "No recent pulse · historical value" : "Measured " + number(metrics.last_pulse_age_s,1) + " sec ago");
    $("recording-dot").classList.toggle("active",state === "RECORDING");
    $("connection-status").classList.toggle("connected",isConnected());
    $("connection-status").innerHTML = "<i></i>" + escape(snapshot?.connection_state || "Offline");
    text("sensor-state",!isConnected() ? "Offline" : snapshot.sensor_state === "unknown" ? "Unconfirmed" : snapshot.sensor_blocked ? "Blocked" : "Clear");
    $("sensor-state").className = "status-pill " + (isConnected() && snapshot.sensor_state !== "unknown" ? snapshot.sensor_blocked ? "blocked" : "clear" : "");
    text("pulse-age",!isConnected() ? "Waiting for a sensor connection" : metrics.last_pulse_age_s == null ? "No accepted pulse yet" : historical ? "Final pulse · trial time " + number(metrics.elapsed_s-metrics.last_pulse_age_s,1) + " sec" : "Last accepted pulse · " + number(metrics.last_pulse_age_s,1) + " sec ago");
    text("header-profile",profileNames[snapshot.simulation?.profile] || snapshot.simulation?.profile || "Steady flight");
    const notes = snapshot.trial?.notes || [];
    const earlierHistory = (snapshot.chart || []).length >= 7200 && Number(snapshot.chart[0]?.event_n) > 1;
    const checkpoint = snapshot.last_checkpoint_at ? " · Last checkpoint at " + number(snapshot.latest_checkpoint_s,1) + " s" : "";
    const durable = hasTrial && shown.durableRows != null ? "Reported durable rows: " + number(shown.durableRows,0) + checkpoint : "Flight-mill measurements · gaps are unobserved intervals";
    text("durability-status",durable + (shown.stopCaption ? " · " + shown.stopCaption : "") + (shown.terminalEvents != null && shown.terminalEvents !== shown.events ? " · Confirmed terminal count: " + number(shown.terminalEvents,0) : "") + (earlierHistory ? " · Earlier events are retained in the saved CSV." : ""));
    text("note-status",notes.length ? notes.length + (notes.length === 1 ? " timestamped note saved." : " timestamped notes saved.") : activeTrial() ? "Add an observation at the current trial time." : "Available once a trial is armed.");
    if (animate && metrics.event_count > lastCount && state === "RECORDING") {
      const schematic = $("pulse-schematic");
      schematic.classList.remove("pulse");
      void schematic.offsetWidth;
      schematic.classList.add("pulse");
    }
    lastCount = metrics.event_count || 0;
    const points = snapshot.chart || [];
    const availablePoints = points.filter((point) => chartSeries === "distance" ? point.cumulative_distance_m != null : point.speed_m_s != null);
    $("chart-empty").hidden = availablePoints.length > 0;
    $("empty-connect").hidden = isConnected() || Boolean(snapshot.trial);
    let title = "A space for your next flight.", description = "Select a source and connect to your instrument.";
    if (state === "RECORDING") {
      title = metrics.event_count > 0 ? "The first revolution is in." : "Listening for a little lift.";
      description = metrics.event_count > 0 ? "The next accepted pulse gives us an interval speed." : "Trial time is advancing. No accepted pulses have arrived yet.";
      if (metrics.speed_after_gap) {
        title = "Awaiting another revolution.";
        description = "The last interval crossed a long gap. Every accepted pass still counts and saves.";
      }
    } else if (state === "ARMED") {
      title = "Ready for takeoff.";
      description = "Your files are reserved. Start recording to begin the trial.";
    } else if (isConnected() && state === "IDLE") {
      title = "Your instrument is ready.";
      description = "Fine-tune the setup, check the sensor, and arm your trial.";
    } else if (snapshot.trial) {
      title = snapshot.trial.incomplete ? "Your trial record is preserved." : metrics.event_count ? "No interval measurement to plot." : "A quiet flight is still a finding.";
      description = snapshot.trial.incomplete ? "An interruption ended this trial. Review its files and diagnostics." : "The trial record retains its duration and any accepted events.";
    }
    text("empty-title",title); text("empty-message",description);
    $("chart-empty").classList.toggle("has-events",state === "RECORDING");
    if (!$("event-data").hidden) renderEventTable();
  }
  function renderWarnings() {
    const warnings = snapshot?.warnings || [];
    const signature = JSON.stringify(warnings);
    if (signature === warningsSignature) return;
    warningsSignature = signature;
    $("warnings").innerHTML = warnings.slice(-4).map((warning) => '<div class="warning-item"><span aria-hidden="true">!</span><span>' + escape(warning.code === "storage_failure" ? "Storage is unusable; acquisition is incomplete. Device stop is unconfirmed unless reported in Diagnostics." : warning.message || warning) + '</span></div>').join("");
    $("diagnostic-warnings").innerHTML = warnings.length ? warnings.map((warning) => '<p><strong>' + escape(warning.code || "Finding") + '</strong><br>' + escape(warning.message || warning) + '</p>').join("") : "<p>No integrity findings reported.</p>";
  }

  function drawChart() {
    const canvas = $("flight-chart"), container = $("chart-stage"), context = canvas.getContext("2d");
    if (!context || container.clientWidth < 1 || container.clientHeight < 1) return;
    const width = container.clientWidth - 18, height = container.clientHeight;
    const scale = Math.min(window.devicePixelRatio || 1,3);
    canvas.width = Math.round(width * scale); canvas.height = Math.round(height * scale);
    canvas.style.width = width + "px"; canvas.style.height = height + "px";
    context.scale(scale,scale);
    const bounds = {left:52,right:width-19,top:22,bottom:height-35};
    const all = snapshot?.chart || [], now = Number(snapshot?.metrics?.elapsed_s || 0);
    const maxTime = Math.max(10,now,...all.slice(-1).map((p) => Number(p.time_s)));
    const retainedStart = all.length >= 7200 && Number(all[0]?.event_n) > 1 ? Number(all[0].time_s) : 0;
    const minTime = chartWindow > 0 ? Math.max(retainedStart,maxTime-chartWindow) : retainedStart;
    const horizon = Math.max(maxTime,chartWindow > 0 ? Math.min(chartWindow,60) : 10);
    const field = chartSeries === "speed" ? "speed_m_s" : "cumulative_distance_m";
    const visible = all.filter((point) => Number(point.time_s) >= minTime && point[field] != null && Number.isFinite(Number(point[field])));
    const rawMax = Math.max(0.05,...visible.map((point) => Number(point[field])));
    const magnitude = Math.pow(10,Math.floor(Math.log10(rawMax)));
    const ymax = Math.max(0.1,Math.ceil(rawMax / magnitude / 0.5) * magnitude * 0.5) * 1.12;
    const x = (value) => bounds.left + (Number(value)-minTime)/(horizon-minTime) * (bounds.right-bounds.left);
    const y = (value) => bounds.bottom - Number(value)/ymax * (bounds.bottom-bounds.top);
    context.font = '10px "Segoe UI",sans-serif'; context.lineWidth = 1; context.textBaseline = "middle";
    for (let i=0;i<=4;i++) {
      const position = bounds.bottom - (bounds.bottom-bounds.top)*i/4;
      context.strokeStyle = i === 0 ? "#35424d" : "#293540";
      context.beginPath(); context.moveTo(bounds.left,position+.5); context.lineTo(bounds.right,position+.5); context.stroke();
      context.fillStyle = "#788997"; context.textAlign = "right";
      context.fillText((ymax*i/4).toFixed(ymax < 2 ? 2 : ymax < 10 ? 1 : 0),bounds.left-11,position);
    }
    for (let i=0;i<=5;i++) {
      const value = minTime+(horizon-minTime)*i/5, position=x(value);
      context.strokeStyle="#25313c"; context.setLineDash([2,5]);
      context.beginPath(); context.moveTo(position,bounds.top); context.lineTo(position,bounds.bottom); context.stroke();
      context.setLineDash([]); context.fillStyle="#788997"; context.textAlign="center";
      context.fillText(value >= 120 ? Math.floor(value/60) + ":" + String(Math.floor(value%60)).padStart(2,"0") : Math.floor(value)+"s",position,bounds.bottom+18);
    }
    context.textAlign="left";context.fillStyle="#8d9ba6";context.font='9px "Segoe UI",sans-serif';
    context.fillText(chartSeries === "speed" ? "m/s" : "m",bounds.left-29,9);
    context.textAlign="right"; context.fillText("ELAPSED TRIAL TIME",bounds.right,9);
    chartPoints = visible.map((point) => ({...point,x:x(point.time_s),y:y(point[field])}));
    chartGeometry = {bounds,width,height};
    // Thin display points only. Every accepted row remains in the backend files.
    const stride = Math.max(1,Math.ceil(chartPoints.length/1600));
    const displayed = window.FlightMillChartData.samplePoints(chartPoints);
    context.save(); context.beginPath(); context.rect(bounds.left-2,bounds.top-4,bounds.right-bounds.left+4,bounds.bottom-bounds.top+7); context.clip();
    context.strokeStyle="#f6c453"; context.lineWidth=1.8; context.lineJoin="round";
    let previous = null, previousInterval = null;
    for (const point of displayed) {
      const delta = previous ? point.time_s-previous.time_s : null;
      const consecutive = window.FlightMillChartData.isContinuous(previous,point);
      const observed = previous && consecutive && delta <= Math.max(2*stride,2*(previousInterval || 1));
      if (observed) {
        context.beginPath();context.moveTo(previous.x,previous.y);context.lineTo(point.x,point.y);context.stroke();
      }
      if (displayed.length < 140 || point === displayed[displayed.length-1]) {
        context.fillStyle="#f6c453"; context.beginPath(); context.arc(point.x,point.y,displayed.length < 30 ? 2.6 : 1.8,0,2*Math.PI);context.fill();
      }
      if (delta != null && delta > 0) previousInterval=delta;
      previous=point;
    }
    context.restore();
    canvas.setAttribute("aria-label",(chartSeries === "speed" ? "Interval speed" : "Cumulative distance") + " plotted at " + visible.length + " accepted event measurements. Latest value " + (visible.length ? number(visible[visible.length-1][field]) : "unavailable") + (chartSeries === "speed" ? " meters per second." : " meters.") + " Use View event data for a table.");
  }
  function renderEventTable() {
    $("event-table-body").innerHTML = (snapshot?.chart || []).slice(-20).reverse().map((point) => "<tr><td>" + escape(point.event_n) + "</td><td>" + number(point.time_s,3) + "</td><td>" + (point.speed_after_gap ? "— (long gap)" : number(point.speed_m_s,4)) + "</td><td>" + number(point.cumulative_distance_m,4) + "</td></tr>").join("") || '<tr><td colspan="4">No accepted event measurements yet.</td></tr>';
  }
  function allTrials() {
    const trials = [...(snapshot?.recent_trials || [])];
    if (snapshot?.trial?.id) {
      const index=trials.findIndex((trial) => trial.id === snapshot.trial.id);
      if (index >= 0) trials[index]={...trials[index],...snapshot.trial}; else trials.unshift(snapshot.trial);
    }
    return trials;
  }
  function trialObservation(trial) {
    const current = Boolean(trial.id && trial.id === snapshot?.trial?.id);
    return window.FlightMillTrialDisplay.observation(trial,current ? {
      current, active:activeTrial(), ...snapshot.metrics,
      device_stop_confirmed:snapshot.device_stop_confirmed,
      durable_row_count:snapshot.durable_row_count
    } : {});
  }
  function renderArchive(force = false) {
    const trials=allTrials(), signature=JSON.stringify(trials);
    text("archive-count",trials.length);text("review-count",trials.length + (trials.length === 1 ? " trial" : " trials"));
    if (!force && signature === archiveSignature) return;
    archiveSignature=signature;
    const focusedTrial = document.activeElement?.dataset?.trialId;
    if (!selectedTrial && trials.length) selectedTrial=trials[0].id;
    if (selectedTrial && !trials.some((trial) => trial.id === selectedTrial)) selectedTrial=trials[0]?.id || null;
    $("trial-list").innerHTML=trials.length ? trials.map((trial) => {
      const active = trial.id === snapshot?.trial?.id && activeTrial();
      const shown = trialObservation(trial);
      return '<button class="trial-list-item' + (trial.id === selectedTrial ? " selected" : "") + '" data-trial-id="' + escape(trial.id) + '"><strong>' + escape(trial.filename || trial.id) + '</strong><div class="trial-list-bottom"><span>' + escape((shown.duration == null ? "Unconfirmed duration" : elapsed(shown.duration) + (active ? " elapsed" : shown.durationCaption === "Confirmed final duration" ? " final" : " observed"))) + '</span><span class="' + (!active && trial.incomplete ? "incomplete" : "saved") + '">' + (active ? "● Active" : trial.incomplete ? "⚠ Incomplete" : "✓ Saved") + '</span></div></button>';
    }).join("") : '<p class="archive-list-empty">No recordings yet.<br>Your next flight starts in the workspace.</p>';
    if (focusedTrial) document.querySelector('[data-trial-id="' + CSS.escape(focusedTrial) + '"]')?.focus({preventScroll:true});
    const trial=trials.find((entry) => entry.id === selectedTrial);
    if (!trial) return;
    const active=trial.id === snapshot?.trial?.id && activeTrial();
    const files=Array.isArray(trial.files) ? trial.files : Object.entries(trial.files || {}).map(([kind,name]) => ({kind,name}));
    const bundle="/api/trials/" + encodeURIComponent(trial.id) + "/bundle";
    const notes=trial.notes || [];
    const shown=trialObservation(trial);
    const detail='<div class="trial-detail-content"><div class="trial-detail-heading"><div><p class="eyebrow">' + escape(window.FlightMillTrialDisplay.sourceLabel(trial)) + ' FLIGHT RECORD</p><h2>' + escape(trial.filename || trial.id) + '</h2><p class="field-help">Stop reason: ' + escape(trial.stop_reason || (active ? "Trial is active" : "Not available")) + '</p></div><span class="detail-badge' + (!active && trial.incomplete ? " incomplete" : "") + '">' + (active ? "Active trial" : trial.incomplete ? "Incomplete" : "Saved") + '</span></div><div class="review-metrics"><div><small>' + escape(shown.durationCaption) + '</small><strong>' + (shown.duration == null ? 'Unconfirmed' : elapsed(shown.duration)) + '</strong></div><div><small>' + escape(shown.eventCaption) + '</small><strong>' + number(shown.events,0) + '</strong></div><div><small>' + escape(shown.rowCaption) + '</small><strong>' + number(shown.rows,0) + '</strong></div></div><div class="inline-title"><h3>Recording bundle</h3>' + (!active && files.length ? '<a class="button primary small" href="' + bundle + '" download>Export ZIP <span aria-hidden="true">↓</span></a>' : '<span class="field-help">Files finalize when the trial stops.</span>') + '</div><div class="file-list">' + files.map((file) => '<div class="file-row"><div><span class="file-icon">' + escape(String(file.kind).slice(0,4).toUpperCase()) + '</span><strong>' + escape(file.name || file.kind) + '</strong></div>' + (!active ? '<a href="/api/trials/' + encodeURIComponent(trial.id) + '/files/' + encodeURIComponent(file.kind) + '" download aria-label="Download ' + escape(file.name || file.kind) + '">Download ↓</a>' : '<span class="field-help">Recording</span>') + '</div>').join("") + '</div><p class="review-notice">' + (shown.terminalEvents != null && shown.terminalEvents !== shown.events ? 'Confirmed terminal count: ' + number(shown.terminalEvents,0) + '. ' : '') + 'Keep the raw CSV with its metadata, journal and source provenance. Simulation manifests identify synthetic data. The raw CSV alone does not identify acquisition mode.' + (!active && trial.incomplete ? " This bundle is incomplete; review its journal and protocol log before analysis." : "") + '</p>' + (notes.length ? '<h3 style="margin-top:24px">Field notes</h3><ul class="trial-note-list">' + notes.map((note) => '<li><time>' + elapsed(note.elapsed_s) + '</time>' + escape(note.text || note) + '</li>').join("") + '</ul>' : "") + '</div>';
    $("trial-detail").innerHTML=detail;
  }
  function renderDiagnostics() {
    const device=snapshot?.device || {}, metrics=snapshot?.metrics || {};
    const fields={"Application":snapshot?.application_version,"Source":snapshot?.source,"Device ID":device.device_id,"Firmware":device.firmware_version,"Protocol":device.protocol_version,"Boot ID":device.boot_id,"State":stateName(),"Sensor":snapshot?.sensor_state,"Device drops":metrics.dropped_events ?? 0,"Requested interval":snapshot?.configuration?.requested_interval_us,"Confirmed interval":snapshot?.configuration?.confirmed_interval_us ?? "Pending ARM echo","File status":snapshot?.save_status || "none","Output folder":snapshot?.output_dir};
    $("device-details").innerHTML=Object.entries(fields).map(([key,value]) => "<dt>" + escape(key) + "</dt><dd>" + escape(value ?? "—") + "</dd>").join("");
    const logs=snapshot?.diagnostics || [], signature=JSON.stringify(logs);
    if (signature === diagnosticsSignature) return;
    diagnosticsSignature=signature;
    text("log-count",logs.length + " entries");
    $("log-entries").innerHTML=logs.length ? logs.slice().reverse().map((entry) => {
      const date = new Date(entry.time), time = Number.isNaN(date.getTime()) ? String(entry.time || "") : date.toLocaleTimeString([], {hour12:false});
      const level=String(entry.level || "info").toLowerCase();
      return '<div class="log-row ' + escape(level) + '"><time>' + escape(time) + '</time><span class="log-level">' + escape(level) + '</span><p>' + escape(entry.message || "") + '</p></div>';
    }).join("") : '<p class="archive-list-empty">The acquisition log will appear when you connect.</p>';
  }
  function showView(name) {
    if (!["trial","review","diagnostics"].includes(name)) return;
    activeView=name;
    $$(".view").forEach((view) => { view.hidden=view.id !== "view-" + name; });
    $$(".nav-tab").forEach((button) => {
      button.classList.toggle("active",button.dataset.view === name);
      if (button.dataset.view === name) button.setAttribute("aria-current","page"); else button.removeAttribute("aria-current");
    });
    history.replaceState(null,"","#"+name);
    if (name === "trial") { renderMetrics(); drawChart(); }
    if (name === "review") renderArchive(true);
    window.scrollTo({top:0,behavior:"instant"});
  }
  function syncSimulationForm() {
    const simulation=snapshot?.simulation || {};
    const form=$("simulation-form");
    if (simulation.profile && form.elements.profile) form.elements.profile.value=simulation.profile;
    if (simulation.seed != null) form.elements.seed.value=simulation.seed;
    if (simulation.rate_hz != null) form.elements.rate_hz.value=simulation.rate_hz;
  }
  function installDesktopControls() {
    if (!window.pywebview?.api || $("choose-output-dir")) return;
    const controls=document.createElement("div");controls.className="desktop-folder-controls";
    const choose=document.createElement("button");choose.type="button";choose.id="choose-output-dir";choose.className="button secondary small";choose.textContent="Browse folder…";
    choose.onclick=async () => {
      try {
        const path=await window.pywebview.api.choose_output_dir();
        if (path) { setupForm.elements.output_dir.value=path;setupForm.dispatchEvent(new Event("input",{bubbles:true})); }
      } catch(error) { toast(error.message || "Folder picker unavailable.",true); }
    };
    const open=document.createElement("button");open.type="button";open.className="button secondary small";open.textContent="Open folder ↗";
    open.onclick=async () => {
      try { await window.pywebview.api.open_output_folder(); }
      catch(error) { toast(error.message || "Could not open the output folder.",true); }
    };
    controls.append(choose,open);setupForm.elements.output_dir.parentElement.after(controls);renderControls();
  }

  const labStop=document.createElement("button");
  labStop.id="lab-stop-button";labStop.type="button";labStop.className="button stop-button small";
  labStop.dataset.action="stop";labStop.innerHTML='<span aria-hidden="true">■</span> Stop trial';
  document.querySelector(".lab-footer").append(labStop);

  document.addEventListener("click",async (event) => {
    const button=event.target.closest("button");
    if (!button || button.disabled) return;
    if (button.dataset.view) showView(button.dataset.view);
    if (["connect", "connect_after_replug"].includes(button.dataset.action)) {
      const connectAction = button.dataset.action;
      const source = $("source-select").value, port = $("serial-port").value.trim();
      if (connectAction === "connect_after_replug" && source !== "serial") {
        toast("Select USB serial device before preparing USB reconnect.", true);
        return;
      }
      if (source !== snapshot?.source || (source === "serial" && port !== (snapshot?.selected_port || ""))) {
        if (!await action("select_source", {source, port:port || null})) return;
      }
      await action(connectAction);
    } else if (button.dataset.action) await action(button.dataset.action,{},button.dataset.action === "self_test" ? "Sensor check requested. Its result is in Diagnostics." : null);
    if (button.dataset.fault) await action("fault",{kind:button.dataset.fault},"Injected simulation fault: " + button.textContent + ".");
    if (button.dataset.trialId) { selectedTrial=button.dataset.trialId;renderArchive(true); }
    if (button.dataset.series) {
      chartSeries=button.dataset.series;
      $$("[data-series]").forEach((entry) => { entry.classList.toggle("active",entry.dataset.series === chartSeries);entry.setAttribute("aria-pressed",String(entry.dataset.series === chartSeries)); });
      text("chart-series-label",chartSeries === "speed" ? "Interval speed" : "Cumulative distance");
      renderMetrics();drawChart();
    }
  });
  function markSetupChanged() {
    setupDirty=true;updateGeometry();
    renderControls();
    text("validation-status","Setup changed · validate or arm to check.");
    $("filename-preview").parentElement.classList.remove("valid","invalid");
  }
  setupForm.addEventListener("input",markSetupChanged);
  $("trial-identity-fields").addEventListener("input",markSetupChanged);
  setupForm.addEventListener("submit",async (event) => {
    event.preventDefault();
    if (!setupForm.reportValidity()) return;
    const result=await action("validate",setupPayload());
    if (result) { setupDirty=false;renderValidation();if (result.validation?.valid) toast("Setup validated. Ready to arm when connected."); }
  });
  $("arm-button").addEventListener("click",async () => {
    if (!setupForm.reportValidity()) return;
    const result=await action("arm",setupPayload());
    if (result && stateName() === "ARMED") { setupDirty=false;renderValidation(); }
  });
  $("new-trial-button").addEventListener("click",async () => {
    const attempt=Number(setupForm.elements.attempt.value) || 1;
    if (stateName() === "ERROR" && isConnected()) {
      const disconnected=await action("disconnect");
      if (!disconnected) return;
    }
    if (!isConnected()) { const connected=await action("connect");if (!connected) return; }
    const result=["COMPLETE","ARMED"].includes(stateName()) ? await action("disarm") : snapshot;
    if (result) {
      setupForm.elements.attempt.value=attempt+1;
      setupDirty=true;showView("trial");
      text("filename-preview","Validate setup to preview filename");
      text("validation-status","New trial · attempt advanced to " + (attempt+1) + ".");
      $("filename-preview").parentElement.classList.remove("valid","invalid");
      toast("Ready for a new trial. Attempt number advanced to avoid a filename collision.");
    }
  });
  $("open-lab").addEventListener("click",() => { syncSimulationForm();$("lab-dialog").showModal(); });
  $("close-lab").addEventListener("click",() => $("lab-dialog").close());
  $("lab-dialog").addEventListener("click",(event) => { if (event.target === $("lab-dialog")) { const rect=$("lab-dialog").getBoundingClientRect();if(event.clientX<rect.left||event.clientX>rect.right||event.clientY<rect.top||event.clientY>rect.bottom) $("lab-dialog").close(); } });
  $("simulation-form").addEventListener("submit",async (event) => {
    event.preventDefault();const data=Object.fromEntries(new FormData(event.target));
    await action("configure_simulation",{profile:data.profile,seed:Number(data.seed),rate_hz:Number(data.rate_hz)},"Simulation profile applied: " + (profileNames[data.profile] || data.profile) + ".");
  });
  $("toggle-sensor").addEventListener("click",() => action("sensor",{blocked:!snapshot?.sensor_blocked}));
  $("short-button").addEventListener("click",() => action("button",{held_ms:100}));
  $("long-button").addEventListener("click",() => action("button",{held_ms:1500}));
  $("animate-pulses").addEventListener("change",(event) => { animate=event.target.checked;if(!animate) $("pulse-schematic").classList.remove("pulse"); });
  $("note-form").addEventListener("submit",async (event) => {
    event.preventDefault();const value=$("note-input").value.trim();if(!value)return;
    const result=await action("note",{text:value},"Field note saved.");
    if (result) $("note-input").value="";
  });
  $("chart-window").addEventListener("change",(event) => { chartWindow=Number(event.target.value);drawChart(); });
  $("toggle-table").addEventListener("click",() => {
    $("event-data").hidden=!$("event-data").hidden;
    $("toggle-table").setAttribute("aria-expanded",String(!$("event-data").hidden));
    $("toggle-table").innerHTML=($("event-data").hidden ? "View event data" : "Hide event data") + ' <span aria-hidden="true">↗</span>';
    renderEventTable();
  });
  $("flight-chart").addEventListener("pointermove",(event) => {
    if (!chartPoints.length || !chartGeometry) return;
    const bounds=event.target.getBoundingClientRect(), x=event.clientX-bounds.left;
    const point=chartPoints.reduce((nearest,candidate) => Math.abs(candidate.x-x)<Math.abs(nearest.x-x)?candidate:nearest);
    const tooltip=$("chart-tooltip"), field=chartSeries==="speed"?"speed_m_s":"cumulative_distance_m";
    tooltip.textContent="Event " + point.event_n + " · " + number(point.time_s,3) + " s\n" + number(point[field],4) + (chartSeries==="speed"?" m/s":" m");
    tooltip.hidden=false;
    tooltip.style.left=Math.max(8,Math.min(point.x+18,chartGeometry.width-180))+"px";
    tooltip.style.top=Math.max(5,point.y-58)+"px";
  });
  $("flight-chart").addEventListener("pointerleave",() => { $("chart-tooltip").hidden=true; });
  $("copy-diagnostics").addEventListener("click",async () => {
    try {
      await navigator.clipboard.writeText(JSON.stringify({
        mode:snapshot?.source,selected_port:snapshot?.selected_port,
        connection_state:snapshot?.connection_state,ready:snapshot?.ready,
        device:snapshot?.device,state:snapshot?.state,sensor_state:snapshot?.sensor_state,
        metrics:snapshot?.metrics,warnings:snapshot?.warnings,diagnostics:snapshot?.diagnostics
      },null,2));
      toast("Diagnostics copied to clipboard.");
    } catch { toast("Clipboard access is unavailable in this window.",true); }
  });
  $("close-toast").addEventListener("click",() => { $("toast").hidden=true; });
  document.querySelector(".brand").addEventListener("click",(event) => { event.preventDefault();showView("trial"); });
  window.addEventListener("pywebviewready",installDesktopControls);
  window.addEventListener("resize",drawChart);
  if (window.ResizeObserver) new ResizeObserver(() => { if(activeView==="trial") drawChart(); }).observe($("chart-stage"));
  window.addEventListener("hashchange",() => showView(location.hash.slice(1)));
  window.addEventListener("online",pollFallback);
  setInterval(pollFallback,1000);
  renderControls();updateGeometry();drawChart();renderDiagnostics();renderArchive();
  if (["trial","review","diagnostics"].includes(location.hash.slice(1))) showView(location.hash.slice(1));
  installDesktopControls();initialize();
})();
