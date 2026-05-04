const extractBtn = document.getElementById("extract_btn");
const copyBtn = document.getElementById("copy_btn");
const statusArea = document.getElementById("status_area");
const outputArea = document.getElementById("output_area");
const authCodeField = document.getElementById("auth_code");

const REQUIRED_COOKIES = ["SID", "HSID", "SSID", "APISID", "SAPISID"];
const CLIENT_ID =
  "733249279899-44tchle2kaa9afr5v9ov7jbuojfr9lrq.apps.googleusercontent.com";

function setStatus(msg, type) {
  statusArea.innerHTML = `<div class="status ${type}">${msg}</div>`;
}

extractBtn.addEventListener("click", async () => {
  extractBtn.disabled = true;
  outputArea.style.display = "none";
  setStatus("Extracting credentials...", "info");

  try {
    // Step 1: Get the active tab — must be home.nest.com
    const [tab] = await chrome.tabs.query({
      active: true,
      currentWindow: true,
    });

    if (!tab || !tab.url || !tab.url.startsWith("https://home.nest.com")) {
      setStatus(
        "Please navigate to <b>home.nest.com</b> and sign in first, then try again.",
        "error"
      );
      extractBtn.disabled = false;
      return;
    }

    // Step 2: Ask content script for login_hint
    const authResult = await chrome.tabs.sendMessage(tab.id, {
      action: "extractLoginHint",
    });

    if (!authResult || authResult.error) {
      const errorMsg =
        authResult && authResult.error === "not_signed_in"
          ? "You are not signed in. Please sign in to your Google account on home.nest.com first."
          : `Could not extract credentials: ${authResult ? authResult.error : "no response"}. Make sure you are signed in on home.nest.com.`;
      setStatus(errorMsg, "error");
      extractBtn.disabled = false;
      return;
    }

    const loginHint = authResult.login_hint;

    // Step 3: Read cookies
    const cookies = await chrome.cookies.getAll({ domain: ".google.com" });
    const relevant = cookies.filter((c) => REQUIRED_COOKIES.includes(c.name));

    const missing = REQUIRED_COOKIES.filter(
      (name) => !relevant.some((c) => c.name === name)
    );

    if (missing.length > 0) {
      setStatus(
        `Missing cookies: <b>${missing.join(", ")}</b>. Please make sure you are signed in to home.nest.com and try again.`,
        "error"
      );
      extractBtn.disabled = false;
      return;
    }

    // Step 4: Build issue_token URL
    const params = new URLSearchParams({
      action: "issueToken",
      response_type: "token id_token",
      login_hint: loginHint,
      client_id: CLIENT_ID,
      origin: "https://home.nest.com",
      scope:
        "openid profile email https://www.googleapis.com/auth/nest-account",
      ss_domain: "https://home.nest.com",
    });
    const issueToken = `https://accounts.google.com/o/oauth2/iframerpc?${params.toString()}`;

    // Step 5: Build cookie string
    const cookieString = relevant
      .map((c) => `${c.name}=${c.value}`)
      .join("; ");

    // Step 6: Encode as base64
    const payload = JSON.stringify({
      issue_token: issueToken,
      cookies: cookieString,
    });
    const code = btoa(payload);

    // Step 7: Display
    authCodeField.value = code;
    outputArea.style.display = "block";
    setStatus(
      `Credentials extracted for <b>${authResult.email || "your account"}</b>. Copy the code below and paste it into Home Assistant.`,
      "success"
    );
  } catch (err) {
    setStatus(`Error: ${err.message}`, "error");
  } finally {
    extractBtn.disabled = false;
  }
});

copyBtn.addEventListener("click", () => {
  navigator.clipboard.writeText(authCodeField.value).then(() => {
    copyBtn.textContent = "Copied!";
    setTimeout(() => {
      copyBtn.textContent = "Copy Code";
    }, 2000);
  });
});
