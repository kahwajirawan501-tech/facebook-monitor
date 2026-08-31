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


async def find_post_elements(page, container_selectors: list[str], logger, post_link_keywords=None, post_action_aria_prefixes=None):
    """يجمع عناصر المنشورات الحقيقية.

    ★ الاستراتيجية الأساسية (جديدة): فيسبوك عاد بيستخدم role='feed' ولا role='article'
    ولا data-pagelet*='FeedUnit' على المنشورات الحقيقية اليوم (تأكدنا من هيك بفحص DOM
    فعلي — الاتنين التانيين كانوا صفر تماماً، والتالت كان بس على تعليقات وعناصر تحميل
    فاضية). بالمقابل، زر "..." (المزيد من الخيارات) على كل منشور حقيقي إله aria-label
    ثابت وواضح بالإنجليزي: "Actions for this post by <اسم الناشر>". هاد marker موثوق
    جداً (باللغة الإنجليزية، مش class مبهم بيتغيّر كل نشر جديد لفيسبوك).

    منطلق من هاد الـ anchor، منطلع بالشجرة لفوق (parentElement) خطوة خطوة لحد ما نلاقي
    أول جد (ancestor) فيه رابط `<a href>` يطابق أحد `post_link_keywords` — هاد أول جد
    منطقياً بيكون حدود المنشور الكامل (نص + صورة + شريط لايك/تعليق/مشاركة)، بغض النظر
    عن أسماء الـ classes الداخلية يلي بتتغيّر.

    لو ما لقينا ولا anchor (يعني فيسبوك غيّر حتى نص الـ aria-label)، منرجع للطريقة
    القديمة (container_selectors) كـ fallback أخير، بس هاي مش المتوقع تنجح اليوم.
    """
    post_link_keywords = post_link_keywords or []
    aria_prefixes = post_action_aria_prefixes or ["Actions for this post"]

    aria_selector = ", ".join(f'div[aria-label^="{p}"]' for p in aria_prefixes)
    anchors = await page.query_selector_all(aria_selector)

    if anchors:
        containers = []
        for anchor in anchors:
            try:
                handle = await anchor.evaluate_handle(
                    """
                    (anchorEl, keywords) => {
                        let node = anchorEl;
                        for (let i = 0; i < 20 && node; i++) {
                            node = node.parentElement;
                            if (!node) break;
                            const links = Array.from(node.querySelectorAll('a[href]'));
                            const hasPostLink = links.some(a =>
                                keywords.some(k => (a.getAttribute('href') || '').includes(k))
                            );
                            if (hasPostLink) return node;
                        }
                        return null;
                    }
                    """,
                    post_link_keywords,
                )
                el = handle.as_element()
                if el:
                    containers.append(el)
                else:
                    await handle.dispose()
            except Exception as walk_err:
                logger.warning(f"⚠️ فشل تتبع الجد لعنصر anchor: {walk_err}")

        if containers:
            return containers, "aria-actions-anchor"

        logger.warning(
            "⚠️ لقينا anchors (زر الخيارات) بس ما قدرنا نطلع بالشجرة لأي جد فيه رابط منشور — "
            "يمكن post_link_keywords احتاجت تحديث."
        )

    # --- Fallback قديم (fيسبوك يمكن يرجّع role='feed'/role='article'/data-pagelet مستقبلاً) ---
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

        import os as _os
        debug_network = _os.environ.get("DEBUG_NETWORK", "false").lower() in ("1", "true", "yes")
        _graphql_log = []
        if debug_network:
            import asyncio as _asyncio

            async def _record_graphql(resp):
                try:
                    body = await resp.body()
                    length = len(body)
                except Exception:
                    length = -1
                _graphql_log.append((resp.status, length, resp.url[:120]))

            def _on_response(resp):
                try:
                    if "graphql" in resp.url.lower():
                        _asyncio.create_task(_record_graphql(resp))
                except Exception:
                    pass
            page.on("response", _on_response)

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
                    await page.screenshot(path=f"downloads/debug_{safe_name}_1_before_scroll.png", full_page=True)
                    logger.info(f"📸 Debug screenshot (before scroll) saved for {target_url}")
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
            _post_link_keywords = selectors.get("post_link_keywords", [])
            _post_action_aria_prefixes = selectors.get(
                "post_action_aria_prefixes", ["Actions for this post"]
            )

            if first_run:
                saved_initial = 0
                for _ in range(3):
                    raw_elements, _ = await find_post_elements(
                        page, container_selectors, logger,
                        post_link_keywords=_post_link_keywords,
                        post_action_aria_prefixes=_post_action_aria_prefixes,
                    )

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
                raw_elements, _ = await find_post_elements(
                    page, container_selectors, logger,
                    post_link_keywords=_post_link_keywords,
                    post_action_aria_prefixes=_post_action_aria_prefixes,
                )

                found_new_this_pass = False
                _dbg_total = len(raw_elements)
                _dbg_already_seen = 0
                _dbg_rejected_no_id = 0

                for post_elem in raw_elements:
                    # نتفادى إعادة معالجة نفس العنصر (وبالتالي إعادة استدعاء Gemini Vision
                    # عليه من جديد) بمرور سكرول لاحق — العنصر بيضل موجود بالـ DOM حتى
                    # بعد ما نكون عالجناه، فبنعلّمه بـ dataset attribute بعد أول معالجة
                    # ونتحقق من العلامة *قبل* ما نستدعي parser.parse() المكلفة.
                    already_processed = await post_elem.evaluate(
                        "el => el.dataset.fbMonitorSeen === '1'"
                    )
                    if already_processed:
                        _dbg_already_seen += 1
                        continue

                    post_id, text, post_url, img_url, has_video = await parser.parse(
                        post_elem, target_type, target_url, http_session
                    )
                    await post_elem.evaluate("el => { el.dataset.fbMonitorSeen = '1'; }")

                    if not post_id:
                        _dbg_rejected_no_id += 1
                        continue
                    if post_id in seen_in_run:
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
                # ملاحظة: هاد السطر مقصود يكون إنجليزي بالكامل (ASCII فقط) بدون خلط عربي/أرقام.
                # لما بيكون فيه عربي وأرقام بنفس السطر، متصفح اللوج (GitHub Actions مثلاً) بيطبّق
                # bidi reordering فبيبعثر ترتيب الأرقام بالنسبة لعناوينها، وبيصير الرقم المعروض
                # ما إلو أي علاقة موثوقة بمكانه البصري. خلينا ASCII صافي هون عشان الأرقام تضل دقيقة.
                _dbg_valid_id = _dbg_total - _dbg_already_seen - _dbg_rejected_no_id
                logger.info(
                    f"[DEBUG_PASS {_scroll_pass + 1}] total_found={_dbg_total} "
                    f"already_seen={_dbg_already_seen} rejected_no_id={_dbg_rejected_no_id} "
                    f"got_valid_id={_dbg_valid_id} new_this_pass={found_new_this_pass}"
                )
                if not found_new_this_pass:
                    consecutive_empty_passes += 1
                    if consecutive_empty_passes >= empty_pass_tolerance:
                        break
                else:
                    consecutive_empty_passes = 0

                for _ in range(scroll_presses_per_pass):
                    await page.keyboard.press("PageDown")
                await page.wait_for_timeout(scroll_wait_ms)

            if debug_network:
                await page.wait_for_timeout(1500)  # نعطي فرصة لمهام قراءة محتوى الردود (async) تخلص
                if not _graphql_log:
                    logger.warning(f"🌐 [DEBUG_NETWORK] ولا طلب GraphQL واحد انبعت أثناء السكرول لـ {target_url}!")
                else:
                    status_counts = {}
                    for status, _cl, _url in _graphql_log:
                        status_counts[status] = status_counts.get(status, 0) + 1
                    sizes = [cl for _s, cl, _u in _graphql_log if isinstance(cl, int) and cl >= 0]
                    logger.info(f"🌐 [DEBUG_NETWORK] عدد طلبات GraphQL: {len(_graphql_log)} | الحالات: {status_counts}")
                    if sizes:
                        logger.info(f"🌐 [DEBUG_NETWORK] أحجام الردود (بايت) — أصغر: {min(sizes)} | أكبر: {max(sizes)} | كلها: {sorted(sizes)}")
                    else:
                        logger.warning("🌐 [DEBUG_NETWORK] ما قدرنا نقرأ محتوى ولا رد (فشل قراءة body لكل الطلبات).")
                    failed = [u for s, _cl, u in _graphql_log if s >= 400]
                    for u in failed[:5]:
                        logger.warning(f"🌐 [DEBUG_NETWORK] طلب فاشل: {u}")

            if _os.environ.get("DEBUG_SCREENSHOT", "false").lower() in ("1", "true", "yes"):
                try:
                    safe_name = detect_target_type(target_url) + "_" + str(abs(hash(target_url)))[:8]
                    await page.screenshot(path=f"downloads/debug_{safe_name}_2_after_scroll.png", full_page=True)
                    logger.info(f"📸 Debug screenshot (after scroll) saved for {target_url}")
                except Exception as shot_err:
                    logger.warning(f"⚠️ فشل التقاط سكرين شوت تشخيصي: {shot_err}")

            # ★ تشخيص مؤقت: نحفظ الـ HTML الفعلي للصفحة (page.content()) في هالنقطة —
            # بعد كل السكرول — عشان نشوف بنية الـ DOM الحقيقية لمنشور حقيقي (مش سكرين شوت
            # بصري، نص HTML خام نقدر نبحث فيه عن الـ tag/attributes الحقيقية اليوم).
            if _os.environ.get("DEBUG_HTML", "false").lower() in ("1", "true", "yes"):
                try:
                    safe_name = detect_target_type(target_url) + "_" + str(abs(hash(target_url)))[:8]
                    html_content = await page.content()
                    with open(f"downloads/debug_{safe_name}_dom.html", "w", encoding="utf-8") as _f:
                        _f.write(html_content)
                    logger.info(
                        f"📄 Debug HTML saved for {target_url} ({len(html_content)} chars)"
                    )
                except Exception as html_err:
                    logger.warning(f"⚠️ فشل حفظ HTML تشخيصي: {html_err}")

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
