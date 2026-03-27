const startBtn = document.getElementById("startBtn");
const stopBtn = document.getElementById("stopBtn");
const resetBtn = document.getElementById("resetBtn");
const statusBar = document.getElementById("statusBar");
const statusText = document.getElementById("statusText");
const issueTokenField = document.getElementById("issueTokenField");
const cookiesField = document.getElementById("cookiesField");

let pollInterval = null;

function updateUI(data) {
  const { issueToken, cookies, listening } = data;

  if (issueToken) {
    issueTokenField.value = issueToken;
  }
  if (cookies) {
    cookiesField.value = cookies;
  }

  if (issueToken && cookies) {
    statusBar.className = "status-bar complete";
    statusText.textContent = "Both values captured!";
    startBtn.disabled = true;
    stopBtn.disabled = true;
    stopPolling();
  } else if (listening) {
    statusBar.className = "status-bar listening";
    const parts = [];
    if (issueToken) parts.push("issue_token ✓");
    if (cookies) parts.push("cookies ✓");
    const captured = parts.length ? ` (${parts.join(", ")})` : "";
    statusText.textContent = `Listening for network requests...${captured}`;
    startBtn.disabled = true;
    stopBtn.disabled = false;
  } else {
    statusBar.className = "status-bar idle";
    statusText.textContent = "Ready — click Start to begin";
    startBtn.disabled = false;
    stopBtn.disabled = true;
  }
}

function startPolling() {
  stopPolling();
  pollInterval = setInterval(() => {
    chrome.runtime.sendMessage({ action: "getStatus" }, (response) => {
      if (response) updateUI(response);
    });
  }, 500);
}

function stopPolling() {
  if (pollInterval) {
    clearInterval(pollInterval);
    pollInterval = null;
  }
}

// Initial status check
chrome.runtime.sendMessage({ action: "getStatus" }, (response) => {
  if (response) {
    updateUI(response);
    if (response.listening && !(response.issueToken && response.cookies)) {
      startPolling();
    }
  }
});

startBtn.addEventListener("click", () => {
  chrome.runtime.sendMessage({ action: "startCapture" }, (response) => {
    if (response && response.status === "started") {
      updateUI({ issueToken: null, cookies: null, listening: true });
      startPolling();
    }
  });
});

stopBtn.addEventListener("click", () => {
  chrome.runtime.sendMessage({ action: "stopCapture" }, () => {
    stopPolling();
    chrome.runtime.sendMessage({ action: "getStatus" }, (response) => {
      if (response) updateUI(response);
    });
  });
});

resetBtn.addEventListener("click", () => {
  chrome.runtime.sendMessage({ action: "reset" }, () => {
    issueTokenField.value = "";
    cookiesField.value = "";
    updateUI({ issueToken: null, cookies: null, listening: false });
    stopPolling();
  });
});

// Copy buttons
document.querySelectorAll(".copy-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const target = document.getElementById(btn.dataset.target);
    if (!target.value) return;

    navigator.clipboard.writeText(target.value).then(() => {
      btn.textContent = "Copied!";
      btn.classList.add("copied");
      setTimeout(() => {
        btn.textContent = "Copy";
        btn.classList.remove("copied");
      }, 1500);
    });
  });
});
