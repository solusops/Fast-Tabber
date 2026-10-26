# Privacy

Fast-Tabber stores browser metadata, first-page text, requested screenshots, selections, saved collections and undo history locally in SQLite. FTS5 search needs no model or inference service. Webpages are inspected only for the selected indexing/inspection scope, without following links. Existing text is reused until an explicit refresh.

The connected agent receives compact tool responses. If it requests titles, URLs, text, accessibility data, links or screenshots, its model provider may receive them. Local agents with local models can keep inference on-device, subject to their other integrations. Fast-Tabber does not request cookies, credentials, form values or incognito content.

The browser bridge binds to loopback and requires authenticated extension pairing. Permissions and privacy are explained during setup. Bookmark access is optional and requires Chrome approval. Cache removal is available through the local_data tool after confirmation; saved collections and undo history are preserved unless all local data is explicitly selected.
