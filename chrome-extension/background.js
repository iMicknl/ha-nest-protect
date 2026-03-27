// Storage for captured values
let capturedData = {
  issueToken: null,
  cookies: null,
  listening: false,
  tabId: null
};

// Start listening for network requests
function startListening(tabId) {
  capturedData = {
    issueToken: null,
    cookies: null,
    listening: true,
    tabId: tabId
  };

  // Listen for iframerpc requests to capture the issue_token (full request URL)
  if (!chrome.webRequest.onBeforeRequest.hasListener(captureIssueToken)) {
    chrome.webRequest.onBeforeRequest.addListener(
      captureIssueToken,
      { urls: ["https://accounts.google.com/*"], types: ["xmlhttprequest", "sub_frame", "main_frame"] }
    );
  }

  // Listen for oauth2/iframe requests to capture cookies from request headers
  if (!chrome.webRequest.onSendHeaders.hasListener(captureCookies)) {
    chrome.webRequest.onSendHeaders.addListener(
      captureCookies,
      { urls: ["https://accounts.google.com/*"], types: ["xmlhttprequest", "sub_frame", "main_frame"] },
      ["requestHeaders", "extraHeaders"]
    );
  }
}

function stopListening() {
  capturedData.listening = false;
  if (chrome.webRequest.onBeforeRequest.hasListener(captureIssueToken)) {
    chrome.webRequest.onBeforeRequest.removeListener(captureIssueToken);
  }
  if (chrome.webRequest.onSendHeaders.hasListener(captureCookies)) {
    chrome.webRequest.onSendHeaders.removeListener(captureCookies);
  }
}

function captureIssueToken(details) {
  // Look for the iframerpc call that contains the issue token
  if (details.url.includes("iframerpc")) {
    capturedData.issueToken = details.url;
    checkComplete();
  }
}

function captureCookies(details) {
  // Look for oauth2/iframe requests and keep the last one's cookies
  if (details.url.includes("oauth2/iframe")) {
    const cookieHeader = details.requestHeaders.find(
      h => h.name.toLowerCase() === "cookie"
    );
    if (cookieHeader) {
      capturedData.cookies = cookieHeader.value;
    }
    checkComplete();
  }
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
