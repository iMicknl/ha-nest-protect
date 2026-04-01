// Storage for captured values
let capturedData = {
  issueToken: null,
  cookies: null,
  listening: false,
  tabId: null
};

// Google session/auth cookie names that provide long-lived authentication.
// These are the first-party cookies that Chrome won't send in cross-origin
// request headers but which the Nest API needs for durable sessions.
const FIRST_PARTY_AUTH_COOKIES = new Set([
  "SID", "HSID", "SSID", "APISID", "SAPISID",
]);

// Start listening for network requests
function startListening(tabId) {
  capturedData = {
    issueToken: null,
    cookies: null,
    listening: true,
    tabId: tabId
  };

  // Listen for iframerpc requests — this fires after sign-in is complete.
  // We capture the issue_token URL from onBeforeRequest and the cookies
  // Chrome actually sends from onSendHeaders (proven to work for auth).
  if (!chrome.webRequest.onBeforeRequest.hasListener(captureIssueToken)) {
    chrome.webRequest.onBeforeRequest.addListener(
      captureIssueToken,
      { urls: ["https://accounts.google.com/o/oauth2/iframerpc*"], types: ["xmlhttprequest", "sub_frame", "main_frame"] }
    );
  }

  if (!chrome.webRequest.onSendHeaders.hasListener(captureRequestCookies)) {
    chrome.webRequest.onSendHeaders.addListener(
      captureRequestCookies,
      { urls: ["https://accounts.google.com/o/oauth2/iframerpc*"], types: ["xmlhttprequest", "sub_frame", "main_frame"] },
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
  // Capture the cookies Chrome actually sends with the iframerpc request.
  // In a cross-origin context these are the __Secure-3P* cookies — enough
  // for initial auth but short-lived. We then supplement with long-lived
  // first-party cookies from the chrome.cookies API.
  const cookieHeader = details.requestHeaders.find(
    h => h.name.toLowerCase() === "cookie"
  );
  if (!cookieHeader) return;

  const requestCookies = cookieHeader.value;

  // Parse the request cookies into a map
  const cookieMap = new Map();
  requestCookies.split("; ").forEach(pair => {
    const eqIdx = pair.indexOf("=");
    if (eqIdx > 0) {
      cookieMap.set(pair.substring(0, eqIdx), pair.substring(eqIdx + 1));
    }
  });

  // Supplement with first-party auth cookies from the cookies API.
  // These (SID, HSID, SSID, APISID, SAPISID) are long-lived and survive
  // restarts, but Chrome doesn't send them in cross-origin headers.
  chrome.cookies.getAll({ url: "https://accounts.google.com" }, (cookies) => {
    if (cookies) {
      for (const c of cookies) {
        if (FIRST_PARTY_AUTH_COOKIES.has(c.name) && !cookieMap.has(c.name)) {
          cookieMap.set(c.name, c.value);
        }
      }
    }

    // Build final cookie string
    capturedData.cookies = Array.from(cookieMap.entries())
      .map(([name, value]) => `${name}=${value}`)
      .join("; ");

    checkComplete();
  });
}

function checkComplete() {
  if (capturedData.issueToken && capturedData.cookies) {
    // Flash the badge to indicate both values captured
    chrome.action.setBadgeText({ text: "✓" });
    chrome.action.setBadgeBackgroundColor({ color: "#4CAF50" });
  }
}

// Handle messages from popup
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === "startCapture") {
    // Open home.nest.com in a new tab and start listening
    chrome.tabs.create({ url: "https://home.nest.com/" }, (tab) => {
      startListening(tab.id);
      chrome.action.setBadgeText({ text: "..." });
      chrome.action.setBadgeBackgroundColor({ color: "#FF9800" });
      sendResponse({ status: "started", tabId: tab.id });
    });
    return true; // async response
  }

  if (message.action === "getStatus") {
    sendResponse({
      issueToken: capturedData.issueToken,
      cookies: capturedData.cookies,
      listening: capturedData.listening
    });
    return false;
  }

  if (message.action === "stopCapture") {
    stopListening();
    chrome.action.setBadgeText({ text: "" });
    sendResponse({ status: "stopped" });
    return false;
  }

  if (message.action === "reset") {
    stopListening();
    capturedData = {
      issueToken: null,
      cookies: null,
      listening: false,
      tabId: null
    };
    chrome.action.setBadgeText({ text: "" });
    sendResponse({ status: "reset" });
    return false;
  }
});
