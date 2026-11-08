// Locally tracked jobs (newest first). The background script is the only writer; the popup
// renders from storage and reacts to chrome.storage.onChanged.

import { isTerminal } from "./format.js";

const MAX_TRACKED = 25;

// Read-modify-write cycles are serialised so a poll result and a new submission arriving at
// the same time cannot overwrite each other.
let queue = Promise.resolve();

function mutate(change) {
  const run = queue.then(async () => {
    const { jobs } = await chrome.storage.local.get("jobs");
    await chrome.storage.local.set({ jobs: change(jobs || []).slice(0, MAX_TRACKED) });
  });
  queue = run.catch(() => {});
  return run;
}

export async function listJobs() {
  const { jobs } = await chrome.storage.local.get("jobs");
  return jobs || [];
}

export const addJob = (job) => mutate((jobs) => [job, ...jobs.filter((j) => j.jobId !== job.jobId)]);

export const updateJob = (jobId, patch) =>
  mutate((jobs) => jobs.map((j) => (j.jobId === jobId ? { ...j, ...patch } : j)));

export const removeJob = (jobId) => mutate((jobs) => jobs.filter((j) => j.jobId !== jobId));

export const clearFinished = () => mutate((jobs) => jobs.filter((j) => !isTerminal(j.status)));

export async function activeJobs() {
  return (await listJobs()).filter((j) => !isTerminal(j.status));
}
