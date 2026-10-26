# Chrome Web Store listing

Updated 2026-10-26. Draft, not submitted.

Name: Fast-Tabber

Short description: Manage, search, group and export Chrome tabs and bookmarks through your existing AI agent.

Detailed description:

Fast-Tabber lets your existing AI agent help organize Chrome tabs and explicitly selected bookmarks.

Search related pages, group open tabs, organize bookmark folders, save collections, undo supported changes and export selected tab groups or bookmark folders as TXT or CSV. Grouping keeps your tabs open. The black-and-white toolbar popup also exports URLs without an agent.

Install the separate local Fast-Tabber toolkit, connect it to a compatible agent, then ask the agent for a pairing code. Open Pair with agent in the extension popup and paste the code. Approve bookmark access only if you want bookmark features. Semantic features require a separately approved local model download.

Indexing and clustering stay on your computer. Selected text or images requested by a cloud-connected agent may reach its model provider. Page inspection may load sleeping tabs or activate Chrome. Screenshots cover the top of a page, not an unlimited full-page image. See the privacy policy for storage and deletion details.

Category: Productivity. Language: English.

Single purpose: Manage and export the user's tabs and explicitly requested bookmarks through their existing agent.

## Permissions

| Permission | Reason |
|---|---|
| tabs | List titles/URLs and open, close, activate, reorder and move requested tabs. |
| tabGroups | Create, name, move and export native tab groups. |
| scripting | Extract requested rendered page content and temporarily mask forms during screenshots. |
| debugger | Capture selected pages and inspect their accessibility information; detach after each operation. |
| alarms | Reconnect to the local toolkit after the background worker sleeps or service restarts. |
| storage | Keep the paired browser credential locally, restricted to trusted extension contexts. |
| bookmarks (optional) | Read, export and modify bookmarks only when the user enables this permission and requests bookmark scope. |
| all_urls | Inspect ordinary HTTP/HTTPS pages across user-selected sites, not a fixed domain list. Browser-internal, incognito and credential-bearing URLs are rejected for page inspection. |

## Privacy and data use

The extension handles web history/URLs, bookmark titles and website content for browser management. These can contain personal, health, financial, location or communication information depending on the page. They are stored locally by the companion service; selected evidence may be sent by the user's chosen agent to its provider. Local pairing credentials are stored but never included in exports or agent tool results. No analytics, advertising, sale, unrelated use, creditworthiness or lending use.

Privacy policy source: PRIVACY.md. A live public URL is required before submission; the repository is currently private. Do not enter a private GitHub URL in the dashboard.

## Assets and publisher

- Required 128x128 store icon and 1280x800 or 640x400 real screenshot: pending approved public assets. Do not reuse private browser contents.
- Publisher account/contact email and distribution regions: publisher must supply in the dashboard.
- Support/homepage: https://github.com/solusops/Fast-Tabber (must be public or replaced by a public support site).
- Before store release, create the dashboard item, obtain its public key, update manifest.key to match its assigned extension ID, rebuild and release the toolkit. Runtime origin validation must match the actual store extension. Never weaken it to accept arbitrary extensions.
- Store ZIP: dist/fast-tabber-extension-0.7.0.zip, extension files only, no pairing.json or runtime data.
- Version 0.7.0: runtime pairing, restricted credential storage and agent-neutral MCP launcher. Draft.

## Review and updates

Reviewers need the local Windows runtime, uv, Chrome and an MCP agent; semantic features additionally need Ollama and the approved embedding model. Native exports and tab operations do not require models. First pair with a five-minute code, then verify browser_status in agent chat. No developer account credentials are supplied.

Chrome controls Web Store update rollout. The agent can request an update check; it cannot bypass review, force an immediate download or approve permission changes. Local toolkit updates are separate package-manager operations. Broad site access and debugger use need review; acceptance is not guaranteed.
