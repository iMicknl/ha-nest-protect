const extractBtn = document.getElementById("extract_btn");
const copyBtn = document.getElementById("copy_btn");
const statusArea = document.getElementById("status_area");
const outputArea = document.getElementById("output_area");
const issueTokenField = document.getElementById("issue_token");
const cookiesField = document.getElementById("cookies");

const REQUIRED_COOKIES = ["SID", "HSID", "SSID", "APISID", "SAPISID"];

function setStatus(msg, type) {
  statusArea.innerHTML = `<div class="status ${type}">${msg}</div>`;
}

extractBtn.addEventListener("click", async () => {
  extractBtn.disabled = true;
  outputArea.style.display = "none";
  setStatus("Extracting cookies...", "info");

  try {
    const cookies = await chrome.cookies.getAll({ domain: ".google.com" });
    const relevant = cookies.filter((c) => REQUIRED_COOKIES.includes(c.name));

    const missing = REQUIRED_COOKIES.filter(
      (name) => !relevant.some((c) => c.name === name)
    );

    if (missing.length > 0) {
      setStatus(
        `Missing cookies: ${missing.join(", ")}. Make sure you are signed in to <a href="https://home.nest.com" target="_blank">home.nest.com</a> first.`,
        "error"
      );
      extractBtn.disabled = false;
      return;
    }

    const cookieString = relevant
      .map((c) => `${c.name}=${c.value}`)
      .join("; ");

    const issueToken =
      `https://accounts.google.com/o/oauth2/iframerpc?action=issueToken` +
      `&response_type=token%20id_token` +
      `&login_hint={{YOUR_EMAIL}}` +
      `&client_id=733249279899-1gpkq9duqmdp55a7e5lft1pr2smurf3u.apps.googleusercontent.com` +
      `&origin=https%3A%2F%2Fhome.nest.com` +
      `&scope=openid%20profile%20email%20https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fnest-account`;

    issueTokenField.value = issueToken;
    cookiesField.value = cookieString;
    outputArea.style.display = "block";

    setStatus(
      "Cookies extracted. Copy the values below into your Home Assistant configuration.",
      "success"
    );
  } catch (err) {
    setStatus(`Error: ${err.message}`, "error");
  } finally {
    extractBtn.disabled = false;
  }
});

copyBtn.addEventListener("click", () => {
  const text =
    `issue_token: ${issueTokenField.value}\ncookies: ${cookiesField.value}`;
  navigator.clipboard.writeText(text).then(() => {
    copyBtn.textContent = "Copied!";
    setTimeout(() => {
      copyBtn.textContent = "Copy to Clipboard";
    }, 2000);
  });
});
