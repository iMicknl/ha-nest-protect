const browser = globalThis.browser ?? globalThis.chrome;
const isFirefox = typeof browser.runtime.getBrowserInfo === "function";

let capturedData = {
  issueToken: null,
  cookies: null,
  iframeCookies: null,
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

// Used when the Cookie header is unavailable, so only Google sign-in cookies
// are exported instead of every accounts.google.com cookie.
const GOOGLE_SESSION_COOKIES = new Set([
  ...FIRST_PARTY_AUTH_COOKIES,
  "SIDCC", "NID", "OSID", "__Secure-OSID", "LSID", "__Host-GAPS",
  "ACCOUNT_CHOOSER", "SMSV",
]);
// Matches the __Secure-/__Host- 1P/3P variants (PSID, PSIDTS, PSIDCC, PSIDRTS,
// PAPISID, PLSID); Google adds to this family over time.
const GOOGLE_SESSION_COOKIE_PATTERN = /^__(Secure|Host)-\dP(SID|APISID|LSID)/;

function isGoogleSessionCookie(name) {
  return GOOGLE_SESSION_COOKIES.has(name) || GOOGLE_SESSION_COOKIE_PATTERN.test(name);
}

function startListening() {
  capturedData = {
    issueToken: null,
    cookies: null,
    iframeCookies: null,
    listening: true,
  };

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

  if (!browser.webRequest.onSendHeaders.hasListener(captureIframeCookies)) {
    const extraOpts = isFirefox ? ["requestHeaders"] : ["requestHeaders", "extraHeaders"];
    browser.webRequest.onSendHeaders.addListener(
      captureIframeCookies,
      {
        urls: ["https://accounts.google.com/o/oauth2/iframe*"],
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
  if (browser.webRequest.onSendHeaders.hasListener(captureIframeCookies)) {
    browser.webRequest.onSendHeaders.removeListener(captureIframeCookies);
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
  log(`onSendHeaders: ${details.url.substring(0, 80)}`);
  if (!details.url.includes("action=issueToken")) return;

  const cookieHeader = details.requestHeaders.find(
    (h) => h.name.toLowerCase() === "cookie"
  );
  log(`cookie header found: ${!!cookieHeader} | headers: ${details.requestHeaders.map(h => h.name).join(", ")}`);

  const cookieMap = cookieHeader ? parseCookieHeader(cookieHeader.value) : new Map();
  if (cookieHeader) {
    log(`cookies from header: ${cookieMap.size}`);
  }

  browser.cookies.getAll({ url: "https://accounts.google.com" }).then((cookies) => {
    log(`cookies.getAll: ${cookies?.length ?? 0} cookies [${cookies?.map(c => c.name).join(", ")}]`);
    if (cookies) {
      const fromHeader = cookieMap.size > 0;
      for (const c of cookies) {
        if (cookieMap.has(c.name)) continue;
        const relevant = fromHeader
          ? FIRST_PARTY_AUTH_COOKIES.has(c.name)
          : isGoogleSessionCookie(c.name);
        if (relevant) cookieMap.set(c.name, c.value);
      }
    }

    log(`final cookieMap: ${cookieMap.size} keys [${[...cookieMap.keys()].join(", ")}]`);
    if (cookieMap.size === 0) {
      log("WARNING: no cookies found, aborting");
      return;
    }

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
    browser.action.setBadgeText({ text: "✓" });
    browser.action.setBadgeBackgroundColor({ color: "#4CAF50" });
    // Firefox (before 149) only allows openPopup() from a user gesture, and
    // this runs from a webRequest callback, so rely on the badge there.
    if (!isFirefox) {
      browser.action.openPopup();
    }
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
    capturedData = {
      issueToken: null,
      cookies: null,
      iframeCookies: null,
      listening: false,
    };
    browser.action.setBadgeText({ text: "" });
    sendResponse({ status: "reset" });
    return false;
  }
});
