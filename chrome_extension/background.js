let capturedData = {
  issueToken: null,
  cookies: null,
  listening: false,
};

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

  capturedData.cookies = cookieHeader.value;
  checkComplete();
}

function checkComplete() {
  if (capturedData.issueToken && capturedData.cookies) {
    chrome.action.setBadgeText({ text: "✓" });
    chrome.action.setBadgeBackgroundColor({ color: "#4CAF50" });
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === "startCapture") {
    startListening();
    chrome.action.setBadgeText({ text: "..." });
    chrome.action.setBadgeBackgroundColor({ color: "#FF9800" });
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
