from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.star.filter.command import GreedyStr

import requests
import json
import asyncio
import re
import time
import logging
from datetime import datetime
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from html2image import Html2Image
from PIL import Image
from .TokenManager import TokenManager, RefreshTokenExpiredError

import urllib3.util.connection
urllib3.util.connection.HAS_IPV6 = False


def admin_command(name: str, **kwargs):
    """管理员专属指令装饰器。

    在普通指令的基础上叠加管理员权限过滤，只有 AstrBot 配置中 ``admins_id``
    列出的用户才能触发。以后新增管理员指令时照此写法即可::

        @admin_command("yourcommand")
        async def yourcommand(self, event: AstrMessageEvent):
            ...

    其余参数（例如 ``alias``）会原样透传给 ``filter.command``。
    """

    def decorator(func):
        return filter.permission_type(filter.PermissionType.ADMIN)(
            filter.command(name, **kwargs)(func)
        )

    return decorator


@register("chunithm_lx", "Lauretta", "中二节奏机器人", "0.4.0")
class Lauretta(Star):
    TOKEN_REFRESH_INTERVAL = 7 * 24 * 3600

    def __init__(self, context: Context):
        super().__init__(context)

        self.apiKey = ""

        self.baseUserApi = "https://maimai.lxns.net/api/v0/user/chunithm"
        self.playerInfoUrl = f"{self.baseUserApi}/player"
        self.scoresUrl = f"{self.baseUserApi}/player/scores"

        self.songListUrl = "https://maimai.lxns.net/api/v0/chunithm/song/list"
        self.aliasListUrl = "https://maimai.lxns.net/api/v0/chunithm/alias/list"
        self.jacketAssetBaseUrl = "https://assets2.lxns.net/chunithm/jacket"
        self.oauthUrl = "https://maimai.lxns.net/oauth/authorize?response_type=code&client_id=8e412bac-aeab-460c-9556-22a17fd31c49&redirect_uri=urn%3Aietf%3Awg%3Aoauth%3A2.0%3Aoob&scope=read_user_profile+read_player"

        dataPath = Path(get_astrbot_data_path())
        self.storagePath = dataPath / "plugin_data" / "astrbot_plugin_chunithm_lx"
        self.storagePath.mkdir(parents=True, exist_ok=True)
        self.jacketPath = self.storagePath / "jacket"
        self.jacketPath.mkdir(parents=True, exist_ok=True)
        self.bestPath = self.storagePath / "best"
        self.bestPath.mkdir(parents=True, exist_ok=True)
        self.ccPath = self.storagePath / "cc"
        self.ccPath.mkdir(parents=True, exist_ok=True)
        self.oauthPath = self.storagePath / "oauth"
        self.db_path = str(self.storagePath / "tokens.db")
        self.oauthidFile = self.oauthPath / "clientid"
        self.oauthsecretFile = self.oauthPath / "clientsecret"
        self.songCacheFile = self.storagePath / "songs.json"

        # 可选的 COS 上传配置（storagePath/cos.json）。配置存在时图片经腾讯内网
        # 上传到 COS，QQ 按预签名 URL 取图，从而绕开本机上行带宽限制，此时保留
        # 高分辨率；没有配置时回退到"小图直传"。
        self.cosConfig = self._load_cos_config()
        if self.cosConfig:
            # COS SDK 的 DEBUG 日志会打印请求签名/密钥信息，这里压到 WARNING
            logging.getLogger("qcloud_cos").setLevel(logging.WARNING)
            self.deviceScaleFactor = self.COS_DEVICE_SCALE_FACTOR
            self.imageMaxMb = self.COS_IMAGE_SIZE_MB
            self.imageMaxWidth = self.COS_IMAGE_WIDTH
            self.imageMaxHeight = self.COS_IMAGE_HEIGHT
            logger.info(
                f"COS 上传已启用: bucket={self.cosConfig['bucket']} region={self.cosConfig['region']}"
            )
        else:
            self.deviceScaleFactor = self.DEVICE_SCALE_FACTOR
            self.imageMaxMb = self.MAX_IMAGE_SIZE_MB
            self.imageMaxWidth = self.MAX_IMAGE_WIDTH
            self.imageMaxHeight = self.MAX_IMAGE_HEIGHT

        self.clientid = self.oauthidFile.read_text(encoding = "utf-8").strip()
        self.clientsecret = self.oauthsecretFile.read_text(encoding = "utf-8").strip()

        if self.clientid and self.clientsecret:
            logger.info("oauth info loaded")

        self.tm = TokenManager(
            db_path = str(self.storagePath / "tokens.db"),
            client_id = self.clientid,
            client_secret = self.clientsecret
        )

        self.diffiMap = {
            0: "BASIC",
            1: "ADVANCED",
            2: "EXPERT",
            3: "MASTER",
            4: "ULTIMA",
            5: "WORLD\'S END",
        }
        self.diffiInverted = {v: k for k, v in self.diffiMap.items()}
        self.rankMap = {
            "sssp": "SSS+",
            "sss": "SSS",
            "ssp": "SS+",
            "ss": "SS",
            "sp": "S+",
            "s": "S"
        }
        self.badgeStyleMap = {
            "alljustice": ("aj", "AJ"),
            "fullcombo": ("fc", "FC"),
            "alljusticecritical": ("ajc", "AJC")
        }
        # 定数索引。键完全由歌曲数据决定，不预设上限，因此游戏以后新增
        # 15.8 / 16.0 之类的定数时会自动适配。
        self.ccMap = {}

        self.songList = []
        self.songMap = {}
        self.versions = []
        self.genres = []
        self.version_by_title = {}
        self.version_by_value = {}
        self.genre_by_name = {}
        self.songs_by_version = {}
        self.songs_by_genre = {}
        self._refresh_task = None
        self._loadSongCache()
        self._build_meta_maps()

    def _build_meta_maps(self):
        self.version_by_title = {}
        self.version_by_value = {}
        for v in self.versions:
            title = v.get("title", "")
            ver_val = v.get("version", 0)
            self.version_by_title[title.lower()] = ver_val
            self.version_by_value[ver_val] = title

        self.genre_by_name = {}
        for g in self.genres:
            name = g.get("genre", "")
            self.genre_by_name[name.lower()] = name

        self.songs_by_version = {}
        self.songs_by_genre = {}
        for song in self.songList:
            sid = song.get("id", 0)
            sver = song.get("version", 0)
            sgenre = song.get("genre", "")
            diffs = song.get("difficulties", [])

            if sver not in self.songs_by_version:
                self.songs_by_version[sver] = []
            for d in diffs:
                diff = d.get("difficulty", 0)
                if diff == 5: continue
                self.songs_by_version[sver].append([sid, diff])

            if sgenre not in self.songs_by_genre:
                self.songs_by_genre[sgenre] = []
            for d in diffs:
                diff = d.get("difficulty", 0)
                if diff == 5: continue
                self.songs_by_genre[sgenre].append([sid, diff])

        for lst in self.songs_by_version.values():
            lst.sort(key=lambda x: x[1])
        for lst in self.songs_by_genre.values():
            lst.sort(key=lambda x: x[1])

    @staticmethod
    def _cc_decimals(base: int):
        """返回某个整数定数下、除 .0 以外的合法小数位。

        规则：1.0~6.0 只有 .0；7.0~9.5 每 0.5；10.0 起每 0.1。
        """
        if base <= 6:
            return []
        if base <= 9:
            return [5]
        return list(range(1, 10))

    def _parse_cc(self, usrcc: str):
        """解析定数字符串，返回目标定数列表（可能为空）。

        支持三种写法，且不预设定数上限：
        - 具体定数：``15.3`` / ``9.5``
        - 整数：``14``（1~6 取 .0；7~9 取 .0/.5；10+ 取 .0~.4）
        - 加号：``14+``（7~9 取 .5；10+ 取 .5~.9）
        """
        usrcc = usrcc.strip()
        if not usrcc:
            return []

        match = re.fullmatch(r"(\d+)\.(\d+)", usrcc)
        if match:
            base, dec = int(match.group(1)), int(match.group(2))
            if base >= 1 and dec in [0, *self._cc_decimals(base)]:
                return [f"{base}.{dec}"]
            return []

        match = re.fullmatch(r"(\d+)\+", usrcc)
        if match:
            base = int(match.group(1))
            if 7 <= base <= 9:
                return [f"{base}.5"]
            if base >= 10:
                return [f"{base}.{dec}" for dec in range(5, 10)]
            return []

        if usrcc.isdigit():
            base = int(usrcc)
            if 1 <= base <= 6:
                return [f"{base}.0"]
            if 7 <= base <= 9:
                return [f"{base}.0", f"{base}.5"]
            if base >= 10:
                return [f"{base}.{dec}" for dec in range(0, 5)]
        return []

    def _match_version(self, query: str):
        """匹配版本名，先精确匹配，再取最短的子串匹配（即最具体的版本）。"""
        query = query.lower().strip()
        if not query:
            return None
        if query in self.version_by_title:
            return self.version_by_title[query]

        matches = [
            (title, ver_val)
            for title, ver_val in self.version_by_title.items()
            if query in title
        ]
        if not matches:
            return None
        matches.sort(key=lambda item: (len(item[0]), item[0]))
        return matches[0][1]

    def _match_genre(self, query: str):
        """匹配分类名，先精确匹配，再取最短的子串匹配。"""
        query = query.lower().strip()
        if not query:
            return None
        if query in self.genre_by_name:
            return self.genre_by_name[query]

        matches = [
            (name_lower, name)
            for name_lower, name in self.genre_by_name.items()
            if query in name_lower
        ]
        if not matches:
            return None
        matches.sort(key=lambda item: len(item[0]))
        return matches[0][1]

    def _detect_query_type(self, usrcc: str):
        """检测查询类型，返回 ("cc", tarccs) / ("version", ver_val) / ("genre", name) / (None, None)"""
        usrcc = usrcc.strip()
        tarccs = self._parse_cc(usrcc)
        if tarccs:
            return "cc", tarccs

        ver_val = self._match_version(usrcc)
        if ver_val is not None:
            return "version", ver_val

        genre = self._match_genre(usrcc)
        if genre is not None:
            return "genre", genre

        return None, None

    def _split_ccomplete_args(self, raw: str):
        """解析 /cc 的整串参数，返回 (query, usrdiff, only, minrank, expbelow)。

        查询词允许包含空格（例如 ``crystal plus``、``流行 & 动漫``），因此不再依赖
        框架按空格切分：这里从头累积查询词，遇到难度关键字（含 ``world's end``）
        之后的部分才按选项处理。选项同时兼容两种写法：

        - 位置写法：``/cc 14+ MASTER 1 SSS 0``
        - 显式写法：``/cc crystal plus diff=MASTER only=0 minrank=SSS expbelow=0``
        """
        tokens = raw.split()

        options_kv = {}
        positional = []
        for token in tokens:
            if "=" in token:
                key, _, value = token.partition("=")
                options_kv[key.strip().lower()] = value.strip()
            else:
                positional.append(token)

        # 合并 "world's end" 这种带空格的难度名
        merged = []
        idx = 0
        while idx < len(positional):
            if (
                positional[idx].lower() == "world's"
                and idx + 1 < len(positional)
                and positional[idx + 1].lower() == "end"
            ):
                merged.append("WORLD'S END")
                idx += 2
            else:
                merged.append(positional[idx])
                idx += 1

        diff_names = {name.lower() for name in self.diffiInverted}
        split_at = len(merged)
        for idx, token in enumerate(merged):
            if token.lower() in diff_names:
                split_at = idx
                break

        query = " ".join(merged[:split_at]).strip()
        options = merged[split_at:]

        usrdiff = options[0] if len(options) >= 1 else "BASIC"
        only = options[1] if len(options) >= 2 else "0"
        minrank = options[2] if len(options) >= 3 else "NONE"
        expbelow = options[3] if len(options) >= 4 else "0"

        usrdiff = options_kv.get("diff", options_kv.get("difficulty", usrdiff))
        only = options_kv.get("only", only)
        minrank = options_kv.get("minrank", options_kv.get("rank", minrank))
        expbelow = options_kv.get("expbelow", expbelow)

        return query, usrdiff, only, minrank, expbelow

    def _build_song_entry(self, song_id, diffi, user_scores_map, rank_order, target_rank, is_conditional):
        satis_inc = 0
        songinfo = self.songMap.get(song_id)
        if not songinfo:
            return None, satis_inc

        jacket_path = self._download_jacket(song_id)
        lookup_key = f"{song_id}_{diffi}"
        played_info = user_scores_map.get(lookup_key)

        is_played = False
        show_check = False
        rank_str = ""
        rank_class = "OTHER"
        badge_type = ""
        badge_name = ""

        if played_info:
            raw_rank = played_info.get("rank", "other").upper().replace("+", "P")
            fc_aj_status = played_info.get("full_combo", "")
            if fc_aj_status:
                fc_aj_status = self.badgeStyleMap.get(fc_aj_status, ["", ""])[1]

            satisfy = False
            if not is_conditional:
                satisfy = True
            else:
                if target_rank in rank_order:
                    if raw_rank in rank_order and rank_order[raw_rank] >= rank_order[target_rank]:
                        satisfy = True
                        satis_inc = 1
                elif target_rank in ["FC", "AJ", "AJC"]:
                    badge_ranks = {"FC": 1, "AJ": 2, "AJC": 3}
                    if fc_aj_status in badge_ranks and badge_ranks[fc_aj_status] >= badge_ranks[target_rank]:
                        satisfy = True
                        satis_inc = 1

            if satisfy:
                is_played = True
                if is_conditional:
                    show_check = True
                else:
                    raw_rank_lower = played_info.get("rank", "other")
                    rank_str = self.rankMap.get(raw_rank_lower, raw_rank_lower.upper())
                    rank_class = raw_rank_lower.upper()
                    raw_fc = played_info.get("full_combo", "")
                    if raw_fc in self.badgeStyleMap:
                        badge_type, badge_name = self.badgeStyleMap[raw_fc]

        entry = {
            "song_id": song_id,
            "song_name": songinfo.get("title", "未知曲目"),
            "diff": diffi,
            "diff_name": self.diffiMap.get(diffi, "UNK"),
            "jacket_url": f"file://{jacket_path}" if jacket_path else "",
            "played": is_played,
            "show_check": show_check,
            "rank": rank_str,
            "rank_class": rank_class,
            "badge_type": badge_type,
            "badge_name": badge_name,
        }
        return entry, satis_inc

    MAX_SONGS_PER_PAGE = 100

    # 无 COS 时，图片要经本机上行发给 QQ（单连接实测约 83KB/s），必须压得很小。
    MAX_IMAGE_SIZE_MB = 0.6
    MAX_IMAGE_WIDTH = 1200
    MAX_IMAGE_HEIGHT = 4000
    DEVICE_SCALE_FACTOR = 1

    # 启用 COS 后，图片经腾讯内网上传、由 QQ 按预签名 URL 取图，不再占用本机
    # 上行，因此保留高分辨率（2x）和更大的体积/尺寸上限。
    COS_IMAGE_SIZE_MB = 4.0
    COS_IMAGE_WIDTH = 3200
    COS_IMAGE_HEIGHT = 12000
    COS_DEVICE_SCALE_FACTOR = 2

    def _split_cc_blocks(self, cc_blocks: list):
        pages = []
        song_tuples = []
        for block in cc_blocks:
            for song in block["songs"]:
                song_tuples.append((block["cc"], song))

        for i in range(0, len(song_tuples), self.MAX_SONGS_PER_PAGE):
            chunk = song_tuples[i:i + self.MAX_SONGS_PER_PAGE]
            page_blocks = {}
            for cc, song in chunk:
                if cc not in page_blocks:
                    page_blocks[cc] = []
                page_blocks[cc].append(song)
            pages.append([
                {"cc": cc, "songs": page_blocks[cc]}
                for cc in sorted(page_blocks.keys(), key=float)
            ])
        return pages

    def _reset_song_state(self):
        """清空歌曲相关的内存索引，便于重新加载缓存或刷新歌曲列表"""
        self.songList = []
        self.songMap = {}
        self.versions = []
        self.genres = []
        self.ccMap = {}
        self.version_by_title = {}
        self.version_by_value = {}
        self.genre_by_name = {}
        self.songs_by_version = {}
        self.songs_by_genre = {}

    def _loadSongCache(self):
        """从本地文件加载歌曲列表"""
        if not self.songCacheFile.exists():
            return
        try:
            with open(self.songCacheFile, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except Exception as e:
            logger.error(f"加载歌曲缓存失败: {e}")
            return

        self._reset_song_state()
        if isinstance(data, list):
            self.songList = data
        else:
            self.songList = data.get("songs", [])
            self.versions = data.get("versions", [])
            self.genres = data.get("genres", [])
        for i in self.songList:
            isongid = i.get("id", 0)
            self.songMap[isongid] = i
            differ = i.get("difficulties", [])
            if not differ:
                continue
            for k in differ:
                oricc = k.get("level_value", 0)
                diffi = k.get("difficulty", 0)
                cc = str(round(float(oricc), 1))
                self.ccMap.setdefault(cc, []).append([isongid, diffi])
        for i in self.ccMap.values():
            i.sort(key = lambda x: x[1])
        logger.info(f"已从缓存加载 {len(self.songList)} 首歌曲, {len(self.versions)} 个版本, {len(self.genres)} 个分类")

    def _saveSongCache(self, songs):
        """保存歌曲列表到本地"""
        try:
            cache_data = {
                "songs": songs,
                "versions": self.versions,
                "genres": self.genres,
            }
            with open(self.songCacheFile, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
            logger.info(f"已缓存 {len(songs)} 首歌曲, {len(self.versions)} 个版本, {len(self.genres)} 个分类")
        except Exception as e:
            logger.error(f"保存歌曲缓存失败: {e}")

    async def loadSongFromApi(self):
        """从 API 获取歌曲列表，成功返回 True，失败返回 False"""
        try:
            response = await asyncio.to_thread(requests.get, self.songListUrl, params={"notes": "true"}, timeout=30)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            logger.error(f"网络请求出错: {e}")
            return False

        # 拉取成功后再重置内存状态，避免请求失败时把现有缓存清空
        self._reset_song_state()
        self.versions = data.get("versions", [])
        self.genres = data.get("genres", [])
        songs = data.get("songs", [])
        self.songList = songs
        for i in self.songList:
            isongid = i.get("id", 0)
            self.songMap[isongid] = i
            differ = i.get("difficulties", [])
            if not differ:
                continue
            for k in differ:
                oricc = k.get("level_value", 0)
                diffi = k.get("difficulty", 0)
                cc = str(round(float(oricc), 1))
                self.ccMap.setdefault(cc, []).append([isongid, diffi])
        self._saveSongCache(songs)
        self._build_meta_maps()
        for i in self.ccMap.values():
            i.sort(key = lambda x: x[1])
        logger.info(f"从网络获取歌曲列表成功，共 {len(songs)} 首, {len(self.versions)} 个版本, {len(self.genres)} 个分类")
        return True

    async def initialize(self):
        """插件初始化时自动调用"""
        if not self.songList:
            await self.loadSongFromApi()
        # 启动令牌自动续期后台任务
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._token_refresh_loop())

    async def _token_refresh_loop(self):
        """后台循环：定期刷新所有用户的令牌，避免刷新令牌因长期未用而过期"""
        while True:
            try:
                await self.tm.refresh_all_tokens()
            except Exception as e:
                logger.error(f"令牌自动续期失败: {e}")
            await asyncio.sleep(self.TOKEN_REFRESH_INTERVAL)

    @filter.command("bind")
    async def bind(self, event: AstrMessageEvent, code: str = ""):
        """OAuth绑定"""
        qqid = event.get_sender_id()
        try:
            testbind = await self.tm.get_valid_token(qqid)
        except RefreshTokenExpiredError:
            testbind = None
        if testbind:
            try:
                headers = {"Authorization": f"Bearer {testbind}"}
                response = await asyncio.to_thread(requests.get, self.playerInfoUrl, headers=headers, timeout=30)
                response.raise_for_status()
                usrdata = response.json()
                name = usrdata.get("data", {}).get("name", "未知用户")
                yield event.plain_result(
                    f"已经绑定为用户{name}了哦"
                )
                return
            except Exception as e:
                yield event.plain_result(f"❌ 网络请求出错: {e}")
                return
        if not code:
            yield event.plain_result(
                f"🔗 请点击以下链接授权：\n{self.oauthUrl}\n\n"
                f"授权完成后会得到一个授权码，之后使用 /bind <授权码> 完成绑定。"
            )
            return

        success = await self.tm.exchange_code(qqid, code.strip())

        if not success:
            yield event.plain_result("❌ 绑定失败，请检查授权码是否正确或是否过期。")
            return

        access_token = await self.tm.get_valid_token(qqid)
        if not access_token:
            yield event.plain_result("❌ 绑定失败，无法获取有效令牌。")
            return

        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            response = await asyncio.to_thread(requests.get, self.playerInfoUrl, headers=headers, timeout=30)
            response.raise_for_status()
            usrdata = response.json()
            if usrdata.get("success"):
                name = usrdata.get("data", {}).get("name", "未知用户")
                yield event.plain_result(f"✅ 绑定成功，用户：{name}")
            else:
                yield event.plain_result(f"❌ API 返回错误: {usrdata.get('message')}")
        except Exception as e:
            yield event.plain_result(f"❌ 网络请求出错: {e}")

    def calcJusticeNumber(self, songid: int, difficulty: int, score: int):
        notes = self.songMap[songid].get("difficulties", [])[difficulty].get("notes", {}).get("total", 0)
        esti = notes - (score - 1000000) * notes / 10000
        low = int(esti)
        high = low + 1
        if int(1010000 / notes * (notes - low) + 1000000 / notes * low) == score:
            return low
        else:
            return high

    def _download_jacket(self, song_id: int):
        """下载单张曲绘"""
        target_file = self.jacketPath / f"{song_id}.png"
        if target_file.exists():
            return str(target_file.absolute())

        url = f"{self.jacketAssetBaseUrl}/{song_id}.png"
        try:
            res = requests.get(url, timeout=10)
            if res.status_code == 200:
                with open(target_file, "wb") as f:
                    f.write(res.content)
                return str(target_file.absolute())
            else:
                logger.warning(f"曲绘下载失败，曲目 ID: {song_id}, 状态码: {res.status_code}")
        except Exception as e:
            logger.error(f"下载曲绘网络出错 (ID: {song_id}): {e}")
        return ""

    async def _ensure_jackets(self, top30_records: list):
        """并发检查并下载所需曲绘"""
        tasks = []
        for rec in top30_records:
            song_id = rec["song_id"]
            tasks.append(asyncio.to_thread(self._download_jacket, song_id))

        paths = await asyncio.gather(*tasks)
        for rec, path in zip(top30_records, paths):
            if path:
                rec["jacket_url"] = f"file://{path}"
            else:
                rec["jacket_url"] = ""

    def _save_jpeg_within_limit(self, img: Image.Image, final_path: Path, max_mb: float | None = None) -> Path:
        """将 PIL 图片保存为 JPEG，并逐级压缩到目标大小以内。

        服务器上行带宽有限，过大的图片会导致 QQ 接口上传超时/失败（日志里表现
        为 API 返回 null）。这里先按 ``MAX_IMAGE_WIDTH``/``MAX_IMAGE_HEIGHT``
        预缩放，再优先降低 JPEG 质量，仍超标时继续缩小分辨率，尽量把最终文件
        压到 ``max_mb`` 之内。
        """
        max_mb = self.imageMaxMb if max_mb is None else max_mb
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")

        # 先按尺寸上限预缩放，超高/超宽的图不必靠反复压缩才达标
        if img.width > self.imageMaxWidth or img.height > self.imageMaxHeight:
            ratio = min(
                self.imageMaxWidth / img.width,
                self.imageMaxHeight / img.height,
            )
            img = img.resize(
                (max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                Image.Resampling.LANCZOS,
            )

        quality = 85
        scale = 1.0
        while True:
            target = img
            if scale < 1.0:
                target = img.resize(
                    (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            target.save(
                final_path,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
            )
            size_mb = final_path.stat().st_size / 1024 / 1024
            if size_mb <= max_mb:
                logger.info(
                    f"图片已保存: {final_path.name} ({size_mb:.2f}MB, quality={quality}, scale={scale:.2f})"
                )
                return final_path
            if quality > 50:
                quality = max(50, quality - 10)
            elif scale > 0.5:
                scale = max(0.5, scale * 0.8)
            else:
                logger.warning(
                    f"图片压缩后仍有 {size_mb:.2f}MB (>{max_mb}MB)，可能无法发送: {final_path.name}"
                )
                return final_path

    def _load_cos_config(self):
        """读取可选的对象存储配置（storagePath/cos.json）。

        文件不存在或字段不全时返回 None，插件自动回退到本地图片直传。
        """
        config_path = self.storagePath / "cos.json"
        if not config_path.exists():
            return None
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"读取 cos.json 失败: {e}")
            return None
        required = ("region", "bucket", "secret_id", "secret_key")
        missing = [key for key in required if not config.get(key)]
        if missing:
            logger.error(f"cos.json 缺少必要字段: {', '.join(missing)}")
            return None
        return config

    def _upload_image_to_cos(self, image_path: Path):
        """上传图片到 COS 并返回预签名 URL；失败返回 None（调用方回退本地发送）。"""
        if not self.cosConfig:
            return None
        try:
            from qcloud_cos import CosConfig, CosS3Client
        except ImportError:
            logger.error("未安装 cos-python-sdk-v5，无法上传 COS")
            return None

        try:
            client = CosS3Client(
                CosConfig(
                    Region=self.cosConfig["region"],
                    SecretId=self.cosConfig["secret_id"],
                    SecretKey=self.cosConfig["secret_key"],
                )
            )
            prefix = str(self.cosConfig.get("prefix", "chunithm")).strip("/")
            # 对象名带时间戳，避免 QQ/COS 把同名 URL 当成缓存
            object_key = f"{image_path.stem}_{int(time.time() * 1000)}{image_path.suffix}"
            if prefix:
                object_key = f"{prefix}/{object_key}"
            with open(image_path, "rb") as image_file:
                client.put_object(
                    Bucket=self.cosConfig["bucket"],
                    Body=image_file,
                    Key=object_key,
                    ContentType="image/jpeg",
                )
            url = client.get_presigned_url(
                Bucket=self.cosConfig["bucket"],
                Key=object_key,
                Method="GET",
                Expired=int(self.cosConfig.get("url_expire_seconds", 86400)),
            )
            logger.info(f"图片已上传到 COS: {object_key}")
            return url
        except Exception as e:
            logger.error(f"上传图片到 COS 失败: {e}")
            return None

    async def _image_ref(self, image_path):
        """返回可发送的图片引用：COS 可用时返回预签名 URL，否则返回本地路径。"""
        if self.cosConfig:
            url = await asyncio.to_thread(self._upload_image_to_cos, Path(image_path))
            if url:
                return url
        return str(image_path)

    def render_aj30_image(self, player_name: str, player_rating: float, top30: list, aj30_avg: float, out_path: str, sender_id: str):
        base_dir = self.storagePath
        env = Environment(loader=FileSystemLoader(base_dir), autoescape=True)
        template = env.get_template("AJ30.html")

        html = template.render(
            player_name = player_name,
            player_rating = player_rating,
            records = top30,
            aj30_avg = aj30_avg,
        )
        hti = Html2Image(output_path = out_path, size = (1800, 1075), custom_flags=[f'--force-device-scale-factor={self.deviceScaleFactor}', '--no-sandbox'])
        tmp_file = f"{sender_id}_AJ30_tmp.png"
        hti.screenshot(
            html_str=html,
            save_as=tmp_file,
        )

        tmp_path = Path(out_path) / tmp_file
        final_path = Path(out_path) / f"{sender_id}_AJ30.jpg"
        self._save_jpeg_within_limit(Image.open(tmp_path), final_path)
        tmp_path.unlink(missing_ok=True)
        return final_path

    @filter.command("caj30")
    async def caj30(self, event: AstrMessageEvent):
        """查询自己的 AJ30"""
        qqid = event.get_sender_id()

        try:
            access_token = await self.tm.get_valid_token(qqid)
        except RefreshTokenExpiredError:
            yield event.plain_result("❌ 你的授权因长期未使用已过期，请使用 /bind 重新绑定。")
            return

        print("Finished fetching user access token for aj30")

        if not access_token:
            yield event.plain_result(
                "❌ 你还未绑定，请使用 /bind 绑定。"
            )
            return

        yield event.plain_result(f"收到，请稍等~")

        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            response = await asyncio.to_thread(requests.get, self.scoresUrl, headers=headers, timeout=30)
            response.raise_for_status()
            scoredata = response.json()
        except Exception as e:
            yield event.plain_result(f"❌ 请求成绩失败: {e}")
            return

        if not scoredata.get("success"):
            yield event.plain_result(f"❌ API返回错误: {scoredata.get('message')}")
            return

        print("Finished fetching score data for aj30")

        scoreList = scoredata.get("data", [])
        ajRecords = []
        for item in scoreList:
            if item.get("full_combo") == "alljustice" or item.get("full_combo") == "alljusticecritical":
                tmpid = item.get("id", 0)
                if tmpid and tmpid in self.songMap:
                    ajRecords.append({
                        "song_id": item.get("id", "0"),
                        "song_name": item.get("song_name", "未知曲目"),
                        "level": self.diffiMap[item.get("level_index", 0)] + " " + item.get("level", "?"),
                        "level_index": item.get("level_index", 0),
                        "cc": self.songMap[item.get("id", 0)].get("difficulties", [])[item.get("level_index", 0)].get("level_value", 0),
                        "score": item.get("score", 0),
                        "rank": self.rankMap[item.get("rank", "sssp")],
                        "justiceCount": self.calcJusticeNumber(item.get("id", 0), item.get("level_index", 0), item.get("score", 0)),
                        "rating": item.get("rating", 0),
                    })

        if not ajRecords:
            yield event.plain_result("⚠️ 暂无 AJ 成绩记录")
            return

        ajRecords.sort(key=lambda x: x["rating"], reverse=True)
        top30 = ajRecords[:30]

        await self._ensure_jackets(top30)

        try:
            response = await asyncio.to_thread(requests.get, self.playerInfoUrl, headers=headers, timeout=30)
            response.raise_for_status()
            playerdata = response.json().get("data", {})
        except Exception as e:
            yield event.plain_result(f"❌ 获取玩家信息失败: {e}")
            return

        msgLines = [f"你最好的 {len(top30)} 条 AJ 成绩:"]
        totRat = 0
        for idx, record in enumerate(top30, 1):
            song = record["song_name"]
            level = record["level"]
            cc = record["cc"]
            rating = record["rating"]
            totRat += rating
            score = record["score"]
            justiceCount = record["justiceCount"]
            msgLines.append(f"{idx}. {song} [{level} ({cc})] {score} {justiceCount}小AJ Rating: {rating:.2f}")
        msgLines.append(f" 你的AJ30为 {(totRat / 30):.2f} ")

        print("Finished aj data processing")

        final_path = await asyncio.to_thread(
            self.render_aj30_image,
            playerdata.get("name", "CHUNITHM"),
            playerdata.get("rating", 0.00),
            top30,
            totRat / 30,
            str(self.bestPath),
            event.get_sender_id()
        )

        yield event.plain_result("您的 AJ30 结果如下：")
        yield event.image_result(await self._image_ref(final_path))

    def render_cc_query_image(self, query_title: str, cc_blocks: list, out_path: str, sender_id: str, page_num: int = 1, total_pages: int = 1):
        """渲染定数查歌结果图片（优化版）"""
        songs_total = sum(len(b["songs"]) for b in cc_blocks)

        base_dir = self.storagePath
        env = Environment(loader=FileSystemLoader(base_dir), autoescape=True)
        template = env.get_template("CSONGLIST.html")

        html = template.render(
            query_title=query_title,
            cc_blocks=cc_blocks,
            total_songs=songs_total
        )

        width = 1600
        songs_per_row = 10
        rows = 0
        for b in cc_blocks:
            songnum = len(b["songs"])
            rows += (songnum + songs_per_row - 1) // songs_per_row
        height = 350 + rows * 185 + len(cc_blocks) * 30

        chrome_flags = [f'--force-device-scale-factor={self.deviceScaleFactor}', '--no-sandbox']
        if songs_total > 80:
            chrome_flags.append('--disable-gpu')

        hti = Html2Image(
            output_path=out_path,
            size=(width, height),
            custom_flags=chrome_flags
        )

        page_suffix = f"_p{page_num}" if total_pages > 1 else ""
        tmp_file = f"{sender_id}_CCQuery_tmp{page_suffix}.png"
        hti.screenshot(
            html_str=html,
            save_as=tmp_file
        )

        tmp_path = Path(out_path) / tmp_file
        final_path = Path(out_path) / f"{sender_id}_CCQuery{page_suffix}.jpg"

        self._save_jpeg_within_limit(Image.open(tmp_path), final_path)
        tmp_path.unlink(missing_ok=True)
        return final_path

    @filter.command("csonglist", alias={"csl"})
    async def csonglist(self, event: AstrMessageEvent, usrcc: str):
        """定数查歌"""
        usrcc = usrcc.strip()
        tarccs = self._parse_cc(usrcc)
        if not tarccs:
            yield event.plain_result("❌ 请输入合法的定数或等级！\n示例：\n/csonglist 14+\n/csonglist 15.3")
            return

        yield event.plain_result("收到，请稍等~")

        cc_blocks = []
        for curcc in tarccs:
            songs_data = []
            for songid, diffi in self.ccMap.get(curcc, []):
                songinfo = self.songMap.get(songid)
                if not songinfo:
                    continue
                jacket_path = self._download_jacket(songid)
                songs_data.append({
                    "song_id": songid,
                    "song_name": songinfo.get("title", "未知曲目"),
                    "diff": diffi,
                    "diff_name": self.diffiMap.get(diffi, "UNK"),
                    "jacket_url": f"file://{jacket_path}" if jacket_path else ""
                })
            if songs_data:
                cc_blocks.append({
                    "cc": curcc,
                    "songs": songs_data
                })

        if not cc_blocks:
            yield event.plain_result("⚠️ 未找到对应定数的歌曲")
            return

        total_songs = sum(len(b["songs"]) for b in cc_blocks)
        if total_songs > self.MAX_SONGS_PER_PAGE:
            pages = self._split_cc_blocks(cc_blocks)
        else:
            pages = [cc_blocks]

        tarcc_label = ", ".join(b["cc"] for b in cc_blocks)
        yield event.plain_result(f"您的定数 {tarcc_label} 查歌结果如下：")

        for i, page in enumerate(pages):
            page_num = i + 1
            total_pages = len(pages)
            query_title = f"定数查歌: {', '.join(b['cc'] for b in cc_blocks)}"
            if total_pages > 1:
                query_title += f" ({page_num}/{total_pages})"

            final_path = await asyncio.to_thread(
                self.render_cc_query_image,
                query_title, page, str(self.ccPath),
                event.get_sender_id(), page_num, total_pages
            )
            yield event.image_result(await self._image_ref(final_path))


    def render_completion_image(self, query_title: str, satis_cnt: int, is_conditional: bool, cc_blocks: list, out_path: str, sender_id: str, page_num: int = 1, total_pages: int = 1):
        """渲染带有用户成绩的完成表图片"""
        songs_total = sum(len(b["songs"]) for b in cc_blocks)
        base_dir = self.storagePath
        env = Environment(loader=FileSystemLoader(base_dir), autoescape=True)
        template = env.get_template("CCOMPLETE.html")

        html = template.render(
            query_title=query_title,
            satis_cnt=satis_cnt,
            is_conditional=is_conditional,
            cc_blocks=cc_blocks,
            total_songs=songs_total
        )

        width = 1600
        songs_per_row = 10
        rows = 0
        for b in cc_blocks:
            songnum = len(b["songs"])
            rows += (songnum + songs_per_row - 1) // songs_per_row
        height = 350 + rows * 185 + len(cc_blocks) * 30

        chrome_flags = [f'--force-device-scale-factor={self.deviceScaleFactor}', '--no-sandbox']
        if songs_total > 80:
            chrome_flags.append('--disable-gpu')

        hti = Html2Image(
            output_path=out_path,
            size=(width, height),
            custom_flags=chrome_flags
        )

        page_suffix = f"_p{page_num}" if total_pages > 1 else ""
        tmp_file = f"{sender_id}_Completion_tmp{page_suffix}.png"
        hti.screenshot(
            html_str=html,
            save_as=tmp_file
        )

        tmp_path = Path(out_path) / tmp_file
        final_path = Path(out_path) / f"{sender_id}_Completion{page_suffix}.jpg"

        self._save_jpeg_within_limit(Image.open(tmp_path), final_path)
        tmp_path.unlink(missing_ok=True)
        return final_path

    @filter.command("ccomplete", alias={"cc", "ccpt"})
    async def ccomplete(self, event: AstrMessageEvent, args: GreedyStr):
        """查询某定数/等级/版本/分类的个人成绩完成表"""
        qqid = event.get_sender_id()
        usrcc, usrdiff, only, minrank, expbelow = self._split_ccomplete_args(str(args))
        usrcc = usrcc.strip()
        minrank = minrank.strip().upper()

        try:
            only_val = int(only)
            if only_val not in [0, 1]:
                raise ValueError
        except ValueError:
            yield event.plain_result(
                "❌ only参数只能为0（默认）或者1！\n"
                "值为0时会显示你输入难度差分及以上（对于EXPERT来说，会显示EXPERT MASTER ULTIMA的谱面）。\n"
                "值为1的时候只会显示对应难度差分的谱面。"
            )
            return

        query_type, query_data = self._detect_query_type(usrcc)
        if query_type is None:
            yield event.plain_result(
                "❌ 无法识别查询类型！\n"
                "请输入合法的定数（如14+，15.3）、版本名（如amazon）或分类名（如pops & anime）。\n"
                "示例：\n/cc 14+ MASTER\n/cc AMAZON\n/cc original"
            )
            return

        try:
            expbelow_val = bool(int(expbelow))
        except ValueError:
            yield event.plain_result("❌ expbelow参数只能为0（默认不显示EXPERT及以下难度）或者1（显示所有难度）！")
            return

        if query_type in ("version", "genre") and not expbelow_val:
            minDiffi = 3
            usrdiff = "MASTER"
        else:
            if not usrdiff.isalpha():
                yield event.plain_result("❌ 请输入合法的难度！\n示例：\n/ccomplete 14+ MASTER\n/ccomplete AMAZON expert")
                return
            usrdiff = usrdiff.upper()
            if usrdiff not in self.diffiInverted:
                yield event.plain_result("❌ 请输入合法的难度！\n示例：\n/ccomplete 14+ MASTER 0\n/ccomplete 13.2 expert 1")
                return
            minDiffi = self.diffiInverted[usrdiff]

        if only_val:
            diffiList = [minDiffi]
        else:
            diffiList = [i for i in range(minDiffi, 6)]

        try:
            access_token = await self.tm.get_valid_token(qqid)
        except RefreshTokenExpiredError:
            yield event.plain_result("❌ 你的授权因长期未使用已过期，请使用 /bind 重新绑定。")
            return

        if not access_token:
            yield event.plain_result("❌ 你还未绑定，请使用 /bind 绑定。")
            return

        yield event.plain_result(f"收到，请稍等~")

        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            response = await asyncio.to_thread(requests.get, self.scoresUrl, headers=headers, timeout=30)
            response.raise_for_status()
            scoredata = response.json()
        except Exception as e:
            yield event.plain_result(f"❌ 请求个人成绩失败: {e}")
            return

        if not scoredata.get("success"):
            yield event.plain_result(f"❌ API返回错误: {scoredata.get('message')}")
            return

        user_scores_map = {}
        for item in scoredata.get("data", []):
            sid = item.get("id")
            l_idx = item.get("level_index")
            if sid is not None and l_idx is not None:
                user_scores_map[f"{sid}_{l_idx}"] = item

        try:
            p_res = await asyncio.to_thread(requests.get, self.playerInfoUrl, headers=headers, timeout=30)
            p_res.raise_for_status()
            player_name = p_res.json().get("data", {}).get("name", "CHUNITHM")
        except Exception:
            player_name = "CHUNITHM"

        print("Finished completion data fetching")

        rank_order = {"D": 1, "C": 2, "B": 3, "BB": 4, "BBB": 5, "A": 6, "AA": 7, "AAA": 8,
                      "S": 9, "SP": 10, "SS": 11, "SSP": 12, "SSS": 13, "SSSP": 14}

        target_rank = minrank.replace("+", "P").upper()
        if target_rank not in rank_order and target_rank not in ["FC", "AJ", "AJC"] and target_rank != "NONE":
            yield event.plain_result(
                "❌ 请输入合法的完成情况！\n"
                "示例：SSS、SSS+、AJ......"
            )
            return
        is_conditional = target_rank in rank_order or target_rank in ["FC", "AJ", "AJC"]
        satis_cnt = 0

        if query_type == "cc":
            tarccs = query_data
            query_label = ", ".join(tarccs)
            cc_blocks = []
            for curcc in tarccs:
                songs_data = []
                for songid, diffi in self.ccMap.get(curcc, []):
                    if diffi not in diffiList:
                        continue
                    entry, inc = self._build_song_entry(songid, diffi, user_scores_map, rank_order, target_rank, is_conditional)
                    if entry is not None:
                        songs_data.append(entry)
                        satis_cnt += inc
                if songs_data:
                    cc_blocks.append({"cc": curcc, "songs": songs_data})
        elif query_type == "version":
            song_list = self.songs_by_version.get(query_data, [])
            query_label = self.version_by_value.get(query_data, usrcc)
            cc_groups = {}
            for song_id, diffi in song_list:
                if diffi not in diffiList:
                    continue
                cc = str(round(float(self.songMap[song_id]["difficulties"][diffi]["level_value"]), 1))
                if cc not in cc_groups:
                    cc_groups[cc] = []
                cc_groups[cc].append((song_id, diffi))
            cc_blocks = []
            for cc in sorted(cc_groups.keys(), key=float):
                songs_data = []
                for song_id, diffi in cc_groups[cc]:
                    entry, inc = self._build_song_entry(song_id, diffi, user_scores_map, rank_order, target_rank, is_conditional)
                    if entry is not None:
                        songs_data.append(entry)
                        satis_cnt += inc
                if songs_data:
                    cc_blocks.append({"cc": cc, "songs": songs_data})
        else:
            song_list = self.songs_by_genre.get(query_data, [])
            query_label = query_data
            cc_groups = {}
            for song_id, diffi in song_list:
                if diffi not in diffiList:
                    continue
                cc = str(round(float(self.songMap[song_id]["difficulties"][diffi]["level_value"]), 1))
                if cc not in cc_groups:
                    cc_groups[cc] = []
                cc_groups[cc].append((song_id, diffi))
            cc_blocks = []
            for cc in sorted(cc_groups.keys(), key=float):
                songs_data = []
                for song_id, diffi in cc_groups[cc]:
                    entry, inc = self._build_song_entry(song_id, diffi, user_scores_map, rank_order, target_rank, is_conditional)
                    if entry is not None:
                        songs_data.append(entry)
                        satis_cnt += inc
                if songs_data:
                    cc_blocks.append({"cc": cc, "songs": songs_data})

        if not cc_blocks:
            yield event.plain_result("⚠️ 未找到对应条件的歌曲")
            return

        rank_label = "" if minrank == "NONE" else f" {minrank}"
        diff_label = f" ({usrdiff}{'' if only_val else '及以上'})"
        base_title = f"{player_name} 的 {query_label}{rank_label} 完成表{diff_label}"

        total_songs = sum(len(b["songs"]) for b in cc_blocks)
        if total_songs > self.MAX_SONGS_PER_PAGE:
            pages = self._split_cc_blocks(cc_blocks)
        else:
            pages = [cc_blocks]

        print("Finished completion data processing")

        yield event.plain_result(f"您的 {query_label}{rank_label} 完成表结果如下：")

        for i, page in enumerate(pages):
            page_num = i + 1
            total_pages = len(pages)
            query_title = base_title
            if total_pages > 1:
                query_title += f" ({page_num}/{total_pages})"

            page_satis = sum(1 for b in page for s in b["songs"] if s.get("show_check"))

            final_path = await asyncio.to_thread(
                self.render_completion_image,
                query_title,
                page_satis,
                is_conditional,
                page,
                str(self.ccPath),
                event.get_sender_id(),
                page_num,
                total_pages,
            )
            yield event.image_result(await self._image_ref(final_path))

    # ------------------------------------------------------------------
    # 管理员专属指令
    #
    # 使用 @admin_command("指令名") 注册，只有 AstrBot 配置 admins_id 中的
    # 用户才能触发。以后要新增类似的管理员指令，照下面 refreshsongs 的写法
    # 加一个 async 生成器方法即可，权限过滤由装饰器统一处理。
    # ------------------------------------------------------------------

    @admin_command("refreshsongs", alias={"refreshcache"})
    async def refreshsongs(self, event: AstrMessageEvent):
        """（管理员）重新拉取歌曲缓存，旧缓存按时间戳备份"""
        yield event.plain_result("收到，请稍等~ 正在刷新歌曲缓存……")

        archived = None
        if self.songCacheFile.exists():
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            archived = self.storagePath / f"songs_{timestamp}.json"
            try:
                self.songCacheFile.rename(archived)
                logger.info(f"旧歌曲缓存已备份为 {archived.name}")
            except Exception as e:
                logger.error(f"备份旧歌曲缓存失败: {e}")
                yield event.plain_result(f"❌ 备份旧缓存失败: {e}")
                return

        success = await self.loadSongFromApi()
        if not success:
            # 刷新失败时把旧缓存恢复回来，避免插件处于无缓存状态
            if archived is not None and archived.exists() and not self.songCacheFile.exists():
                try:
                    archived.rename(self.songCacheFile)
                except Exception as e:
                    logger.error(f"恢复旧歌曲缓存失败: {e}")
            yield event.plain_result("❌ 刷新歌曲缓存失败，请查看日志。")
            return

        msgLines = [
            f"✅ 歌曲缓存已刷新：{len(self.songList)} 首歌曲，"
            f"{len(self.versions)} 个版本，{len(self.genres)} 个分类。"
        ]
        if archived is not None:
            msgLines.append(f"旧缓存已备份为 {archived.name}")
        yield event.plain_result("\n".join(msgLines))

    @filter.command("help")
    async def help(self, event: AstrMessageEvent):
        msgLines = ["可用的指令："]
        msgLines.append("/bind -- 绑定落雪账号。请先不带参数直接输入/bind得到授权链接")
        msgLines.append("/caj30 -- 生成中二节奏AJ30")
        msgLines.append("/csonglist -- 根据定数/等级查歌")
        msgLines.append("/ccomplete -- 定数/等级/版本/分类进度表")
        msgLines.append("  定数查询: /cc 14+  /cc 15.3  /cc 14  (默认显示所有难度)")
        msgLines.append("  版本查询: /cc AMAZON  /cc crystal plus  (默认仅显示MASTER及以上)")
        msgLines.append("  分类查询: /cc 原创  /cc 流行 & 动漫  (默认仅显示MASTER及以上)")
        msgLines.append("  完整参数: /cc <查询词(可含空格)> [难度] [only] [minrank] [expbelow]")
        msgLines.append("  也可显式指定: /cc crystal plus diff=MASTER only=1 minrank=SSS expbelow=0")
        msgLines.append("  expbelow=0 默认隐藏版本/分类查询中的EXPERT及以下难度")
        if event.is_admin():
            msgLines.append("")
            msgLines.append("管理员指令：")
            msgLines.append("/refreshsongs -- 刷新歌曲缓存，旧缓存自动备份为 songs_时间戳.json")

        yield event.plain_result("\n".join(msgLines))

    # @filter.command("hello")
    # async def hello(self, event: astrmessageevent):
    #     name = event.get_sender_name()
    #     id = event.get_sender_id()
    #     n1 = event.message_obj.group_id
    #     n2 = event.message_obj.sender.__str__()
    #
    #     yield event.plain_result(f"{name} {id} {n1} {n2}")

    async def terminate(self):
        """插件卸载时调用"""
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            self._refresh_task = None
