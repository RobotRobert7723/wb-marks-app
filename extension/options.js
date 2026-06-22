const input = document.getElementById("backendBaseUrl");
const status = document.getElementById("status");
const save = document.getElementById("save");

chrome.storage.sync.get({ backendBaseUrl: "http://localhost:8000" }, ({ backendBaseUrl }) => {
  input.value = backendBaseUrl;
});

save.addEventListener("click", () => {
  chrome.storage.sync.set({ backendBaseUrl: input.value.trim() || "http://localhost:8000" }, () => {
    status.textContent = "Сохранено";
    setTimeout(() => {
      status.textContent = "";
    }, 1500);
  });
});
