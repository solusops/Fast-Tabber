"""One-time privacy disclosure and local setup status."""
import json
from mcp.server.mcpserver.exceptions import ToolError

DISCLOSURE = ("Page text, browser metadata and saved collections are cached locally. Your connected agent may send requested "
    "titles, URLs, text, accessibility data and screenshots to its model provider. Fast-Tabber does not request cookies "
    "or form values. No inference runtime or downloaded model is required.")

class Onboarding:
    def __init__(self, state, bridge, busy, processing_lock):
        self.state, self.bridge, self.busy = state, bridge, busy

    async def call(self, action, confirm=False, wait_seconds=0):
        path = self.state / "settings.json"
        settings = json.loads(path.read_text()) if path.exists() else {}
        if action == "acknowledge_privacy":
            if not confirm:
                return {"requiresDisclosure": True, "privacyDisclosure": DISCLOSURE, "nextAction": "ask_user"}
            settings["privacy_acknowledged"] = True
            path.write_text(json.dumps(settings))
            return {"acknowledged": True}
        if action != "status":
            raise ToolError("Choose status, browser or acknowledge_privacy")
        return {"privacyAcknowledged": settings.get("privacy_acknowledged", False), "privacyDisclosure": DISCLOSURE,
                "browserConnected": self.bridge.peer is not None, "busy": self.busy(),
                "nextStep": "Show the privacy disclosure once, check Chrome with onboarding(action='browser'), then use the browser tools."}
