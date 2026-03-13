import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


logger = logging.getLogger("definition-manager")


@dataclass
class ReminderDefinition:
    definition_id: str
    creator_user_id: str
    target_user_id: str
    title: str
    message: str
    schedule_type: str
    run_at_ts: float | None
    interval_seconds: int | None
    next_run_ts: float
    timezone: str
    source_text: str
    status: str
    created_at_ts: float
    updated_at_ts: float
    last_run_ts: float | None = None


class DefinitionManager:
    def __init__(
        self,
        notify_fn: Callable[[str, str], None],
        db_path: str | None = None,
        poll_interval_seconds: int | None = None,
    ) -> None:
        self.notify_fn = notify_fn
        self.db_path = db_path or os.getenv("DEFINITION_DB_PATH", "/app/data/definitions.db")
        self.poll_interval_seconds = poll_interval_seconds or int(os.getenv("DEFINITION_POLL_INTERVAL_SECONDS", "10"))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        logger.info(
            "definition manager initialized db_path=%s poll_interval_seconds=%s",
            self.db_path,
            self.poll_interval_seconds,
        )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="definition-manager", daemon=True)
        self._thread.start()
        logger.info("definition manager started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        logger.info("definition manager stopped")

    def create_definition(
        self,
        *,
        creator_user_id: str,
        target_user_id: str,
        title: str,
        message: str,
        schedule_type: str,
        run_at_ts: float | None,
        interval_seconds: int | None,
        timezone: str,
        source_text: str,
    ) -> ReminderDefinition:
        now = time.time()
        definition = ReminderDefinition(
            definition_id=str(uuid.uuid4()),
            creator_user_id=creator_user_id,
            target_user_id=target_user_id,
            title=title,
            message=message,
            schedule_type=schedule_type,
            run_at_ts=run_at_ts,
            interval_seconds=interval_seconds,
            next_run_ts=run_at_ts if schedule_type == "once" and run_at_ts else now + (interval_seconds or 0),
            timezone=timezone,
            source_text=source_text,
            status="active",
            created_at_ts=now,
            updated_at_ts=now,
        )
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO reminder_definitions (
                    definition_id, creator_user_id, target_user_id, title, message,
                    schedule_type, run_at_ts, interval_seconds, next_run_ts, timezone,
                    source_text, status, created_at_ts, updated_at_ts, last_run_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    definition.definition_id,
                    definition.creator_user_id,
                    definition.target_user_id,
                    definition.title,
                    definition.message,
                    definition.schedule_type,
                    definition.run_at_ts,
                    definition.interval_seconds,
                    definition.next_run_ts,
                    definition.timezone,
                    definition.source_text,
                    definition.status,
                    definition.created_at_ts,
                    definition.updated_at_ts,
                    definition.last_run_ts,
                ),
            )
            conn.commit()
        logger.info(
            "definition created definition_id=%s schedule_type=%s target_user_id=%s title=%r next_run_ts=%s",
            definition.definition_id,
            definition.schedule_type,
            definition.target_user_id,
            definition.title,
            definition.next_run_ts,
        )
        return definition

    def list_active_definitions(self, creator_user_id: str) -> list[ReminderDefinition]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM reminder_definitions
                WHERE creator_user_id = ? AND status = 'active'
                ORDER BY next_run_ts ASC
                """,
                (creator_user_id,),
            ).fetchall()
        return [self._row_to_definition(row) for row in rows]

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._dispatch_due_definitions()
            except Exception:
                logger.exception("definition dispatch loop failed")
            self._stop_event.wait(self.poll_interval_seconds)

    def _dispatch_due_definitions(self) -> None:
        now = time.time()
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM reminder_definitions
                WHERE status = 'active' AND next_run_ts <= ?
                ORDER BY next_run_ts ASC
                LIMIT 20
                """,
                (now,),
            ).fetchall()
        for row in rows:
            definition = self._row_to_definition(row)
            notification = self._build_notification(definition)
            try:
                self.notify_fn(definition.target_user_id, notification)
                logger.info(
                    "definition dispatched definition_id=%s target_user_id=%s schedule_type=%s",
                    definition.definition_id,
                    definition.target_user_id,
                    definition.schedule_type,
                )
            except Exception:
                logger.exception("definition notification failed definition_id=%s", definition.definition_id)
                continue
            self._mark_dispatched(definition)

    def _mark_dispatched(self, definition: ReminderDefinition) -> None:
        now = time.time()
        if definition.schedule_type == "interval" and definition.interval_seconds:
            next_run_ts = max(definition.next_run_ts + definition.interval_seconds, now + definition.interval_seconds)
            status = "active"
        else:
            next_run_ts = definition.next_run_ts
            status = "completed"
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE reminder_definitions
                SET status = ?, next_run_ts = ?, last_run_ts = ?, updated_at_ts = ?
                WHERE definition_id = ?
                """,
                (status, next_run_ts, now, now, definition.definition_id),
            )
            conn.commit()

    def _build_notification(self, definition: ReminderDefinition) -> str:
        prefix = f"提醒任务：{definition.title}"
        if definition.schedule_type == "interval":
            schedule_text = f"每隔 {definition.interval_seconds // 60} 分钟"
        else:
            schedule_text = "单次提醒"
        return f"{prefix}\n类型：{schedule_text}\n内容：{definition.message}"

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reminder_definitions (
                    definition_id TEXT PRIMARY KEY,
                    creator_user_id TEXT NOT NULL,
                    target_user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    schedule_type TEXT NOT NULL,
                    run_at_ts REAL,
                    interval_seconds INTEGER,
                    next_run_ts REAL NOT NULL,
                    timezone TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at_ts REAL NOT NULL,
                    updated_at_ts REAL NOT NULL,
                    last_run_ts REAL
                )
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row_to_definition(row: sqlite3.Row) -> ReminderDefinition:
        return ReminderDefinition(
            definition_id=row["definition_id"],
            creator_user_id=row["creator_user_id"],
            target_user_id=row["target_user_id"],
            title=row["title"],
            message=row["message"],
            schedule_type=row["schedule_type"],
            run_at_ts=row["run_at_ts"],
            interval_seconds=row["interval_seconds"],
            next_run_ts=row["next_run_ts"],
            timezone=row["timezone"],
            source_text=row["source_text"],
            status=row["status"],
            created_at_ts=row["created_at_ts"],
            updated_at_ts=row["updated_at_ts"],
            last_run_ts=row["last_run_ts"],
        )
