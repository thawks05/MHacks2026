const els = Object.fromEntries([...document.querySelectorAll("[id]")].map((el) => [el.id, el]));
const metrics = ["rms", "peak", "frequency", "kurtosis"];
const traces = Object.fromEntries(metrics.map((name) => [name, []]));
const state = {
  phase: "idle",
  baseline: null,
  baselineTimer: null,
  monitorTimer: null,
  elapsed: 0,
  monitorElapsed: 0,
  sums: {},
  readings: [],
  serialPort: null,
  serialReader: null,
  serialBuffer: "",
  sampleWindow: [],
  lastSampleIndex: null,
  latestUsbWindow: null,
  incidentOpen: false,
};

const noise = (amount) => (Math.random() - 0.5) * amount;
const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const formatTime = (seconds) => `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
const healthyWindow = () => ({ rms: 0.018 + noise(0.0015), peak: 0.053 + noise(0.004), frequency: 48.1 + noise(0.45), kurtosis: 3.08 + noise(0.22) });
const validWindow = (sample) => sample && metrics.every((name) => Number.isFinite(Number(sample[name])));
const inputWindow = (mode) => {
  if (els["demo-mode"].checked) return mode === "baseline" ? healthyWindow() : monitoringWindow();
  return validWindow(state.latestUsbWindow) ? state.latestUsbWindow : null;
};

function renderTrace(name, value, min, max) {
  traces[name].push(clamp((value - min) / (max - min), 0.06, 1));
  if (traces[name].length > 30) traces[name].shift();
  els[`trace-${name}`].innerHTML = traces[name].map((height) => `<i style="height:${Math.round(height * 100)}%"></i>`).join("");
}

function showFeatureWindow(sample) {
  els["capture-rms"].textContent = sample.rms.toFixed(3);
  els["capture-peak"].textContent = sample.peak.toFixed(3);
  els["capture-frequency"].textContent = sample.frequency.toFixed(1);
  els["capture-kurtosis"].textContent = sample.kurtosis.toFixed(2);
  renderTrace("rms", sample.rms, 0.005, 0.06);
  renderTrace("peak", sample.peak, 0.015, 0.16);
  renderTrace("frequency", sample.frequency, 40, 52);
  renderTrace("kurtosis", sample.kurtosis, 2.5, 9);
}

function setRuntimeState(label, substate, color, podMode) {
  els["session-state-label"].textContent = label;
  els["session-substate"].textContent = substate;
  els["session-state-dot"].style.background = color;
  els["rail-pod-mode"].textContent = podMode.toLowerCase();
  els["pod-mode-main"].textContent = podMode;
}

function startBaseline() {
  if (state.phase === "calibrating") return;
  stopMonitoring();
  state.phase = "calibrating";
  state.elapsed = 0;
  state.readings = [];
  state.sums = { rms: 0, peak: 0, frequency: 0, kurtosis: 0 };
  metrics.forEach((name) => { traces[name] = []; });
  els["capture-panel"].classList.add("visible");
  els["baseline-summary"].hidden = true;
  els["baseline-step"].className = "run-step running";
  els["baseline-button"].disabled = true;
  els["baseline-button"].textContent = "Training baseline…";
  els["capture-status"].textContent = "Accepting healthy one-second windows";
  els["watcher-state"].textContent = "Active · training";
  els["rail-watcher"].textContent = "training";
  setRuntimeState("Training healthy state", "Pod 01 · baseline", "var(--purple)", "Training");

  const tickMs = 200;
  state.baselineTimer = setInterval(() => {
    const acceleration = els["demo-mode"].checked ? 10 : 1;
    const sample = inputWindow("baseline");
    if (!sample) {
      els["sample-count"].textContent = "Waiting for a complete 1,000-sample USB window…";
      return;
    }
    if (sample.rms < 0.003) {
      els["sample-count"].textContent = "Window rejected: motor off or pod loose";
      return;
    }
    state.elapsed = Math.min(60, state.elapsed + tickMs / 1000 * acceleration);
    state.readings.push(sample);
    metrics.forEach((name) => { state.sums[name] += sample[name]; });
    showFeatureWindow(sample);
    els["baseline-timer"].textContent = formatTime(state.elapsed);
    els["baseline-progress"].style.width = `${state.elapsed / 60 * 100}%`;
    els["sample-count"].textContent = `${state.readings.length} healthy windows accepted`;
    if (state.elapsed >= 60) finishBaseline();
  }, tickMs);
}

function finishBaseline() {
  clearInterval(state.baselineTimer);
  state.baselineTimer = null;
  state.baseline = Object.fromEntries(metrics.map((name) => [name, state.sums[name] / state.readings.length]));
  state.phase = "baseline-ready";
  metrics.forEach((name) => {
    const digits = name === "frequency" ? 1 : name === "kurtosis" ? 2 : 3;
    els[`baseline-${name}`].textContent = state.baseline[name].toFixed(digits);
    els[`compare-${name}-base`].textContent = state.baseline[name].toFixed(digits);
  });
  els["baseline-summary"].hidden = false;
  els["baseline-step"].className = "run-step complete";
  els["baseline-button"].disabled = false;
  els["baseline-button"].textContent = "Retrain baseline";
  els["monitor-step"].className = "run-step";
  els["monitor-button"].disabled = false;
  els["monitor-state"].textContent = "Baseline fitted";
  els["watcher-state"].textContent = "Active · monitoring ready";
  els["rail-watcher"].textContent = "ready";
  setRuntimeState("Ready for monitoring", "Pod 01 · monitoring", "var(--green)", "Monitoring");
}

function monitoringWindow() {
  const progression = clamp((state.monitorElapsed - 8) / 36, 0, 1);
  const base = state.baseline || healthyWindow();
  return {
    rms: base.rms * (1 + progression * 1.5) + noise(0.001),
    peak: base.peak * (1 + progression * 2.1) + noise(0.003),
    frequency: base.frequency * (1 - progression * 0.085) + noise(0.25),
    kurtosis: base.kurtosis * (1 + progression * 1.35) + noise(0.15),
  };
}

function healthFromZ(z) {
  if (z <= 2) return 100;
  if (z >= 50) return 0;
  return Math.round(clamp(100 - 100 * Math.log(z / 2) / Math.log(25), 0, 100));
}

function severityFromCount(count) {
  if (count >= 30) return "Critical";
  if (count >= 20) return "Degraded";
  if (count >= 10) return "Watch";
  return "Healthy";
}

function updateSpectrum(progression) {
  const bands = {
    low: Math.max(0, progression * 6 + noise(0.4)),
    mid: Math.max(0, progression * 9 + noise(0.5)),
    high: Math.max(0, progression * 23 + noise(0.8)),
  };
  Object.entries(bands).forEach(([name, value]) => {
    els[`band-${name}`].style.width = `${clamp(value / 25 * 100, 2, 100)}%`;
    els[`band-${name}`].style.background = value >= 10 ? "var(--red)" : value >= 4 ? "var(--yellow)" : "var(--purple)";
    els[`band-${name}-value`].textContent = `${value.toFixed(1)}σ`;
  });
  return Math.max(...Object.values(bands));
}

function updateLifecycle(count) {
  const activeIndex = count >= 30 ? 2 : count >= 10 ? 1 : 0;
  document.querySelectorAll(".lifecycle-step").forEach((step, index) => step.classList.toggle("active", index <= activeIndex));
}

function updateIncident(severity, health, count) {
  if (severity === "Healthy") return;
  state.incidentOpen = true;
  els["incident-console"].classList.add("open");
  els["incident-console"].classList.toggle("critical", severity === "Critical");
  els["incident-state"].textContent = `${severity.toUpperCase()} · open`;
  els["incident-title"].textContent = "Incident #1 · drive_gear";
  els["incident-detail"].textContent = severity === "Watch"
    ? "The Watcher opened one incident and pushed a PartAlert to the Analyst. The operator observation conversation is now active."
    : `The same incident escalated to ${severity.toLowerCase()}; no duplicate incident was created.`;
  els["incident-id"].textContent = "#1 · open";
  els["incident-health"].textContent = `${health}%`;
  els["analyst-state"].textContent = "Active · observing";
  els["rail-analyst"].textContent = count >= 30 ? "critical update" : "text queued";
  updateLifecycle(count);
}

function updateComparison(sample) {
  const progression = clamp((state.monitorElapsed - 8) / 36, 0, 1);
  const worstZ = updateSpectrum(progression);
  const count = worstZ >= 4 ? Math.floor(Math.max(0, state.monitorElapsed - 8)) : 0;
  const health = healthFromZ(worstZ);
  const severity = severityFromCount(count);
  const color = severity === "Healthy" ? "var(--green)" : severity === "Watch" ? "var(--yellow)" : "var(--red)";

  metrics.forEach((name) => {
    const base = state.baseline[name];
    const deviation = (sample[name] - base) / base * 100;
    const digits = name === "frequency" ? 1 : name === "kurtosis" ? 2 : 3;
    els[`compare-${name}-current`].textContent = sample[name].toFixed(digits);
    els[`deviation-${name}`].textContent = `${deviation >= 0 ? "+" : ""}${deviation.toFixed(1)}%`;
    els[`bar-${name}`].style.width = `${clamp(Math.abs(deviation) * 1.5, 2, 100)}%`;
    const card = document.querySelector(`[data-metric="${name}"]`);
    card.classList.toggle("warning", Math.abs(deviation) >= 8);
    card.classList.toggle("critical", Math.abs(deviation) >= 25);
  });

  els["health-value"].textContent = health;
  els["health-label"].textContent = severity;
  els["health-label"].style.color = color;
  els["health-ring"].style.background = `radial-gradient(circle,var(--panel) 57%,transparent 58%),conic-gradient(${color} 0 ${health}%,#303735 ${health}% 100%)`;
  els["health-explanation"].textContent = severity === "Healthy"
    ? "All features remain below the 4σ deviation threshold."
    : severity === "Watch"
      ? "The signal is far from baseline; persistence has reached the first hold threshold."
      : severity === "Degraded"
        ? "Deviation has persisted for at least 20 consecutive one-second windows."
        : "High-band impacts and falling shaft frequency have persisted for 30 windows.";
  els["deviation-count"].textContent = `${count} consecutive deviating windows`;
  els["hold-progress"].style.width = `${clamp(count / 30 * 100, 0, 100)}%`;
  els["hold-progress"].style.background = color;
  els["eta-value"].textContent = progression < 0.18 ? "Not enough decline" : `~${Math.max(2, Math.round(18 - progression * 12))} machine hours`;
  updateIncident(severity, health, count);
}

function startMonitoring() {
  if (!state.baseline || state.phase === "monitoring") return;
  state.phase = "monitoring";
  state.monitorElapsed = 0;
  state.incidentOpen = false;
  els["monitor-step"].className = "run-step running";
  els["monitor-button"].textContent = "Stop monitoring";
  els["monitor-button"].classList.add("stop");
  els["monitor-state"].textContent = "Streaming and scoring";
  els["watcher-state"].textContent = "Active · scoring windows";
  els["rail-watcher"].textContent = "monitoring";
  setRuntimeState("Official monitoring active", "Pod 01 · monitoring", "var(--green)", "Monitoring");
  state.monitorTimer = setInterval(() => {
    const acceleration = els["demo-mode"].checked ? 5 : 1;
    const sample = inputWindow("monitor");
    if (!sample) {
      els["monitor-state"].textContent = "Waiting for a complete USB window";
      return;
    }
    state.monitorElapsed += 0.5 * acceleration;
    els["monitor-duration"].textContent = `${formatTime(state.monitorElapsed)} runtime`;
    showFeatureWindow(sample);
    updateComparison(sample);
  }, 500);
}

function stopMonitoring() {
  if (state.monitorTimer) clearInterval(state.monitorTimer);
  state.monitorTimer = null;
  if (state.phase === "monitoring") {
    state.phase = "baseline-ready";
    els["monitor-step"].className = "run-step";
    els["monitor-button"].textContent = "Resume monitoring";
    els["monitor-button"].classList.remove("stop");
    els["monitor-state"].textContent = "Monitoring paused";
    setRuntimeState("Official monitoring paused", "Pod 01 · monitoring", "var(--yellow)", "Monitoring");
  }
}

function computeSerialWindow(samples) {
  const axes = [0, 1, 2].map((axis) => {
    const mean = samples.reduce((sum, row) => sum + row[axis], 0) / samples.length;
    return samples.map((row) => row[axis] - mean);
  });
  const energy = samples.map((_, index) => Math.sqrt(axes[0][index] ** 2 + axes[1][index] ** 2 + axes[2][index] ** 2));
  const rms = Math.sqrt(energy.reduce((sum, value) => sum + value ** 2, 0) / energy.length);
  const peak = Math.max(...energy);
  const axis = axes.sort((a, b) => b.reduce((s, v) => s + v * v, 0) - a.reduce((s, v) => s + v * v, 0))[0];
  let crossings = 0;
  for (let i = 1; i < axis.length; i += 1) if ((axis[i - 1] <= 0 && axis[i] > 0) || (axis[i - 1] >= 0 && axis[i] < 0)) crossings += 1;
  const frequency = crossings / 2;
  const meanEnergy = energy.reduce((sum, value) => sum + value, 0) / energy.length;
  const centered = energy.map((value) => value - meanEnergy);
  const variance = centered.reduce((sum, value) => sum + value ** 2, 0) / centered.length || 1e-9;
  const kurtosis = centered.reduce((sum, value) => sum + value ** 4, 0) / centered.length / (variance ** 2);
  return { rms, peak, frequency, kurtosis };
}

function parseSerialLine(line) {
  const trimmed = line.trim();
  if (!trimmed || trimmed.startsWith("#")) return;
  const fields = trimmed.split(",");
  if (fields.length !== 5) return;
  const [podText, indexText, ...axisText] = fields;
  const podId = Number(podText);
  const sampleIndex = Number(indexText);
  if (podId !== 1 || !Number.isInteger(sampleIndex)) return;
  if (state.lastSampleIndex !== null && sampleIndex !== state.lastSampleIndex + 1) state.sampleWindow = [];
  state.lastSampleIndex = sampleIndex;
  const xyz = axisText.map((value) => value.includes(".") ? Number(value) : Number(value) / 8192);
  if (xyz.some((value) => !Number.isFinite(value))) return;
  state.sampleWindow.push(xyz);
  if (state.sampleWindow.length >= 1000) {
    state.latestUsbWindow = computeSerialWindow(state.sampleWindow.slice(0, 1000));
    state.sampleWindow = state.sampleWindow.slice(1000);
  }
}

async function readSerialLoop() {
  while (state.serialReader) {
    try {
      const { value, done } = await state.serialReader.read();
      if (done) break;
      state.serialBuffer += value;
      const lines = state.serialBuffer.split(/\r?\n/);
      state.serialBuffer = lines.pop() ?? "";
      lines.forEach(parseSerialLine);
    } catch {
      break;
    }
  }
}

async function connectUsb() {
  if (els["demo-mode"].checked) {
    els["usb-button"].classList.add("connected");
    els["usb-label"].textContent = "Simulator connected";
    return;
  }
  if (!("serial" in navigator)) {
    els["usb-label"].textContent = "Use Chrome on localhost";
    return;
  }
  try {
    state.serialPort = await navigator.serial.requestPort();
    await state.serialPort.open({ baudRate: 921600 });
    const decoder = new TextDecoderStream();
    state.serialPort.readable.pipeTo(decoder.writable).catch(() => {});
    state.serialReader = decoder.readable.getReader();
    readSerialLoop();
    els["usb-button"].classList.add("connected");
    els["usb-label"].textContent = "Host ESP32 streaming";
    els["source-summary"].textContent = "Host ESP32 serial";
  } catch (error) {
    if (error.name !== "NotFoundError") els["usb-label"].textContent = "USB connection failed";
  }
}

function updateSourceMode() {
  if (els["demo-mode"].checked) {
    els["source-summary"].textContent = "Simulated motor";
    els["usb-label"].textContent = "Connect host ESP32";
  } else {
    els["source-summary"].textContent = "Serial source selected";
    els["usb-label"].textContent = "Choose USB port";
    els["usb-button"].classList.remove("connected");
  }
}

const navLinks = [...document.querySelectorAll(".rail-nav a")];
const trackedSections = [...document.querySelectorAll(".anchor-section")];
function updateActiveNavigation() {
  const threshold = window.innerHeight * 0.3;
  const current = trackedSections.reduce((active, section) => section.getBoundingClientRect().top <= threshold ? section : active, trackedSections[0]);
  navLinks.forEach((link) => link.classList.toggle("active", link.getAttribute("href") === `#${current.id}`));
}
window.addEventListener("scroll", updateActiveNavigation, { passive: true });
updateActiveNavigation();

els["baseline-button"].addEventListener("click", startBaseline);
els["monitor-button"].addEventListener("click", () => state.phase === "monitoring" ? stopMonitoring() : startMonitoring());
els["usb-button"].addEventListener("click", connectUsb);
els["demo-mode"].addEventListener("change", updateSourceMode);
