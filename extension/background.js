// Background service worker: submits jobs to the gateway, polls their status, relays it to
// the popup (through chrome.storage) and notifies the user when a job finishes.

import { ApiError, getJob, getResults, submitJob } from "./lib/api.js";
import { isTerminal } from "./lib/format.js";
import { activeJobs, addJob, clearFinished, removeJob, updateJob } from "./lib/jobs.js";
import { connectNotion } from "./lib/notion-auth.js";
import { clearNotionAuth, getNotionAuth, getSettings } from "./lib/storage.js";

const POLL_INTERVAL_MS = 3000;
const WATCHDOG_ALARM = "poll-watchdog";

// ---- Message handlers (content script and popup) -------------------------------------

const handlers = {
  async SUBMIT_JOB({ videoId, title, idempotencyKey }) {
    const auth = await getNotionAuth();
    if (!auth?.accessToken) {
      Promise.resolve(chrome.action.openPopup?.()).catch(() => {});
      throw new ApiError("NOTION_NOT_CONNECTED", "Connect Notion first: click the Video Notes icon in the toolbar.");
    }
    const settings = await getSettings();
    // The same video clicked again while its job is still running (same gateway and Notion
    // destination) joins that job instead of starting another. The gateway checks this too.
    const parentId = settings.notionParentId || "";
    const running = (await activeJobs()).find(
      (j) => j.videoId === videoId && j.gatewayUrl === settings.gatewayUrl && (j.notionParentId || "") === parentId,
    );
    if (running) return { jobId: running.jobId, alreadyRunning: true };
    const key = `${settings.gatewayUrl}|${parentId}|${videoId}`;
    const inFlight = submitting.get(key);
    if (inFlight) return { ...(await inFlight), alreadyRunning: true };
    const submission = submitNew(settings, auth, videoId, title, idempotencyKey);
    submitting.set(key, submission);
    try {
      return await submission;
    } finally {
      submitting.delete(key);
    }
  },

  async CONNECT_NOTION() {
    return connectNotion();
  },

  async DISCONNECT_NOTION() {
    await clearNotionAuth();
    return {};
  },

  async POLL_NOW() {
    await pollActiveJobs();
    return {};
  },

  async REMOVE_JOB({ jobId }) {
    await removeJob(jobId);
    return {};
  },

  async CLEAR_FINISHED() {
    await clearFinished();
    return {};
  },
};

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  const handler = handlers[message?.type];
  if (!handler) return false;
  handler(message)
    .then((result) => sendResponse({ ok: true, ...result }))
    .catch((error) =>
      sendResponse({
        ok: false,
        error: { code: error.code || "UNEXPECTED", message: error.message || "Something went wrong." },
      }),
    );
  return true; // keeps the channel open for the async response
});

// Clicks on a video whose submission is still on its way to the gateway wait for it.
const submitting = new Map();

async function submitNew(settings, auth, videoId, title, idempotencyKey) {
  const payload = {
    youtube_url: `https://www.youtube.com/watch?v=${videoId}`,
    notion_token: auth.accessToken,
    ...(settings.notionParentId ? { notion_parent_id: settings.notionParentId } : {}),
  };
  const job = await submitJob(settings.gatewayUrl, payload, idempotencyKey);
  await addJob({
    jobId: job.job_id,
    videoId: job.video_id,
    title: title || null,
    status: job.status,
    progress: 0,
    submittedAt: Date.now(),
    gatewayUrl: settings.gatewayUrl,
    notionParentId: settings.notionParentId || "",
  });
  schedulePolling(0);
  return { jobId: job.job_id, alreadyRunning: Boolean(job.already_running) };
}

// ---- Status polling -----------------------------------------------------------------

let pollTimer = null;
let polling = false;

function schedulePolling(delay = POLL_INTERVAL_MS) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    await pollActiveJobs();
    if ((await activeJobs()).length) schedulePolling();
  }, delay);
}

async function pollActiveJobs() {
  if (polling) return;
  polling = true;
  try {
    const jobs = await activeJobs();
    await Promise.all(jobs.map(refreshJob));
  } finally {
    polling = false;
  }
}

// Notion rejected the token this job was sent with. If it is still the stored one, drop it so
// the popup offers "Connect Notion" again instead of claiming to be connected. A token saved
// after the job was submitted (the user already reconnected) is left alone.
async function forgetRejectedToken(job) {
  const auth = await getNotionAuth();
  if (auth?.accessToken && (auth.connectedAt || 0) <= (job.submittedAt || Date.now())) {
    await clearNotionAuth();
  }
}

async function refreshJob(job) {
  const baseUrl = job.gatewayUrl || (await getSettings()).gatewayUrl;
  let status;
  try {
    status = await getJob(baseUrl, job.jobId);
  } catch (error) {
    if (error.status === 404) {
      await updateJob(job.jobId, {
        status: "FAILED",
        error: { code: "JOB_NOT_FOUND", message: "The server no longer knows this job." },
      });
    } else {
      await updateJob(job.jobId, { connectionProblem: error.message });
    }
    return;
  }

  const patch = {
    status: status.status,
    progress: status.progress,
    title: status.video_title || job.title,
    durationSec: status.duration_sec,
    detail: status.detail,
    retry: status.retry,
    warnings: status.warnings,
    error: status.error,
    result: status.result,
    connectionProblem: null,
  };
  if (status.error?.code === "NOTION_UNAUTHORIZED") await forgetRejectedToken(job);
  // Remember when the current step started, so the popup can show how long it has been running.
  if (!job.detailSince || job.status !== status.status || job.detail !== status.detail) {
    patch.detailSince = Date.now();
  }
  if (isTerminal(status.status)) {
    // The results endpoint is the canonical outcome (and returns 409 until terminal).
    try {
      const results = await getResults(baseUrl, job.jobId);
      Object.assign(patch, { result: results.result, warnings: results.warnings, error: results.error });
    } catch {
      // Keep what the status endpoint returned.
    }
    await updateJob(job.jobId, patch);
    notifyFinished({ ...job, ...patch });
    return;
  }
  await updateJob(job.jobId, patch);
}

function notifyFinished(job) {
  const title = job.title || "Your video";
  const messages = {
    COMPLETED: ["Notes ready", `${title} is in Notion.`],
    COMPLETED_WITH_WARNINGS: ["Notes ready", `${title} is in Notion.`],
    FAILED: ["Couldn't take notes", `${title}: ${job.error?.message || "the job failed."}`],
    DEAD_LETTERED: ["Couldn't take notes", `${title}: gave up after several attempts.`],
  };
  const [heading, message] = messages[job.status];
  chrome.notifications.create(job.jobId, {
    type: "basic",
    iconUrl: "icons/icon128.png",
    title: heading,
    message,
    priority: 1,
  });
}

chrome.notifications.onClicked.addListener(async (jobId) => {
  const job = (await chrome.storage.local.get("jobs")).jobs?.find((j) => j.jobId === jobId);
  if (job?.result?.notion_page_url) chrome.tabs.create({ url: job.result.notion_page_url });
  chrome.notifications.clear(jobId);
});

// The service worker can be stopped while idle; the alarm restarts polling if jobs are
// still running when it wakes up again.
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === WATCHDOG_ALARM && (await activeJobs()).length) schedulePolling(0);
});

function ensureWatchdog() {
  chrome.alarms.create(WATCHDOG_ALARM, { periodInMinutes: 0.5 });
  schedulePolling(0);
}

chrome.runtime.onInstalled.addListener(ensureWatchdog);
// First install: open the setup guide so a new user knows what to do.
chrome.runtime.onInstalled.addListener(({ reason }) => {
  if (reason === chrome.runtime.OnInstalledReason.INSTALL) {
    chrome.tabs.create({ url: chrome.runtime.getURL("setup.html") });
  }
});
chrome.runtime.onStartup.addListener(ensureWatchdog);
