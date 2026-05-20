from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

import requests
import json
import asyncio
from pathlib import Path

@register("chunithm_lx", "Lauretta", "中二节奏机器人", "0.1.1")
class Lauretta(Star):
    def __init__(self, context: Context):
        super().__init__(context)

        self.apiKey = ""

        self.baseUserApi = "https://maimai.lxns.net/api/v0/user/chunithm"
        self.playerInfoUrl = f"{self.baseUserApi}/player"
        self.scoresUrl = f"{self.baseUserApi}/player/scores"

        self.songListUrl = "https://maimai.lxns.net/api/v0/chunithm/song/list"
        self.aliasListUrl = "https://maimai.lxns.net/api/v0/chunithm/alias/list"

        dataPath = Path(get_astrbot_data_path())
        self.storagePath = dataPath / "plugin_data" / "astrbot_plugin_chunithm_lx"
        self.storagePath.mkdir(parents=True, exist_ok=True)
        self.songCacheFile = self.storagePath / "songs.json"

        self.diffiMap = {
            0: "BASIC",
            1: "ADVANCED",
            2: "EXPERT",
            3: "MASTER",
            4: "ULTIMA",
            5: "WORLD\'S END",
        }

        self.songList = []
        self.songMap = {}
        self._loadSongCache()

    def _loadSongCache(self):
        """从本地文件加载歌曲列表"""
        if self.songCacheFile.exists():
            try:
                with open(self.songCacheFile, 'r', encoding='utf-8') as f:
                    self.songList = json.load(f)
                for i in self.songList:
                    self.songMap[i.get("id", 0)] = i
                logger.info(f"已从缓存加载 {len(self.songList)} 首歌曲")
            except Exception as e:
                logger.error(f"加载歌曲缓存失败: {e}")
                self.songList = []
                self.songMap = []

    def _saveSongCache(self, songs):
        """保存歌曲列表到本地"""
        try:
            with open(self.songCacheFile, 'w', encoding='utf-8') as f:
                json.dump(songs, f, ensure_ascii=False, indent=2)
            logger.info(f"已缓存 {len(songs)} 首歌曲")
        except Exception as e:
            logger.error(f"保存歌曲缓存失败: {e}")

    async def loadSongFromApi(self):
        """从 API 获取歌曲列表"""
        try:
            response = await asyncio.to_thread(requests.get, self.songListUrl, params={"notes": "true"})
            response.raise_for_status()
            data = response.json()
            songs = data.get("songs", [])
            self.songList = songs
            for i in self.songList:
                self.songMap[i.get("id", 0)] = i
            self._saveSongCache(songs)
            logger.info(f"从网络获取歌曲列表成功，共 {len(songs)} 首")
        except Exception as e:
            logger.error(f"网络请求出错: {e}")

    async def initialize(self):
        """插件初始化时自动调用"""
        if not self.songList:
            await self.loadSongFromApi()

    @filter.command("bind")
    async def bind(self, event: AstrMessageEvent, usrapi: str):
        """绑定个人API Token，格式：/bind <token>"""
        self.apiKey = usrapi
        headers = {"X-User-Token": self.apiKey}
        try:
            response = await asyncio.to_thread(requests.get, self.playerInfoUrl, headers=headers)
            response.raise_for_status()
            usrdata = response.json()
            if usrdata.get("success"):
                name = usrdata.get("data", {}).get("name", "未知用户")
                yield event.plain_result(f"✅ API绑定成功，用户：{name}")
            else:
                yield event.plain_result(f"❌ API返回错误: {usrdata.get('message')}")
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

    @filter.command("caj30")
    async def caj30(self, event: AstrMessageEvent):
        """查询自己的 AJ30"""
        if not self.apiKey:
            yield event.plain_result("❌ 请先使用 /bind 绑定你的 API Token")
            return

        headers = {"X-User-Token": self.apiKey}
        try:
            response = await asyncio.to_thread(requests.get, self.scoresUrl, headers=headers)
            response.raise_for_status()
            scoredata = response.json()
        except Exception as e:
            yield event.plain_result(f"❌ 请求成绩失败: {e}")
            return

        if not scoredata.get("success"):
            yield event.plain_result(f"❌ API返回错误: {scoredata.get('message')}")
            return

        scoreList = scoredata.get("data", [])
        ajRecords = []
        for item in scoreList:
            if item.get("full_combo") == "alljustice":
                ajRecords.append({
                    "song_name": item.get("song_name", "未知曲目"),
                    "level": self.diffiMap[item.get("level_index", 0)] + " " + item.get("level", "?"),
                    "cc": self.songMap[item.get("id", 0)].get("difficulties", [])[item.get("level_index", 0)].get("level_value", 0),
                    "score": item.get("score", 0),
                    "justiceCount": self.calcJusticeNumber(item.get("id", 0), item.get("level_index", 0), item.get("score", 0)),
                    "rating": item.get("rating", 0),
                })

        if not ajRecords:
            yield event.plain_result("⚠️ 暂无 AJ 成绩记录")
            return

        ajRecords.sort(key=lambda x: x["rating"], reverse=True)
        top30 = ajRecords[:30]

        msgLines = [f" 你最好的 {len(top30)} 条 AJ 成绩:"]
        totRat = 0
        for idx, record in enumerate(top30, 1):
            song = record["song_name"]
            level = record["level"]
            cc = record["cc"]
            rating = record["rating"]
            totRat += eval(rating)
            score = record["score"]
            justiceCount = record["justiceCount"]
            msgLines.append(f"{idx}. {song} [{level} ({cc})] {score} {justiceCount}小AJ Rating: {rating:.2f}")
        msgLines.append(f" 你的AJ30为 {(totRat / 30):.2f} ")

        yield event.plain_result("\n".join(msgLines))

    async def terminate(self):
        """插件卸载时调用"""
