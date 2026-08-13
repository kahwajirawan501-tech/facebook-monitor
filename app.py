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

TARGET_URLS = [
    "https://mbasic.facebook.com/syriahr",
    "https://mbasic.facebook.com/AlHadathSyria",
    "https://mbasic.facebook.com/syriahro",
]

def _require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"متغير البيئة {name} غير مضبوط. اضبطه من لوحة Render قبل التشغيل."
        )
    return value


TELEGRAM_BOT_TOKEN = _require_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _require_env("TELEGRAM_CHAT_ID")

TURSO_DATABASE_URL = _require_env("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = _require_env("TURSO_AUTH_TOKEN")

CHECK_SECRET = os.environ.get("CHECK_SECRET", "")

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/90.0.4430.91 Mobile Safari/537.36"
    ),
}

MAX_NEW_POSTS_PER_TARGET = 5

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
    resp.raise_for_status()
    return resp.text


# ==================== 🔄 دورة فحص واحدة (تُستدعى من /check) ====================
def run_check_cycle():
    summary = []
    client = None

    try:
        client = get_db_client()
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

    except Exception as db_err:
        log.error(f"❌ خطأ في التعامل مع قاعدة البيانات Turso: {db_err}")
        raise db_err
    finally:
        if client and hasattr(client, "close"):
            client.close()

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
