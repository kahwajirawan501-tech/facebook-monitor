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
4. إذا لم يوجد أي نص مقروء داخل الصورة إطلاقاً، أعد سلسلة فارغة تماماً بدون أي كلمات — لا تكتب جملة تشرح عدم وجود نص.
"""

# ردود شائعة يمكن يرجعها Gemini لما ما يلاقي نص، رغم تعليمات البرومبت — منعاملها
# كنص فاضي بدل ما نستخدمها كأنها محتوى المنشور الفعلي.
_NO_TEXT_PHRASES = {
    "لا يوجد نص في الصورة",
    "لا يوجد نص في الصورة.",
    "لا يوجد نص",
    "لا يوجد نص واضح في الصورة",
    "no text found in the image",
    "no text found",
    "no text in the image",
    "no readable text",
}


class GeminiOCR:
    def __init__(self, api_keys: list[str], model: str, download_dir: str, logger):
        if not api_keys:
            raise ValueError("لازم مفتاح Gemini واحد على الأقل.")
        self.api_keys = api_keys
        self.model = model
        self.download_dir = download_dir
        self.logger = logger

        # ★ تشغيل بالتوازي: ما بنقدر نعتمد على `self._current_key_index` وحيد
        # (شير بين كل الاستدعاءات) لأنو لما نشغّل عدة استدعاءات OCR مع بعض عبر
        # asyncio.gather()، كلها كانت رح تبلش من نفس المفتاح وتتزاحم عليه (race)
        # بدل ما تتوزع فعلياً على الأربع مفاتيح. البديل: عداد دوّار (round-robin)
        # محمي بـ Lock، كل استدعاء ياخد رقم مفتاح بداية مختلف، بالإضافة لمجموعة
        # "مفاتيح مستنفدة" مشتركة (429) نتجنبها بالاختيار بدل ما نلغيها نهائياً.
        self._key_lock = asyncio.Lock()
        self._next_key_index = 0
        self._exhausted_keys: set[int] = set()

    def _client(self, key_index: int) -> genai.Client:
        return genai.Client(api_key=self.api_keys[key_index])

    async def _acquire_key_index(self) -> int:
        """يرجّع رقم مفتاح للاستدعاء الحالي، موزّع round-robin بين المفاتيح غير
        المستنفدة. لو كل المفاتيح مستنفدة مؤقتاً (429)، منرجّع أي مفتاح ونجرب
        عليه (ممكن تكون الحصة تجددت)."""
        async with self._key_lock:
            n = len(self.api_keys)
            for _ in range(n):
                idx = self._next_key_index
                self._next_key_index = (self._next_key_index + 1) % n
                if idx not in self._exhausted_keys:
                    return idx
            idx = self._next_key_index
            self._next_key_index = (self._next_key_index + 1) % n
            return idx

    async def _mark_exhausted(self, key_index: int):
        async with self._key_lock:
            self._exhausted_keys.add(key_index)

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
        def call_gemini(key_index: int):
            client = self._client(key_index)
            response = client.models.generate_content(model=self.model, contents=[OCR_PROMPT, img])
            if response.text is None:
                # ردّ فاضي (None) غالباً معناه فلاتر أمان Gemini حجبت الرد (صور فيها
                # محتوى عسكري/سلاح مثلاً) — هاد مش خطأ مؤقت وما رح يتغيّر بإعادة
                # المحاولة، فبنرجّع نص فاضي فوراً بدل ما نكسر بـ AttributeError.
                return ""
            text = response.text.strip()
            if text.lower() in _NO_TEXT_PHRASES:
                # Gemini أحياناً بيرد بجملة وصفية ("لا يوجد نص في الصورة") بدل ما يرجع
                # نص فاضي، رغم تعليمات البرومبت. منعاملها كأنها فاضية.
                return ""
            return text

        # ★ رقم المفتاح صار محلي (local) لهاد الاستدعاء تحديداً، مش شير مع باقي
        # الاستدعاءات المتوازية — كل استدعاء بياخد نقطة بداية مختلفة من
        # _acquire_key_index() (round-robin محمي بـ lock)، فبتتوزع الأربع مفاتيح
        # فعلياً بدل ما كلها تبلش من نفس المفتاح وتتزاحم عليه.
        key_index = await self._acquire_key_index()

        for _key_attempt in range(len(self.api_keys)):
            for attempt in range(1, max_retries + 1):
                try:
                    text = await asyncio.to_thread(call_gemini, key_index)
                    await asyncio.sleep(5)
                    return text
                except Exception as gemini_err:
                    err_str = str(gemini_err)
                    is_quota_error = "429" in err_str or "RESOURCE_EXHAUSTED" in err_str
                    is_overloaded = "UNAVAILABLE" in err_str or "503" in err_str

                    if is_quota_error:
                        self.logger.warning(
                            f"⚠️ المفتاح رقم {key_index + 1} استنفد حصته (429)، جاري التبديل للمفتاح التالي..."
                        )
                        await self._mark_exhausted(key_index)
                        key_index = await self._acquire_key_index()
                        break
                    elif attempt < max_retries:
                        # overloaded أو أي خطأ تاني غير متوقع بمعاملهم نفس المعاملة: إعادة محاولة
                        # بنفس المفتاح مع backoff، بدل ما نرمي الاستثناء فوراً من أول محاولة.
                        wait_s = 5 * attempt
                        label = "Gemini overloaded" if is_overloaded else f"خطأ غير متوقع ({gemini_err})"
                        self.logger.info(
                            f"⏳ {label} (محاولة {attempt}/{max_retries}) للمفتاح {key_index + 1}، الانتظار {wait_s} ثانية..."
                        )
                        await asyncio.sleep(wait_s)
                        continue
                    else:
                        self.logger.warning(f"⚠️ فشل المفتاح الحالي بعد عدة محاولات: {gemini_err}")
                        key_index = await self._acquire_key_index()
                        break
        return ""
