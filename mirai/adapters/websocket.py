# -*- coding: utf-8 -*-
"""
此模块提供正向 WebSocket 适配器，适用于 mirai-api-http 的 websocket adapter。
"""
import asyncio
import json
import logging
import random
from collections import defaultdict, deque
from typing import Optional

import websockets

from mirai import exceptions
from mirai.adapters.base import (
    Adapter, AdapterInterface, error_handler_async, json_dumps
)
from mirai.api_provider import Method
from mirai.tasks import Tasks

logger = logging.getLogger(__name__)

_error_handler_async_local = error_handler_async(
    (ConnectionRefusedError, websockets.InvalidURI)
)


class WebSocketAdapter(Adapter):
    """WebSocket 适配器。作为 WebSocket 客户端与 mirai-api-http 沟通。
    """
    host_name: str
    """WebSocket Server 的地址。"""
    sync_id: str
    """mirai-api-http 配置的同步 ID。"""
    qq: int
    """机器人的 QQ 号。"""
    connection: websockets.WebSocketClientProtocol
    """WebSocket 客户端连接。"""
    def __init__(
        self,
        verify_key: Optional[str],
        host: str,
        port: int,
        sync_id: str = '-1',
        single_mode: bool = False
    ):
        """
        `verify_key: str` mirai-api-http 配置的认证 key，关闭认证时为 None。

        `host: str` WebSocket Server 的地址。

        `port: int` WebSocket Server 的端口。

        `sync_id: int` mirai-api-http 配置的同步 ID。

        `single_mode: bool = False` 是否启用单例模式。
        """
        super().__init__(verify_key=verify_key, single_mode=single_mode)

        self._host = host
        self._port = port

        if host[:2] == '//':
            host = 'ws:' + host
        elif host[:7] == 'http://' or host[:8] == 'https://':
            raise exceptions.NetworkError(f'{host} 不是一个可用的 WebSocket 地址！')
        elif host[:5] != 'ws://':
            host = 'ws://' + host

        if host[-1:] == '/':
            host = host[:-1]

        self.host_name = f'{host}:{port}/all'

        self.sync_id = sync_id # 这个神奇的 sync_id，默认值 -1，居然是个字符串
        # 既然这样不如把 sync_id 全改成字符串好了

        # 接收 WebSocket 数据的 Task
        self._receiver_task = None
        # 用于临时保存接收到的数据，以便根据 sync_id 进行同步识别
        self._recv_dict = defaultdict(deque)
        # 本地同步 ID，每次调用 API 递增。
        self._local_sync_id = random.randint(1, 1024) * 1024
        # 事件处理任务管理器
        self._tasks = Tasks()

    @property
    def adapter_info(self):
        return {
            'verify_key': self.verify_key,
            'session': self.session,
            'single_mode': self.single_mode,
            'host': self._host,
            'port': self._port,
            'sync_id': self.sync_id,
        }

    @classmethod
    def via(cls, adapter_interface: AdapterInterface) -> "WebSocketAdapter":
        info = adapter_interface.adapter_info
        adapter = cls(
            verify_key=info['verify_key'],
            **{
                key: info[key]
                for key in ['host', 'port', 'sync_id', 'single_mode']
                if info.get(key) is not None
            }
        )
        adapter.session = info.get('session')
        return adapter

    @_error_handler_async_local
    async def _receiver(self):
        """开始接收 websocket 数据。"""
        if not self.connect:
            raise exceptions.NetworkError(
                f'WebSocket 通道 {self.host_name} 未连接！'
            )
        while self._started:
            try:
                # 数据格式：
                # {
                #   'syncId': '-1',
                #   'data': {
                #       // Event Content
                #   }
                # }
                response = json.loads(await self.connection.recv())
                data = response['data']

                if data.get('code', 0) != 0:
                    raise exceptions.ApiError(data)

                logger.debug(
                    f"[WebSocket] 收到 WebSocket 数据，同步 ID：{response['syncId']}。"
                )
                self._recv_dict[response['syncId']].append(data)
            except KeyError:
                logger.error(f'[WebSocket] 不正确的数据：{response}')
            except websockets.ConnectionClosedOK:
                return
            except websockets.ConnectionClosed as e:
                logger.error(
                    f'[WebSocket] WebSocket 通道意外关闭。code: {e.code}, reason: {e.reason}'
                )
                return

    async def _recv(self, sync_id: str = '-1') -> dict:
        """接收并解析 websocket 数据。"""
        for _ in range(100):
            if self._recv_dict[sync_id]:
                return self._recv_dict[sync_id].popleft()
            else:
                # 如果没有对应同步 ID 的数据，则等待 websocket 数据
                # 目前存在问题：如果 mah 发回的数据不含 sync_id，
                # 这里就会无限循环……
                # 所以还是限制 100 次好了。
                await asyncio.sleep(0.1)
        else:
            raise TimeoutError(
                f'[WebSocket] mirai-api-http 响应超时，可能是由于调用出错。同步 ID：{sync_id}。'
            )

    @_error_handler_async_local
    async def login(self, qq: int):
        headers = {
            'verifyKey': self.verify_key or
                         '', # 关闭认证时，WebSocket 可传入任意 verify_key
            'qq': qq,
        }
        if self.session:
            headers['sessionKey'] = self.session

        self.connection = await websockets.connect(
            self.host_name, extra_headers=headers
        )
        self._receiver_task = asyncio.create_task(self._receiver())

        verify_response = await self._recv('') # 神奇现象：这里的 syncId 是个空字符串
        self.session = verify_response['session']

        self.qq = qq
        logger.info(f'[WebSocket] 成功登录到账号{qq}。')

    @_error_handler_async_local
    async def logout(self, terminate: bool = True):
        if self.connection:
            await self.connection.close()

            await self._receiver_task

            logger.info(f"[WebSocket] 从账号{self.qq}退出。")

    async def poll_event(self):
        """获取并处理事件。"""
        event = await self._recv(self.sync_id)

        self._tasks.create_task(self.emit(event['type'], event))

    async def call_api(self, api: str, method: Method = Method.GET, **params):
        self._local_sync_id += 1 # 使用不同的 sync_id
        sync_id = str(self._local_sync_id)
        content = {
            'syncId': sync_id,
            'command': api.replace('/', '_'),
            'content': params
        }
        if method == Method.RESTGET:
            content['subCommand'] = 'get'
        elif method == Method.RESTPOST:
            content['subCommand'] = 'update'
        elif method == Method.MULTIPART:
            raise NotImplementedError(
                "WebSocket 适配器不支持上传操作。请使用 bot.use_adapter 临时调用 HTTP 适配器。"
            )

        await self.connection.send(json_dumps(content))
        logger.debug(f"[WebSocket] 发送 WebSocket 数据，同步 ID：{sync_id}。")
        try:
            return await self._recv(sync_id)
        except TimeoutError as e:
            logger.debug(e)

    async def _background(self):
        """开始接收事件。"""
        logger.info('[WebSocket] 机器人开始运行。按 Ctrl + C 停止。')

        try:
            while True:
                await self.poll_event()
        finally:
            self._tasks.cancel_all()
