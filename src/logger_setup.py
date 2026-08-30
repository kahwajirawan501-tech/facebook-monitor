"""
إعداد نظام تسجيل (logging) موحّد لكل المشروع بدل الاعتماد على print().
- سجل يومي دوّار على القرص (rotating) للاحتفاظ بالتاريخ.
- سجل بالكونسول بمستوى قابل للتحكم عبر LOG_LEVEL.
"""

import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logger(name: str = "fb_monitor", log_dir: str = "logs", level: str = "INFO") -> logging.Logger:
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger(name)
    if logger.handlers:
        # تجنب تكرار الـ handlers لو تم استدعاء الدالة أكتر من مرة
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "fb_monitor.log"),
        maxBytes=5 * 1024 * 1024,  # 5MB لكل ملف
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger
