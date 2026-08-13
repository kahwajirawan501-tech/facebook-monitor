"""
Facebook Lightweight Monitor
============================
نسخة خفيفة الاستهلاك من مشروع مراقبة صفحات فيسبوك:

- بدون Playwright / Chromium وبدون EasyOCR → استهلاك ذاكرة أقل بكثير
  (يعتمد على mbasic.facebook.com، النسخة النصية البسيطة من فيسبوك)
- بدون حلقة "while True" دائمة → السكربت يعمل كخدمة ويب صغيرة (Flask)
  فيها endpoint واحد GET /check ينفذ "دورة فحص واحدة" فقط ثم يرد ويتوقف
- التخزين على Turso (SQLite متوافق، سحابي) بدل ملف SQLite محلي،
  لأن خدمات الاستضافة المجانية (مثل Render Free) لا تضمن بقاء الملفات
  المحلية بين كل سكون/استيقاظ للخدمة.

طريقة التشغيل: يُستدعى GET /check دوريًا من مجدول خارجي مجاني
(cron-job.org أو GitHub Actions) كل 3-5 دقائق. هذا الاستدعاء نفسه
يوقظ الخدمة من وضع السكون على Render المجاني.

⚠️ ملاحظة مهمة: بنية صفحات mbasic.facebook.com (أسماء العناصر/الروابط)
قد تتغير من طرف فيسبوك بدون إشعار مسبق. إذا توقف استخراج المنشورات
عن العمل، الدالة extract_posts() هي أول مكان يجب فحصه وتحديثه بعد
معاينة الـ HTML الفعلي الذي يعيده الطلب.
"""

import os
import re
import hashlib
import logging
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request
import libsql_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fb-monitor")

# ==================== ⚙️ الإعدادات ====================
# لا أسرار مكتوبة داخل الكود إطلاقًا - كل شيء حساس يأتي من متغيرات البيئة
# (يُضبط من لوحة Render: Environment > Add Environment Variable)

TARGET_URLS = [
    "https://mbasic.facebook.com/syriahr",
    "https://mbasic.facebook.com/AlHadathSyria",
    "https://mbasic.facebook.com/syriahro",
]

def _require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"متغير البيئة {name} غير مضبوط. اضبطه من لوحة Render قبل التشغيل — "
            "لا تكتب القيم الحساسة مباشرة داخل الكود."
        )
    return value


TELEGRAM_BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _require_env("TELEGRAM_CHAT_ID")

TURSO_DATABASE_URL = _require_env("TURSO_DATABASE_URL")  # مثال: libsql://your-db-name.turso.io
TURSO_AUTH_TOKEN = _require_env("TURSO_AUTH_TOKEN")

# مفتاح بسيط لحماية /check من أي زائر عشوائي يستدعيه من المتصفح
CHECK_SECRET = os.environ.get("CHECK_SECRET", "")

# كوكيز جلسة فيسبوك (اختياري لكن غالبًا ضروري) - انسخها من متصفح مسجّل دخوله
# انظر تعليمات الحصول عليها في README. بدونها mbasic يعيد التوجيه لصفحة تسجيل الدخول.
FB_COOKIE = os.environ.get("FB_COOKIE", "")

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36"
    ),
    "Accept-Language": "ar,en-US;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
if FB_COOKIE:
    REQUEST_HEADERS["Cookie"] = FB_COOKIE

MAX_NEW_POSTS_PER_TARGET = 5  # حماية من إغراق تيليجرام لو تغيرت بنية الصفحة فجأة

app = Flask(__name__)


# ==================== 🗄️ قاعدة البيانات (Turso) ====================
def get_db_client():
    return libsql_client.create_client_sync(url=TURSO_DATABASE_URL, auth_token=TURSO_AUTH_TOKEN)


def init_db(client):
    client.execute(
        """
        CREATE TABLE IF NOT EXISTS posts (
            id TEXT PRIMARY KEY,
            text TEXT,
            post_url TEXT,
            target_url TEXT,
            created_at TEXT
        )
        """
    )


def is_post_exists(client, post_id):
    result = client.execute("SELECT 1 FROM posts WHERE id = ?", [post_id])
    return len(result.rows) > 0


def save_post(client, post_id, text, post_url, target_url):
    client.execute(
        "INSERT INTO posts (id, text, post_url, target_url, created_at) VALUES (?, ?, ?, ?, ?)",
        [post_id, text, post_url, target_url, datetime.now(timezone.utc).isoformat()],
    )


# ==================== ✈️ تيليجرام ====================
def send_to_telegram(page_title, text, post_url):
    caption = f"📢 *{page_title}*\n\n{text[:800]}\n\n🔗 [رابط المنشور]({post_url})"
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": caption,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            log.info(f"✈️ تم الإرسال: {page_title}")
        else:
            log.warning(f"⚠️ فشل إرسال تيليجرام ({resp.status_code}): {resp.text[:200]}")
    except Exception as e:
        log.warning(f"⚠️ خطأ إرسال تيليجرام: {e}")


# ==================== 🔍 استخراج المنشورات من mbasic ====================
STORY_LINK_PATTERN = re.compile(r"(story_fbid=|/posts/|/permalink\.php|story\.php)")
SKIP_TEXT_PATTERNS = re.compile(r"^(أعجبني|تعليق|مشاركة|Like|Comment|Share|·|\d+\s*(د|س|سا))$")


def extract_page_title(soup, fallback):
    title_tag = soup.find("title")
    if title_tag and title_tag.text.strip():
        return title_tag.text.strip()
    return fallback


def extract_posts(html):
    """
    يستخرج قائمة منشورات (id, text, post_url) من HTML صفحة mbasic.
    الاستراتيجية: نبحث عن روابط تشبه روابط منشورات (story_fbid / posts / permalink)
    ثم نصعد لأقرب حاوية أب ونجمع نصها كنص المنشور.
    """
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    seen_links = set()

    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not STORY_LINK_PATTERN.search(href):
            continue

        post_url = href
        if post_url.startswith("/"):
            post_url = "https://mbasic.facebook.com" + post_url

        clean_link_key = post_url.split("&")[0].split("?")[0]
        if clean_link_key in seen_links:
            continue
        seen_links.add(clean_link_key)

        container = link.find_parent(["div", "article"])
        text = ""
        if container:
            lines = [
                t.strip()
                for t in container.stripped_strings
                if t.strip() and not SKIP_TEXT_PATTERNS.match(t.strip())
            ]
            text = "\n".join(lines[:6]).strip()

        if len(text) < 5:
            continue

        normalized = re.sub(r"\s+", "", text).lower()
        seed = f"{clean_link_key}|{normalized[:150]}"
        post_id = hashlib.md5(seed.encode("utf-8")).hexdigest()

        posts.append({"id": post_id, "text": text, "post_url": post_url})

    return posts


def fetch_target(target_url):
    resp = requests.get(target_url, headers=REQUEST_HEADERS, timeout=20)

    if "login.php" in resp.url or "checkpoint" in resp.url:
        raise RuntimeError(
            "فيسبوك أعاد التوجيه لصفحة تسجيل الدخول - غالبًا FB_COOKIE غير مضبوط "
            "أو انتهت صلاحية الجلسة ويجب استخراج كوكيز جديدة."
        )

    if "unsupported-interstitial" in resp.text or "<title>خطأ</title>" in resp.text[:2000]:
        raise RuntimeError(
            "فيسبوك أرجع صفحة \"المتصفح غير مدعوم\" بدل المحتوى - "
            "غالبًا بسبب User-Agent قديم يجب تحديثه."
        )

    resp.raise_for_status()
    return resp.text


# ==================== 🔄 دورة فحص واحدة (تُستدعى من /check) ====================
def run_check_cycle():
    summary = []

    with get_db_client() as client:
        init_db(client)

        for target_url in TARGET_URLS:
            try:
                html = fetch_target(target_url)
            except Exception as e:
                log.warning(f"❌ فشل تحميل {target_url}: {e}")
                summary.append({"target": target_url, "error": str(e)})
                continue

            soup = BeautifulSoup(html, "html.parser")
            page_title = extract_page_title(soup, target_url)
            posts = extract_posts(html)

            new_count = 0
            for post in posts:
                if new_count >= MAX_NEW_POSTS_PER_TARGET:
                    break
                if is_post_exists(client, post["id"]):
                    continue
                save_post(client, post["id"], post["text"], post["post_url"], target_url)
                send_to_telegram(page_title, post["text"], post["post_url"])
                new_count += 1

            log.info(f"✅ {page_title}: {new_count} منشور جديد من أصل {len(posts)} مكتشف")
            summary.append({"target": target_url, "new_posts": new_count, "found": len(posts)})

    return summary


# ==================== 🌐 نقاط النهاية ====================
@app.route("/")
def home():
    return jsonify({"status": "alive", "service": "fb-monitor-lite"})


@app.route("/check")
def check():
    if CHECK_SECRET and request.args.get("key", "") != CHECK_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    result = run_check_cycle()
    return jsonify({"status": "done", "results": result})


@app.route("/debug")
def debug():
    """
    نقطة تشخيصية مؤقتة: تجلب صفحة هدف واحد وتُرجع عيّنة من HTML وكل روابط <a>
    الموجودة فيها، لمساعدتنا على معرفة بنية mbasic الحالية الفعلية وتصحيح
    extract_posts() على أساسها. احذفها بعد انتهاء التشخيص.
    """
    if CHECK_SECRET and request.args.get("key", "") != CHECK_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    target = request.args.get("target", TARGET_URLS[0])
    try:
        html = fetch_target(target)
    except Exception as e:
        return jsonify({"error": str(e), "target": target}), 200

    soup = BeautifulSoup(html, "html.parser")
    hrefs = [a["href"] for a in soup.find_all("a", href=True)]

    return jsonify({
        "target": target,
        "html_length": len(html),
        "sample_html": html[:4000],
        "all_hrefs": hrefs[:80],
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
