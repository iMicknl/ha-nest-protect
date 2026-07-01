// Firefox uses the `browser` namespace (Promise-based). For compatibility with
// both Chrome and Firefox, we alias `chrome` if `browser` is available.
const api = typeof browser !== "undefined" ? browser : chrome;

const extractBtn = document.getElementById("extract_btn");
const copyBtn = document.getElementById("copy_btn");
const statusArea = document.getElementById("status_area");
const outputArea = document.getElementById("output_area");
const authCodeField = document.getElementById("auth_code");

let pollInterval = null;

function setStatus(msg, type) {
  statusArea.innerHTML = `<div class="status ${type}">${msg}</div>`;
}

function startPolling() {
  stopPolling();
  pollInterval = setInterval(() => {
    api.runtime.sendMessage({ action: "getStatus" }).then((r) => {
      if (r && r.issueToken && r.cookies) {
        stopPolling();
        buildCode(r.issueToken, r.cookies);
      }
    }).catch(() => {
      // background may not be ready yet; ignore and retry
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
  extractBtn.disabled = false;
  extractBtn.textContent = "Extract Credentials";
  setStatus(
    "Credentials captured! Copy the code below and paste it into Home Assistant.",
    "success"
  );
}

// On popup open, check if already captured
api.runtime.sendMessage({ action: "getStatus" }).then((r) => {
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
}).catch(() => {
  // background not ready; start fresh
});

extractBtn.addEventListener("click", () => {
  extractBtn.disabled = true;
  extractBtn.textContent = "Waiting...";
  outputArea.style.display = "none";

  api.runtime.sendMessage({ action: "reset" }).then(() => {
    return api.runtime.sendMessage({ action: "startCapture" });
  }).then(() => {
    setStatus(
      "Opening home.nest.com... Sign in if needed. Credentials will be captured automatically. Click the toolbar icon when the badge shows ✓.",
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
