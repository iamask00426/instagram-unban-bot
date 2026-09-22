import logging
from io import BytesIO
import aiohttp

async def forward_to_telegram(bot_token: str, chat_id: str, text: str, photo_io: BytesIO = None):
    """
    Sends an alert and optional profile card photo to Telegram via Telegram Bot API.
    Silently logs errors so Telegram delivery issues never disrupt Discord notifications.
    """
    if not bot_token or not chat_id:
        return

    base_url = f"https://api.telegram.org/bot{bot_token.strip()}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            if photo_io:
                photo_io.seek(0)
                data = aiohttp.FormData()
                data.add_field("chat_id", str(chat_id).strip())
                data.add_field("caption", text, parse_mode="HTML")
                data.add_field("photo", photo_io.getvalue(), filename="card.png", content_type="image/png")
                async with session.post(f"{base_url}/sendPhoto", data=data) as resp:
                    if resp.status != 200:
                        err_text = await resp.text()
                        logging.warning(f"Telegram sendPhoto failed ({resp.status}): {err_text}")
                        # Fallback to text message if photo fails
                        await session.post(f"{base_url}/sendMessage", json={
                            "chat_id": str(chat_id).strip(),
                            "text": text,
                            "parse_mode": "HTML"
                        })
            else:
                await session.post(f"{base_url}/sendMessage", json={
                    "chat_id": str(chat_id).strip(),
                    "text": text,
                    "parse_mode": "HTML"
                })
    except Exception as e:
        logging.warning(f"Failed to forward alert to Telegram: {e}")
