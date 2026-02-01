from __future__ import annotations

import asyncio
import datetime
import os
import tempfile
import time
from typing import Any, Dict, List, Tuple, Type

import pyautogui
from peewee import BooleanField, FloatField, TextField

from src.common.database.database import db
from src.common.database.database_model import BaseModel
from src.common.logger import get_logger
from src.plugin_system import (
    BaseCommand,
    BaseEventHandler,
    BasePlugin,
    CommandInfo,
    ConfigField,
    ComponentInfo,
    EventType,
    MaiMessages,
    register_plugin,
)
from src.plugin_system.apis import database_api, send_api

logger = get_logger("desktop_companion")

# =========================
# 共享状态：最近活跃 stream_id
# =========================
STREAM_STATE: Dict[str, str] = {"last_stream_id": ""}


class DesktopEvent(BaseModel):
    """桌面日程表"""

    stream_id = TextField(index=True)
    event_time = TextField()  # YYYY-MM-DD HH:MM
    event_ts = FloatField(index=True)
    content = TextField()
    reminded = BooleanField(default=False)

    class Meta:
        table_name = "desktop_events"


class DesktopStreamTracker(BaseEventHandler):
    """记录最近的聊天流 ID，用于系统提醒/截图推送"""

    event_type = EventType.ON_MESSAGE
    handler_name = "desktop_stream_tracker"
    handler_description = "记录最近的 stream_id"

    async def execute(
        self, message: MaiMessages | None
    ) -> Tuple[bool, bool, str | None, None, None]:
        if message and message.stream_id:
            STREAM_STATE["last_stream_id"] = message.stream_id
        return True, True, None, None, None


class AddEventCommand(BaseCommand):
    """添加日程命令"""

    command_name = "add_event"
    command_description = "添加日程"
    command_pattern = (
        r"^/add_event\s+(?P<event_time>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s+(?P<content>.+)$"
    )

    async def execute(self) -> Tuple[bool, str, int]:
        try:
            event_time = self.matched_groups.get("event_time", "").strip()
            content = self.matched_groups.get("content", "").strip()
            if not event_time or not content:
                await self.send_text("格式错误：/add_event YYYY-MM-DD HH:MM 内容")
                return False, "参数错误", 1

            try:
                dt = datetime.datetime.strptime(event_time, "%Y-%m-%d %H:%M")
                event_ts = dt.timestamp()
            except Exception:
                await self.send_text("时间格式错误，请使用 YYYY-MM-DD HH:MM")
                return False, "时间格式错误", 1

            stream_id = self.message.chat_stream.stream_id
            STREAM_STATE["last_stream_id"] = stream_id

            await database_api.db_query(
                DesktopEvent,
                query_type="create",
                data={
                    "stream_id": stream_id,
                    "event_time": event_time,
                    "event_ts": event_ts,
                    "content": content,
                    "reminded": False,
                },
            )

            await self.send_text(f"已添加日程：{event_time} {content}")
            return True, "添加成功", 1
        except Exception as e:
            logger.error(f"添加日程失败: {e}")
            await self.send_text("添加日程失败，请稍后重试")
            return False, "添加失败", 1


class ListEventsCommand(BaseCommand):
    """列出日程命令"""

    command_name = "list_events"
    command_description = "列出当前聊天流的日程"
    command_pattern = r"^/list_events$"

    async def execute(self) -> Tuple[bool, str, int]:
        try:
            stream_id = self.message.chat_stream.stream_id
            STREAM_STATE["last_stream_id"] = stream_id

            rows = await database_api.db_query(
                DesktopEvent,
                query_type="get",
                filters={"stream_id": stream_id},
                order_by=["event_ts"],
            )
            if not rows:
                await self.send_text("当前没有日程。")
                return True, "无日程", 1

            lines = ["当前日程如下："]
            for row in rows:
                flag = "✅" if row.get("reminded") else "⏳"
                lines.append(f"{flag} {row.get('event_time')} - {row.get('content')}")
            await self.send_text("\n".join(lines))
            return True, "列出日程", 1
        except Exception as e:
            logger.error(f"列出日程失败: {e}")
            await self.send_text("列出日程失败，请稍后重试")
            return False, "列出失败", 1


@register_plugin
class DesktopCompanionPlugin(BasePlugin):
    """桌面伴侣插件"""

    plugin_name = "desktop_companion_plugin"
    enable_plugin = True
    dependencies: List[str] = []
    python_dependencies: List[str] = ["pyautogui"]
    config_file_name = "config.toml"

    config_section_descriptions = {
        "plugin": "插件基础配置",
        "schedule": "日程与提醒配置",
        "screenshot": "截图配置",
        "target": "推送目标配置",
    }

    config_schema = {
        "plugin": {
            "enabled": ConfigField(
                type=bool, default=True, description="是否启用插件"
            ),
        },
        "schedule": {
            "event_check_seconds": ConfigField(
                type=int, default=10, description="日程检查间隔(秒)"
            ),
        },
        "screenshot": {
            "enabled": ConfigField(
                type=bool, default=True, description="是否启用定时截图"
            ),
            "interval_minutes": ConfigField(
                type=int, default=30, description="截图间隔(分钟)"
            ),
            "cleanup_file": ConfigField(
                type=bool, default=True, description="截图后是否删除临时文件"
            ),
        },
        "target": {
            "default_stream_id": ConfigField(
                type=str, default="", description="默认推送 stream_id"
            ),
        },
    }

    def __init__(self, plugin_dir: str, **kwargs: Any):
        super().__init__(plugin_dir, **kwargs)
        self._tasks: List[asyncio.Task] = []
        self._last_morning_date: str = ""
        self._last_night_date: str = ""

        # 确保表存在
        try:
            db.connect(reuse_if_open=True)
            db.create_tables([DesktopEvent], safe=True)
        except Exception as e:
            logger.error(f"初始化桌面日程表失败: {e}")

        # 启动后台任务
        self._tasks.append(asyncio.create_task(self._event_reminder_loop()))
        self._tasks.append(asyncio.create_task(self._daily_reminder_loop()))
        if self.get_config("screenshot.enabled", True):
            self._tasks.append(asyncio.create_task(self._screenshot_loop()))

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        if not self.get_config("plugin.enabled", True):
            return []
        return [
            (AddEventCommand.get_command_info(), AddEventCommand),
            (ListEventsCommand.get_command_info(), ListEventsCommand),
            (DesktopStreamTracker.get_event_handler_info(), DesktopStreamTracker),
        ]

    def _get_target_stream_id(self) -> str:
        return STREAM_STATE.get("last_stream_id") or self.get_config(
            "target.default_stream_id", ""
        )

    async def _event_reminder_loop(self) -> None:
        """每 10 秒检查一次日程提醒。"""
        interval = int(self.get_config("schedule.event_check_seconds", 10))
        while True:
            try:
                now = time.time()
                rows = await database_api.db_query(
                    DesktopEvent,
                    query_type="get",
                    filters={"reminded": False},
                    order_by=["event_ts"],
                )
                for row in rows or []:
                    if float(row.get("event_ts", 0)) <= now:
                        stream_id = row.get("stream_id") or self._get_target_stream_id()
                        if stream_id:
                            await send_api.text_to_stream(
                                text=f"⏰ 日程提醒：{row.get('event_time')} {row.get('content')}",
                                stream_id=stream_id,
                            )
                        await database_api.db_query(
                            DesktopEvent,
                            query_type="update",
                            data={"reminded": True},
                            filters={"id": row.get("id")},
                        )
            except Exception as e:
                logger.error(f"日程检查异常: {e}")
            await asyncio.sleep(interval)

    async def _daily_reminder_loop(self) -> None:
        """每天 09:00/23:00 提醒。"""
        while True:
            try:
                now = datetime.datetime.now()
                today = now.strftime("%Y-%m-%d")
                stream_id = self._get_target_stream_id()

                if now.hour == 9 and now.minute == 0 and self._last_morning_date != today:
                    if stream_id:
                        await send_api.text_to_stream(
                            "早安，开始工作了吗？", stream_id=stream_id
                        )
                    self._last_morning_date = today

                if now.hour == 23 and now.minute == 0 and self._last_night_date != today:
                    if stream_id:
                        await send_api.text_to_stream(
                            "夜深了，早点休息。", stream_id=stream_id
                        )
                    self._last_night_date = today
            except Exception as e:
                logger.error(f"定时提醒异常: {e}")
            await asyncio.sleep(30)

    async def _screenshot_loop(self) -> None:
        """每 30 分钟截图一次，发送摘要文本。"""
        interval_minutes = int(self.get_config("screenshot.interval_minutes", 30))
        cleanup_file = bool(self.get_config("screenshot.cleanup_file", True))

        while True:
            try:
                stream_id = self._get_target_stream_id()
                if stream_id:
                    temp_dir = os.path.join(self.plugin_dir, "screenshots")
                    os.makedirs(temp_dir, exist_ok=True)
                    with tempfile.NamedTemporaryFile(
                        suffix=".png", delete=False, dir=temp_dir
                    ) as tmp:
                        path = tmp.name

                    pyautogui.screenshot(path)

                    await send_api.text_to_stream(
                        text="[系统] 已完成 30 分钟定时截屏，已保存至本地。",
                        stream_id=stream_id,
                    )

                    if cleanup_file:
                        try:
                            os.remove(path)
                        except Exception:
                            pass
            except Exception as e:
                logger.error(f"定时截图异常: {e}")
            await asyncio.sleep(interval_minutes * 60)
