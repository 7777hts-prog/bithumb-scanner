import time, httpx
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ALERT_COOLDOWN_SEC

_last_sent = {}

def can_send(key: str) -> bool:
    now = time.time()
    last = _last_sent.get(key, 0.0)
    if now - last >= ALERT_COOLDOWN_SEC:
        _last_sent[key] = now
        return True
    return False

async def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode":"HTML"}, timeout=8.0)
