"""
طبقة الوصول لقاعدة بيانات Turso (libsql عبر HTTP pipeline API).
كل استعلامات SQL محصورة هون — باقي الكود ما بيعرف تفاصيل Turso إطلاقاً.
"""

import re
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlunsplit, urlparse

import aiohttp


_REQUIRED_POSTS_COLUMNS = {
    "text": "TEXT",
    "post_url": "TEXT",
    "image_url": "TEXT",
    "video_url": "TEXT",
    "target_type": "TEXT",
    "target_url": "TEXT",
    "created_at": "TIMESTAMP",
}


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


class Database:
    """طبقة قاعدة بيانات مع lock داخلي — استخدمها عبر `async with db.lock:` عند الحاجة لعملية atomic."""

    def __init__(self, database_url: str, auth_token: str, logger):
        import asyncio

        self._pipeline_url = f"{_to_http_url(database_url).rstrip('/')}/v2/pipeline"
        self._auth_token = auth_token
        self._session: aiohttp.ClientSession | None = None
        self.lock = asyncio.Lock()
        self.logger = logger

    async def connect(self):
        self._session = aiohttp.ClientSession()
        await self._execute(
            """
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
            """
        )
        await self._ensure_posts_columns()
        self.logger.info("🗄️ Connected to Turso and ensured `posts` table exists.")

    async def close(self):
        if self._session:
            await self._session.close()

    async def _execute(self, sql: str, args=None):
        payload = {
            "requests": [
                {"type": "execute", "stmt": {"sql": sql, "args": [_hrana_value(a) for a in (args or [])]}},
                {"type": "close"},
            ]
        }
        headers = {"Authorization": f"Bearer {self._auth_token}"}
        async with self._session.post(self._pipeline_url, json=payload, headers=headers, timeout=20) as resp:
            data = await resp.json()
            if resp.status != 200:
                raise RuntimeError(f"Turso HTTP {resp.status}: {data}")
            first = data["results"][0]
            if first.get("type") != "ok":
                raise RuntimeError(f"Turso query error: {first}")
            result = first["response"]["result"]
            return [_unwrap_row(r) for r in result.get("rows", [])]

    async def _ensure_posts_columns(self):
        rows = await self._execute("PRAGMA table_info(posts)")
        existing_cols = {r[1] for r in rows}
        for col, col_type in _REQUIRED_POSTS_COLUMNS.items():
            if col not in existing_cols:
                await self._execute(f"ALTER TABLE posts ADD COLUMN {col} {col_type}")
                self.logger.info(f"🔧 Added missing column `{col}` to `posts` table.")

    async def is_empty_for_target(self, target_url: str) -> bool:
        rows = await self._execute("SELECT COUNT(*) FROM posts WHERE target_url = ?", [target_url])
        return rows[0][0] == 0

    async def post_exists(self, post_id: str) -> bool:
        rows = await self._execute("SELECT 1 FROM posts WHERE id = ?", [post_id])
        return len(rows) > 0

    async def is_recent_content_duplicate(
        self, target_url: str, clean_text: str, hours: int = 24, prefix_len: int = 60
    ) -> bool:
        if not clean_text:
            return False

        stripped = clean_text.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            return False

        threshold = (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        rows = await self._execute(
            "SELECT text FROM posts WHERE target_url = ? AND created_at >= ?",
            [target_url, threshold],
        )
        normalized_new = re.sub(r"\s+", "", clean_text).strip().lower()[:prefix_len]
        if not normalized_new:
            return False
        for row in rows:
            existing_text = row[0]
            if not existing_text:
                continue
            normalized_existing = re.sub(r"\s+", "", existing_text).strip().lower()[:prefix_len]
            if normalized_existing and normalized_existing == normalized_new:
                return True
        return False

    async def save_post(
        self,
        post_id: str,
        text: str,
        post_url: str,
        image_url: str | None,
        video_url: str | None,
        target_type: str,
        target_url: str,
    ):
        await self._execute(
            """INSERT INTO posts (id, text, post_url, image_url, video_url, target_type, target_url, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                post_id,
                text,
                post_url,
                image_url,
                video_url,
                target_type,
                target_url,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ],
        )
        self.logger.info(f"💾 Saved [{post_id[:12]}] to Turso!")
