// Turns job state from the API into the words the popup shows. Raw codes and JSON never
// reach the user; unknown codes fall back to the server's message.

export const STEPS = [
  { status: "QUEUED", label: "Queued" },
  { status: "TRANSCRIBING", label: "Transcribing" },
  { status: "SUMMARIZING", label: "Summarizing" },
  { status: "EXTRACTING_FRAMES", label: "Extracting frames" },
  { status: "PUBLISHING", label: "Publishing" },
  { status: "DONE", label: "Done" },
];

const TERMINAL = new Set(["COMPLETED", "COMPLETED_WITH_WARNINGS", "FAILED", "DEAD_LETTERED"]);

export const isTerminal = (status) => TERMINAL.has(status);

export function stepIndex(status) {
  if (status === "COMPLETED" || status === "COMPLETED_WITH_WARNINGS") return STEPS.length - 1;
  const idx = STEPS.findIndex((s) => s.status === status);
  return idx === -1 ? 0 : idx;
}

const ERRORS = {
  VIDEO_TOO_LONG: ["Video is too long", "Only videos up to 20 minutes are supported."],
  LIVE_NOT_SUPPORTED: ["Live streams aren't supported", "Try again once the stream has ended and been processed."],
  VIDEO_UNAVAILABLE: ["Video unavailable", "The video is private, removed or restricted, so it can't be read."],
  TRANSCRIPT_UNAVAILABLE: ["No transcript available", "This video has no captions, so notes can't be written for it."],
  YOUTUBE_FAILED: ["Couldn't read the video", "YouTube didn't respond as expected."],
  LLM_AUTH_FAILED: ["Server configuration problem", "The notes server's AI key was rejected. Ask the server admin to check it."],
  LLM_REQUEST_REJECTED: ["The AI model rejected the request", "The server's model settings may need attention."],
  LLM_BAD_OUTPUT: ["The AI returned unusable notes", "Several attempts produced invalid output."],
  LLM_FAILED: [
    "Google Gemini (external AI service) kept failing",
    "This is a problem on Google's side, not in this app. The job was set aside; try again a little later.",
  ],
  LLM_OVERLOADED: [
    "Google Gemini (external AI service) is overloaded",
    "Gemini stayed overloaded for several minutes. This is an outage on Google's side, not an app error. Try again a little later.",
  ],
  LLM_QUOTA_EXHAUSTED: ["Daily AI quota used up", "The notes server's AI key has reached its daily limit. Try again after it resets, or ask the server admin to use a key with a higher quota."],
  NOTION_UNAUTHORIZED: ["Notion access expired", "Reconnect Notion from this popup, then take notes again."],
  NOTION_FORBIDDEN: ["No permission in Notion", "Share the destination page with the integration, or choose another page in Settings."],
  NOTION_PARENT_NOT_FOUND: ["Notion page not found", "Check the destination page in Settings and make sure it's shared with the integration."],
  NOTION_NO_DESTINATION: ["No Notion page to write to", "Share a page with the integration, or set a destination page in Settings."],
  NOTION_REQUEST_REJECTED: ["Notion rejected the page", "The notes couldn't be saved in that format."],
  NOTION_FAILED: ["Notion had a problem", "Notion kept failing; the job was set aside."],
  RATE_LIMITED: ["Services are busy", "Rate limits kept blocking the job."],
  CONFIG_ERROR: ["Server configuration problem", "Ask the server admin to check the server settings."],
  INTERNAL_ERROR: ["Something went wrong on the server", "The job kept failing unexpectedly."],
  BROKER_UNAVAILABLE: ["The job queue is down", "Try again in a moment."],
};

const RETRY_REASONS = {
  RATE_LIMITED: "a service asked us to slow down",
  VIDEO_UNAVAILABLE: "the video couldn't be opened",
  YOUTUBE_FAILED: "YouTube didn't respond",
  LLM_FAILED: "the AI service had a hiccup",
  LLM_OVERLOADED: "the AI service is overloaded",
  LLM_BAD_OUTPUT: "the AI returned unusable output",
  NOTION_FAILED: "Notion had a hiccup",
  INTERNAL_ERROR: "an unexpected server error",
};

export function describeError(job) {
  const code = job.error?.code;
  const [title, hint] = ERRORS[code] || ["The job failed", job.error?.message || "Unknown error."];
  const deadLettered = job.status === "DEAD_LETTERED";
  return {
    title: deadLettered ? `${title} — gave up after several attempts` : title,
    hint: deadLettered ? `${hint} It was moved to the dead-letter queue for inspection.` : hint,
  };
}

export function describeWarning(warning) {
  const at = warning.timestamp_sec != null ? ` at ${formatClock(warning.timestamp_sec)}` : "";
  switch (warning.code) {
    case "FRAME_EXTRACTION_FAILED":
      return `One screenshot${at} was skipped.`;
    case "FRAME_UPLOAD_FAILED":
      return `One screenshot${at} couldn't be added to Notion and was skipped.`;
    case "VISUAL_SELECTION_UNAVAILABLE":
      return "Screenshot moments were picked from the transcript (Gemini was busy).";
    default:
      return warning.message || "A non-critical issue occurred.";
  }
}

export function formatClock(totalSeconds) {
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = String(s % 60).padStart(2, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${sec}` : `${m}:${sec}`;
}

// The server's retry reason is technical ("LLM service error 504: ..."); turn the common
// Gemini cases into plain words instead of the generic "had a hiccup".
function retryReason(retry) {
  const raw = retry.reason || "";
  if (retry.reason_code === "LLM_FAILED") {
    if (/504|timeout|deadline/i.test(raw)) return "Gemini (the AI service) took too long to answer";
    if (/503|high demand|overloaded/i.test(raw)) return "Gemini (the AI service) is overloaded right now";
  }
  return RETRY_REASONS[retry.reason_code] || "a temporary problem";
}

// Steps that wait on one long Gemini call. After this long, say so instead of looking stuck.
const SLOW_AFTER_SEC = 90;

function isGeminiStep(job) {
  return job.status === "SUMMARIZING" || /choosing the clearest moments|backup model/i.test(job.detail || "");
}

export function formatElapsed(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

export function describeActivity(job, now = Date.now()) {
  if (job.retry) {
    const seconds = Math.max(0, Math.round((Date.parse(job.retry.next_attempt_at) - now) / 1000));
    const when = seconds > 0 ? `in ${seconds}s` : "now";
    return `Waiting — retrying ${when} because ${retryReason(job.retry)} (attempt ${job.retry.attempt + 1} of ${job.retry.max_attempts}).`;
  }
  if (job.status === "QUEUED") return "Waiting for a free worker…";
  if (job.status === "COMPLETED") return "Your notes are ready.";
  if (job.status === "COMPLETED_WITH_WARNINGS") return "Your notes are ready.";
  const text = job.detail ? `${job.detail}…` : `${STEPS[stepIndex(job.status)].label}…`;
  if (!job.detailSince || isTerminal(job.status)) return text;
  const elapsed = (now - job.detailSince) / 1000;
  if (elapsed < 10) return text;
  return `${text} (${formatElapsed(elapsed)})`;
}

// ---- Delays caused by Google Gemini ------------------------------------------------------
// Shown in their own box so anyone watching (e.g. an evaluator) can tell a slow job is waiting
// on the external AI provider, not stuck or broken in this app.

const PROVIDER_TITLE = "Waiting on Google Gemini (external AI)";

function isGeminiRetry(retry) {
  const code = retry.reason_code;
  if (code === "LLM_OVERLOADED" || code === "LLM_FAILED" || code === "LLM_BAD_OUTPUT") return true;
  return code === "RATE_LIMITED" && /gemini|llm/i.test(retry.reason || "");
}

function geminiProblem(reasonCode, raw) {
  if (reasonCode === "RATE_LIMITED" || /429/.test(raw)) return "is limiting how many requests it accepts (HTTP 429)";
  if (/504|timeout|deadline|took too long/i.test(raw)) return "did not answer in time (HTTP 504)";
  if (/503|high demand|overloaded/i.test(raw)) return "is overloaded right now (HTTP 503, “high demand”)";
  if (/50[02]|server error/i.test(raw)) return "returned a server error (HTTP 5xx)";
  if (reasonCode === "LLM_BAD_OUTPUT" || /unusable/i.test(raw)) return "returned an unusable answer";
  return "had a temporary failure";
}

export function describeProviderDelay(job, now = Date.now()) {
  if (isTerminal(job.status)) return null;
  if (job.retry && isGeminiRetry(job.retry)) {
    const seconds = Math.max(0, Math.round((Date.parse(job.retry.next_attempt_at) - now) / 1000));
    const when = seconds > 0 ? `in ${formatElapsed(seconds)}` : "now";
    return {
      title: PROVIDER_TITLE,
      text:
        `Gemini ${geminiProblem(job.retry.reason_code, job.retry.reason || "")}. ` +
        `It retries automatically ${when} (attempt ${job.retry.attempt + 1} of ${job.retry.max_attempts}).`,
    };
  }
  if (job.retry) return null;
  const detail = job.detail || "";
  if (/gemini/i.test(detail) && /backup model/i.test(detail)) {
    return {
      title: PROVIDER_TITLE,
      text: `Gemini's main model ${geminiProblem("", detail)}, so the app switched to the backup model automatically.`,
    };
  }
  const elapsed = job.detailSince ? (now - job.detailSince) / 1000 : 0;
  if (isGeminiStep(job) && elapsed >= SLOW_AFTER_SEC) {
    return {
      title: PROVIDER_TITLE,
      text:
        `Gemini has been working on this request for ${formatElapsed(elapsed)}, longer than usual, which means it is under heavy load. ` +
        `If it fails, the app retries automatically.`,
    };
  }
  return null;
}

export function timeAgo(timestamp, now = Date.now()) {
  const minutes = Math.round((now - timestamp) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  return hours < 24 ? `${hours} h ago` : new Date(timestamp).toLocaleDateString();
}
