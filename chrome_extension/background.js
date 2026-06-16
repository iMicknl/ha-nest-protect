let capturedData = {
  issueToken: null,
  cookies: null,
  iframeCookies: null,
  listening: false,
};

const FIRST_PARTY_AUTH_COOKIES = new Set([
  "SID", "HSID", "SSID", "APISID", "SAPISID",
]);

function startListening() {
  capturedData = {
    issueToken: null,
    cookies: null,
    iframeCookies: null,
    listening: true,
  };

  if (!chrome.webRequest.onBeforeRequest.hasListener(captureIssueToken)) {
    chrome.webRequest.onBeforeRequest.addListener(captureIssueToken, {
      urls: ["https://accounts.google.com/o/oauth2/iframerpc*"],
      types: ["xmlhttprequest", "sub_frame", "main_frame"],
    });
  }

  if (!chrome.webRequest.onSendHeaders.hasListener(captureRequestCookies)) {
    chrome.webRequest.onSendHeaders.addListener(
      captureRequestCookies,
      {
        urls: ["https://accounts.google.com/o/oauth2/iframerpc*"],
        types: ["xmlhttprequest", "sub_frame", "main_frame"],
      },
      ["requestHeaders", "extraHeaders"]
    );
  }

  if (!chrome.webRequest.onSendHeaders.hasListener(captureIframeCookies)) {
    chrome.webRequest.onSendHeaders.addListener(
      captureIframeCookies,
      {
        urls: ["https://accounts.google.com/o/oauth2/iframe*"],
        types: ["xmlhttprequest", "sub_frame", "main_frame"],
      },
      ["requestHeaders", "extraHeaders"]
    );
  }
}

function stopListening() {
  capturedData.listening = false;
  if (chrome.webRequest.onBeforeRequest.hasListener(captureIssueToken)) {
    chrome.webRequest.onBeforeRequest.removeListener(captureIssueToken);
  }
  if (chrome.webRequest.onSendHeaders.hasListener(captureRequestCookies)) {
    chrome.webRequest.onSendHeaders.removeListener(captureRequestCookies);
  }
  if (chrome.webRequest.onSendHeaders.hasListener(captureIframeCookies)) {
    chrome.webRequest.onSendHeaders.removeListener(captureIframeCookies);
  }
}

function captureIssueToken(details) {
  if (details.url.includes("action=issueToken")) {
    capturedData.issueToken = details.url;
    checkComplete();
  }
}

function parseCookieHeader(cookieHeader) {
  const cookieMap = new Map();
  cookieHeader.split("; ").forEach((pair) => {
    const eqIdx = pair.indexOf("=");
    if (eqIdx > 0) {
      cookieMap.set(pair.substring(0, eqIdx), pair.substring(eqIdx + 1));
    }
  });
  return cookieMap;
}

function mergeCookieMaps(baseMap, extraCookies) {
  if (extraCookies) {
    for (const c of extraCookies) {
      if (FIRST_PARTY_AUTH_COOKIES.has(c.name) && !baseMap.has(c.name)) {
        baseMap.set(c.name, c.value);
      }
    }
  }
  return baseMap;
}

function serializeCookieMap(cookieMap) {
  return Array.from(cookieMap.entries())
    .map(([name, value]) => `${name}=${value}`)
    .join("; ");
}

function captureIframeCookies(details) {
  const cookieHeader = details.requestHeaders.find(
    (h) => h.name.toLowerCase() === "cookie"
  );
  if (!cookieHeader?.value) return;

  // Prefer the fullest oauth2/iframe cookie header (homebridge-nest guidance).
  if (
    !capturedData.iframeCookies ||
    cookieHeader.value.length > capturedData.iframeCookies.length
  ) {
    capturedData.iframeCookies = cookieHeader.value;
    capturedData.cookies = cookieHeader.value;
    checkComplete();
  }
}

function captureRequestCookies(details) {
  if (!details.url.includes("action=issueToken")) return;

  const cookieHeader = details.requestHeaders.find(
    (h) => h.name.toLowerCase() === "cookie"
  );
  if (!cookieHeader) return;

  const cookieMap = parseCookieHeader(cookieHeader.value);

  chrome.cookies.getAll({ url: "https://accounts.google.com" }, (cookies) => {
    mergeCookieMaps(cookieMap, cookies);

    const issueTokenCookies = serializeCookieMap(cookieMap);

    // Prefer oauth2/iframe cookies when they are fuller than issueToken cookies.
    if (
      !capturedData.iframeCookies ||
      issueTokenCookies.length > capturedData.iframeCookies.length
    ) {
      capturedData.cookies = issueTokenCookies;
    }

    checkComplete();
  });
}

function checkComplete() {
  if (capturedData.issueToken && capturedData.cookies) {
    stopListening();
    chrome.action.setBadgeText({ text: "✓" });
    chrome.action.setBadgeBackgroundColor({ color: "#4CAF50" });
    // Open the popup by programmatically triggering the action
    chrome.action.openPopup();
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === "startCapture") {
    startListening();
    chrome.action.setBadgeText({ text: "..." });
    chrome.action.setBadgeBackgroundColor({ color: "#FF9800" });

    // Always open a fresh tab to ensure the OAuth iframe fires
    chrome.tabs.create({ url: "https://home.nest.com/" }, () => {
      sendResponse({ status: "started" });
    });
    return true;
  }

  if (message.action === "getStatus") {
    sendResponse(capturedData);
    return false;
  }

  if (message.action === "reset") {
    stopListening();
    capturedData = {
      issueToken: null,
      cookies: null,
      iframeCookies: null,
      listening: false,
    };
    chrome.action.setBadgeText({ text: "" });
    sendResponse({ status: "reset" });
    return false;
  }
});
