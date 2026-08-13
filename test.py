import os
import re
import io
import asyncio
import sqlite3
import hashlib
import warnings
from datetime import datetime
from urllib.parse import urlparse, urljoin

import aiohttp
import aiofiles
from playwright.async_api import async_playwright
import easyocr
from PIL import Image

# ==================== 🔕 SUPPRESS WARNINGS ====================
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

# ==================== ⚙️ CONFIGURATION ====================
TARGET_URLS = [
    "https://www.facebook.com/syriahr",
    "https://www.facebook.com/AlHadathSyria",
    "https://www.facebook.com/syriahro"
]

TELEGRAM_BOT_TOKEN = "6445446101:AAGSjIdYPkmiOpDJ0qTaTwyzKNN4s2Musfg"
TELEGRAM_CHAT_ID = "-1004348269004"

MAX_CONCURRENT_TASKS = 3      # عدد الصفحات التي يتم فحصها بالتوازي بنفس الوقت
CHECK_INTERVAL_SECONDS = 120  # الوقت بالثواني بين كل دورة رصد كاملة

DB_NAME = "facebook_monitor.db"
USER_DATA_DIR = os.path.abspath("user_data")
DOWNLOAD_DIR = "downloads"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ==================== 👁️ OCR ENGINE ====================
print("⏳ Initializing EasyOCR Engine for Arabic & English text extraction...")
ocr_reader = easyocr.Reader(['ar', 'en'], gpu=False)

# ==================== 🗄️ ASYNC SAFE DATABASE MANAGEMENT ====================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('PRAGMA journal_mode=WAL;')
    cursor.execute('''
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
    conn.commit()
    conn.close()

async def is_db_empty_for_target(target_url):
    def _query():
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM posts WHERE target_url = ?', (target_url,))
        count = cursor.fetchone()[0]
        conn.close()
        return count == 0
    return await asyncio.to_thread(_query)

async def is_post_exists(post_id):
    def _query():
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM posts WHERE id = ?', (post_id,))
        result = cursor.fetchone()
        conn.close()
        return result is not None
    return await asyncio.to_thread(_query)

async def is_content_duplicate(clean_text):
    if not clean_text or len(clean_text) < 10:
        return False
    def _query():
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM posts WHERE text = ? ORDER BY created_at DESC LIMIT 30', (clean_text,))
        result = cursor.fetchone()
        conn.close()
        return result is not None
    return await asyncio.to_thread(_query)

async def save_post_to_db(post_id, text, post_url, image_url, video_url, target_type, target_url):
    def _insert():
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO posts (id, text, post_url, image_url, video_url, target_type, target_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (post_id, text, post_url, image_url, video_url, target_type, target_url, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
    await asyncio.to_thread(_insert)
    print(f"💾 Saved [{post_id[:8]}] to Database!")

# ==================== ✈️ ASYNC TELEGRAM BOT CLIENT ====================
async def send_to_telegram(session, page_title, text, post_url, image_url=None, has_video=False):
    if not TELEGRAM_BOT_TOKEN:
        print("⚠️ لم يتم ضبط Telegram Bot Token!")
        return

    caption = f"📢 *{page_title}*\n\n{text[:800]}\n\n"
    if has_video:
        caption += f"🎬 [مشاهدة الفيديو على فيسبوك]({post_url})\n\n"
    caption += f"🔗 [رابط المنشور الأصلي]({post_url})"

    try:
        if image_url:
            temp_img_path = os.path.join(DOWNLOAD_DIR, f"tg_{hashlib.md5(image_url.encode()).hexdigest()[:8]}.jpg")
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
    temp_img_path = os.path.join(DOWNLOAD_DIR, f"temp_ocr_{hashlib.md5(image_url.encode()).hexdigest()[:8]}.jpg")

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

async def parse_single_post(post_element, target_type, target_url, http_session):
    is_valid = await post_element.evaluate("""
        e => {
            let aria = (e.getAttribute('aria-label') || '').toLowerCase();
            if (aria.includes('comment by') || aria.includes('reply by') || aria.includes('تعليق') || aria.includes('رد')) return false;
            let isInsideUl = e.closest('ul') !== null;
            let isComposer = e.querySelector('div[aria-label*="Write something"]') !== null || 
                             e.querySelector('div[aria-label*="بماذا تفكر"]') !== null ||
                             e.querySelector('div[aria-label*="أنشئ منشوراً"]') !== null;
            return !isInsideUl && !isComposer;
        }
    """)
    if not is_valid:
        return None, None, None, None, False

    await post_element.evaluate("""
        el => {
            const btns = Array.from(el.querySelectorAll('*'));
            btns.forEach(b => {
                if (b.innerText && (b.innerText === 'عرض المزيد' || b.innerText === 'See more')) {
                    try { b.click(); } catch(e) {}
                }
            });
        }
    """)

    # استخراج النص مع استبعاد الهيدر (اسم الصفحة وتوقيت النشر)
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
                'Like', 'Comment', 'Share', 'منشورات جديدة', 'منشور مثبت', 'Pinned post', 'عرض المزيد من التعليقات'
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
                if ((src.includes('scontent') || src.includes('fbcdn')) && 
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
        if any(keyword in href for keyword in ["/posts/", "/permalink", "pfbid", "story_fbid", "/photo", "/videos/", "/reel/"]):
            if not any(skip in href for skip in ["comment_id", "reply_comment_id", "/user/", "/friends/"]):
                post_url = f"https://www.facebook.com{href}" if href.startswith('/') else href
                break

    has_video = await post_element.evaluate("""
        e => e.querySelector('video') !== null || e.querySelector('a[href*="/videos/"], a[href*="/reel/"]') !== null
    """)

    # تنظيف وتصفية النص المجلوب
    lines = [line.strip() for line in clean_post_text.split('\n') if line.strip()]
    lines = [l for l in lines if not re.match(r'^(المرصد السوري|الحدث السوري|\d+\s*د|\d+\s*س|\.)$', l)]
    real_post_text = "\n".join(lines).strip()

    # 🎯 الشرط الذكي المحدث:
    # إذا كان هناك نص منشور حقيقي (أكثر من 20 حرف)، نكتفي به ولا نقرأ الصورة.
    # أما إذا كان المنشور بدون نص أصلي (أو مجرد توقيت واسم صفحة)، نقرأ الصورة عبر الـ OCR فوراً.
    if len(real_post_text) >= 20:
        clean_post_text = real_post_text
    elif image_url:
        ocr_text = await extract_text_from_image_url(http_session, image_url)
        if ocr_text:
            clean_post_text = ocr_text
    elif len(real_post_text) < 10:
        if has_video:
            clean_post_text = "[منشور يحتوي على فيديو فقط]"
        else:
            return None, None, None, None, False

    normalized_text_payload = re.sub(r'\s+', '', clean_post_text).strip().lower()
    clean_img_key = image_url.split('?')[0] if image_url else ""
    clean_post_key = post_url.split('?')[0] if post_url != target_url else ""

    unique_seed = f"{clean_post_key}|{clean_img_key}|{normalized_text_payload[:150]}"
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
            
            if first_run:
                raw_elements = await page.query_selector_all(element_selector)
                saved_initial = 0
                for post_elem in raw_elements:
                    post_id, text, post_url, img_url, has_video = await parse_single_post(post_elem, target_type, target_url, http_session)
                    if not post_id:
                        continue
                    if not await is_post_exists(post_id):
                        await save_post_to_db(post_id, text, post_url, img_url, post_url if has_video else None, target_type, target_url)
                        await send_to_telegram(http_session, page_title, text, post_url, img_url, has_video)
                        saved_initial += 1

                    if saved_initial >= 2:
                        break
                print(f"✅ Baseline established ({saved_initial} posts) for: {page_title}")
                return

            seen_in_run = set()
            for _ in range(5):
                raw_elements = await page.query_selector_all(element_selector)
                found_stored = False

                for post_elem in raw_elements:
                    post_id, text, post_url, img_url, has_video = await parse_single_post(post_elem, target_type, target_url, http_session)
                    if not post_id or post_id in seen_in_run:
                        continue
                    
                    seen_in_run.add(post_id)

                    if await is_post_exists(post_id) or await is_content_duplicate(text):
                        found_stored = True
                        break

                    print(f"🚨 New Post Found in [{page_title}]!")

                    await save_post_to_db(post_id, text, post_url, img_url, post_url if has_video else None, target_type, target_url)
                    await send_to_telegram(http_session, page_title, text, post_url, img_url, has_video)

                if found_stored:
                    break

                await page.keyboard.press("PageDown")
                await page.wait_for_timeout(2000)

        except Exception as e:
            print(f"❌ Error while monitoring {target_url}: {e}")
        finally:
            await page.close()

# ==================== 🚀 MAIN ASYNC LOOP ====================
async def main():
    init_db()
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)

    print("🚀 Facebook Parallel Engine Started...")
    print(f"🤖 Bot Connected: @RayImage_bot")
    print(f"🎯 Total Targets: {len(TARGET_URLS)} | Concurrency Limit: {MAX_CONCURRENT_TASKS}")

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=USER_DATA_DIR,
            headless=True,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            viewport={"width": 1366, "height": 768},
            locale="ar-SY"
        )
        
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
                
                print(f"💤 Waiting {CHECK_INTERVAL_SECONDS}s before next cycle...\n")
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    asyncio.run(main())