// Setup guide: step-by-step instructions plus a live checklist of what is already done.

import { getHealth, getOAuthConfig } from "./lib/api.js";
import { oauthRedirectUri } from "./lib/notion-auth.js";
import { getNotionAuth, getSettings } from "./lib/storage.js";

const $ = (selector) => document.querySelector(selector);

function setCheck(id, state, detail) {
  const item = $(`#${id}`);
  item.dataset.state = state;
  item.querySelector(".check-detail").textContent = detail;
}

async function checkServer(gatewayUrl) {
  try {
    await getHealth(gatewayUrl);
    setCheck("check-server", "ok", `Running at ${gatewayUrl}.`);
    return true;
  } catch (error) {
    const detail =
      error.status === 503
        ? "The server is starting or one of its parts is down. Wait a moment and check again."
        : `Not reachable at ${gatewayUrl}. Start it with step 1.`;
    setCheck("check-server", "bad", detail);
    return false;
  }
}

async function checkOAuth(gatewayUrl, serverUp) {
  const note = $("#oauth-state");
  if (!serverUp) {
    note.textContent = "Start the server first to see whether one-click sign-in is set up.";
    return;
  }
  try {
    const config = await getOAuthConfig(gatewayUrl);
    note.textContent = config.enabled
      ? "One-click sign-in is set up on your server. Use Connect Notion in the popup."
      : "Not set up on your server yet. Follow these steps, or use Option A instead.";
  } catch {
    note.textContent = "";
  }
}

async function refresh() {
  for (const id of ["check-server", "check-notion", "check-destination"]) setCheck(id, "pending", "Checking…");
  const settings = await getSettings();
  const auth = await getNotionAuth();

  setCheck(
    "check-notion",
    auth?.accessToken ? "ok" : "bad",
    auth?.accessToken
      ? auth.method === "oauth"
        ? `Signed in to ${auth.workspaceName}.`
        : "Using your integration token."
      : "Not connected yet. Follow step 2.",
  );
  setCheck(
    "check-destination",
    settings.notionParentId ? "ok" : "optional",
    settings.notionParentId
      ? "Notes go inside the page you chose in Settings."
      : "Optional. Not set, so notes go to the page you shared with the integration.",
  );

  const serverUp = await checkServer(settings.gatewayUrl);
  await checkOAuth(settings.gatewayUrl, serverUp);
}

for (const button of document.querySelectorAll(".copy")) {
  button.addEventListener("click", async () => {
    const text = button.previousElementSibling.textContent.trim();
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "Copied";
    } catch {
      button.textContent = "Select and copy";
    }
    setTimeout(() => (button.textContent = "Copy"), 1500);
  });
}

$("#redirect-uri").textContent = oauthRedirectUri();
$("#recheck").addEventListener("click", refresh);
chrome.storage.onChanged.addListener((_changes, area) => {
  if (area === "local") refresh();
});
// Re-check when the user comes back from a terminal or Notion.
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refresh();
});

refresh();
