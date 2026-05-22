from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

import requests
import json
import asyncio
from pathlib import Path
from jinja2 import Environment, FileSystemLoader
from html2image import Html2Image
from PIL import Image
from .TokenManager import TokenManager

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
        }
        self.ccs = []
        self.ccMap = {}
        tmpi = 0
        while tmpi <= 157:
            curcc = str(round(tmpi / 10.0, 1))
            self.ccMap[curcc] = []
            self.ccs.append(curcc)
            if tmpi <= 60:
                tmpi += 10
            elif tmpi <= 95:
                tmpi += 5
            else:
                tmpi += 1

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
                    isongid = i.get("id", 0)
                    self.songMap[isongid] = i
                    differ = i.get("difficulties", [])
                    if not differ:
                        continue
                    for k in differ:
                        oricc = k.get("level_value", 0)
                        diffi = k.get("difficulty", 0)
                        cc = str(round(float(oricc), 1))
                        self.ccMap[cc].append([isongid, diffi])
                for i in self.ccMap.values():
                    i.sort(key = lambda x: x[1])
                logger.info(f"已从缓存加载 {len(self.songList)} 首歌曲")
            except Exception as e:
                logger.error(f"加载歌曲缓存失败: {e}")
                self.songList = []
                self.songMap = {}

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
                isongid = i.get("id", 0)
                self.songMap[isongid] = i
                differ = i.get("difficulties", [])
                if not differ:
                    continue
                for k in differ:
                    oricc = k.get("level_value", 0)
                    diffi = k.get("difficulty", 0)
                    cc = str(round(float(oricc), 1))
                    self.ccMap[cc].append([isongid, diffi])
            self._saveSongCache(songs)
            for i in self.ccMap.values():
                i.sort(key = lambda x: x[1])
            logger.info(f"从网络获取歌曲列表成功，共 {len(songs)} 首")
        except Exception as e:
            logger.error(f"网络请求出错: {e}")

    async def initialize(self):
        """插件初始化时自动调用"""
        if not self.songList:
            await self.loadSongFromApi()

    @filter.command("bind")
    async def bind(self, event: AstrMessageEvent, code: str = ""):
        """OAuth绑定"""
        qqid = event.get_sender_id()
        testbind = await self.tm.get_valid_token(qqid)
        if testbind:
            try:
                headers = {"Authorization": f"Bearer {testbind}"}
                response = await asyncio.to_thread(requests.get, self.playerInfoUrl, headers=headers)
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
            response = await asyncio.to_thread(requests.get, self.playerInfoUrl, headers=headers)
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
        hti = Html2Image(output_path = out_path, size = (1800, 1075), custom_flags=['--force-device-scale-factor=4'])
        hti.screenshot(
            html_str=html,
            save_as=f"{sender_id}_AJ30.png",
        )

    @filter.command("caj30")
    async def caj30(self, event: AstrMessageEvent):
        """查询自己的 AJ30"""
        qqid = event.get_sender_id()

        access_token = await self.tm.get_valid_token(qqid)

        if not access_token:
            yield event.plain_result(
                "❌ 你还未绑定或授权已过期，请使用 /bind 重新绑定。"
            )
            return

        headers = {"Authorization": f"Bearer {access_token}"}
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
            response = await asyncio.to_thread(requests.get, self.playerInfoUrl, headers=headers)
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

        await asyncio.to_thread(
            self.render_aj30_image,
            playerdata.get("name", "CHUNITHM"),
            playerdata.get("rating", 0.00),
            top30,
            totRat / 30,
            str(self.bestPath),
            event.get_sender_id()
        )

        yield event.image_result(str(self.bestPath) + "/" + f"{event.get_sender_id()}_AJ30.png")

    def render_cc_query_image(self, query_title: str, cc_blocks: list, out_path: str, sender_id: str):
        """渲染定数查歌结果图片（优化版）"""

        base_dir = self.storagePath
        env = Environment(loader=FileSystemLoader(base_dir), autoescape=True)
        template = env.get_template("CSONGLIST.html")

        html = template.render(
            query_title=query_title,
            cc_blocks=cc_blocks,
            total_songs=sum(len(b["songs"]) for b in cc_blocks)
        )

        width = 1600
        songs_per_row = 10
        rows = 0
        for b in cc_blocks:
            songnum = len(b["songs"])
            rows += (songnum + songs_per_row - 1) // songs_per_row
        height = 350 + rows * 185 + len(cc_blocks) * 30

        hti = Html2Image(
            output_path=out_path,
            size=(width, height),
            custom_flags=['--force-device-scale-factor=2']
        )

        tmp_file = f"{sender_id}_CCQuery_tmp.png"
        hti.screenshot(
            html_str=html,
            save_as=tmp_file
        )

        tmp_path = Path(out_path) / tmp_file
        final_path = Path(out_path) / f"{sender_id}_CCQuery.jpg"

        img = Image.open(tmp_path)
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')

        img.save(
            final_path,
            format='JPEG',
            quality=85,
            optimize=True,
            progressive=True
        )

        tmp_path.unlink(missing_ok=True)

        file_size = final_path.stat().st_size / 1024 / 1024  # MB
        if file_size > 10:
            logger.warning(f"图片仍然过大: {file_size:.2f}MB，尝试进一步压缩")
            img = Image.open(final_path)
            img.save(
                final_path,
                format='JPEG',
                quality=75,
                optimize=True,
                progressive=True
            )

    @filter.command("csonglist", alias={"csl"})
    async def csonglist(self, event: AstrMessageEvent, usrcc: str):
        """定数查歌"""
        usrcc = usrcc.strip()
        tarccs = []
        if usrcc in self.ccs:
            tarccs.append(usrcc)
        elif usrcc.endswith("+"):
            baseStr = usrcc[:-1]
            if baseStr.isdigit():
                baseVal = int(baseStr)
                if 7 <= baseVal <= 9:
                    tarccs.append(f"{baseVal}.5")
                elif 10 <= baseVal <= 14:
                    for dec in range(5, 10):
                        tarccs.append(f"{baseVal}.{dec}")
                elif baseVal == 15:
                    for dec in range(5, 8):
                        tarccs.append(f"{baseVal}.{dec}")
        elif usrcc.isdigit():
            baseVal = int(usrcc)
            if 1 <= baseVal <= 6:
                tarccs.append(f"{baseVal}.0")
            elif 7 <= baseVal <= 9:
                tarccs.append(f"{baseVal}.0")
                tarccs.append(f"{baseVal}.5")
            elif 10 <= baseVal <= 15:
                for dec in range(0, 5):
                    tarccs.append(f"{baseVal}.{dec}")
        if not tarccs:
            yield event.plain_result("❌ 请输入合法的定数或等级！\n示例：\n/csonglist 14+\n/csonglist 15.3")
            return

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

        query_title = f"定数查歌: {', '.join(b['cc'] for b in cc_blocks)}"

        await asyncio.to_thread(
            self.render_cc_query_image,
            query_title,
            cc_blocks,
            str(self.ccPath),
            event.get_sender_id()
        )

        yield event.image_result(f"{self.ccPath}/{event.get_sender_id()}_CCQuery.jpg")


    def render_completion_image(self, query_title: str, cc_blocks: list, out_path: str, sender_id: str):
        """渲染带有用户成绩的完成表图片"""
        base_dir = self.storagePath
        env = Environment(loader=FileSystemLoader(base_dir), autoescape=True)
        template = env.get_template("CCOMPLETE.html")

        html = template.render(
            query_title=query_title,
            cc_blocks=cc_blocks,
            total_songs=sum(len(b["songs"]) for b in cc_blocks)
        )

        width = 1600
        songs_per_row = 10
        rows = 0
        for b in cc_blocks:
            songnum = len(b["songs"])
            rows += (songnum + songs_per_row - 1) // songs_per_row
        height = 350 + rows * 185 + len(cc_blocks) * 30

        hti = Html2Image(
            output_path=out_path,
            size=(width, height),
            custom_flags=['--force-device-scale-factor=2']
        )

        tmp_file = f"{sender_id}_Completion_tmp.png"
        hti.screenshot(
            html_str=html,
            save_as=tmp_file
        )

        tmp_path = Path(out_path) / tmp_file
        final_path = Path(out_path) / f"{sender_id}_Completion.jpg"

        img = Image.open(tmp_path)
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')

        img.save(
            final_path,
            format='JPEG',
            quality=85,
            optimize=True,
            progressive=True
        )

        tmp_path.unlink(missing_ok=True)

        file_size = final_path.stat().st_size / 1024 / 1024  # MB
        if file_size > 10:
            logger.warning(f"图片仍然过大: {file_size:.2f}MB，尝试进一步压缩")
            img = Image.open(final_path)
            img.save(
                final_path,
                format='JPEG',
                quality=75,
                optimize=True,
                progressive=True
            )

    @filter.command("ccomplete", alias={"cc", "ccpt"})
    async def ccomplete(self, event: AstrMessageEvent, usrcc: str, usrdiff: str = "BASIC", only: str = "0", minrank: str = "NONE"):
        """查询某定数/等级的个人成绩完成表"""
        qqid = event.get_sender_id()
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

        if not usrdiff.isalpha():
            yield event.plain_result("❌ 请输入合法的难度！\n示例：\n/ccomplete 14+ MASTER\n/ccomplete 13.2 expert")
            return

        usrdiff = usrdiff.upper()
        if usrdiff not in self.diffiInverted.keys():
            yield event.plain_result("❌ 请输入合法的难度！\n示例：\n/ccomplete 14+ MASTER 0\n/ccomplete 13.2 expert 1")
            return

        minDiffi = self.diffiInverted[usrdiff]
        if only_val:
            diffiList = [minDiffi]
        else:
            diffiList = [i for i in range(minDiffi, 6)]

        access_token = await self.tm.get_valid_token(qqid)
        if not access_token:
            yield event.plain_result("❌ 你还未绑定或授权已过期，请使用 /bind 重新绑定。")
            return

        tarccs = []
        if usrcc in self.ccs:
            tarccs.append(usrcc)
        elif usrcc.endswith("+"):
            baseStr = usrcc[:-1]
            if baseStr.isdigit():
                baseVal = int(baseStr)
                if 7 <= baseVal <= 9:
                    tarccs.append(f"{baseVal}.5")
                elif 10 <= baseVal <= 14:
                    for dec in range(5, 10):
                        tarccs.append(f"{baseVal}.{dec}")
                elif baseVal == 15:
                    for dec in range(5, 8):
                        tarccs.append(f"{baseVal}.{dec}")
        elif usrcc.isdigit():
            baseVal = int(usrcc)
            if 1 <= baseVal <= 6:
                tarccs.append(f"{baseVal}.0")
            elif 7 <= baseVal <= 9:
                tarccs.append(f"{baseVal}.0")
                tarccs.append(f"{baseVal}.5")
            elif 10 <= baseVal <= 15:
                for dec in range(0, 5):
                    tarccs.append(f"{baseVal}.{dec}")

        if not tarccs:
            yield event.plain_result("❌ 请输入合法的定数或等级！\n示例：\n/ccomplete 14+\n/ccomplete 13.2")
            return

        headers = {"Authorization": f"Bearer {access_token}"}
        try:
            response = await asyncio.to_thread(requests.get, self.scoresUrl, headers=headers)
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
            p_res = await asyncio.to_thread(requests.get, self.playerInfoUrl, headers=headers)
            p_res.raise_for_status()
            player_name = p_res.json().get("data", {}).get("name", "CHUNITHM")
        except Exception:
            player_name = "CHUNITHM"

        rank_order = {"D": 1, "C": 2, "B": 3, "BB": 4, "BBB": 5, "A": 6, "AA": 7, "AAA": 8,
                      "S": 9, "SP": 10, "SS": 11, "SSP": 12, "SSS": 13, "SSSP": 14}

        target_rank = minrank.replace("+", "P")
        target_rank = target_rank.upper()
        if target_rank not in rank_order and target_rank not in ["FC", "AJ"] and target_rank != "NONE":
            yield event.plain_result(
                "❌ 请输入合法的完成情况！\n"
                "示例：SSS、SSS+、AJ......"
            )
            return
        is_conditional = target_rank in rank_order or target_rank in ["FC", "AJ"]

        cc_blocks = []
        for curcc in tarccs:
            songs_data = []
            for songid, diffi in self.ccMap.get(curcc, []):
                if diffi not in diffiList:
                    continue

                songinfo = self.songMap.get(songid)
                if not songinfo:
                    continue

                jacket_path = self._download_jacket(songid)
                lookup_key = f"{songid}_{diffi}"
                played_info = user_scores_map.get(lookup_key)

                is_played = False
                show_check = False
                rank_str = ""
                rank_class = "OTHER"
                badge_type = ""
                badge_name = ""
                satis_cnt = 0

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
                                satis_cnt += 1
                        elif target_rank in ["FC", "AJ"]:
                            badge_ranks = {"FC": 1, "AJ": 2}

                            if fc_aj_status in badge_ranks.keys() and badge_ranks[fc_aj_status] >= badge_ranks[target_rank]:
                                satisfy = True
                                satis_cnt += 1

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

                songs_data.append({
                    "song_id": songid,
                    "song_name": songinfo.get("title", "未知曲目"),
                    "diff": diffi,
                    "diff_name": self.diffiMap.get(diffi, "UNK"),
                    "jacket_url": f"file://{jacket_path}" if jacket_path else "",
                    "played": is_played,
                    "show_check": show_check,
                    "rank": rank_str,
                    "rank_class": rank_class,
                    "badge_type": badge_type,
                    "badge_name": badge_name
                })

            if songs_data:
                cc_blocks.append({
                    "cc": curcc,
                    "songs": songs_data
                })

        if not cc_blocks:
            yield event.plain_result("⚠️ 未找到对应条件的歌曲")
            return

        query_title = f"{player_name} 的 {usrcc} {'' if minrank == 'NONE' else minrank} 完成表 ({usrdiff}{'' if only_val else '及以上'})"

        await asyncio.to_thread(
            self.render_completion_image,
            query_title,
            cc_blocks,
            str(self.ccPath),
            event.get_sender_id()
        )

        yield event.image_result(f"{self.ccPath}/{event.get_sender_id()}_Completion.jpg")

    @filter.command("help")
    async def help(self, event: AstrMessageEvent):
        msgLines = ["可用的指令："]
        msgLines.append("/bind -- 绑定落雪账号。请先不带参数直接输入/bind得到授权链接")
        msgLines.append("/caj30 -- 生成中二节奏AJ30")
        msgLines.append("/csonglist -- 根据定数/等级查歌")
        msgLines.append("/ccomplete -- 定数/等级进度表")

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
