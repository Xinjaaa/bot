import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.openai_compat import request_text

logger = logging.getLogger("reminder-parser")


@dataclass
class ReminderDraft:
    title: str
    message: str
    schedule_type: str
    run_at_ts: float | None
    interval_seconds: int | None
    target_user_id: str
    timezone: str
    source_text: str


class ReminderParseError(Exception):
    pass


class ReminderDefinitionParser:
    def __init__(self) -> None:
        self.timezone = os.getenv("APP_TIMEZONE", "Asia/Shanghai")
        self._client: Any | None = None
        if all(os.getenv(name) for name in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL")):
            try:
                from openai import OpenAI
            except ImportError:
                logger.warning("openai sdk not installed locally, reminder parser model fallback disabled")
            else:
                self._client = OpenAI(
                    base_url=os.getenv("OPENAI_BASE_URL"),
                    api_key=os.getenv("OPENAI_API_KEY"),
                    timeout=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "20")),
                )
        self.model = os.getenv("OPENAI_MODEL", "")

    def parse(self, message: str, requester_user_id: str) -> ReminderDraft | None:
        parsed = self._parse_by_rule(message, requester_user_id)
        if parsed:
            return parsed
        if self._client:
            return self._parse_by_model(message, requester_user_id)
        return None

    def _parse_by_rule(self, message: str, requester_user_id: str) -> ReminderDraft | None:
        text = message.strip()
        interval_match = re.search(
            r"(?:(?P<title>[\u4e00-\u9fa5A-Za-z0-9_-]{0,20}))?每隔(?P<num>\d+)(?P<unit>分钟|小时|天)提醒(?P<target>我|[A-Za-z0-9._-]+)(?P<content>.+)",
            text,
        )
        if interval_match:
            interval_seconds = self._interval_to_seconds(int(interval_match.group("num")), interval_match.group("unit"))
            target = self._resolve_target(interval_match.group("target"), requester_user_id)
            content = interval_match.group("content").strip()
            title = interval_match.group("title").strip() or self._build_title(content)
            return ReminderDraft(
                title=title,
                message=content,
                schedule_type="interval",
                run_at_ts=None,
                interval_seconds=interval_seconds,
                target_user_id=target,
                timezone=self.timezone,
                source_text=text,
            )

        one_time_match = re.search(
            r"(?P<day>今天|明天|后天)(?P<hour>\d{1,2})点(?:(?P<minute>\d{1,2})分?)?提醒(?P<target>我|[A-Za-z0-9._-]+)(?P<content>.+)",
            text,
        )
        if one_time_match:
            target = self._resolve_target(one_time_match.group("target"), requester_user_id)
            content = one_time_match.group("content").strip()
            run_at_ts = self._relative_day_time_to_ts(
                one_time_match.group("day"),
                int(one_time_match.group("hour")),
                int(one_time_match.group("minute") or "0"),
            )
            return ReminderDraft(
                title=self._build_title(content),
                message=content,
                schedule_type="once",
                run_at_ts=run_at_ts,
                interval_seconds=None,
                target_user_id=target,
                timezone=self.timezone,
                source_text=text,
            )
        return None

    def _parse_by_model(self, message: str, requester_user_id: str) -> ReminderDraft | None:
        now = datetime.now(ZoneInfo(self.timezone)).strftime("%Y-%m-%d %H:%M:%S %Z")
        instructions = (
            "你是一个提醒定义解析器。"
            "从用户消息里提取一个定时提醒定义。"
            "只输出 JSON，不要输出 markdown。"
            "如果不是明确的提醒/定时任务请求，输出 null。"
            "字段必须是: title, message, schedule_type, run_at_iso, interval_seconds, target_user_id."
            "schedule_type 只能是 once 或 interval。"
            "target_user_id 如果是提醒我，就使用请求者 user id。"
        )
        payload = (
            f"当前时间: {now}\n"
            f"请求者 user_id: {requester_user_id}\n"
            f"用户消息: {message}\n"
        )
        try:
            text = request_text(
                self._client,
                model=self.model,
                instructions=instructions,
                input_text=payload,
            )
        except Exception as exc:
            logger.exception("reminder parser model request failed")
            raise ReminderParseError(str(exc)) from exc
        text = text.strip()
        if not text or text == "null":
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ReminderParseError(f"invalid reminder parser json: {text}") from exc

        schedule_type = data.get("schedule_type")
        run_at_iso = data.get("run_at_iso")
        run_at_ts = datetime.fromisoformat(run_at_iso).timestamp() if run_at_iso else None
        target_user_id = data.get("target_user_id") or requester_user_id
        if target_user_id == "我":
            target_user_id = requester_user_id
        if schedule_type not in {"once", "interval"}:
            return None
        return ReminderDraft(
            title=(data.get("title") or self._build_title(data.get("message") or message)).strip(),
            message=(data.get("message") or message).strip(),
            schedule_type=schedule_type,
            run_at_ts=run_at_ts,
            interval_seconds=data.get("interval_seconds"),
            target_user_id=target_user_id,
            timezone=self.timezone,
            source_text=message.strip(),
        )

    @staticmethod
    def _build_title(content: str) -> str:
        text = content.strip()
        return text[:20] if text else "提醒事项"

    @staticmethod
    def _resolve_target(target: str, requester_user_id: str) -> str:
        return requester_user_id if target == "我" else target.strip()

    @staticmethod
    def _interval_to_seconds(number: int, unit: str) -> int:
        mapping = {"分钟": 60, "小时": 3600, "天": 86400}
        return number * mapping[unit]

    def _relative_day_time_to_ts(self, day_word: str, hour: int, minute: int) -> float:
        tz = ZoneInfo(self.timezone)
        now = datetime.now(tz)
        offset = {"今天": 0, "明天": 1, "后天": 2}[day_word]
        target = (now + timedelta(days=offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target.timestamp() <= time.time():
            target = target + timedelta(days=1)
        return target.timestamp()
