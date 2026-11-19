// Popup UI. Renders tracked jobs from storage (written by the background script), asks the
// background to poll while the popup is open, and offers Notion connect/disconnect.

import {
  STEPS,
  describeActivity,
  describeProviderDelay,
  describeError,
  describeWarning,
  formatClock,
  isTerminal,
  stepIndex,
  timeAgo,
} from "./lib/format.js";
import { listJobs } from "./lib/jobs.js";
import { getOAuthConfig } from "./lib/api.js";
import { getNotionAuth, getSettings } from "./lib/storage.js";

const STAGE_STEP = { transcribe: 1, summarize: 2, extract_frames: 3, publish: 4 };
const $ = (selector, root = document) => root.querySelector(selector);

function el(tag, props = {}, children = []) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

async function send(type, payload = {}) {
  const response = await chrome.runtime.sendMessage({ type, ...payload });
  if (!response?.ok) {
    const error = new Error(response?.error?.message || "Something went wrong.");
    error.code = response?.error?.code;
    throw error;
  }
  return response;
}

// ---- Notion connection -----------------------------------------------------------------

// Whether the server offers one-click Notion sign-in (it needs an OAuth app in .env).
async function notionSignIn() {
  try {
    const { gatewayUrl } = await getSettings();
    const config = await getOAuthConfig(gatewayUrl);
    return { enabled: Boolean(config?.enabled) };
  } catch (error) {
    return { enabled: false, problem: error.message };
  }
}

function openTokenSettings() {
  chrome.tabs.create({ url: chrome.runtime.getURL("options.html#token-form") });
}

function openSetupGuide() {
  chrome.tabs.create({ url: chrome.runtime.getURL("setup.html") });
}

let notionRenderId = 0;

// Builds the whole card before swapping it in, so overlapping renders can't duplicate buttons.
// A message stays on the card until the next render instead of disappearing on a timer.
async function renderNotion(errorMessage) {
  const renderId = ++notionRenderId;
  const auth = await getNotionAuth();
  const status = el("div", { className: "notion-status" }, [
    el("span", { className: "eyebrow", textContent: "Notion" }),
  ]);
  let action;
  let hint = errorMessage ? { text: errorMessage, error: true } : null;

  if (auth?.accessToken) {
    status.append(el("strong", { textContent: `Connected to ${auth.workspaceName || "your workspace"}` }));
    action = el("button", { className: "link-button", type: "button", textContent: "Disconnect" });
    action.addEventListener("click", async () => {
      await send("DISCONNECT_NOTION");
      renderNotion();
    });
  } else {
    status.append(el("strong", { textContent: "Not connected" }));
    const signIn = await notionSignIn();
    if (signIn.enabled) {
      action = el("button", { className: "button primary", type: "button", textContent: "Connect Notion" });
      action.addEventListener("click", async () => {
        action.disabled = true;
        action.textContent = "Connecting…";
        try {
          await send("CONNECT_NOTION");
          renderNotion();
        } catch (error) {
          renderNotion(error.message);
        }
      });
    } else {
      action = el("button", { className: "button primary", type: "button", textContent: "Add token" });
      action.addEventListener("click", openTokenSettings);
      hint ??= signIn.problem
        ? { text: signIn.problem, error: true }
        : { text: "One-click sign-in isn't set up on the server. Add a Notion integration token to start taking notes." };
    }
  }

  if (renderId !== notionRenderId) return; // a newer render has taken over
  if (hint) status.append(el("p", { className: hint.error ? "notion-hint error" : "notion-hint", textContent: hint.text }));
  if (!auth?.accessToken) {
    const guide = el("button", { className: "link-button notion-guide", type: "button", textContent: "Open the setup guide" });
    guide.addEventListener("click", openSetupGuide);
    status.append(guide);
  }
  $("#notion-card").replaceChildren(status, action);
}

// ---- "Take notes" for the active tab -------------------------------------------------------

async function renderCurrentTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.url) return;
  const url = new URL(tab.url);
  const videoId = url.hostname === "www.youtube.com" && url.pathname === "/watch" ? url.searchParams.get("v") : null;
  if (!videoId) return;

  const title = (tab.title || "").replace(/ - YouTube$/, "").replace(/^\(\d+\)\s*/, "");
  $("#current-video-title").textContent = title || "This video";
  $("#current-video").hidden = false;
  const button = $("#take-notes-here");
  button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = "Sending…";
    try {
      const { alreadyRunning } = await send("SUBMIT_JOB", { videoId, title, idempotencyKey: crypto.randomUUID() });
      button.textContent = alreadyRunning ? "Already in progress ✓" : "Queued ✓";
    } catch (error) {
      if (error.code === "VIDEO_TOO_LONG") {
        // Refused at once by the gateway; retrying cannot help, so the button stays off.
        button.textContent = "Too long";
        const note = $("#current-video-error");
        note.textContent = error.message;
        note.hidden = false;
        return;
      }
      button.textContent = "Take Notes";
      button.disabled = false;
      renderNotion(error.message);
    }
  });
}

// ---- Jobs ------------------------------------------------------------------------------

let currentJobs = [];
const openNotes = new Set();

function renderSteps(list, job) {
  const failed = job.status === "FAILED" || job.status === "DEAD_LETTERED";
  const current = failed ? (STAGE_STEP[job.error?.stage] ?? 0) : stepIndex(job.status);
  const complete = job.status === "COMPLETED" || job.status === "COMPLETED_WITH_WARNINGS";
  list.replaceChildren(
    ...STEPS.map((step, i) => {
      let state = "";
      if (complete || i < current) state = "done";
      else if (i === current) state = failed ? "failed" : "active";
      const item = el("li", { className: `step ${state}`, textContent: step.label });
      if (i === current && !complete) item.setAttribute("aria-current", "step");
      return item;
    }),
  );
}

function tone(job) {
  if (job.status === "FAILED" || job.status === "DEAD_LETTERED") return "error";
  if (job.status === "COMPLETED" || job.status === "COMPLETED_WITH_WARNINGS") return "success";
  if (job.retry || job.connectionProblem) return "waiting";
  return "running";
}

function activityText(job) {
  if (job.connectionProblem) return `${job.connectionProblem} Retrying…`;
  if (job.status === "FAILED" || job.status === "DEAD_LETTERED") return "";
  // A Gemini retry is already explained (with its countdown) on the line under the bar.
  if (job.retry && describeProviderDelay(job)) return "";
  return describeActivity(job);
}

// Says plainly when a job is waiting on Google Gemini rather than on this app.
function renderProviderDelay(line, job) {
  const delay = job.connectionProblem ? null : describeProviderDelay(job);
  line.hidden = !delay;
  if (!delay) return;
  line.replaceChildren(el("strong", { textContent: `${delay.title}: ` }), delay.text);
}

function renderJob(job) {
  const node = $("#job-template").content.firstElementChild.cloneNode(true);
  node.dataset.jobId = job.jobId;
  node.dataset.tone = tone(job);

  $(".job-title", node).textContent = job.title || `YouTube video ${job.videoId}`;
  const meta = [timeAgo(job.submittedAt)];
  if (job.durationSec) meta.push(formatClock(job.durationSec));
  $(".job-meta", node).textContent = meta.join(" · ");

  renderSteps($(".steps", node), job);
  const progress = Math.max(0, Math.min(100, job.progress || 0));
  $(".progress-fill", node).style.width = `${isTerminal(job.status) && tone(job) === "error" ? 100 : progress}%`;
  $(".progress", node).setAttribute("aria-valuenow", String(progress));
  $(".activity", node).textContent = activityText(job);
  renderProviderDelay($(".provider-delay", node), job);

  const warnings = job.warnings || [];
  if (warnings.length && isTerminal(job.status)) {
    // Warnings are informational (the page was published): a small, collapsed note, not an alert.
    const box = $(".job-notes", node);
    box.hidden = false;
    // The list is rebuilt on every status update; keep a note the user opened open.
    box.open = openNotes.has(job.jobId);
    box.addEventListener("toggle", () => (box.open ? openNotes.add(job.jobId) : openNotes.delete(job.jobId)));
    $(".job-notes-title", box).textContent = warnings.length === 1 ? "1 note about this page" : `${warnings.length} notes about this page`;
    $(".job-notes-list", box).replaceChildren(...warnings.map((w) => el("li", { textContent: describeWarning(w) })));
  }

  if (job.status === "FAILED" || job.status === "DEAD_LETTERED") {
    const { title, hint } = describeError(job);
    const box = $(".notice.error", node);
    box.hidden = false;
    $(".notice-title", box).textContent = title;
    $(".notice-text", box).textContent = hint;
  }

  const pageUrl = job.result?.notion_page_url;
  if (pageUrl) {
    const link = $(".view-notion", node);
    link.href = pageUrl;
    link.hidden = false;
  }

  $(".dismiss", node).addEventListener("click", () => send("REMOVE_JOB", { jobId: job.jobId }));
  return node;
}

function renderJobs(jobs) {
  currentJobs = jobs;
  $("#jobs").replaceChildren(...jobs.map(renderJob));
  $("#empty").hidden = jobs.length > 0;
  $("#clear-finished").hidden = !jobs.some((j) => isTerminal(j.status));
}

// Keeps retry countdowns and "(2m 10s)" step timers current without rebuilding the list.
function refreshCountdowns() {
  for (const job of currentJobs) {
    if (isTerminal(job.status) || job.connectionProblem) continue;
    const card = document.querySelector(`.job[data-job-id="${CSS.escape(job.jobId)}"]`);
    if (!card) continue;
    $(".activity", card).textContent = activityText(job);
    renderProviderDelay($(".provider-delay", card), job);
  }
}

// ---- Wiring ----------------------------------------------------------------------------

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.jobs) renderJobs(changes.jobs.newValue || []);
  if (changes.notionAuth) renderNotion();
});

$("#open-settings").addEventListener("click", () => chrome.runtime.openOptionsPage());
$("#open-setup").addEventListener("click", openSetupGuide);
$("#clear-finished").addEventListener("click", () => send("CLEAR_FINISHED"));

async function poll() {
  try {
    await send("POLL_NOW");
  } catch {
    // Connection problems are recorded on each job by the background script.
  }
}

renderNotion();
renderCurrentTab();
renderJobs(await listJobs());
poll();
setInterval(poll, 2000);
setInterval(refreshCountdowns, 1000);
