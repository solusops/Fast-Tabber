const config = __CONFIG__;
const code = location.hash.slice(1);
history.replaceState(null, "", "/setup");
document.getElementById("folder").textContent = config.folder;
const button = document.getElementById("connect");
const status = document.getElementById("status");
button.onclick = async () => {
  button.disabled = true;
  try {
    if (!/^[0-9a-f]{32}$/.test(code)) throw new Error("Ask your agent for a new setup link and open that link in Chrome.");
    if (!globalThis.chrome?.runtime?.sendMessage) throw new Error("Open this link in Chrome and load the Fast-Tabber extension using the instructions above.");
    const result = await chrome.runtime.sendMessage(config.extensionId, { type: "pair", code });
    if (!result || result.error) throw new Error(result?.error || "The extension did not accept the request.");
    status.textContent = "Request sent. Return to your agent so it can verify the live connection. If verification fails, ask for a new setup link.";
  } catch (error) {
    status.textContent = `${error.message} If Fast-Tabber is not loaded, follow First installation above, then try Connect again.`;
    button.disabled = false;
  }
};
