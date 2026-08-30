"""
إرسال المنشورات والتنبيهات إلى Telegram.
"""

import os
import re
import uuid

import aiofiles
import aiohttp


class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str, download_dir: str, logger):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.download_dir = download_dir
        self.logger = logger

    async def send_post(
        self,
        session: aiohttp.ClientSession,
        page_title: str,
        text: str,
        post_url: str,
        image_url: str | None = None,
        has_video: bool = False,
    ):
        if not self.bot_token:
            self.logger.warning("⚠️ لم يتم ضبط Telegram Bot Token!")
            return

        safe_title = re.sub(r"[*_`\[\]()]", "", page_title)
        caption = f"📢 {safe_title}\n\n{text[:800]}\n\n"
        if has_video:
            caption += f"🎬 رابط الفيديو: {post_url}\n\n"
        caption += f"🔗 رابط المنشور: {post_url}"

        sent = False
        if image_url:
            sent = await self._send_photo(session, image_url, caption)
            if sent:
                self.logger.info(f"✈️ [Telegram]: Photo + Link sent for {safe_title}")

        if not sent:
            await self._send_text(session, caption, safe_title)

    async def send_alert(self, session: aiohttp.ClientSession, message: str):
        """تنبيه تشغيلي (زي احتمال تغيّر بنية فيسبوك) — مستقل عن send_post."""
        if not self.bot_token:
            self.logger.warning("⚠️ لم يتم ضبط Telegram Bot Token — تعذّر إرسال التنبيه.")
            return
        await self._send_text(session, f"⚠️ {message}", "system-alert")

    async def _send_photo(self, session: aiohttp.ClientSession, image_url: str, caption: str) -> bool:
        unique_filename = f"tg_{uuid.uuid4().hex}.jpg"
        temp_img_path = os.path.join(self.download_dir, unique_filename)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        }
        try:
            async with session.get(image_url, headers=headers, timeout=15) as img_resp:
                if img_resp.status != 200:
                    return False
                img_bytes = await img_resp.read()
                async with aiofiles.open(temp_img_path, "wb") as f:
                    await f.write(img_bytes)

            url = f"https://api.telegram.org/bot{self.bot_token}/sendPhoto"
            async with aiofiles.open(temp_img_path, "rb") as f:
                photo_data = await f.read()

            data = aiohttp.FormData()
            data.add_field("chat_id", self.chat_id)
            data.add_field("caption", caption)
            data.add_field("photo", photo_data, filename="image.jpg")

            async with session.post(url, data=data, timeout=20) as resp:
                if resp.status == 200:
                    return True
                err_txt = await resp.text()
                self.logger.error(f"❌ Telegram Photo Send Failed ({resp.status}): {err_txt}")
                return False
        except Exception as img_err:
            self.logger.warning(f"⚠️ Failed to process image ({img_err}), falling back to text.")
            return False
        finally:
            if os.path.exists(temp_img_path):
                try:
                    os.remove(temp_img_path)
                except Exception:
                    pass

    async def _send_text(self, session: aiohttp.ClientSession, caption: str, label: str):
        try:
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {"chat_id": self.chat_id, "text": caption, "disable_web_page_preview": False}
            async with session.post(url, json=payload, timeout=15) as resp:
                if resp.status == 200:
                    self.logger.info(f"✈️ [Telegram]: Text sent for {label}")
                else:
                    err_txt = await resp.text()
                    self.logger.error(f"❌ Telegram Send Failed ({resp.status}): {err_txt}")
        except Exception as e:
            self.logger.warning(f"⚠️ Telegram text sending failed: {e}")
