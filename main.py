"""
نقطة الدخول: يشبك كل الموديولات ويشغّل دورة المراقبة المتوازية.
"""

import asyncio
import json
import random
import warnings

import aiohttp

try:
    # patchright: بديل شبه متطابق لـ playwright بس بيرقّع أشهر نقاط كشف
    # الأتمتة (navigator.webdriver, CDP leaks...). لو مو مثبّت، منرجع لـ
    # playwright العادي تلقائياً بدون ما ينكسر شي.
    from patchright.async_api import async_playwright
    _BROWSER_ENGINE = "patchright"
except ImportError:
    from playwright.async_api import async_playwright
    _BROWSER_ENGINE = "playwright (patchright غير مثبّت)"

from src.config import Config
from src.db import Database
from src.logger_setup import setup_logger
from src.monitor import HealthTracker, monitor_target
from src.ocr_engine import GeminiOCR
from src.post_parser import PostParser
from src.telegram_client import TelegramClient

warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)


async def run():
    cfg = Config()
    logger = setup_logger(log_dir=cfg.LOG_DIR, level=cfg.LOG_LEVEL)

    logger.info(f"⏳ Initializing Google GenAI Clients ({len(cfg.GEMINI_KEYS)} keys loaded)...")
    logger.info(f"🕵️ Browser engine: {_BROWSER_ENGINE}")
    ocr = GeminiOCR(cfg.GEMINI_KEYS, cfg.GEMINI_MODEL, cfg.DOWNLOAD_DIR, logger)
    parser = PostParser(cfg.selectors, ocr, logger)
    telegram = TelegramClient(cfg.TELEGRAM_BOT_TOKEN, cfg.TELEGRAM_CHAT_ID, cfg.DOWNLOAD_DIR, logger)
    health = HealthTracker(cfg.EMPTY_CYCLES_ALERT_THRESHOLD)

    db = Database(cfg.TURSO_DATABASE_URL, cfg.TURSO_AUTH_TOKEN, logger)
    await db.connect()

    semaphore = asyncio.Semaphore(cfg.MAX_CONCURRENT_TASKS)

    logger.info("🚀 Facebook Parallel Engine Started...")
    logger.info(f"🎯 Total Targets: {len(cfg.TARGET_URLS)} | Concurrency Limit: {cfg.MAX_CONCURRENT_TASKS}")

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

            if cfg.FB_AUTH_STATE_JSON:
                try:
                    storage_state = json.loads(cfg.FB_AUTH_STATE_JSON)
                    context = await browser.new_context(storage_state=storage_state, **context_kwargs)
                    logger.info("🔐 Loaded authenticated Facebook session from FB_AUTH_STATE_JSON.")
                except json.JSONDecodeError as e:
                    logger.warning(f"⚠️ FB_AUTH_STATE_JSON is not valid JSON ({e}) — falling back to anonymous session.")
                    context = await browser.new_context(**context_kwargs)
            else:
                context = await browser.new_context(**context_kwargs)
                logger.warning("⚠️ No FB_AUTH_STATE_JSON set — running as anonymous guest (limited content).")

            async with aiohttp.ClientSession() as http_session:
                while True:
                    start_time = asyncio.get_event_loop().time()
                    logger.info("⚡ [Cycle Start]: Monitoring pages in parallel...")

                    tasks = [
                        monitor_target(
                            context,
                            url,
                            semaphore,
                            http_session,
                            db=db,
                            telegram=telegram,
                            parser=parser,
                            selectors=cfg.selectors,
                            health=health,
                            logger=logger,
                            scroll_config={
                                "max_scroll_passes": cfg.MAX_SCROLL_PASSES,
                                "empty_pass_tolerance": cfg.SCROLL_EMPTY_PASS_TOLERANCE,
                                "scroll_presses_per_pass": cfg.SCROLL_PRESSES_PER_PASS,
                                "scroll_wait_ms": cfg.SCROLL_WAIT_MS,
                            },
                        )
                        for url in cfg.TARGET_URLS
                    ]
                    await asyncio.gather(*tasks)

                    elapsed = asyncio.get_event_loop().time() - start_time
                    logger.info(f"🏁 Cycle finished in {elapsed:.0f}s.")

                    if cfg.RUN_ONCE:
                        logger.info("🏁 RUN_ONCE=true → إنهاء التنفيذ بعد دورة واحدة.")
                        break

                    # ★ فاصل زمني عشوائي (مش رقم ثابت) — نمط تشغيل بفاصل ثابت
                    # طول الوقت (كل 120 ثانية بالظبط مثلاً) هو نفسه توقيع سلوكي
                    # سهل الكشف. منسحب رقم عشوائي بين حد أدنى وأقصى بكل دورة.
                    wait_s = random.uniform(cfg.CHECK_INTERVAL_MIN_SECONDS, cfg.CHECK_INTERVAL_MAX_SECONDS)
                    logger.info(f"💤 Waiting {wait_s:.0f}s (randomized) before next cycle...")
                    await asyncio.sleep(wait_s)
    finally:
        await db.close()
        try:
            if context:
                await context.close()
            if browser:
                await browser.close()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(run())
