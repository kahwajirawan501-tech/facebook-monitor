"""
استخراج النص من الصور عبر Gemini Vision، مع تدوير تلقائي بين عدة مفاتيح API
عند نفاد الحصة (429) أو ازدحام الخدمة (503/UNAVAILABLE).
"""

import asyncio
import os
import uuid
from urllib.parse import urljoin

import aiofiles
import aiohttp
from PIL import Image
from google import genai

OCR_PROMPT = """
أنت خبير في استخراج النصوص (OCR) من الصور بدقة متناهية.
قم باستخراج النص العربي الموجود في هذه الصورة.
التعليمات:
1. استخرج النص كما هو تماماً، وحافظ على تنسيق الفقرات والأسطر.
2. السياق العام للنص يتعلق بالأخبار والأحداث الميدانية والسياسية. استخدم هذا السياق لتصحيح أي كلمات مشوهة بصرياً.
3. أعد النص المستخرج فقط. لا تضف أي مقدمات، شروحات، أو علامات تنسيق مثل (```text).
"""


class GeminiOCR:
    def __init__(self, api_keys: list[str], model: str, download_dir: str, logger):
        if not api_keys:
            raise ValueError("لازم مفتاح Gemini واحد على الأقل.")
        self.api_keys = api_keys
        self.model = model
        self.download_dir = download_dir
        self.logger = logger
        self._current_key_index = 0

    def _client(self) -> genai.Client:
        return genai.Client(api_key=self.api_keys[self._current_key_index])

    async def extract_text_from_image_url(self, session: aiohttp.ClientSession, image_url: str) -> str:
        if not image_url:
            return ""

        full_url = urljoin("https://www.facebook.com", image_url)
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        temp_img_path = os.path.join(self.download_dir, f"gemini_ocr_{uuid.uuid4().hex}.jpg")

        try:
            async with session.get(full_url, headers=headers, timeout=15) as resp:
                if resp.status != 200:
                    return ""
                img_bytes = await resp.read()
                async with aiofiles.open(temp_img_path, "wb") as f:
                    await f.write(img_bytes)

            img = await asyncio.to_thread(Image.open, temp_img_path)
            return await self._call_with_failover(img)
        except Exception as e:
            self.logger.warning(f"⚠️ Gemini Vision OCR error: {e}")
            return ""
        finally:
            if os.path.exists(temp_img_path):
                try:
                    os.remove(temp_img_path)
                except Exception:
                    pass

    async def _call_with_failover(self, img: Image.Image, max_retries: int = 3) -> str:
        def call_gemini():
            client = self._client()
            response = client.models.generate_content(model=self.model, contents=[OCR_PROMPT, img])
            return response.text.strip()

        for _key_attempt in range(len(self.api_keys)):
            for attempt in range(1, max_retries + 1):
                try:
                    text = await asyncio.to_thread(call_gemini)
                    await asyncio.sleep(5)
                    return text
                except Exception as gemini_err:
                    err_str = str(gemini_err)
                    is_quota_error = "429" in err_str or "RESOURCE_EXHAUSTED" in err_str
                    is_overloaded = "UNAVAILABLE" in err_str or "503" in err_str

                    if is_quota_error:
                        self.logger.warning(
                            f"⚠️ المفتاح رقم {self._current_key_index + 1} استنفد حصته (429)، جاري التبديل للمفتاح التالي..."
                        )
                        self._current_key_index = (self._current_key_index + 1) % len(self.api_keys)
                        break
                    elif is_overloaded and attempt < max_retries:
                        wait_s = 5 * attempt
                        self.logger.info(
                            f"⏳ Gemini overloaded (محاولة {attempt}/{max_retries}) للمفتاح {self._current_key_index + 1}، الانتظار {wait_s} ثانية..."
                        )
                        await asyncio.sleep(wait_s)
                        continue
                    else:
                        if attempt == max_retries:
                            self.logger.warning(f"⚠️ فشل المفتاح الحالي بعد عدة محاولات: {gemini_err}")
                            self._current_key_index = (self._current_key_index + 1) % len(self.api_keys)
                            break
                        raise
        return ""
