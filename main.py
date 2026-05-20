from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

import requests
import json

API_BASE_URL = "https://maimai.lxns.net/api/v0/user/chunithm"
API_KEY = ""

@register("helloworld", "YourName", "一个简单的 Hello World 插件", "1.0.0")
class MyPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""

    # 注册指令的装饰器。指令名为 helloworld。注册成功后，发送 `/helloworld` 就会触发这个指令，并回复 `你好, {user_name}!`
    @filter.command("helloworld")
    async def helloworld(self, event: AstrMessageEvent):
        """这是一个 hello world 指令""" # 这是 handler 的描述，将会被解析方便用户了解插件内容。建议填写。
        user_name = event.get_sender_name()
        message_str = event.message_str # 用户发的纯文本消息字符串
        message_chain = event.get_messages() # 用户所发的消息的消息链 # from astrbot.api.message_components import *
        logger.info(message_chain)
        yield event.plain_result(f"Hello, {user_name}, 你发了 {message_str}!") # 发送一条纯文本消息

    @filter.command("bind")
    async def bind(self, event: AstrMessageEvent, usrapi: str):
        API_KEY = usrapi
        url = f"{API_BASE_URL}/player"
        queryHeaders = {
            "X-User-Token": API_KEY
        }
        try:
            response = requests.get(url, headers=queryHeaders)
            response.raise_for_status() # 检查HTTP请求是否出错
            data = response.json()

            if data.get("success"):
                # 请求成功，处理data字段中的数据
                yield event.plain_result(f"API绑定为用户{data.data.name}") # 发送一条纯文本消息
            else:
                yield event.plain_result(f"API返回错误: {data.get('message')}")
        except requests.exceptions.RequestException as e:
            yield event.plain_result(f"网络请求出错: {e}")

    @filter.command("caj30")
    async def caj30(self, event: AstrMessageEvent):
        pass

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
