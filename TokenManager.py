import sqlite3
import time
import requests
import asyncio
import logging

import urllib3.util.connection
urllib3.util.connection.HAS_IPV6 = False

logger = logging.getLogger(__name__)


class RefreshTokenExpiredError(Exception):
    """刷新令牌已失效（invalid_grant），通常因长期未使用导致过期，需重新绑定。"""


class TokenManager:
    def __init__(self, db_path: str, client_id: str, client_secret: str):
        self.db_path = db_path
        self.client_id = client_id
        self.client_secret = client_secret
        self._refresh_lock = asyncio.Lock()
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
        """获取有效的 access_token，如果过期则自动异步刷新。

        若刷新令牌已失效（invalid_grant）会抛出 RefreshTokenExpiredError。
        """
        row = self._get_row(qq_id)
        if row is None:
            return None

        access_token, _refresh_token, expires_at = row

        # 如果还没过期（留 60 秒缓冲），直接返回
        if time.time() < expires_at - 60:
            return access_token

        # 过期了，异步去刷新（内部会重新读库、加锁，避免并发竞态）
        return await self._refresh(qq_id)

    def _get_row(self, qq_id: str):
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                "SELECT access_token, refresh_token, expires_at FROM user_tokens WHERE qq_id = ?",
                (str(qq_id),)
            )
            return cur.fetchone()

    async def _refresh(self, qq_id: str) -> str | None:
        """异步刷新令牌并更新数据库（加锁、重读库，幂等且并发安全）。

        返回新的 access_token；若刷新令牌已失效则抛出 RefreshTokenExpiredError。
        """
        async with self._refresh_lock:
            # 重新读取最新的记录，避免与其它流程并发刷新时使用已被消费的旧刷新令牌
            row = self._get_row(qq_id)
            if row is None:
                return None

            access_token, refresh_token, expires_at = row

            # 双重检查：可能在等待锁期间已被其它流程刷新过
            if time.time() < expires_at - 60:
                return access_token

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

                # 刷新失败：若为 invalid_grant（刷新令牌已失效/过期），清除本地记录并抛出特定异常
                if res_json.get("error") == "invalid_grant":
                    logger.warning(f"用户 {qq_id} 的刷新令牌已失效（invalid_grant），已清除本地记录，需重新绑定")
                    self.remove_user(qq_id)
                    raise RefreshTokenExpiredError(f"刷新令牌已失效（invalid_grant），用户 {qq_id} 需重新绑定")

                return None
            except RefreshTokenExpiredError:
                raise
            except Exception:
                return None

    def _get_all_qq_ids(self) -> list[str]:
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute("SELECT qq_id FROM user_tokens")
            return [row[0] for row in cur.fetchall()]

    async def refresh_all_tokens(self) -> int:
        """后台自动续期：刷新所有用户的令牌，保持刷新令牌不过期。

        返回成功刷新（或仍有效）的用户数量。刷新令牌已失效的用户会被清理，
        不影响其它用户。
        """
        qq_ids = self._get_all_qq_ids()
        if not qq_ids:
            logger.info("令牌自动续期：当前无已绑定用户，跳过")
            return 0

        refreshed = 0
        cleaned = 0
        for qq_id in qq_ids:
            try:
                token = await self.get_valid_token(qq_id)
                if token:
                    refreshed += 1
            except RefreshTokenExpiredError:
                cleaned += 1

        logger.info(f"令牌自动续期完成：共 {len(qq_ids)} 位用户，有效/已刷新 {refreshed} 位，清理过期 {cleaned} 位")
        return refreshed

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
