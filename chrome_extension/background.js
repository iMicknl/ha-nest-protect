const browser = globalThis.browser ?? globalThis.chrome;
const isFirefox = typeof browser.runtime.getBrowserInfo === "function";

let capturedData = {
  issueToken: null,
  cookies: null,
  listening: false,
};

const debugLogs = [];
function log(msg) {
  const entry = `${new Date().toISOString().substring(11, 23)} ${msg}`;
  console.log("[nest-auth]", msg);
  debugLogs.push(entry);
  if (debugLogs.length > 200) debugLogs.shift();
}

const FIRST_PARTY_AUTH_COOKIES = new Set([
  "SID", "HSID", "SSID", "APISID", "SAPISID",
]);

function startListening() {
  capturedData = { issueToken: null, cookies: null, listening: true };

  if (!browser.webRequest.onBeforeRequest.hasListener(captureIssueToken)) {
    browser.webRequest.onBeforeRequest.addListener(captureIssueToken, {
      urls: ["https://accounts.google.com/o/oauth2/iframerpc*"],
      types: ["xmlhttprequest", "sub_frame", "main_frame"],
    });
  }

  if (!browser.webRequest.onSendHeaders.hasListener(captureRequestCookies)) {
    const extraOpts = isFirefox ? ["requestHeaders"] : ["requestHeaders", "extraHeaders"];
    browser.webRequest.onSendHeaders.addListener(
      captureRequestCookies,
      {
        urls: ["https://accounts.google.com/o/oauth2/iframerpc*"],
        types: ["xmlhttprequest", "sub_frame", "main_frame"],
      },
      extraOpts
    );
  }
}

function stopListening() {
  capturedData.listening = false;
  if (browser.webRequest.onBeforeRequest.hasListener(captureIssueToken)) {
    browser.webRequest.onBeforeRequest.removeListener(captureIssueToken);
  }
  if (browser.webRequest.onSendHeaders.hasListener(captureRequestCookies)) {
    browser.webRequest.onSendHeaders.removeListener(captureRequestCookies);
  }
}

function captureIssueToken(details) {
  log(`onBeforeRequest: ${details.url.substring(0, 80)}`);
  if (details.url.includes("action=issueToken")) {
    log("issueToken captured");
    capturedData.issueToken = details.url;
    checkComplete();
  }
}

function captureRequestCookies(details) {
  log(`onSendHeaders: ${details.url.substring(0, 80)}`);
  if (!details.url.includes("action=issueToken")) return;

  const cookieMap = new Map();

  const cookieHeader = details.requestHeaders.find(
    (h) => h.name.toLowerCase() === "cookie"
  );
  log(`cookie header found: ${!!cookieHeader} | headers: ${details.requestHeaders.map(h => h.name).join(", ")}`);

  if (cookieHeader) {
    cookieHeader.value.split("; ").forEach((pair) => {
      const eqIdx = pair.indexOf("=");
      if (eqIdx > 0) {
        cookieMap.set(pair.substring(0, eqIdx), pair.substring(eqIdx + 1));
      }
    });
    log(`cookies from header: ${cookieMap.size}`);
  }

  browser.cookies.getAll({ url: "https://accounts.google.com" }).then((cookies) => {
    log(`cookies.getAll: ${cookies?.length ?? 0} cookies [${cookies?.map(c => c.name).join(", ")}]`);
    if (cookies) {
      for (const c of cookies) {
        const relevant = cookieMap.size === 0
          ? true
          : FIRST_PARTY_AUTH_COOKIES.has(c.name) && !cookieMap.has(c.name);
        if (relevant) cookieMap.set(c.name, c.value);
      }
    }

    log(`final cookieMap: ${cookieMap.size} keys [${[...cookieMap.keys()].join(", ")}]`);
    if (cookieMap.size === 0) {
      log("WARNING: no cookies found, aborting");
      return;
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
    browser.action.setBadgeText({ text: "✓" });
    browser.action.setBadgeBackgroundColor({ color: "#4CAF50" });
  }
}

browser.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.action === "getLogs") {
    sendResponse({ logs: debugLogs.slice() });
    return false;
  }

  if (message.action === "startCapture") {
    log("startCapture received");
    startListening();
    browser.action.setBadgeText({ text: "..." });
    browser.action.setBadgeBackgroundColor({ color: "#FF9800" });

    browser.tabs.create({ url: "https://home.nest.com/" }).then(() => {
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
    browser.action.setBadgeText({ text: "" });
    sendResponse({ status: "reset" });
    return false;
  }
});
