chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type !== "wb-launch") {
    return false;
  }

  chrome.storage.sync.get({ backendBaseUrl: "http://localhost:8000" }, async ({ backendBaseUrl }) => {
    try {
      const response = await fetch(`${backendBaseUrl}/api/launches`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          draft_id: message.draftId,
          source_url: message.sourceUrl || "",
        }),
      });
      const payload = await response.json();
      if (response.status === 409 && payload.settings_required) {
        chrome.tabs.create({ url: `${backendBaseUrl}${payload.settings_url}` });
        sendResponse({ ok: true, redirected: "settings" });
        return;
      }
      if (!response.ok) {
        sendResponse({ ok: false, error: payload.detail || JSON.stringify(payload) });
        return;
      }
      chrome.tabs.create({ url: `${backendBaseUrl}${payload.status_url}` });
      sendResponse({ ok: true, redirected: "status" });
    } catch (error) {
      sendResponse({ ok: false, error: String(error) });
    }
  });

  return true;
});
