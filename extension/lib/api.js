// Thin client for the gateway's REST API. Errors are normalised to ApiError so callers can
// show a human-readable message instead of a raw response body.

export class ApiError extends Error {
  constructor(code, message, status = 0) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

async function request(baseUrl, path, { method = "GET", body, headers = {} } = {}) {
  let response;
  try {
    response = await fetch(`${baseUrl}${path}`, {
      method,
      headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...headers },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(
      "GATEWAY_UNREACHABLE",
      `Can't reach the notes server at ${baseUrl}. Is it running? (Check the address in Settings.)`,
    );
  }
  let data = null;
  try {
    data = await response.json();
  } catch {
    // Non-JSON body (e.g. a proxy error page); handled below.
  }
  if (!response.ok) {
    const error = data?.error;
    throw new ApiError(
      error?.code || `HTTP_${response.status}`,
      error?.message || `The notes server answered with an error (${response.status}).`,
      response.status,
    );
  }
  return data;
}

export const submitJob = (baseUrl, payload, idempotencyKey) =>
  request(baseUrl, "/jobs", {
    method: "POST",
    body: payload,
    headers: idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {},
  });

export const getJob = (baseUrl, jobId) => request(baseUrl, `/jobs/${encodeURIComponent(jobId)}`);

export const getResults = (baseUrl, jobId) => request(baseUrl, `/jobs/${encodeURIComponent(jobId)}/results`);

export const getHealth = (baseUrl) => request(baseUrl, "/healthz");

export const getOAuthConfig = (baseUrl) => request(baseUrl, "/oauth/notion/config");

export const exchangeNotionCode = (baseUrl, code, redirectUri) =>
  request(baseUrl, "/oauth/notion/token", { method: "POST", body: { code, redirect_uri: redirectUri } });
