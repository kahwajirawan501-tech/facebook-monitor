"""
كل إعدادات المشروع بمكان واحد: متغيرات بيئة + ملف selectors.json.
أي جزء تاني بالكود لازم يستورد إعداداته من هون، مش يقرأ os.environ مباشرة.
"""

import json
import os

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    pass


def _get_env(name: str, required: bool = True, default=None) -> str | None:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        if required:
            raise ConfigError(f"❌ متغير البيئة المطلوب غير موجود: {name}")
        return default
    return val.strip()


class Config:
    def __init__(self):
        self.TARGET_URLS = [u.strip() for u in _get_env("TARGET_URLS").split(",") if u.strip()]
        if not self.TARGET_URLS:
            raise ConfigError("❌ TARGET_URLS فارغة — لازم رابط واحد على الأقل.")

        self.TELEGRAM_BOT_TOKEN = _get_env("TELEGRAM_BOT_TOKEN")
        self.TELEGRAM_CHAT_ID = _get_env("TELEGRAM_CHAT_ID")

        self.TURSO_DATABASE_URL = _get_env("TURSO_DATABASE_URL")
        self.TURSO_AUTH_TOKEN = _get_env("TURSO_AUTH_TOKEN")

        self.GEMINI_KEYS = [
            k
            for k in [
                _get_env("GEMINI_API_KEY"),
                _get_env("GEMINI_API_KEY_2", required=False),
                _get_env("GEMINI_API_KEY_3", required=False),
                _get_env("GEMINI_API_KEY_4", required=False),
            ]
            if k
        ]
        self.GEMINI_MODEL = _get_env("GEMINI_MODEL", required=False, default="gemini-3.6-flash")

        self.MAX_CONCURRENT_TASKS = int(_get_env("MAX_CONCURRENT_TASKS", required=False, default="3"))
        # فاصل عشوائي بين دورتين (بدل رقم ثابت) — قلل الحد الأدنى/الأقصى لو
        # بدك مراقبة أسرع، بس خلي بينهم مسافة معقولة (مش 90-95) عشان يضل
        # فيه تنوّع حقيقي بالتوقيت.
        self.CHECK_INTERVAL_MIN_SECONDS = int(_get_env("CHECK_INTERVAL_MIN_SECONDS", required=False, default="90"))
        self.CHECK_INTERVAL_MAX_SECONDS = int(_get_env("CHECK_INTERVAL_MAX_SECONDS", required=False, default="240"))
        self.RUN_ONCE = _get_env("RUN_ONCE", required=False, default="false").lower() in ("1", "true", "yes")
        self.EMPTY_CYCLES_ALERT_THRESHOLD = int(
            _get_env("EMPTY_CYCLES_ALERT_THRESHOLD", required=False, default="5")
        )

        # إعدادات السكرول: كم مرة نحاول نكمل ننزل بالصفحة كل دورة، وكم مرة نسمح
        # نلاقي "ولا منشور جديد" متتالية قبل ما نعتبرها نهاية فعلية (مش بس تأخر تحميل).
        self.MAX_SCROLL_PASSES = int(_get_env("MAX_SCROLL_PASSES", required=False, default="30"))
        self.SCROLL_EMPTY_PASS_TOLERANCE = int(
            _get_env("SCROLL_EMPTY_PASS_TOLERANCE", required=False, default="4")
        )
        self.SCROLL_PRESSES_PER_PASS = int(_get_env("SCROLL_PRESSES_PER_PASS", required=False, default="3"))
        self.SCROLL_WAIT_MS = int(_get_env("SCROLL_WAIT_MS", required=False, default="3500"))

        self.USER_DATA_DIR = os.path.abspath(_get_env("USER_DATA_DIR", required=False, default="user_data"))
        self.DOWNLOAD_DIR = _get_env("DOWNLOAD_DIR", required=False, default="downloads")
        self.FB_AUTH_STATE_JSON = _get_env("FB_AUTH_STATE_JSON", required=False, default=None)

        self.LOG_DIR = _get_env("LOG_DIR", required=False, default="logs")
        self.LOG_LEVEL = _get_env("LOG_LEVEL", required=False, default="INFO")

        # مسار ملف selectors.json — بمجلد config/ افتراضياً
        selectors_path = _get_env("SELECTORS_FILE", required=False, default="config/selectors.json")
        self.selectors = self._load_selectors(selectors_path)

        os.makedirs(self.DOWNLOAD_DIR, exist_ok=True)

    @staticmethod
    def _load_selectors(path: str) -> dict:
        if not os.path.exists(path):
            raise ConfigError(f"❌ ملف الـ selectors غير موجود: {path}")
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def reload_selectors(self, path: str | None = None):
        """يسمح بإعادة تحميل selectors.json أثناء التشغيل بدون إعادة تشغيل السكربت كامل."""
        # التعديل هنا أيضاً لتوجيه المسار نحو مجلد config
        path = path or _get_env("SELECTORS_FILE", required=False, default="config/selectors.json")
        self.selectors = self._load_selectors(path)
        return self.selectors
