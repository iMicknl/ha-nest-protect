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
    chrome.runtime.sendMessage({ action: "getStatus" }, (r) => {
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
  extractBtn.disabled = false;
  extractBtn.textContent = "Extract Credentials";
  setStatus(
    "Credentials captured! Copy the code below and paste it into Home Assistant.",
    "success"
  );
}

// On popup open, check if already captured
chrome.runtime.sendMessage({ action: "getStatus" }, (r) => {
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

  chrome.runtime.sendMessage({ action: "reset" }, () => {
    chrome.runtime.sendMessage({ action: "startCapture" }, () => {
      setStatus(
        "Opening home.nest.com... Sign in if needed. Credentials will be captured automatically.",
        "info"
      );
      startPolling();
    });
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
