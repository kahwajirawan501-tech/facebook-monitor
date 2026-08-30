"""
تحليل عنصر منشور واحد داخل الصفحة واستخراج (id, نص, رابط, صورة, هل فيه فيديو).
كل الـ selectors والعبارات مستوردة من config.selectors — أي تعديل لبنية فيسبوك
بيصير بتعديل selectors.json فقط، بدون لمس هالملف.
"""

import hashlib
import json
import re

from .ocr_engine import GeminiOCR
from .text_filters import looks_like_garbage_line, strip_boilerplate_lines
from .url_utils import clean_facebook_url, extract_facebook_post_id

class PostParser:
    def __init__(self, selectors: dict, ocr: GeminiOCR, logger):
        self.selectors = selectors
        self.ocr = ocr
        self.logger = logger

    async def parse(self, post_element, target_type: str, target_url: str, http_session):
        s = self.selectors

        is_valid = await post_element.evaluate(
            """
            (e, cfg) => {
                let aria = (e.getAttribute('aria-label') || '').toLowerCase();
                let commentMarkers = cfg.commentMarkers || [];
                if (commentMarkers.some(m => aria.includes(m))) return false;

                let isInsideUl = e.closest('ul') !== null;
                let composerSelectors = cfg.composerSelectors || [];
                let isComposer = composerSelectors.some(sel => e.querySelector(sel) !== null);
                if (isInsideUl || isComposer) return false;

                let innerText = e.innerText || '';
                let ignoredHeadings = cfg.ignoredHeadings || [];
                if (ignoredHeadings.some(h => innerText.includes(h))) return false;

                let isInsideIgnored = ignoredHeadings.some(h =>
                    e.closest(`div[aria-label*="${h}"]`) !== null
                ) || (e.closest('div[role="region"]') !== null &&
                      ignoredHeadings.some(h => {
                          let region = e.closest('div[role="region"]');
                          return region && region.innerText && region.innerText.includes(h);
                      }));

                return !isInsideIgnored;
            }
            """,
            {
                "commentMarkers": [m.lower() for m in s.get("comment_or_reply_aria_markers", [])],
                "composerSelectors": s.get("composer_selectors", []),
                "ignoredHeadings": s.get("ignored_headings", []),
            },
        )
        if not is_valid:
            return None, None, None, None, False

        await post_element.evaluate(
            """
            (el, seeMoreTexts) => {
                const btns = Array.from(el.querySelectorAll('div[role="button"], span[role="button"], a, span'));
                const texts = seeMoreTexts || [];
                btns.forEach(b => {
                    let txt = (b.innerText || '').trim();
                    if (texts.includes(txt)) {
                        try { b.click(); } catch(e) {}
                    }
                });
            }
            """,
            s.get("see_more_button_texts", []),
        )

        clean_post_text = await post_element.evaluate(
            """
            (container, cfg) => {
                const isHeaderOrComment = (el) => {
                    return el.closest('h2') !== null ||
                           el.closest('h3') !== null ||
                           el.closest('ul[role="group"]') !== null ||
                           el.closest('div[aria-label*="تعليق"]') !== null ||
                           el.closest('div[aria-label*="Comment"]') !== null ||
                           el.closest('div[role="toolbar"]') !== null;
                };

                const isLikelyGarbage = (txt) => {
                    const t = txt.trim();
                    if (!t) return true;
                    if (/[\u0600-\u06FF]/.test(t)) return false;
                    if (t.split(/\s+/).length > 1) return false;
                    if (t.length >= 8 && /^[A-Za-z0-9.]+$/.test(t)) {
                        const hasUpper = /[A-Z]/.test(t);
                        const hasLower = /[a-z]/.test(t);
                        const hasDigit = /[0-9]/.test(t);
                        if ((hasUpper && hasLower) || (hasDigit && (hasUpper || hasLower))) return true;
                    }
                    return false;
                };

                let selectorsList = cfg.textNodeSelectors || [];
                const textNodes = selectorsList.length > 0 ? Array.from(container.querySelectorAll(selectorsList.join(', '))) : [];
                let chunks = [];

                textNodes.forEach(node => {
                    const isVisible = node.offsetParent !== null;
                    if (!isHeaderOrComment(node) && isVisible) {
                        let txt = node.innerText ? node.innerText.trim() : '';
                        if (txt.length > 1 && !isLikelyGarbage(txt)) chunks.push(txt);
                    }
                });

                let fullText = chunks.join('\\n');
                let uiPhrases = cfg.uiPhrases || [];

                return fullText
                    .split('\\n')
                    .map(line => line.trim())
                    .filter(line => line && !uiPhrases.includes(line) && !/^[0-9٠-٩\s٫,KkMmAaدس]+$/.test(line))
                    .filter((item, index, self) => self.indexOf(item) === index)
                    .join('\\n');
            }
            """,
            {"textNodeSelectors": s.get("text_node_selectors", []), "uiPhrases": s.get("ui_phrases", [])},
        )

        image_url = await post_element.evaluate(
            """
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
            """
        )

        all_links = await post_element.query_selector_all("a[href]")
        post_url = target_url
        found_specific_link = False
        for link in all_links:
            href = await link.get_attribute("href") or ""
            if any(keyword in href for keyword in s.get("post_link_keywords", [])):
                if not any(skip in href for skip in s.get("post_link_skip_keywords", [])):
                    raw_url = f"https://www.facebook.com{href}" if href.startswith("/") else href
                    post_url = clean_facebook_url(raw_url)
                    found_specific_link = True
                    break

        if not found_specific_link:
            return None, None, None, None, False

        has_video = await post_element.evaluate(
            """
            (e, cfg) => {
                const cssList = cfg.css || [];
                const linkCssList = cfg.linkCss || [];
                const keywordsList = cfg.keywords || [];

                const cssHit = cssList.some(sel => e.querySelector(sel) !== null);
                const linkHit = linkCssList.some(sel => e.querySelector(sel) !== null);
                const textHit = e.innerText && keywordsList.some(k => e.innerText.includes(k));
                return cssHit || linkHit || !!textHit;
            }
            """,
            s.get("video_indicators", {}),
        )

        lines = [line.strip() for line in clean_post_text.split("\n") if line.strip()]
        lines = strip_boilerplate_lines(lines, s.get("boilerplate_line_patterns", []))
        lines = [l for l in lines if not looks_like_garbage_line(l)]
        real_post_text = "\n".join(lines).strip()

        is_only_link = bool(real_post_text.startswith("http") or "http" in real_post_text) and len(real_post_text) < 120

        is_mostly_hashtags = False
        if real_post_text:
            post_lines = [l.strip() for l in real_post_text.split("\n") if l.strip()]
            hashtag_lines = sum(1 for l in post_lines if l.startswith("#") or l in [".", "-", "..."])
            if len(post_lines) > 0 and (hashtag_lines / len(post_lines) >= 0.5 or len(real_post_text) < 25):
                is_mostly_hashtags = True

        needs_gemini = image_url and (not real_post_text or is_mostly_hashtags or is_only_link)

        if needs_gemini:
            self.logger.info("👁️ المنشور يحتاج إلى استخراج النص من الصورة، جاري القراءة عبر Gemini Vision...")
            ocr_text = await self.ocr.extract_text_from_image_url(http_session, image_url)
            clean_post_text = ocr_text if ocr_text else (real_post_text if real_post_text else "[منشور يحتوي على صورة]")
        elif len(real_post_text) >= 15 and not is_only_link:
            clean_post_text = real_post_text
        elif has_video:
            clean_post_text = real_post_text if real_post_text and not is_only_link else "[بث مباشر / مقطع فيديو]"
        elif len(real_post_text) >= 1:
            clean_post_text = real_post_text
        else:
            return None, None, None, None, False

        fb_id = extract_facebook_post_id(post_url)
        if not fb_id and image_url:
            fb_id = extract_facebook_post_id(image_url)

        if fb_id:
            post_id = fb_id
        else:
            normalized_text = re.sub(r"[^ء-يa-zA-Z0-9]", "", clean_post_text).strip().lower()
            unique_seed = f"{target_url}|{normalized_text[:120]}"
            post_id = hashlib.md5(unique_seed.encode("utf-8")).hexdigest()

        return post_id, clean_post_text, post_url, image_url, has_video
