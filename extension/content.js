// Content script for youtube.com: adds a "Take Notes" button to the player toolbar on watch
// pages. YouTube is a single-page app, so the button is (re)attached on every in-app
// navigation and the video id is read at click time, never cached from page load.
(() => {
  const BUTTON_ID = "vn-take-notes";
  const TOAST_ID = "vn-toast";
  const TOOLBAR_SELECTORS = ["#movie_player .ytp-right-controls", ".html5-video-player .ytp-right-controls"];

  const ICON_PATH =
    "M6 2h9l5 5v13a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2Zm8 1.5V8h4.5L14 3.5ZM7 11v1.6h10V11H7Zm0 3.4V16h10v-1.6H7Zm0 3.4v1.6h6.5v-1.6H7Z";

  // Built with DOM APIs rather than innerHTML: YouTube enforces Trusted Types.
  function createIcon() {
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", "20");
    svg.setAttribute("height", "20");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const path = document.createElementNS(ns, "path");
    path.setAttribute("fill", "currentColor");
    path.setAttribute("d", ICON_PATH);
    svg.appendChild(path);
    return svg;
  }

  function currentVideoId() {
    if (location.pathname !== "/watch") return null;
    const id = new URLSearchParams(location.search).get("v");
    return id && /^[\w-]{11}$/.test(id) ? id : null;
  }

  function currentTitle() {
    const heading = document.querySelector("ytd-watch-metadata h1 yt-formatted-string, h1.title");
    return (heading?.textContent || document.title.replace(/ - YouTube$/, "")).trim() || null;
  }

  function findToolbar() {
    for (const selector of TOOLBAR_SELECTORS) {
      const el = document.querySelector(selector);
      if (el) return el;
    }
    return null;
  }

  function showToast(message, kind = "info") {
    const player = document.querySelector("#movie_player") || document.body;
    let toast = document.getElementById(TOAST_ID);
    if (!toast) {
      toast = document.createElement("div");
      toast.id = TOAST_ID;
      toast.setAttribute("role", "status");
      player.appendChild(toast);
    }
    toast.textContent = message;
    toast.dataset.kind = kind;
    toast.classList.add("vn-visible");
    clearTimeout(showToast.timer);
    showToast.timer = setTimeout(() => toast.classList.remove("vn-visible"), kind === "error" ? 7000 : 4000);
  }

  function setButtonState(button, state, label) {
    button.dataset.state = state;
    button.disabled = state === "sending";
    button.querySelector(".vn-label").textContent = label;
  }

  async function onClick(event) {
    event.preventDefault();
    event.stopPropagation();
    const button = event.currentTarget;
    const videoId = currentVideoId();
    if (!videoId || button.disabled) return;

    setButtonState(button, "sending", "Sending…");
    let response;
    try {
      response = await chrome.runtime.sendMessage({
        type: "SUBMIT_JOB",
        videoId,
        title: currentTitle(),
        idempotencyKey: crypto.randomUUID(),
      });
    } catch {
      response = { ok: false, error: { message: "The extension was updated. Reload this page and try again." } };
    }

    if (response?.ok) {
      setButtonState(button, "done", "Queued ✓");
      showToast("Taking notes on this video — open the Video Notes icon to follow progress.");
    } else {
      setButtonState(button, "error", "Take Notes");
      showToast(response?.error?.message || "Couldn't start the notes job.", "error");
    }
    setTimeout(() => setButtonState(button, "idle", "Take Notes"), 2500);
  }

  function createButton() {
    const button = document.createElement("button");
    button.id = BUTTON_ID;
    button.type = "button";
    button.className = "vn-take-notes";
    button.title = "Take Notes — summarise this video into Notion";
    button.setAttribute("aria-label", "Take Notes");
    const label = document.createElement("span");
    label.className = "vn-label";
    label.textContent = "Take Notes";
    button.append(createIcon(), label);
    button.addEventListener("click", onClick);
    button.dataset.state = "idle";
    return button;
  }

  function sync() {
    const existing = document.getElementById(BUTTON_ID);
    if (!currentVideoId()) {
      existing?.remove();
      return;
    }
    const toolbar = findToolbar();
    if (!toolbar) return;
    if (existing && existing.parentElement === toolbar) return;
    toolbar.prepend(existing || createButton());
  }

  // In-app navigation events fired by YouTube, plus a DOM observer as a safety net for
  // player re-renders (theatre/fullscreen/miniplayer) that drop our button.
  document.addEventListener("yt-navigate-finish", sync);
  document.addEventListener("yt-page-data-updated", sync);
  window.addEventListener("popstate", sync);

  let scheduled = false;
  new MutationObserver(() => {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      sync();
    });
  }).observe(document.documentElement, { childList: true, subtree: true });

  sync();
})();
