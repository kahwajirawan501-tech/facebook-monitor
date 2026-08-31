"""
مراقبة هدف واحد (صفحة/مجموعة/بروفايل) والبحث عن منشورات جديدة.
يتضمن آلية fallback على أكثر من selector لحاوية المنشورات، وتتبع دورات فارغة
متتالية لإرسال تنبيه لو فيسبوك غيّر بنيته وكسر الاستخراج.
"""

from .url_utils import detect_target_type

class HealthTracker:
    """يتتبع عدد الدورات الفارغة المتتالية لكل هدف، ويقرر متى يجب التنبيه."""

    def __init__(self, threshold: int):
        self.threshold = threshold
        self._empty_counts: dict[str, int] = {}
        self._alerted: set[str] = set()

    def record(self, target_url: str, found_any: bool) -> bool:
        """يرجع True لو لازم نرسل تنبيه الآن (أول مرة نتجاوز فيها العتبة)."""
        if found_any:
            self._empty_counts[target_url] = 0
            self._alerted.discard(target_url)
            return False

        self._empty_counts[target_url] = self._empty_counts.get(target_url, 0) + 1
        if self._empty_counts[target_url] >= self.threshold and target_url not in self._alerted:
            self._alerted.add(target_url)
            return True
        return False


async def find_post_elements(page, container_selectors: list[str], logger):
    """يجمع كل العناصر التي تطابق أياً من الـ selectors (union) بدل التوقف عند أول selector
    يعطي نتيجة. لو بنينا هيك: أول selector لحاله (مثلاً div[role='feed'] > div) بيرجع بس جزء
    من المنشورات، وبنضيّع منشورات تانية بتطابق selector تاني بس (div[role='article'] أو
    FeedUnit pagelet) — وهاد كان سبب توقف السكرول بدري لأنو found_new_this_pass بتصير False
    غلط، مع إنو في منشورات جديدة فعلياً بس ما انلقطوا."""
    primary_elements = await page.query_selector_all(container_selectors[0])
    combined_selector = ", ".join(container_selectors)
    elements = await page.query_selector_all(combined_selector)

    if not elements:
        return [], None

    if not primary_elements:
        logger.warning("⚠️ الـ selector الأساسي ما طابق ولا عنصر — تم الاعتماد على selectors احتياطية.")

    return elements, combined_selector


async def detect_page_title(page, selectors: dict) -> str:
    title_selectors = selectors["page_title_selectors"]
    excluded = selectors["page_title_excluded_texts"]

    return await page.evaluate(
        """
        (cfg) => {
            let h1 = document.querySelector(cfg.h1);
            let excludedList = cfg.excluded || [];
            if (h1 && h1.innerText.trim()) {
                let text = h1.innerText.trim();
                if (text && !excludedList.some(x => text.includes(x))) return text;
            }

            let profileLink = document.querySelector(cfg.profile);
            if (profileLink && profileLink.innerText.trim()) {
                let text = profileLink.innerText.trim();
                if (text && !excludedList.some(x => text.includes(x))) return text;
            }

            let headerElements = document.querySelectorAll(cfg.header_fallback);
            for (let el of headerElements) {
                let txt = el.innerText ? el.innerText.trim() : '';
                if (txt.length > 2 && txt.length < 50 &&
                    !['إعجاب', 'مشاركة', 'تعليق', 'Like', 'Share', 'Comment', ...excludedList].includes(txt)) {
                    if (el.closest('h2') || el.closest('h3') || el.closest('div[role="banner"]')) {
                        return txt;
                    }
                }
            }
            return '';
        }
        """,
        {**title_selectors, "excluded": excluded},
    )


async def dismiss_dialogs(page, close_selectors: list[str]):
    for sel in close_selectors:
        try:
            btn = await page.query_selector(sel)
            if btn:
                await btn.click()
                await page.wait_for_timeout(500)
        except Exception:
            pass


async def monitor_target(context, target_url, semaphore, http_session, *, db, telegram, parser, selectors, health, logger, scroll_config=None):
    scroll_config = scroll_config or {}
    max_scroll_passes = scroll_config.get("max_scroll_passes", 15)
    empty_pass_tolerance = scroll_config.get("empty_pass_tolerance", 2)
    scroll_presses_per_pass = scroll_config.get("scroll_presses_per_pass", 2)
    scroll_wait_ms = scroll_config.get("scroll_wait_ms", 2500)

    async with semaphore:
        target_type = detect_target_type(target_url)
        first_run = await db.is_empty_for_target(target_url)

        page = await context.new_page()
        found_any_new = False
        try:
            logger.info(f"🔄 [Start Check]: {target_url}")
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)

            # ★ تشخيص مؤقت: نلتقط سكرين شوت فعلي من الصفحة زي ما فيسبوك رجعها
            # للجلسة الآلية — عشان نتأكد هل عم يرجع الفيد الكامل ولا نسخة مقيّدة
            # (شكل شائع لما فيسبوك بيكشف إنو المتصفح Playwright headless).
            import os as _os
            if _os.environ.get("DEBUG_SCREENSHOT", "false").lower() in ("1", "true", "yes"):
                try:
                    safe_name = detect_target_type(target_url) + "_" + str(abs(hash(target_url)))[:8]
                    await page.screenshot(path=f"downloads/debug_{safe_name}.png", full_page=True)
                    logger.info(f"📸 Debug screenshot saved for {target_url}")
                except Exception as shot_err:
                    logger.warning(f"⚠️ فشل التقاط سكرين شوت تشخيصي: {shot_err}")

            await dismiss_dialogs(page, selectors["close_dialog_buttons"])

            await page.keyboard.press("PageDown")
            await page.wait_for_timeout(2000)

            page_title = await detect_page_title(page, selectors)
            if not page_title or page_title in selectors["page_title_excluded_texts"]:
                from urllib.parse import urlparse

                parsed_path = urlparse(target_url).path.strip("/")
                page_title = parsed_path if parsed_path else target_type

            container_selectors = selectors["post_container"]

            if first_run:
                saved_initial = 0
                for _ in range(3):
                    raw_elements, _ = await find_post_elements(page, container_selectors, logger)

                    for post_elem in raw_elements:
                        already_processed = await post_elem.evaluate(
                            "el => el.dataset.fbMonitorSeen === '1'"
                        )
                        if already_processed:
                            continue

                        post_id, text, post_url, img_url, has_video = await parser.parse(
                            post_elem, target_type, target_url, http_session
                        )
                        await post_elem.evaluate("el => { el.dataset.fbMonitorSeen = '1'; }")

                        if not post_id:
                            continue

                        async with db.lock:
                            if not await db.post_exists(post_id):
                                await db.save_post(post_id, text, post_url, img_url, post_url if has_video else None, target_type, target_url)
                                await telegram.send_post(http_session, page_title, text, post_url, img_url, has_video)
                                saved_initial += 1

                    if saved_initial >= 2:
                        break
                    await page.keyboard.press("PageDown")
                    await page.wait_for_timeout(2000)

                logger.info(f"✅ Baseline established ({saved_initial} posts) for: {page_title}")
                found_any_new = saved_initial > 0
                return

            seen_in_run = set()
            consecutive_empty_passes = 0

            for _scroll_pass in range(max_scroll_passes):
                raw_elements, _ = await find_post_elements(page, container_selectors, logger)

                found_new_this_pass = False

                for post_elem in raw_elements:
                    # نتفادى إعادة معالجة نفس العنصر (وبالتالي إعادة استدعاء Gemini Vision
                    # عليه من جديد) بمرور سكرول لاحق — العنصر بيضل موجود بالـ DOM حتى
                    # بعد ما نكون عالجناه، فبنعلّمه بـ dataset attribute بعد أول معالجة
                    # ونتحقق من العلامة *قبل* ما نستدعي parser.parse() المكلفة.
                    already_processed = await post_elem.evaluate(
                        "el => el.dataset.fbMonitorSeen === '1'"
                    )
                    if already_processed:
                        continue

                    post_id, text, post_url, img_url, has_video = await parser.parse(
                        post_elem, target_type, target_url, http_session
                    )
                    await post_elem.evaluate("el => { el.dataset.fbMonitorSeen = '1'; }")

                    if not post_id or post_id in seen_in_run:
                        continue

                    seen_in_run.add(post_id)
                    is_new = False

                    async with db.lock:
                        if await db.post_exists(post_id):
                            continue

                        if await db.is_recent_content_duplicate(target_url, text):
                            logger.info(f"🔁 Skipping likely duplicate content (recent match) in [{page_title}].")
                            await db.save_post(post_id, text, post_url, img_url, post_url if has_video else None, target_type, target_url)
                            continue

                        logger.info(f"🚨 New Post Found in [{page_title}]!")
                        found_new_this_pass = True
                        is_new = True
                        found_any_new = True
                        await db.save_post(post_id, text, post_url, img_url, post_url if has_video else None, target_type, target_url)

                    if is_new:
                        await telegram.send_post(http_session, page_title, text, post_url, img_url, has_video)

                # ما منوقف السكرول بمجرد أول مرور "فاضي" — فيسبوك ممكن يكون لسا عم يحمّل
                # منشورات جديدة (lazy load) وياخد وقت أطول من مرور واحد. منعطيه
                # `empty_pass_tolerance` مرات متتالية بلا جديد قبل ما نعتبرها نهاية فعلية.
                if not found_new_this_pass:
                    consecutive_empty_passes += 1
                    if consecutive_empty_passes >= empty_pass_tolerance:
                        break
                else:
                    consecutive_empty_passes = 0

                for _ in range(scroll_presses_per_pass):
                    await page.keyboard.press("PageDown")
                await page.wait_for_timeout(scroll_wait_ms)

        except Exception as e:
            logger.error(f"❌ Error while monitoring {target_url}: {e}")
        finally:
            await page.close()

        if not first_run:
            should_alert = health.record(target_url, found_any_new)
            if should_alert:
                await telegram.send_alert(
                    http_session,
                    f"لم يتم العثور على أي منشور جديد لـ {target_url} خلال {health.threshold} دورات متتالية.\n"
                    f"احتمال كبير إنو فيسبوك غيّر بنية الصفحة (HTML) وانكسر الاستخراج — يرجى المراجعة اليدوية.",
                )
