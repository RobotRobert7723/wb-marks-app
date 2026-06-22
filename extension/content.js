(function () {
  const BUTTON_ID = "wb-marks-launch-button";

  function detectDraftId() {
    const url = window.location.href;
    const directMatch = url.match(/(?:preorderID|draft_id|draftId)=([0-9]+)/i);
    if (directMatch) {
      return directMatch[1];
    }

    const pathMatches = window.location.pathname.match(/([0-9]{6,})/g);
    if (pathMatches && pathMatches.length > 0) {
      return pathMatches[pathMatches.length - 1];
    }

    const html = document.documentElement.innerHTML;
    const htmlMatch = html.match(/"preorderID"\s*:\s*([0-9]+)/i);
    if (htmlMatch) {
      return htmlMatch[1];
    }
    return "";
  }

  function looksLikeDraftPage() {
    const text = document.body?.innerText || "";
    return text.includes("Черновик") || text.includes("Поставки");
  }

  function ensureButton() {
    if (!looksLikeDraftPage()) {
      return;
    }
    if (document.getElementById(BUTTON_ID)) {
      return;
    }

    const draftId = detectDraftId();
    if (!draftId) {
      return;
    }

    const target = document.querySelector("header") || document.body;
    const button = document.createElement("button");
    button.id = BUTTON_ID;
    button.textContent = "ВыпуститьЧЗ";
    button.style.position = "fixed";
    button.style.top = "18px";
    button.style.right = "24px";
    button.style.zIndex = "99999";
    button.style.padding = "10px 14px";
    button.style.borderRadius = "10px";
    button.style.border = "0";
    button.style.background = "#276ef1";
    button.style.color = "#fff";
    button.style.cursor = "pointer";
    button.addEventListener("click", () => {
      chrome.runtime.sendMessage(
        {
          type: "wb-launch",
          draftId,
          sourceUrl: window.location.href,
        },
        (response) => {
          if (!response?.ok) {
            window.alert(`Не удалось запустить выпуск ЧЗ: ${response?.error || "unknown error"}`);
          }
        }
      );
    });
    target.appendChild(button);
  }

  ensureButton();
  const observer = new MutationObserver(() => ensureButton());
  observer.observe(document.documentElement, { childList: true, subtree: true });
})();
