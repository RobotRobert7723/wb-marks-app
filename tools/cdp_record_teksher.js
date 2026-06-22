const fs = require("fs");
const path = require("path");

const CDP_LIST_URL = "http://127.0.0.1:9222/json/list";
const TARGET_URL_PART = "label.teksher.kg";
const OUT_DIR = path.join(process.cwd(), "output");
const STARTED_AT = new Date();
const STAMP = STARTED_AT.toISOString().replace(/[:.]/g, "-");
const EVENTS_PATH = path.join(OUT_DIR, `teksher_network_${STAMP}.jsonl`);
const SUMMARY_PATH = path.join(OUT_DIR, `teksher_network_${STAMP}_summary.json`);

fs.mkdirSync(OUT_DIR, { recursive: true });

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} for ${url}`);
  }
  return response.json();
}

async function findTarget() {
  const tabs = await fetchJson(CDP_LIST_URL);
  const target = tabs.find(
    (tab) =>
      tab.type === "page" &&
      typeof tab.url === "string" &&
      tab.url.includes(TARGET_URL_PART) &&
      !tab.url.startsWith("devtools://")
  );
  if (!target) {
    throw new Error(`No page target found for ${TARGET_URL_PART}`);
  }
  return target;
}

async function main() {
  const target = await findTarget();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  const requests = new Map();
  let id = 0;
  const pending = new Map();

  function send(method, params = {}) {
    const msgId = ++id;
    ws.send(JSON.stringify({ id: msgId, method, params }));
    return new Promise((resolve, reject) => {
      pending.set(msgId, { resolve, reject });
      setTimeout(() => {
        if (pending.has(msgId)) {
          pending.delete(msgId);
          reject(new Error(`Timeout for ${method}`));
        }
      }, 15000);
    });
  }

  function append(event) {
    fs.appendFileSync(EVENTS_PATH, JSON.stringify(event) + "\n", "utf8");
  }

  function relevant(url) {
    return (
      typeof url === "string" &&
      (url.includes("/api/") ||
        url.includes("/facade/") ||
        url.includes("/files/") ||
        url.includes("label.teksher.kg"))
    );
  }

  ws.onmessage = async (event) => {
    const message = JSON.parse(event.data);

    if (message.id && pending.has(message.id)) {
      const handler = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) {
        handler.reject(message.error);
      } else {
        handler.resolve(message.result);
      }
      return;
    }

    if (message.method === "Network.requestWillBeSent") {
      const { requestId, request, type, wallTime, documentURL } = message.params;
      if (!relevant(request.url)) {
        return;
      }
      requests.set(requestId, {
        requestId,
        type,
        url: request.url,
        method: request.method,
        headers: request.headers,
        postData: request.postData || null,
        wallTime,
        documentURL,
      });
      append({ kind: "request", at: new Date().toISOString(), ...requests.get(requestId) });
      return;
    }

    if (message.method === "Network.responseReceived") {
      const { requestId, response, type } = message.params;
      const request = requests.get(requestId);
      if (!request || !relevant(response.url)) {
        return;
      }

      let body = null;
      try {
        const result = await send("Network.getResponseBody", { requestId });
        body = result.base64Encoded
          ? { base64Encoded: true, text: result.body.slice(0, 4000) }
          : { base64Encoded: false, text: String(result.body).slice(0, 4000) };
      } catch (error) {
        body = { error: String(error.message || error) };
      }

      append({
        kind: "response",
        at: new Date().toISOString(),
        requestId,
        type,
        url: response.url,
        status: response.status,
        statusText: response.statusText,
        mimeType: response.mimeType,
        headers: response.headers,
        body,
      });
      return;
    }

    if (message.method === "Page.downloadWillBegin") {
      append({
        kind: "download",
        at: new Date().toISOString(),
        url: message.params.url,
        suggestedFilename: message.params.suggestedFilename,
      });
      return;
    }
  };

  ws.onopen = async () => {
    await send("Network.enable");
    await send("Page.enable");
    await send("Page.setDownloadBehavior", {
      behavior: "allow",
      downloadPath: path.join(process.cwd(), "output"),
    }).catch(() => null);

    append({
      kind: "session",
      at: new Date().toISOString(),
      note: "Recorder started",
      targetId: target.id,
      targetUrl: target.url,
      wsUrl: target.webSocketDebuggerUrl,
    });

    fs.writeFileSync(
      SUMMARY_PATH,
      JSON.stringify(
        {
          startedAt: STARTED_AT.toISOString(),
          eventsPath: EVENTS_PATH,
          targetId: target.id,
          targetUrl: target.url,
          note: "Perform actions manually in the already open Teksher tab. Stop recorder with Ctrl+C in its console.",
        },
        null,
        2
      ),
      "utf8"
    );

    console.log(`Recorder attached to ${target.url}`);
    console.log(`Events file: ${EVENTS_PATH}`);
    console.log(`Summary file: ${SUMMARY_PATH}`);
  };

  ws.onerror = (error) => {
    console.error("WebSocket error:", error.message || error);
  };

  ws.onclose = () => {
    console.log("Recorder stopped.");
  };
}

main().catch((error) => {
  console.error(error.stack || String(error));
  process.exit(1);
});
