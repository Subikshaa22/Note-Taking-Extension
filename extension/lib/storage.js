// Persistent extension state, kept in chrome.storage.local (never synced: it holds a token).
//   settings    { gatewayUrl, notionParentId }
//   notionAuth  { accessToken, method: "oauth" | "manual", workspaceName, workspaceIcon, connectedAt }
//   jobs        tracked jobs, newest first (see lib/jobs.js)

export const DEFAULT_SETTINGS = Object.freeze({
  gatewayUrl: "http://localhost:8000",
  notionParentId: "",
});

export async function getSettings() {
  const { settings } = await chrome.storage.local.get("settings");
  return { ...DEFAULT_SETTINGS, ...(settings || {}) };
}

export async function saveSettings(patch) {
  const next = { ...(await getSettings()), ...patch };
  next.gatewayUrl = next.gatewayUrl.trim().replace(/\/+$/, "") || DEFAULT_SETTINGS.gatewayUrl;
  next.notionParentId = next.notionParentId.trim();
  await chrome.storage.local.set({ settings: next });
  return next;
}

export async function getNotionAuth() {
  const { notionAuth } = await chrome.storage.local.get("notionAuth");
  return notionAuth || null;
}

export async function saveNotionAuth(auth) {
  await chrome.storage.local.set({ notionAuth: { ...auth, connectedAt: Date.now() } });
}

export async function clearNotionAuth() {
  await chrome.storage.local.remove("notionAuth");
}
