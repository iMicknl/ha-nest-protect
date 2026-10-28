const browser = globalThis.browser ?? globalThis.chrome;

const extractBtn = document.getElementById("extract_btn");
const copyBtn = document.getElementById("copy_btn");
const statusArea = document.getElementById("status_area");
const outputArea = document.getElementById("output_area");
const authCodeField = document.getElementById("auth_code");
const debugToggle = document.getElementById("debug_toggle");
const debugArea = document.getElementById("debug_area");
const debugLog = document.getElementById("debug_log");
const debugDecoded = document.getElementById("debug_decoded");
const debugIssueToken = document.getElementById("debug_issue_token");
const debugCookies = document.getElementById("debug_cookies");

let debugVisible = false;
let debugInterval = null;

function updateDecodedFields() {
  if (!debugVisible || !authCodeField.value) {
    debugDecoded.style.display = "none";
    return;
  }
  try {
    const decoded = JSON.parse(atob(authCodeField.value));
    debugIssueToken.value = decoded.issue_token ?? "";
    debugCookies.value = decoded.cookies ?? "";
    debugDecoded.style.display = "block";
  } catch (e) {
    debugDecoded.style.display = "none";
  }
}

function refreshLogs() {
  browser.runtime.sendMessage({ action: "getLogs" }).then((r) => {
    if (r?.logs?.length) {
      debugLog.textContent = r.logs.join("\n");
      debugArea.scrollTop = debugArea.scrollHeight;
    }
  });
}

debugToggle.addEventListener("click", () => {
  debugVisible = !debugVisible;
  debugArea.style.display = debugVisible ? "block" : "none";
  debugToggle.textContent = (debugVisible ? "▼" : "▶") + " Debug logs";
  if (debugVisible) {
    refreshLogs();
    debugInterval = setInterval(refreshLogs, 500);
  } else {
    clearInterval(debugInterval);
    debugInterval = null;
  }
  updateDecodedFields();
});

let pollInterval = null;

function setStatus(msg, type) {
  statusArea.innerHTML = `<div class="status ${type}">${msg}</div>`;
}

function startPolling() {
  stopPolling();
  pollInterval = setInterval(() => {
    browser.runtime.sendMessage({ action: "getStatus" }).then((r) => {
      if (r && r.issueToken && r.cookies) {
        stopPolling();
        buildCode(r.issueToken, r.cookies);
      }
    });
  }, 500);
}

function stopPolling() {
  if (pollInterval) {
    clearInterval(pollInterval);
    pollInterval = null;
  }
}

function buildCode(issueToken, cookies) {
  const payload = JSON.stringify({
    issue_token: issueToken,
    cookies: cookies,
  });
  const code = btoa(payload);

  authCodeField.value = code;
  outputArea.style.display = "block";
  updateDecodedFields();
  extractBtn.disabled = false;
  extractBtn.textContent = "Extract Credentials";
  setStatus(
    "Credentials captured! Copy the code below and paste it into Home Assistant.",
    "success"
  );
}

// On popup open, check if already captured
browser.runtime.sendMessage({ action: "getStatus" }).then((r) => {
  if (r && r.issueToken && r.cookies) {
    buildCode(r.issueToken, r.cookies);
  } else if (r && r.listening) {
    extractBtn.disabled = true;
    extractBtn.textContent = "Waiting...";
    setStatus(
      "Waiting for credentials... Open or refresh <b>home.nest.com</b> and sign in.",
      "info"
    );
    startPolling();
  }
});

extractBtn.addEventListener("click", () => {
  extractBtn.disabled = true;
  extractBtn.textContent = "Waiting...";
  outputArea.style.display = "none";

  browser.runtime.sendMessage({ action: "reset" }).then(() => {
    return browser.runtime.sendMessage({ action: "startCapture" });
  }).then(() => {
    setStatus(
      "Opening home.nest.com... Sign in if needed. Credentials will be captured automatically.",
      "info"
    );
    startPolling();
  });
});

copyBtn.addEventListener("click", () => {
  navigator.clipboard.writeText(authCodeField.value).then(() => {
    copyBtn.textContent = "Copied!";
    setTimeout(() => {
      copyBtn.textContent = "Copy Code";
    }, 2000);
  });
});
