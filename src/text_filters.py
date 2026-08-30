"""
فلترة الأسطر "القمامة" (توكنز/hashes داخلية من فيسبوك) وعبارات الواجهة —
طبقة حماية ثانية بعد الفلترة اللي بتصير جوا الصفحة عبر JS.
"""

import re


def looks_like_garbage_line(line: str) -> bool:
    """
    يكشف أسطر شكلها توكن/hash داخلي (زي KyH4JKc1x.com أو B6WItNycywf63...)
    مش نص منشور حقيقي.
    """
    t = line.strip()
    if not t:
        return True
    if re.search(r"[\u0600-\u06FF]", t):
        return False
    if len(t.split()) > 1:
        return False
    if len(t) >= 8 and re.fullmatch(r"[A-Za-z0-9.]+", t):
        has_upper = any(c.isupper() for c in t)
        has_lower = any(c.islower() for c in t)
        has_digit = any(c.isdigit() for c in t)
        if (has_upper and has_lower) or (has_digit and (has_upper or has_lower)):
            return True
    return False


def strip_boilerplate_lines(lines: list[str], patterns: list[str]) -> list[str]:
    compiled = [re.compile(p) for p in patterns]
    return [l for l in lines if not any(p.match(l) for p in compiled)]
