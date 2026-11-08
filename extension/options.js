// Settings page: gateway address, default Notion destination and a manual integration token
// for servers without an OAuth app.

import { getHealth } from "./lib/api.js";
import { oauthRedirectUri } from "./lib/notion-auth.js";
import { getNotionAuth, getSettings, saveNotionAuth, saveSettings } from "./lib/storage.js";

const $ = (selector) => document.querySelector(selector);

function showResult(node, ok, message) {
  node.className = `result ${ok ? "ok" : "bad"}`;
  node.textContent = message;
}

function parseGatewayUrl(raw) {
  try {
    const url = new URL(raw.trim());
    if (!["http:", "https:"].includes(url.protocol)) return null;
    return url.origin;
  } catch {
    return null;
  }
}

// localhost is covered by the manifest; any other server needs a one-time grant.
async function ensureHostPermission(origin) {
  const host = new URL(origin).hostname;
  if (host === "localhost" || host === "127.0.0.1") return true;
  return chrome.permissions.request({ origins: [`${origin}/*`] });
}

async function renderAuthState() {
  const auth = await getNotionAuth();
  $("#auth-state").textContent = auth?.accessToken
    ? `Connected (${auth.method === "oauth" ? `signed in to ${auth.workspaceName}` : "integration token"}).`
    : "Not connected yet.";
}

async function init() {
  const settings = await getSettings();
  $("#gateway-url").value = settings.gatewayUrl;
  $("#parent-id").value = settings.notionParentId;
  $("#redirect-uri").textContent = oauthRedirectUri();
  renderAuthState();
}

$("#settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const origin = parseGatewayUrl($("#gateway-url").value);
  if (!origin) {
    showResult($("#save-result"), false, "Enter a valid http(s) address.");
    return;
  }
  if (!(await ensureHostPermission(origin))) {
    showResult($("#save-result"), false, "Permission to contact that server was not granted.");
    return;
  }
  await saveSettings({ gatewayUrl: origin, notionParentId: $("#parent-id").value });
  showResult($("#save-result"), true, "Saved.");
});

$("#test-connection").addEventListener("click", async () => {
  const origin = parseGatewayUrl($("#gateway-url").value);
  const result = $("#connection-result");
  if (!origin) {
    showResult(result, false, "Enter a valid http(s) address.");
    return;
  }
  if (!(await ensureHostPermission(origin))) {
    showResult(result, false, "Permission to contact that server was not granted.");
    return;
  }
  showResult(result, true, "Checking…");
  try {
    await getHealth(origin);
    showResult(result, true, "Connected: gateway, queue and state store are all reachable.");
  } catch (error) {
    showResult(result, false, error.status === 503 ? "The server is up but its queue or state store is not ready." : error.message);
  }
});

$("#token-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = $("#manual-token").value.trim();
  if (token.length < 20) {
    showResult($("#token-result"), false, "That doesn't look like a Notion integration secret.");
    return;
  }
  await saveNotionAuth({ accessToken: token, method: "manual", workspaceName: "Notion (integration token)" });
  $("#manual-token").value = "";
  showResult($("#token-result"), true, "Saved. Take Notes will now publish with this integration.");
  renderAuthState();
});

init();
