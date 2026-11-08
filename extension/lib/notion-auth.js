// One-time Notion OAuth flow. The browser handles Notion's consent screen through
// chrome.identity; the gateway swaps the code for a token (it holds the client secret) and
// the token is then stored locally so the user is not asked again on every job.

import { ApiError, exchangeNotionCode, getOAuthConfig } from "./api.js";
import { getSettings, saveNotionAuth } from "./storage.js";

export function oauthRedirectUri() {
  return chrome.identity.getRedirectURL("notion");
}

export async function connectNotion() {
  const { gatewayUrl } = await getSettings();
  const config = await getOAuthConfig(gatewayUrl);
  if (!config.enabled) {
    throw new ApiError(
      "OAUTH_NOT_CONFIGURED",
      "Notion sign-in isn't set up on the server. Paste an integration token in Settings instead.",
    );
  }

  const redirectUri = oauthRedirectUri();
  const state = crypto.randomUUID();
  const authorizeUrl = new URL(config.authorize_url);
  authorizeUrl.search = new URLSearchParams({
    client_id: config.client_id,
    response_type: "code",
    owner: "user",
    redirect_uri: redirectUri,
    state,
  }).toString();

  let responseUrl;
  try {
    responseUrl = await chrome.identity.launchWebAuthFlow({ url: authorizeUrl.toString(), interactive: true });
  } catch {
    throw new ApiError("OAUTH_CANCELLED", "Notion sign-in was cancelled.");
  }
  const params = new URL(responseUrl).searchParams;
  if (params.get("error")) {
    throw new ApiError("OAUTH_DENIED", "Notion access was not granted.");
  }
  if (params.get("state") !== state || !params.get("code")) {
    throw new ApiError("OAUTH_STATE_MISMATCH", "The Notion sign-in response was not valid. Please try again.");
  }

  const token = await exchangeNotionCode(gatewayUrl, params.get("code"), redirectUri);
  await saveNotionAuth({
    accessToken: token.access_token,
    method: "oauth",
    workspaceName: token.workspace_name || "Notion workspace",
    workspaceIcon: token.workspace_icon || null,
  });
  return { workspaceName: token.workspace_name };
}
