<h1 align="center">Fast-Tabber</h1>

https://github.com/user-attachments/assets/c547f1a5-fc6a-445d-a279-b2ac1fccd223

<p align="center">Manage Chrome tabs and bookmarks through the AI agent you already use.</p>

Group tabs by project, find a page from its content, save a collection, organize bookmarks, or export selected links. Fast-Tabber connects Chrome to Codex, OpenCode, Claude Code, and other MCP-compatible agents.

## Installation

### 1. Add the Chrome extension

1. Download the **fast-tabber-extension ZIP** from the [latest release](https://github.com/solusops/Fast-Tabber/releases/latest).
2. Extract it into a folder you will keep on your computer.
3. Open `chrome://extensions` in Chrome and turn on **Developer mode**.
4. Click **Load unpacked** and select the extracted folder containing `manifest.json`.
5. Pin **Fast-Tabber** to your toolbar for quick access to exports and bookmark permissions.

Keep the extracted folder in place while the extension is installed.

### 2. Connect your agent

On Windows, install [Node.js 22 or later](https://nodejs.org/) and run:

```powershell
npx -y fast-tabber-toolkit@latest setup
```

Choose **Codex**, **OpenCode**, or **Claude Code** when prompted. For OpenCode, run the command from your project folder. The installer preserves your existing agent and model settings.

For another MCP-compatible agent, run:

```powershell
npx -y fast-tabber-toolkit@latest setup --agent generic
```

Use the connection settings and Skill location printed by the installer.

<details>
<summary>Install through the Codex plugin marketplace</summary>

```powershell
codex plugin marketplace add solusops/Fast-Tabber
codex plugin add fast-tabber@fast-tabber
```

</details>

### 3. Pair Chrome

Restart your agent and ask:

> Connect Fast-Tabber to my browser.

Follow the pairing link it provides, then return to chat so the agent can confirm the connection. To manage bookmarks, open the extension and click **Enable bookmarks**, then approve Chrome's permission request.

### Updating

Run the setup command again to update the toolkit. To update the Chrome extension, extract the new extension ZIP into the same folder and click **Reload** at `chrome://extensions`. Your saved collections and cached content stay on your computer.

## What you can ask

- **Organize:** "Group my coding tabs by topic. Keep entertainment separate."
- **Refine:** "Move the GitHub Actions tab into DevOps."
- **Find:** "Find the page explaining CSS grid placement."
- **Save:** "Save these tabs as bookmarks in Frontend references."
- **Restore:** "Reopen my saved research collection."
- **Export:** "Download this group as CSV."
- **Clean up:** "Find duplicate bookmarks and show me before deleting them."
- **Undo:** "Undo the last grouping change."

Grouping keeps tabs open. Closing tabs or deleting bookmarks requires confirmation. Bookmark operations apply when you ask for them explicitly. You can also export open tabs, selected groups, or bookmark folders directly from the extension as TXT or CSV.

## Search and privacy

Page text is cached locally and reused across searches and reopened URLs. Ask your agent to refresh a page when you need a new copy. Fast-Tabber reads the selected page without crawling its links, and your agent can inspect specific page text, screenshots, or accessibility information when needed.

Bulk indexing and storage stay on your computer. Information requested by a cloud-connected agent may be sent to its model provider. You can ask your agent to clear cached data. See the [privacy policy](PRIVACY.md) for details.

## License

[MIT](LICENSE).
