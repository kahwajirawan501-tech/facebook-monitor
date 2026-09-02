"""
أدوات مساعدة لروابط فيسبوك: استخراج معرّف المنشور، تنظيف معاملات التتبع، وتحديد نوع الهدف.
"""

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunsplit

_TRACKING_PARAM_PREFIXES = ("__cft__", "__tn__", "__xts__")
_TRACKING_PARAM_EXACT = {
    "notif_id", "notif_t", "ref", "eid", "acontext", "hc_ref",
    "extid", "mibextid", "rdid", "fs", "s", "source", "paipv",
}


def extract_facebook_post_id(url: str) -> str | None:
    if not url:
        return None

    pfbid_match = re.search(r"pfbid([a-zA-Z0-9]+)", url)
    if pfbid_match:
        return f"pfbid_{pfbid_match.group(1)}"

    posts_match = re.search(r"/posts/(\d+)", url)
    if posts_match:
        return f"post_{posts_match.group(1)}"

    fbid_match = re.search(r"(?:story_fbid=|fbid=)(\d+)", url)
    if fbid_match:
        return f"fbid_{fbid_match.group(1)}"

    # ★ صيغة روابط المشاركة الأحدث عند فيسبوك: /share/p/<token>/ (صور)
    # و /share/v/<token>/ (فيديو) — بديل عن pfbid/posts القديمة بمعظم أماكن
    # المشاركة اليوم. الـ token هون أبجدي رقمي، مش أرقام بس.
    share_match = re.search(r"/share/[pv]/([a-zA-Z0-9_-]+)", url)
    if share_match:
        return f"share_{share_match.group(1)}"

    media_match = re.search(r"/(?:videos|reel|watch)/(\d+)", url)
    if media_match:
        return f"media_{media_match.group(1)}"

    return None


def clean_facebook_url(raw_href: str) -> str:
    """ينظف رابط فيسبوك من معاملات التتبع (__cft__, __tn__...) قبل حفظه/إرساله."""
    if not raw_href:
        return raw_href
    href = raw_href.replace("&amp;", "&").strip()
    if href.startswith("/"):
        href = "https://www.facebook.com" + href
    parsed = urlparse(href)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    cleaned_pairs = [
        (k, v)
        for k, v in query_pairs
        if not k.startswith(_TRACKING_PARAM_PREFIXES) and k not in _TRACKING_PARAM_EXACT
    ]
    cleaned_query = urlencode(cleaned_pairs)
    return urlunsplit((parsed.scheme or "https", parsed.netloc or "www.facebook.com", parsed.path, cleaned_query, ""))


def detect_target_type(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.lower()
    if "/groups/" in path:
        return "GROUP"
    elif "/profile.php" in path or "/people/" in path or "id=" in parsed.query:
        return "PROFILE"
    return "PAGE"
