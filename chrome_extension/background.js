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
}

function stopListening() {
  capturedData.listening = false;
  if (chrome.webRequest.onBeforeRequest.hasListener(captureIssueToken)) {
    chrome.webRequest.onBeforeRequest.removeListener(captureIssueToken);
  }
  if (chrome.webRequest.onSendHeaders.hasListener(captureRequestCookies)) {
    chrome.webRequest.onSendHeaders.removeListener(captureRequestCookies);
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

  chrome.cookies.getAll({ url: "https://accounts.google.com" }, (cookies) => {
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
    capturedData = { issueToken: null, cookies: null, listening: false };
    chrome.action.setBadgeText({ text: "" });
    sendResponse({ status: "reset" });
    return false;
  }
});
