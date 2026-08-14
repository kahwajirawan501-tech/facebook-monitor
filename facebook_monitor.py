import os
import re
import io
import json
import uuid
import asyncio
import hashlib
import warnings
from datetime import datetime
from urllib.parse import urlparse, urljoin

import aiohttp
import aiofiles
from dotenv import load_dotenv
from playwright.async_api import async_playwright
import easyocr
from PIL import Image

# ==================== 🔕 SUPPRESS WARNINGS ====================
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

# ==================== ⚙️ CONFIGURATION ====================
load_dotenv()

def _get_env(name, required=True, default=None):
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        if required:
            raise RuntimeError(f"❌ متغير البيئة المطلوب غير موجود: {name}")
        return default
    return val.strip()

TARGET_URLS = [u.strip() for u in _get_env("TARGET_URLS").split(",") if u.strip()]

TELEGRAM_BOT_TOKEN = _get_env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = _get_env("TELEGRAM_CHAT_ID")

TURSO_DATABASE_URL = _get_env("TURSO_DATABASE_URL")
TURSO_AUTH_TOKEN = _get_env("TURSO_AUTH_TOKEN")

MAX_CONCURRENT_TASKS = int(_get_env("MAX_CONCURRENT_TASKS", required=False, default="3"))
CHECK_INTERVAL_SECONDS = int(_get_env("CHECK_INTERVAL_SECONDS", required=False, default="120"))
RUN_ONCE = _get_env("RUN_ONCE", required=False, default="false").lower() in ("1", "true", "yes")

USER_DATA_DIR = os.path.abspath(_get_env("USER_DATA_DIR", required=False, default="user_data"))
FB_AUTH_STATE_JSON = _get_env("FB_AUTH_STATE_JSON", required=False, default=None)
DOWNLOAD_DIR = _get_env("DOWNLOAD_DIR", required=False, default="downloads")

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# قفل لمنع تضارب الكتابة في قاعدة البيانات
db_lock: asyncio.Lock | None = None

# ==================== 👁️ OCR ENGINE ====================
print("⏳ Initializing EasyOCR Engine for Arabic & English text extraction...")
ocr_reader = easyocr.Reader(['ar', 'en'], gpu=False, verbose=False)

# ==================== 🗄️ TURSO DATABASE LAYER ====================
db_session: aiohttp.ClientSession | None = None
TURSO_PIPELINE_URL: str | None = None

def _to_http_url(url: str) -> str:
    if url.startswith("libsql://"):
        return "https://" + url[len("libsql://"):]
    if url.startswith("ws://"):
        return "http://" + url[len("ws://"):]
    return url

def _hrana_value(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": "1" if v else "0"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    return {"type": "text", "value": str(v)}

def _unwrap_row(row):
    out = []
    for cell in row:
        t = cell.get("type")
        if t == "null":
            out.append(None)
        elif t == "integer":
            out.append(int(cell["value"]))
        elif t == "float":
            out.append(float(cell["value"]))
        else:
            out.append(cell.get("value"))
    return out

async def _turso_execute(sql, args=None):
    payload = {
        "requests": [
            {"type": "execute", "stmt": {"sql": sql, "args": [_hrana_value(a) for a in (args or [])]}},
            {"type": "close"},
        ]
    }
    headers = {"Authorization": f"Bearer {TURSO_AUTH_TOKEN}"}
    async with db_session.post(TURSO_PIPELINE_URL, json=payload, headers=headers, timeout=20) as resp:
        data = await resp.json()
        if resp.status != 200:
            raise RuntimeError(f"Turso HTTP {resp.status}: {data}")
        first = data["results"][0]
        if first.get("type") != "ok":
            raise RuntimeError(f"Turso query error: {first}")
        result = first["response"]["result"]
        return [_unwrap_row(r) for r in result.get("rows", [])]

async def init_db():
    global db_session, TURSO_PIPELINE_URL
    base = _to_http_url(TURSO_DATABASE_URL).rstrip("/")
    TURSO_PIPELINE_URL = f"{base}/v2/pipeline"
    db_session = aiohttp.ClientSession()
    await _turso_execute('''
        CREATE TABLE IF NOT EXISTS posts (
            id TEXT PRIMARY KEY,
            text TEXT,
            post_url TEXT,
            image_url TEXT,
            video_url TEXT,
            target_type TEXT,
            target_url TEXT,
            created_at TIMESTAMP
        )
    ''')
    await _ensure_posts_columns()
    print("🗄️ Connected to Turso and ensured `posts` table exists.")

_REQUIRED_POSTS_COLUMNS = {
    "text": "TEXT",
    "post_url": "TEXT",
    "image_url": "TEXT",
    "video_url": "TEXT",
    "target_type": "TEXT",
    "target_url": "TEXT",
    "created_at": "TIMESTAMP",
}

async def _ensure_posts_columns():
    rows = await _turso_execute("PRAGMA table_info(posts)")
    existing_cols = {r[1] for r in rows}
    for col, col_type in _REQUIRED_POSTS_COLUMNS.items():
        if col not in existing_cols:
            await _turso_execute(f"ALTER TABLE posts ADD COLUMN {col} {col_type}")
            print(f"🔧 Added missing column `{col}` to `posts` table.")

async def close_db():
    if db_session:
        await db_session.close()

async def is_db_empty_for_target(target_url):
    rows = await _turso_execute('SELECT COUNT(*) FROM posts WHERE target_url = ?', [target_url])
    return rows[0][0] == 0

async def is_post_exists(post_id):
    rows = await _turso_execute('SELECT 1 FROM posts WHERE id = ?', [post_id])
    return len(rows) > 0

async def is_content_duplicate(clean_text):
    if not clean_text or len(clean_text) < 15 or clean_text.startswith("["):
        return False
    rows = await _turso_execute(
        'SELECT 1 FROM posts WHERE text = ? ORDER BY created_at DESC LIMIT 30', [clean_text]
    )
    return len(rows) > 0

async def save_post_to_db(post_id, text, post_url, image_url, video_url, target_type, target_url):
    await _turso_execute(
        '''INSERT INTO posts (id, text, post_url, image_url, video_url, target_type, target_url, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
        [post_id, text, post_url, image_url, video_url, target_type, target_url,
         datetime.now().strftime('%Y-%m-%d %H:%M:%S')]
    )
    print(f"💾 Saved [{post_id[:12]}] to Turso!")

# ==================== ✈️ ASYNC TELEGRAM BOT CLIENT ====================
async def send_to_telegram(session, page_title, text, post_url, image_url=None, has_video=False):
    if not TELEGRAM_BOT_TOKEN:
        print("⚠️ لم يتم ضبط Telegram Bot Token!")
        return

    caption = f"📢 *{page_title}*\n\n{text[:800]}\n\n"
    if has_video:
        caption += f"🎬 [مشاهدة البث / الفيديو على فيسبوك]({post_url})\n\n"
    caption += f"🔗 [رابط المنشور الأصلي]({post_url})"

    try:
        if image_url:
            unique_filename = f"tg_{uuid.uuid4().hex}.jpg"
            temp_img_path = os.path.join(DOWNLOAD_DIR, unique_filename)
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

            try:
                async with session.get(image_url, headers=headers, timeout=15) as img_resp:
                    if img_resp.status == 200:
                        img_bytes = await img_resp.read()
                        async with aiofiles.open(temp_img_path, 'wb') as f:
                            await f.write(img_bytes)

                        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
                        async with aiofiles.open(temp_img_path, 'rb') as f:
                            photo_data = await f.read()

                        data = aiohttp.FormData()
                        data.add_field('chat_id', TELEGRAM_CHAT_ID)
                        data.add_field('caption', caption)
                        data.add_field('parse_mode', 'Markdown')
                        data.add_field('photo', photo_data, filename="image.jpg")

                        async with session.post(url, data=data, timeout=20) as resp:
                            if resp.status == 200:
                                print(f"✈️ [Telegram]: Photo + Link sent for {page_title}")

                        if os.path.exists(temp_img_path):
                            try:
                                os.remove(temp_img_path)
                            except Exception:
                                pass
                        return
            except Exception as img_err:
                print(f"⚠️ Failed to process image for Telegram: {img_err}")
                if os.path.exists(temp_img_path):
                    try:
                        os.remove(temp_img_path)
                    except Exception:
                        pass

        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {'chat_id': TELEGRAM_CHAT_ID, 'text': caption, 'parse_mode': 'Markdown', 'disable_web_page_preview': False}
        async with session.post(url, json=payload, timeout=15) as resp:
            if resp.status == 200:
                print(f"✈️ [Telegram]: Text & Link sent for {page_title}")

    except Exception as e:
        print(f"⚠️ Telegram sending failed: {e}")

# ==================== 👁️ RAW EASYOCR ENGINE ====================
async def extract_text_from_image_url(session, image_url):
    if not image_url:
        return ""

    full_url = urljoin("https://www.facebook.com", image_url)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    unique_filename = f"temp_ocr_{uuid.uuid4().hex}.jpg"
    temp_img_path = os.path.join(DOWNLOAD_DIR, unique_filename)

    try:
        async with session.get(full_url, headers=headers, timeout=15) as resp:
            if resp.status == 200:
                img_bytes = await resp.read()

                async with aiofiles.open(temp_img_path, 'wb') as f:
                    await f.write(img_bytes)

                results = await asyncio.to_thread(
                    ocr_reader.readtext,
                    temp_img_path,
                    detail=0,
                    paragraph=True
                )

                if os.path.exists(temp_img_path):
                    try:
                        os.remove(temp_img_path)
                    except Exception:
                        pass

                extracted_text = "\n".join(results).strip()
                if extracted_text:
                    return extracted_text
    except Exception as e:
        print(f"⚠️ OCR error: {e}")
        if os.path.exists(temp_img_path):
            try:
                os.remove(temp_img_path)
            except Exception:
                pass

    return ""

# ==================== 🔑 FB ID EXTRACTION ====================
def extract_facebook_post_id(url: str) -> str | None:
    if not url:
        return None
    
    pfbid_match = re.search(r'pfbid([a-zA-Z0-9]+)', url)
    if pfbid_match:
        return f"pfbid_{pfbid_match.group(1)}"

    posts_match = re.search(r'/posts/(\d+)', url)
    if posts_match:
        return f"post_{posts_match.group(1)}"

    fbid_match = re.search(r'(?:story_fbid=|fbid=)(\d+)', url)
    if fbid_match:
        return f"fbid_{fbid_match.group(1)}"

    media_match = re.search(r'/(?:videos|reel|watch)\/(\d+)', url)
    if media_match:
        return f"media_{media_match.group(1)}"

    return None

# ==================== 📄 POST PARSER ====================
async def parse_single_post(post_element, target_type, target_url, http_session):
    is_valid = await post_element.evaluate("""
        e => {
            let aria = (e.getAttribute('aria-label') || '').toLowerCase();
            if (aria.includes('comment by') || aria.includes('reply by') || aria.includes('تعليق') || aria.includes('رد')) return false;
            let isInsideUl = e.closest('ul') !== null;
            let isComposer = e.querySelector('div[aria-label*="Write something"]') !== null || 
                             e.querySelector('div[aria-label*="بماذا تفكر"]') !== null || 
                             e.querySelector('div[aria-label*="أنشئ منشوراً"]') !== null;
            if (isInsideUl || isComposer) return false;

            let innerText = e.innerText || '';
            if (innerText.includes('العناصر المميزة') || innerText.includes('Featured') || 
                innerText.includes('منشورات أخرى') || innerText.includes('More posts')) return false;

            let isInsideIgnored = e.closest('div[aria-label*="العناصر المميزة"]') !== null ||
                                  e.closest('div[aria-label*="Featured"]') !== null ||
                                  e.closest('div[aria-label*="منشورات أخرى"]') !== null ||
                                  e.closest('div[aria-label*="More posts"]') !== null ||
                                  (e.closest('div[role="region"]') !== null && e.closest('div[role="region"]').innerText.includes('العناصر المميزة'));
            
            return !isInsideIgnored;
        }
    """)
    if not is_valid:
        return None, None, None, None, False

    # توسيع النصوص الطويلة
    await post_element.evaluate("""
        el => {
            const btns = Array.from(el.querySelectorAll('div[role="button"], span[role="button"], a, span'));
            btns.forEach(b => {
                let txt = (b.innerText || '').trim();
                if (txt === 'عرض المزيد' || txt === 'See more') {
                    try { b.click(); } catch(e) {}
                }
            });
        }
    """)

    clean_post_text = await post_element.evaluate("""
        container => {
            const isHeaderOrComment = (el) => {
                return el.closest('h2') !== null ||
                       el.closest('h3') !== null ||
                       el.closest('ul[role="group"]') !== null || 
                       el.closest('div[aria-label*="تعليق"]') !== null ||
                       el.closest('div[aria-label*="Comment"]') !== null ||
                       el.closest('div[role="toolbar"]') !== null;
            };

            const textNodes = Array.from(container.querySelectorAll('div[dir="auto"], span[dir="auto"]'));
            let chunks = [];

            textNodes.forEach(node => {
                if (!isHeaderOrComment(node)) {
                    let txt = node.innerText ? node.innerText.trim() : '';
                    if (txt.length > 1) chunks.push(txt);
                }
            });

            let fullText = chunks.join('\\n');
            const uiPhrases = [
                'عرض المزيد', 'See more', 'عرض أقل', 'See less', 'أعجبني', 'تعليق', 'مشاركة', 
                'Like', 'Comment', 'Share', 'منشورات جديدة', 'منشور مثبت', 'Pinned post', 
                'عرض المزيد من التعليقات', 'العناصر المميزة', 'Featured',
                'منشورات أخرى', 'More posts', 'More Posts'
            ];
            
            return fullText
                .split('\\n')
                .map(line => line.trim())
                .filter(line => line && !uiPhrases.includes(line) && !/^[0-9٠-٩\\s٫,KkMmAaدس]+$/.test(line))
                .filter((item, index, self) => self.indexOf(item) === index)
                .join('\\n');
        }
    """)

    image_url = await post_element.evaluate("""
        e => {
            let imgs = Array.from(e.querySelectorAll('img'));
            for (let img of imgs) {
                let src = img.src || img.getAttribute('data-src') || '';
                if ((src.includes('scontent') || src.includes('fbcdn') || src.includes('external')) && 
                    !src.includes('rsrc.php') && !src.includes('emoji.php') && 
                    !src.includes('p50x50') && !src.includes('p160x160')) {
                    return src;
                }
            }
            return null;
        }
    """)

    all_links = await post_element.query_selector_all('a[href]')
    post_url = target_url
    for link in all_links:
        href = await link.get_attribute('href') or ""
        if any(keyword in href for keyword in [
            "/posts/", "/permalink", "pfbid", "story_fbid", "/photo", 
            "/videos/", "/reel/", "/watch", "/live/", "l.facebook.com"
        ]):
            if not any(skip in href for skip in ["comment_id", "reply_comment_id", "/user/", "/friends/"]):
                post_url = f"https://www.facebook.com{href}" if href.startswith('/') else href
                break

    # 🎬 كشف دقيق للفيديوهات والبث المباشر
    has_video = await post_element.evaluate("""
        e => e.querySelector('video, [data-video-id], [aria-label*="تشغيل"], [aria-label*="Play"]') !== null || 
             e.querySelector('a[href*="/videos/"], a[href*="/reel/"], a[href*="/watch"], a[href*="/live/"]') !== null ||
             (e.innerText && (e.innerText.includes('مباشر') || e.innerText.includes('LIVE') || e.innerText.includes('فيديو')))
    """)

    lines = [line.strip() for line in clean_post_text.split('\n') if line.strip()]
    lines = [l for l in lines if not re.match(r'^(المرصد السوري|الحدث السوري|رامي عبد الرحمن|\d+\s*د|\d+\s*س|\.|\s*)$', l)]
    real_post_text = "\n".join(lines).strip()

    # 📝 تحديد النص النهائي دون إهمال أي منشور
    if len(real_post_text) >= 10:
        clean_post_text = real_post_text
    elif has_video:
        clean_post_text = real_post_text if real_post_text else "[بث مباشر / مقطع فيديو]"
    elif image_url:
        ocr_text = await extract_text_from_image_url(http_session, image_url)
        clean_post_text = ocr_text if ocr_text else (real_post_text if real_post_text else "[منشور يحتوي على صورة]")
    elif len(real_post_text) >= 1:
        clean_post_text = real_post_text
    else:
        clean_post_text = "[منشور على فيسبوك]"

    # 🎯 توليد الـ Post ID الحقيقي الثابت بدقة
    fb_id = extract_facebook_post_id(post_url)
    if not fb_id and image_url:
        fb_id = extract_facebook_post_id(image_url)

    if fb_id:
        post_id = fb_id
    else:
        normalized_text = re.sub(r'[^ء-يa-zA-Z0-9]', '', clean_post_text).strip().lower()
        unique_seed = f"{target_url}|{normalized_text[:120]}"
        post_id = hashlib.md5(unique_seed.encode('utf-8')).hexdigest()

    return post_id, clean_post_text, post_url, image_url, has_video

# ==================== 🛠️ EXTRACTION & HELPER FUNCTIONS ====================
def detect_target_type(url):
    parsed = urlparse(url)
    path = parsed.path.lower()
    if "/groups/" in path:
        return "GROUP"
    elif "/profile.php" in path or "/people/" in path or "id=" in parsed.query:
        return "PROFILE"
    else:
        return "PAGE"

# ==================== ⚡ PARALLEL MONITORING WORKER ====================
async def monitor_target_worker(context, target_url, semaphore, http_session):
    async with semaphore:
        target_type = detect_target_type(target_url)
        first_run = await is_db_empty_for_target(target_url)

        page = await context.new_page()
        try:
            print(f"🔄 [Start Check]: {target_url}")
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)

            close_selectors = [
                'div[role="dialog"] div[aria-label="إغلاق"]', 
                'div[role="dialog"] div[aria-label="Close"]',
                'div[aria-label="Decline optional cookies"]',
                'div[aria-label="رفض الكوكيز الاختيارية"]'
            ]
            for sel in close_selectors:
                try:
                    btn = await page.query_selector(sel)
                    if btn:
                        await btn.click()
                        await page.wait_for_timeout(500)
                except Exception:
                    pass

            await page.keyboard.press("PageDown")
            await page.wait_for_timeout(2000)

            page_title = await page.evaluate("""
                () => {
                    let h1 = document.querySelector('h1');
                    if (h1 && h1.innerText.trim()) return h1.innerText.trim();
                    let spans = Array.from(document.querySelectorAll('span[dir="auto"]'));
                    for (let s of spans) {
                        if (s.innerText && s.innerText.length > 2 && !s.innerText.includes('الإشعارات') && !s.innerText.includes('تسجيل الدخول')) {
                            return s.innerText.trim();
                        }
                    }
                    return '';
                }
            """)

            if not page_title or page_title in ['الإشعارات', 'تسجيل الدخول']:
                parsed_path = urlparse(target_url).path.strip('/')
                page_title = parsed_path if parsed_path else target_type

            element_selector = 'div[role="feed"] > div, div[role="article"], div[data-pagelet*="FeedUnit"]'

            # 🎯 الدورة الأولى: تأسيس الحد المرجعي (Baseline)
            if first_run:
                saved_initial = 0
                for _ in range(3):
                    raw_elements = await page.query_selector_all(element_selector)
                    for post_elem in raw_elements:
                        post_id, text, post_url, img_url, has_video = await parse_single_post(post_elem, target_type, target_url, http_session)
                        if not post_id:
                            continue
                        
                        async with db_lock:
                            if not await is_post_exists(post_id) and not await is_content_duplicate(text):
                                await save_post_to_db(post_id, text, post_url, img_url, post_url if has_video else None, target_type, target_url)
                                await send_to_telegram(http_session, page_title, text, post_url, img_url, has_video)
                                saved_initial += 1

                        if saved_initial >= 2:
                            break
                    if saved_initial >= 2:
                        break
                    await page.keyboard.press("PageDown")
                    await page.wait_for_timeout(2000)

                print(f"✅ Baseline established ({saved_initial} posts) for: {page_title}")
                return

            # 🎯 الدورات التالية: الفحص حتى الوصول لأول منشور مسجل مسبقاً
            seen_in_run = set()
            hit_known_post = False

            for scroll_pass in range(5):
                raw_elements = await page.query_selector_all(element_selector)

                for post_elem in raw_elements:
                    post_id, text, post_url, img_url, has_video = await parse_single_post(post_elem, target_type, target_url, http_session)
                    if not post_id or post_id in seen_in_run:
                        continue

                    seen_in_run.add(post_id)

                    async with db_lock:
                        if await is_post_exists(post_id) or await is_content_duplicate(text):
                            print(f"🛑 Reached already known post [{post_id[:12]}] in [{page_title}]. Stopping scroll.")
                            hit_known_post = True
                            break

                        # ✨ منشور جديد تماماً
                        print(f"🚨 New Post Found in [{page_title}]!")
                        await save_post_to_db(post_id, text, post_url, img_url, post_url if has_video else None, target_type, target_url)
                        await send_to_telegram(http_session, page_title, text, post_url, img_url, has_video)

                if hit_known_post:
                    break

                await page.keyboard.press("PageDown")
                await page.wait_for_timeout(2000)

        except Exception as e:
            print(f"❌ Error while monitoring {target_url}: {e}")
        finally:
            await page.close()

# ==================== 🚀 MAIN ASYNC LOOP ====================
async def main():
    global db_lock
    db_lock = asyncio.Lock()
    await init_db()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

    print("🚀 Facebook Parallel Engine Started...")
    print(f"🎯 Total Targets: {len(TARGET_URLS)} | Concurrency Limit: {MAX_CONCURRENT_TASKS}")

    browser = None
    context = None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)

            context_kwargs = dict(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                viewport={"width": 1366, "height": 768},
                locale="ar-SY",
            )

            if FB_AUTH_STATE_JSON:
                try:
                    storage_state = json.loads(FB_AUTH_STATE_JSON)
                    context = await browser.new_context(storage_state=storage_state, **context_kwargs)
                    print("🔐 Loaded authenticated Facebook session from FB_AUTH_STATE_JSON.")
                except json.JSONDecodeError as e:
                    print(f"⚠️ FB_AUTH_STATE_JSON is not valid JSON ({e}) — falling back to anonymous session.")
                    context = await browser.new_context(**context_kwargs)
            else:
                context = await browser.new_context(**context_kwargs)
                print("⚠️ No FB_AUTH_STATE_JSON set — running as anonymous guest (limited content).")

            async with aiohttp.ClientSession() as http_session:
                while True:
                    start_time = datetime.now()
                    print(f"\n⚡ [Cycle Start]: Monitoring pages in parallel...")

                    tasks = [
                        monitor_target_worker(context, url, semaphore, http_session)
                        for url in TARGET_URLS
                    ]
                    await asyncio.gather(*tasks)

                    elapsed = (datetime.now() - start_time).seconds
                    print(f"🏁 Cycle finished in {elapsed}s.")

                    if RUN_ONCE:
                        print("🏁 RUN_ONCE=true → إنهاء التنفيذ بعد دورة واحدة.")
                        break

                    print(f"💤 Waiting {CHECK_INTERVAL_SECONDS}s before next cycle...\n")
                    await asyncio.sleep(CHECK_INTERVAL_SECONDS)
    finally:
        await close_db()
        try:
            if context:
                await context.close()
            if browser:
                await browser.close()
        except Exception:
            pass

if __name__ == "__main__":
    asyncio.run(main())
