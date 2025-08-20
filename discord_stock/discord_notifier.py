import json, time
import requests
from pathlib import Path

class DiscordNotifier:
    def __init__(self, config_path: str = "discord_stock\\webhook_meneger.json"):
        self.webhooks = self._load_webhooks(config_path)

    def _load_webhooks(self, config_path: str) -> dict:
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    # ---- generic POST with retry for 429 ----
    def _post_with_retry(self, url: str, *, json_payload=None, files=None, max_retries: int = 3):
        for attempt in range(max_retries + 1):
            if files is None:
                r = requests.post(url, json=json_payload, timeout=20)
            else:
                # When sending files, the json must go in 'payload_json'
                r = requests.post(url, data={"payload_json": json.dumps(json_payload)}, files=files, timeout=30)

            if r.status_code in (200, 204):
                return r

            # Handle rate limit
            if r.status_code == 429:
                try:
                    info = r.json()
                    wait_s = float(info.get("retry_after", 0.5))
                except Exception:
                    wait_s = 0.5
                time.sleep(min(wait_s, 5.0))  # back off a bit
                continue

            # Other errors: raise immediately
            raise Exception(f"Failed to send to Discord: {r.status_code} {r.text}")

        raise Exception("Failed to send to Discord after retries due to rate limits.")

    def send_message(self, channel_name: str, content: str):
        url = self.webhooks.get(channel_name)
        if not url:
            raise ValueError(f"No webhook URL found for channel '{channel_name}'")
        payload = {"content": content}
        self._post_with_retry(url, json_payload=payload)

    def send_embed(self, channel_name: str, embed: dict):
        """Send a single embed (no image)."""
        url = self.webhooks.get(channel_name)
        if not url:
            raise ValueError(f"No webhook URL found for channel '{channel_name}'")
        payload = {"embeds": [embed]}
        self._post_with_retry(url, json_payload=payload)

    def send_embed_with_image(self, channel_name: str, embed: dict, image_path: str, image_name: str | None = None):
        """
        Send an embed and attach a local image file to display inside the embed.
        """
        url = self.webhooks.get(channel_name)
        if not url:
            raise ValueError(f"No webhook URL found for channel '{channel_name}'")

        fname = (image_name or Path(image_path).name)
        # Tell Discord to display the attached file within the embed
        embed_with_img = dict(embed)
        embed_with_img["image"] = {"url": f"attachment://{fname}"}

        with open(image_path, "rb") as f:
            files = [("file", (fname, f, "image/png"))]
            payload = {"embeds": [embed_with_img]}
            self._post_with_retry(url, json_payload=payload, files=files)
