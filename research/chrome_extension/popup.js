const goBtn = document.getElementById("go_btn");
let pollInterval = null;

function setStatus(msg, type) {
  document.getElementById("status_area").innerHTML =
    `<div class="status ${type}">${msg}</div>`;
}

// Restore saved URL
const savedUrl = localStorage.getItem("ha_url");
if (savedUrl) document.getElementById("ha_url").value = savedUrl;

// On open, check if already captured
chrome.runtime.sendMessage({ action: "getStatus" }, (r) => {
  if (r && r.issueToken && r.cookies) {
    setStatus("Credentials captured! Sending to Home Assistant...", "info");
    sendToHA(r.issueToken, r.cookies);
  } else if (r && r.listening) {
    goBtn.disabled = true;
    setStatus("Waiting... open or refresh home.nest.com.", "waiting");
    startPolling();
  }
});

goBtn.addEventListener("click", () => {
  const haUrl = document.getElementById("ha_url").value.trim().replace(/\/+$/, "");
  if (!haUrl) {
    setStatus("Please enter your Home Assistant URL.", "error");
    return;
  }
  localStorage.setItem("ha_url", haUrl);
  goBtn.disabled = true;

  chrome.runtime.sendMessage({ action: "reset" }, () => {
    chrome.runtime.sendMessage({ action: "startCapture" }, () => {
      setStatus("Opened home.nest.com. Waiting for credentials...", "waiting");
      startPolling();
    });
  });
});

function startPolling() {
  stopPolling();
  pollInterval = setInterval(() => {
    chrome.runtime.sendMessage({ action: "getStatus" }, (r) => {
      if (r && r.issueToken && r.cookies) {
        stopPolling();
        setStatus("Credentials captured! Sending to Home Assistant...", "success");
        sendToHA(r.issueToken, r.cookies);
      }
    });
  }, 500);
}

function stopPolling() {
  if (pollInterval) { clearInterval(pollInterval); pollInterval = null; }
}

async function sendToHA(issueToken, cookies) {
  const haUrl = document.getElementById("ha_url").value.trim().replace(/\/+$/, "") ||
    localStorage.getItem("ha_url");
  if (!haUrl) {
    setStatus("Enter your Home Assistant URL and click Connect again.", "error");
    goBtn.disabled = false;
    return;
  }

  try {
    const resp = await fetch(`${haUrl}/api/nest_protect/auth_callback`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ issue_token: issueToken, cookies }),
    });

    if (resp.ok) {
      setStatus("Done! Return to Home Assistant. You can remove this extension.", "success");
      chrome.runtime.sendMessage({ action: "reset" });
    } else {
      const data = await resp.json().catch(() => ({}));
      setStatus(
        `Error: ${data.error || resp.status}. ` +
        "Make sure the Nest Protect setup is in progress in HA.",
        "error"
      );
      goBtn.disabled = false;
    }
  } catch (err) {
    setStatus(`Cannot reach Home Assistant: ${err.message}`, "error");
    goBtn.disabled = false;
  }
}
