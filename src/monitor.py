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
    """يجرب selectors حاوية المنشورات بالترتيب لحد ما وحدة ترجع نتائج، ويسجل أيها نجح."""
    for selector in container_selectors:
        elements = await page.query_selector_all(selector)
        if elements:
            return elements, selector
    return [], None


async def detect_page_title(page, selectors: dict) -> str:
    title_selectors = selectors["page_title_selectors"]
    excluded = selectors["page_title_excluded_texts"]

    return await page.evaluate(
        """
        (cfg) => {
            let h1 = document.querySelector(cfg.h1);
            if (h1 && h1.innerText.trim()) {
                let text = h1.innerText.trim();
                if (text && !cfg.excluded.some(x => text.includes(x))) return text;
            }

            let profileLink = document.querySelector(cfg.profile);
            if (profileLink && profileLink.innerText.trim()) {
                let text = profileLink.innerText.trim();
                if (text && !cfg.excluded.some(x => text.includes(x))) return text;
            }

            let headerElements = document.querySelectorAll(cfg.header_fallback);
            for (let el of headerElements) {
                let txt = el.innerText.trim();
                if (txt.length > 2 && txt.length < 50 &&
                    !['إعجاب', 'مشاركة', 'تعليق', 'Like', 'Share', 'Comment', ...cfg.excluded].includes(txt)) {
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


async def monitor_target(context, target_url, semaphore, http_session, *, db, telegram, parser, selectors, health, logger):
    async with semaphore:
        target_type = detect_target_type(target_url)
        first_run = await db.is_empty_for_target(target_url)

        page = await context.new_page()
        found_any_new = False
        try:
            logger.info(f"🔄 [Start Check]: {target_url}")
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4000)

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
                    raw_elements, used_selector = await find_post_elements(page, container_selectors, logger)
                    if used_selector and used_selector != container_selectors[0]:
                        logger.warning(f"⚠️ استخدم fallback selector: '{used_selector}' بدل الأساسي — قد يشير لتغيّر ببنية فيسبوك.")

                    for post_elem in raw_elements:
                        post_id, text, post_url, img_url, has_video = await parser.parse(
                            post_elem, target_type, target_url, http_session
                        )
                        if not post_id:
                            continue

                        async with db.lock:
                            if not await db.post_exists(post_id):
                                await db.save_post(post_id, text, post_url, img_url, post_url if has_video else None, target_type, target_url)
                                await telegram.send_post(http_session, page_title, text, post_url, img_url, has_video)
                                saved_initial += 1

                        if saved_initial >= 2:
                            break
                    if saved_initial >= 2:
                        break
                    await page.keyboard.press("PageDown")
                    await page.wait_for_timeout(2000)

                logger.info(f"✅ Baseline established ({saved_initial} posts) for: {page_title}")
                found_any_new = saved_initial > 0
                return

            seen_in_run = set()

            for _scroll_pass in range(5):
                raw_elements, used_selector = await find_post_elements(page, container_selectors, logger)
                if used_selector and used_selector != container_selectors[0]:
                    logger.warning(f"⚠️ استخدم fallback selector: '{used_selector}' بدل الأساسي — قد يشير لتغيّر ببنية فيسبوك.")

                found_new_this_pass = False

                for post_elem in raw_elements:
                    post_id, text, post_url, img_url, has_video = await parser.parse(
                        post_elem, target_type, target_url, http_session
                    )
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

                if not found_new_this_pass:
                    break

                await page.keyboard.press("PageDown")
                await page.wait_for_timeout(2000)

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
