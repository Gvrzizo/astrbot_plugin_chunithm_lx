import sqlite3
import time
import requests
import asyncio

import urllib3.util.connection
urllib3.util.connection.HAS_IPV6 = False

class TokenManager:
    def __init__(self, db_path: str, client_id: str, client_secret: str):
        self.db_path = db_path
        self.client_id = client_id
        self.client_secret = client_secret
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_tokens (
                    qq_id TEXT PRIMARY KEY,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT NOT NULL,
                    expires_at INTEGER NOT NULL
                )
            """)
            conn.commit()

    async def get_valid_token(self, qq_id: str) -> str | None:
        """获取有效的 access_token，如果过期则自动异步刷新"""
        row = self._get_row(qq_id)
        if row is None:
            return None

        access_token, refresh_token, expires_at = row

        # 如果还没过期（留 60 秒缓冲），直接返回
        if time.time() < expires_at - 60:
            return access_token

        # 过期了，异步去刷新
        return await self._refresh(qq_id, refresh_token)

    def _get_row(self, qq_id: str):
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "SELECT access_token, refresh_token, expires_at FROM user_tokens WHERE qq_id = ?",
                (str(qq_id),)
            )
            return cur.fetchone()

    async def _refresh(self, qq_id: str, refresh_token: str) -> str | None:
        """异步刷新令牌并更新数据库"""
        try:
            resp = await asyncio.to_thread(
                requests.post,
                "https://maimai.lxns.net/api/v0/oauth/token",
                json={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                timeout=10
            )

            res_json = resp.json()

            # 新格式：令牌字段位于响应顶层（OAuth 2.0 标准）
            access_token = res_json.get("access_token")
            if access_token:
                self._save(qq_id, access_token, res_json.get("refresh_token", ""), res_json.get("expires_in", 0))
                return access_token

            # 旧格式兼容：令牌字段包裹在 data 内
            if res_json.get("success"):
                token_data = res_json.get("data", {})
                access_token = token_data.get("access_token")
                if access_token:
                    self._save(qq_id, access_token, token_data.get("refresh_token", ""), token_data.get("expires_in", 0))
                    return access_token

            return None
        except Exception:
            return None

    async def exchange_code(self, qq_id: str, code: str) -> bool:
        """用授权码换取 token 并存入数据库"""
        try:
            resp = await asyncio.to_thread(
                requests.post,
                "https://maimai.lxns.net/api/v0/oauth/token",
                json={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": "urn:ietf:wg:oauth:2.0:oob"
                },
                timeout=10
            )

            res_json = resp.json()

            # 新格式：令牌字段位于响应顶层（OAuth 2.0 标准）
            access_token = res_json.get("access_token")
            if access_token:
                self._save(qq_id, access_token, res_json.get("refresh_token", ""), res_json.get("expires_in", 0))
                return True

            # 旧格式兼容：令牌字段包裹在 data 内
            if res_json.get("success"):
                token_data = res_json.get("data", {})
                access_token = token_data.get("access_token")
                if access_token:
                    self._save(qq_id, access_token, token_data.get("refresh_token", ""), token_data.get("expires_in", 0))
                    return True

            return False
        except Exception:
            return False

    def _save(self, qq_id: str, access_token: str, refresh_token: str, expires_in: int):
        now = int(time.time())
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO user_tokens (qq_id, access_token, refresh_token, expires_at)
                   VALUES (?, ?, ?, ?)""",
                (str(qq_id), access_token, refresh_token, now + expires_in)
            )
            conn.commit()

    def remove_user(self, qq_id: str):
        """用户解绑"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM user_tokens WHERE qq_id = ?", (str(qq_id),))
            conn.commit()
