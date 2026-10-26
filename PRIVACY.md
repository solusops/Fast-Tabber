# Fast-Tabber privacy

Updated 2026-10-26.

Fast-Tabber reads browser tabs and, only when requested and permitted, bookmarks. Requested page inspection can include rendered text, links, accessibility information and masked screenshots. This material may itself contain personal or sensitive information. Incognito pages are excluded. Cookies, credentials and form values are not requested; masking cannot guarantee removal of every secret rendered by a website.

The extension sends requested browser information to the paired service on this computer. Indexes, embeddings, saved collections, recovery records and cached images stay in that service's local SQLite storage. Browser pairing credentials stay in this Chrome profile's local extension storage; they are not synced. There is no Fast-Tabber analytics service or developer-operated data collection endpoint.

The connected agent may receive selected browser evidence and send it to its model provider. A cloud agent is not a fully local environment. With a local agent and local model, inference can remain local, subject to that agent's settings and other integrations. Installing software or approved models contacts the relevant package/model distributors. Loading websites contacts those websites normally.

Data is used only for requested browser management and is not sold, used for advertising, creditworthiness or lending. Bookmarks require optional Chrome permission and explicit scope. Routine indexing requires authorization. Closing tabs and deleting bookmarks are separate actions from grouping.

Use the agent's local-data preview and confirmed deletion to remove cached data. Deleting all local data also removes saved collections and recovery history. Uninstalling the extension removes its Chrome storage but does not remove the separate local service's database. Disable the extension or stop the service to disconnect browser access.

Questions: https://github.com/solusops/Fast-Tabber/issues . A public contact/support URL must be available before a public store launch.
