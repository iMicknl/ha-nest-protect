// content_script.js
// Injected into home.nest.com — extracts login_hint from the page's gapi.auth2 instance.

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action !== "extractLoginHint") return false;

  const script = document.createElement("script");
  script.textContent = `
    (function() {
      try {
        var auth = gapi.auth2.getAuthInstance();
        var user = auth.currentUser.get();
        var resp = user.getAuthResponse(true);
        if (!resp || !resp.access_token) {
          window.postMessage({type: "NEST_AUTH_RESULT", error: "not_signed_in"}, "*");
          return;
        }
        window.postMessage({
          type: "NEST_AUTH_RESULT",
          login_hint: resp.login_hint,
          email: user.getBasicProfile().getEmail()
        }, "*");
      } catch(e) {
        window.postMessage({type: "NEST_AUTH_RESULT", error: e.message}, "*");
      }
    })();
  `;
  document.documentElement.appendChild(script);
  script.remove();

  const handler = (event) => {
    if (event.data && event.data.type === "NEST_AUTH_RESULT") {
      window.removeEventListener("message", handler);
      sendResponse(event.data);
    }
  };
  window.addEventListener("message", handler);

  // Timeout after 3 seconds
  setTimeout(() => {
    window.removeEventListener("message", handler);
    sendResponse({ error: "timeout" });
  }, 3000);

  return true; // Keep message channel open for async sendResponse
});
