// Firefox uses the `browser` namespace (Promise-based). For compatibility with
// both Chrome and Firefox, we alias `chrome` if `browser` is available.
const api = typeof browser !== "undefined" ? browser : chrome;

let capturedData = {
  issueToken: null,
  cookies: null,
  listening: false,
};

const FIRST_PARTY_AUTH_COOKIES = new Set([
  "SID", "HSID", "SSID", "APISID", "SAPISID",
]);

function startListening() {
  capturedData = { issueToken: null, cookies: null, listening: true };

  if (!api.webRequest.onBeforeRequest.hasListener(captureIssueToken)) {
    api.webRequest.onBeforeRequest.addListener(captureIssueToken, {
      urls: ["https://accounts.google.com/o/oauth2/iframerpc*"],
      types: ["xmlhttprequest", "sub_frame", "main_frame"],
    });
  }

  if (!api.webRequest.onSendHeaders.hasListener(captureRequestCookies)) {
    // NOTE: Firefox supports "requestHeaders" in onSendHeaders.
    // "extraHeaders" is a Chrome-only hint and must be omitted in Firefox MV2,
    // otherwise the listener registration throws. Firefox always exposes
    // cookie headers in webRequest without the extra flag.
    api.webRequest.onSendHeaders.addListener(
      captureRequestCookies,
      {
        urls: ["https://accounts.google.com/o/oauth2/iframerpc*"],
        types: ["xmlhttprequest", "sub_frame", "main_frame"],
      },
      ["requestHeaders"]
    );
  }
}

function stopListening() {
  capturedData.listening = false;
  if (api.webRequest.onBeforeRequest.hasListener(captureIssueToken)) {
    api.webRequest.onBeforeRequest.removeListener(captureIssueToken);
  }
  if (api.webRequest.onSendHeaders.hasListener(captureRequestCookies)) {
    api.webRequest.onSendHeaders.removeListener(captureRequestCookies);
  }
}

function captureIssueToken(details) {
  if (details.url.includes("action=issueToken")) {
    capturedData.issueToken = details.url;
    checkComplete();
  }
}

function captureRequestCookies(details) {
  if (!details.url.includes("action=issueToken")) return;

  const cookieHeader = details.requestHeaders.find(
    (h) => h.name.toLowerCase() === "cookie"
  );
  if (!cookieHeader) return;

  const cookieMap = new Map();
  cookieHeader.value.split("; ").forEach((pair) => {
    const eqIdx = pair.indexOf("=");
    if (eqIdx > 0) {
      cookieMap.set(pair.substring(0, eqIdx), pair.substring(eqIdx + 1));
    }
  });

  // browser.cookies.getAll returns a Promise in Firefox
  api.cookies.getAll({ url: "https://accounts.google.com" }).then((cookies) => {
    if (cookies) {
      for (const c of cookies) {
        if (FIRST_PARTY_AUTH_COOKIES.has(c.name) && !cookieMap.has(c.name)) {
          cookieMap.set(c.name, c.value);
        }
      }
    }

    capturedData.cookies = Array.from(cookieMap.entries())
      .map(([name, value]) => `${name}=${value}`)
      .join("; ");

    checkComplete();
  });
}

function checkComplete() {
  if (capturedData.issueToken && capturedData.cookies) {
    stopListening();
    // browser_action badge API (MV2)
    api.browserAction.setBadgeText({ text: "✓" });
    api.browserAction.setBadgeBackgroundColor({ color: "#4CAF50" });
    // NOTE: browser.browserAction.openPopup() is NOT supported in Firefox.
    // The user will see the badge change and can click the toolbar button
    // manually to open the popup and retrieve their credentials.
  }
}

api.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === "startCapture") {
    startListening();
    api.browserAction.setBadgeText({ text: "..." });
    api.browserAction.setBadgeBackgroundColor({ color: "#FF9800" });

    // Always open a fresh tab to ensure the OAuth iframe fires
    api.tabs.create({ url: "https://home.nest.com/" }).then(() => {
      sendResponse({ status: "started" });
    });
    return true; // keep message channel open for async sendResponse
  }

  if (message.action === "getStatus") {
    sendResponse(capturedData);
    return false;
  }

  if (message.action === "reset") {
    stopListening();
    capturedData = { issueToken: null, cookies: null, listening: false };
    api.browserAction.setBadgeText({ text: "" });
    sendResponse({ status: "reset" });
    return false;
  }
});
