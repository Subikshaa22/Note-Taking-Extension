// Popup UI. Renders tracked jobs from storage (written by the background script), asks the
// background to poll while the popup is open, and offers Notion connect/disconnect.

import {
  STEPS,
  describeActivity,
  describeError,
  describeWarning,
  formatClock,
  isTerminal,
  stepIndex,
  timeAgo,
} from "./lib/format.js";
import { listJobs } from "./lib/jobs.js";
import { getNotionAuth } from "./lib/storage.js";

const STAGE_STEP = { transcribe: 1, summarize: 2, extract_frames: 3, publish: 4 };
const $ = (selector, root = document) => root.querySelector(selector);

function el(tag, props = {}, children = []) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

async function send(type, payload = {}) {
  const response = await chrome.runtime.sendMessage({ type, ...payload });
  if (!response?.ok) throw new Error(response?.error?.message || "Something went wrong.");
  return response;
}

// ---- Notion connection -----------------------------------------------------------------

async function renderNotion(errorMessage) {
  const card = $("#notion-card");
  const auth = await getNotionAuth();
  const status = el("div", { className: "notion-status" });
  card.replaceChildren(status);

  if (auth?.accessToken) {
    status.append(
      el("span", { className: "eyebrow", textContent: "Notion" }),
      el("strong", { textContent: `Connected to ${auth.workspaceName || "your workspace"}` }),
    );
    const disconnect = el("button", { className: "link-button", type: "button", textContent: "Disconnect" });
    disconnect.addEventListener("click", async () => {
      await send("DISCONNECT_NOTION");
      renderNotion();
    });
    card.append(disconnect);
  } else {
    status.append(
      el("span", { className: "eyebrow", textContent: "Notion" }),
      el("strong", { textContent: "Not connected" }),
    );
    const connect = el("button", { className: "button primary", type: "button", textContent: "Connect Notion" });
    connect.addEventListener("click", async () => {
      connect.disabled = true;
      connect.textContent = "Connecting…";
      try {
        await send("CONNECT_NOTION");
        renderNotion();
      } catch (error) {
        renderNotion(error.message);
      }
    });
    card.append(connect);
  }
  if (errorMessage) {
    const note = el("p", { className: "inline-error", textContent: errorMessage });
    card.after(note);
    setTimeout(() => note.remove(), 8000);
  }
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
      await send("SUBMIT_JOB", { videoId, title, idempotencyKey: crypto.randomUUID() });
      button.textContent = "Queued ✓";
    } catch (error) {
      button.textContent = "Take Notes";
      button.disabled = false;
      renderNotion(error.message);
    }
  });
}

// ---- Jobs ------------------------------------------------------------------------------

let currentJobs = [];

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
  return describeActivity(job);
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

  const warnings = job.warnings || [];
  if (warnings.length && isTerminal(job.status)) {
    const box = $(".notice.warning", node);
    box.hidden = false;
    $(".notice-title", box).textContent =
      warnings.length === 1 ? "Completed with 1 warning" : `Completed with ${warnings.length} warnings`;
    $(".notice-list", box).replaceChildren(...warnings.map((w) => el("li", { textContent: describeWarning(w) })));
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

// Keeps the "retrying in Ns" countdown current without rebuilding the list.
function refreshCountdowns() {
  for (const job of currentJobs) {
    if (!job.retry) continue;
    const node = document.querySelector(`.job[data-job-id="${CSS.escape(job.jobId)}"] .activity`);
    if (node) node.textContent = activityText(job);
  }
}

// ---- Wiring ----------------------------------------------------------------------------

chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes.jobs) renderJobs(changes.jobs.newValue || []);
  if (changes.notionAuth) renderNotion();
});

$("#open-settings").addEventListener("click", () => chrome.runtime.openOptionsPage());
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
