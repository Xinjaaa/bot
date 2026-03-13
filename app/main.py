import logging
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response

from app.agent import AgentError, OpenAIAgent, is_agent_configured
from app.crypto import WeComCrypto, WeComCryptoError
from app.dedupe import TTLMessageDeduper
from app.definition_manager import DefinitionManager, ReminderDefinition
from app.image_analyzer import ImageAnalyzer, ImageAnalyzerError, is_image_analyzer_configured
from app.identity import UserIdentityStore
from app.memory import InMemoryConversationStore
from app.reminder_parser import ReminderDefinitionParser
from app.skill_router import SkillRouter
from app.weather_skill import WeatherSkill, WeatherSkillError, is_weather_skill_configured
from app.wecom_api import WeComAPIClient, WeComAPIError, is_wecom_api_configured


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("wecom-callback")


def get_required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value


TOKEN = get_required_env("WECOM_TOKEN")
ENCODING_AES_KEY = get_required_env("WECOM_ENCODING_AES_KEY")
CORP_ID = get_required_env("WECOM_CORP_ID")

crypto = WeComCrypto(
    token=TOKEN,
    encoding_aes_key=ENCODING_AES_KEY,
    receive_id=CORP_ID,
)
agent: OpenAIAgent | None = OpenAIAgent() if is_agent_configured() else None
wecom_api: WeComAPIClient | None = WeComAPIClient() if is_wecom_api_configured() else None
message_deduper = TTLMessageDeduper(ttl_seconds=int(os.getenv("MESSAGE_DEDUPE_TTL_SECONDS", "600")))
memory_enabled = os.getenv("MEMORY_ENABLED", "true").lower() == "true"
conversation_store = InMemoryConversationStore(
    max_turns=int(os.getenv("MEMORY_MAX_TURNS", "6")),
    ttl_seconds=int(os.getenv("MEMORY_TTL_SECONDS", "1800")),
)
identity_store = UserIdentityStore()
weather_skill: WeatherSkill | None = WeatherSkill() if is_weather_skill_configured() else None
image_analyzer: ImageAnalyzer | None = ImageAnalyzer() if is_image_analyzer_configured() else None
reminder_parser = ReminderDefinitionParser()
definition_manager: DefinitionManager | None = None
skill_router = SkillRouter()

app = FastAPI(title="WeCom Callback Service")

logger.info(
    "service startup corp_id=%s agent_configured=%s wecom_api_configured=%s weather_skill_configured=%s image_analyzer_configured=%s skill_router_skills=%s memory_enabled=%s memory_max_turns=%s memory_ttl_seconds=%s identity_dir=%s definition_db_path=%s log_level=%s dedupe_ttl_seconds=%s",
    CORP_ID,
    bool(agent),
    bool(wecom_api),
    bool(weather_skill),
    bool(image_analyzer),
    [skill.name for skill in skill_router.skills],
    memory_enabled,
    os.getenv("MEMORY_MAX_TURNS", "6"),
    os.getenv("MEMORY_TTL_SECONDS", "1800"),
    os.getenv("IDENTITY_DIR", "/app/identities"),
    os.getenv("DEFINITION_DB_PATH", "/app/data/definitions.db"),
    os.getenv("LOG_LEVEL", "INFO"),
    os.getenv("MESSAGE_DEDUPE_TTL_SECONDS", "600"),
)


def build_text_reply(to_user: str, from_user: str, content: str) -> str:
    safe_content = content[:2048]
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{safe_content}]]></Content>"
        "</xml>"
    )


def send_text_to_user(user_id: str, content: str) -> None:
    if wecom_api is None:
        raise WeComAPIError("wecom api is not configured")
    wecom_api.send_text_message(user_id, content)


def format_definition_confirmation(definition: ReminderDefinition) -> str:
    if definition.schedule_type == "interval":
        schedule_text = f"每隔 {definition.interval_seconds // 60} 分钟"
    else:
        dt = datetime.fromtimestamp(definition.next_run_ts, ZoneInfo(definition.timezone))
        schedule_text = dt.strftime("%Y-%m-%d %H:%M")
    return (
        f"已创建提醒任务\n"
        f"ID: {definition.definition_id[:8]}\n"
        f"标题: {definition.title}\n"
        f"目标用户: {definition.target_user_id}\n"
        f"时间: {schedule_text}\n"
        f"内容: {definition.message}"
    )


def process_text_message_async(*, from_user: str, msg_id: str, user_message: str) -> None:
    logger.info(
        "async processing start msg_id=%s from_user=%s user_message_len=%s",
        msg_id,
        from_user,
        len(user_message),
    )
    selected_skill = skill_router.select_skill(user_message)
    logger.info("async skill routing msg_id=%s from_user=%s selected_skill=%s", msg_id, from_user, selected_skill)
    if user_message.strip() in {"/reset", "重置", "清空记忆", "清除记忆"}:
        conversation_store.clear(from_user)
        logger.info("conversation memory cleared from_user=%s msg_id=%s", from_user, msg_id)
        agent_reply = "已清空当前会话记忆。"
        try:
            if wecom_api is None:
                raise WeComAPIError("wecom api is not configured")
            result = wecom_api.send_text_message(from_user, agent_reply)
            logger.info("async wecom send success msg_id=%s from_user=%s result=%s", msg_id, from_user, result)
        except WeComAPIError as exc:
            logger.exception("async wecom send failed msg_id=%s from_user=%s error=%s", msg_id, from_user, exc)
        return

    if definition_manager and selected_skill == "reminder-definition":
        try:
            reminder_draft = reminder_parser.parse(user_message, from_user)
        except Exception as exc:
            logger.exception("reminder parse failed msg_id=%s from_user=%s", msg_id, from_user)
            reminder_draft = None
        if reminder_draft:
            definition = definition_manager.create_definition(
                creator_user_id=from_user,
                target_user_id=reminder_draft.target_user_id,
                title=reminder_draft.title,
                message=reminder_draft.message,
                schedule_type=reminder_draft.schedule_type,
                run_at_ts=reminder_draft.run_at_ts,
                interval_seconds=reminder_draft.interval_seconds,
                timezone=reminder_draft.timezone,
                source_text=reminder_draft.source_text,
            )
            try:
                send_text_to_user(from_user, format_definition_confirmation(definition))
                logger.info("reminder definition handled msg_id=%s from_user=%s definition_id=%s", msg_id, from_user, definition.definition_id)
            except WeComAPIError as exc:
                logger.exception("reminder confirmation send failed msg_id=%s from_user=%s error=%s", msg_id, from_user, exc)
            return

    if weather_skill and selected_skill == "weather-zh":
        try:
            agent_reply = weather_skill.query(user_message)
            logger.info("weather skill handled msg_id=%s from_user=%s", msg_id, from_user)
        except WeatherSkillError as exc:
            logger.warning("weather skill failed msg_id=%s from_user=%s error=%s", msg_id, from_user, exc)
            agent_reply = "天气查询暂时不可用，请稍后再试。"
        try:
            if wecom_api is None:
                raise WeComAPIError("wecom api is not configured")
            result = wecom_api.send_text_message(from_user, agent_reply)
            logger.info("async wecom send success msg_id=%s from_user=%s result=%s", msg_id, from_user, result)
        except WeComAPIError as exc:
            logger.exception("async wecom send failed msg_id=%s from_user=%s error=%s", msg_id, from_user, exc)
        return

    history = conversation_store.get_turns(from_user) if memory_enabled else []
    identity_path = identity_store.ensure_file(from_user)
    updated_facts = identity_store.update_from_message(from_user, user_message)
    identity_markdown = identity_store.load_markdown(from_user)
    logger.info(
        "loaded conversation memory from_user=%s msg_id=%s history_turns=%s identity_file=%s updated_identity_facts=%s",
        from_user,
        msg_id,
        len(history),
        identity_path,
        [f"{fact.label}={fact.value}" for fact in updated_facts],
    )
    try:
        if agent is None:
            raise AgentError("agent is not configured")
        agent_reply = agent.reply(
            user_message=user_message,
            user_id=from_user,
            history=history,
            identity_markdown=identity_markdown,
        )
        logger.info(
            "async agent reply ready msg_id=%s from_user=%s reply_len=%s reply_preview=%r",
            msg_id,
            from_user,
            len(agent_reply),
            agent_reply[:300],
        )
        if memory_enabled:
            conversation_store.append_turn(from_user, "user", user_message)
            conversation_store.append_turn(from_user, "assistant", agent_reply)
            logger.info(
                "conversation memory updated from_user=%s msg_id=%s stored_turns=%s",
                from_user,
                msg_id,
                len(conversation_store.get_turns(from_user)),
            )
    except AgentError as exc:
        logger.warning("async agent failed msg_id=%s from_user=%s error=%s", msg_id, from_user, exc)
        agent_reply = "暂时无法处理你的消息，请稍后再试。"

    try:
        if wecom_api is None:
            raise WeComAPIError("wecom api is not configured")
        result = wecom_api.send_text_message(from_user, agent_reply)
        logger.info("async wecom send success msg_id=%s from_user=%s result=%s", msg_id, from_user, result)
    except WeComAPIError as exc:
        logger.exception("async wecom send failed msg_id=%s from_user=%s error=%s", msg_id, from_user, exc)


def process_image_message_async(*, from_user: str, msg_id: str, pic_url: str) -> None:
    logger.info(
        "async image processing start msg_id=%s from_user=%s pic_url_preview=%r",
        msg_id,
        from_user,
        pic_url[:200],
    )
    try:
        if image_analyzer is None:
            raise ImageAnalyzerError("image analyzer is not configured")
        agent_reply = image_analyzer.describe(pic_url)
        logger.info(
            "image analysis ready msg_id=%s from_user=%s reply_len=%s reply_preview=%r",
            msg_id,
            from_user,
            len(agent_reply),
            agent_reply[:300],
        )
    except ImageAnalyzerError as exc:
        logger.warning("image analysis failed msg_id=%s from_user=%s error=%s", msg_id, from_user, exc)
        agent_reply = "这张图片我暂时没识别出来，你可以稍后再发一次试试。"

    try:
        if wecom_api is None:
            raise WeComAPIError("wecom api is not configured")
        result = wecom_api.send_text_message(from_user, agent_reply)
        logger.info("async image reply send success msg_id=%s from_user=%s result=%s", msg_id, from_user, result)
    except WeComAPIError as exc:
        logger.exception("async image reply send failed msg_id=%s from_user=%s error=%s", msg_id, from_user, exc)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {
        "status": "ok",
        "agent_configured": "true" if agent else "false",
        "wecom_api_configured": "true" if wecom_api else "false",
        "definition_manager_configured": "true" if definition_manager else "false",
        "weather_skill_configured": "true" if weather_skill else "false",
        "image_analyzer_configured": "true" if image_analyzer else "false",
        "memory_enabled": "true" if memory_enabled else "false",
        "identity_dir": os.getenv("IDENTITY_DIR", "/app/identities"),
    }


@app.on_event("startup")
def on_startup() -> None:
    global definition_manager
    if wecom_api is None:
        logger.warning("skip definition manager startup because wecom api is not configured")
        return
    definition_manager = DefinitionManager(notify_fn=send_text_to_user)
    definition_manager.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    if definition_manager:
        definition_manager.stop()


@app.get("/wecom/callback")
def verify_callback(
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
) -> Response:
    logger.info(
        "verify callback request timestamp=%s nonce=%s echostr_len=%s signature_prefix=%s",
        timestamp,
        nonce,
        len(echostr),
        msg_signature[:8],
    )
    try:
        plaintext = crypto.decrypt(msg_signature, timestamp, nonce, echostr)
    except WeComCryptoError as exc:
        logger.warning("failed to verify callback URL: %s", exc)
        raise HTTPException(status_code=401, detail="invalid callback signature") from exc
    logger.info("verify callback success plaintext_len=%s plaintext_preview=%r", len(plaintext), plaintext[:200])
    return Response(content=plaintext, media_type="text/plain")


@app.post("/wecom/callback")
async def receive_callback(
    request: Request,
    background_tasks: BackgroundTasks,
    msg_signature: str = Query(...),
    timestamp: str = Query(...),
    nonce: str = Query(...),
) -> Response:
    body = await request.body()
    logger.info(
        "receive callback request client=%s query_timestamp=%s nonce=%s body_len=%s body_preview=%r",
        request.client.host if request.client else "unknown",
        timestamp,
        nonce,
        len(body),
        body[:300].decode("utf-8", errors="ignore"),
    )
    try:
        root = ET.fromstring(body)
        encrypted = root.findtext("Encrypt")
        if not encrypted:
            raise WeComCryptoError("missing Encrypt field")
        logger.info("callback encrypted payload len=%s preview=%r", len(encrypted), encrypted[:120])
        plaintext = crypto.decrypt(msg_signature, timestamp, nonce, encrypted)
        logger.info("callback decrypted plaintext_len=%s plaintext_preview=%r", len(plaintext), plaintext[:500])
        event_root = ET.fromstring(plaintext)
    except (ET.ParseError, WeComCryptoError) as exc:
        logger.exception("failed to process callback")
        raise HTTPException(status_code=400, detail="invalid callback payload") from exc

    event_type = event_root.findtext("Event") or "message"
    msg_type = event_root.findtext("MsgType") or "unknown"
    from_user = event_root.findtext("FromUserName") or ""
    to_user = event_root.findtext("ToUserName") or ""
    agent_id = event_root.findtext("AgentID") or ""
    msg_id = event_root.findtext("MsgId") or ""
    create_time = event_root.findtext("CreateTime") or ""
    logger.info(
        "parsed callback event_type=%s msg_type=%s from=%s to=%s agent_id=%s msg_id=%s create_time=%s",
        event_type,
        msg_type,
        from_user,
        to_user,
        agent_id,
        msg_id,
        create_time,
    )

    if msg_type == "image":
        pic_url = (event_root.findtext("PicUrl") or "").strip()
        if not pic_url:
            logger.info("skip image callback without PicUrl from=%s", from_user)
            return Response(content="success", media_type="text/plain")
        logger.info("image callback pic_url_preview=%r", pic_url[:300])
        dedupe_key = msg_id or f"{from_user}:{create_time}:{pic_url}"
        if message_deduper.seen(dedupe_key):
            logger.info("skip duplicated image callback dedupe_key=%s", dedupe_key)
            return Response(content="success", media_type="text/plain")
        background_tasks.add_task(
            process_image_message_async,
            from_user=from_user,
            msg_id=msg_id or "unknown",
            pic_url=pic_url,
        )
        logger.info("scheduled image processing msg_id=%s from_user=%s", msg_id, from_user)
        return Response(content="success", media_type="text/plain")

    if msg_type != "text":
        logger.info("skip unsupported callback msg_type=%s event_type=%s", msg_type, event_type)
        return Response(content="success", media_type="text/plain")

    user_message = (event_root.findtext("Content") or "").strip()
    if not user_message:
        logger.info("skip empty text callback from=%s", from_user)
        return Response(content="success", media_type="text/plain")
    logger.info("text callback content_len=%s content_preview=%r", len(user_message), user_message[:300])

    dedupe_key = msg_id or f"{from_user}:{create_time}:{user_message}"
    if message_deduper.seen(dedupe_key):
        logger.info("skip duplicated callback dedupe_key=%s", dedupe_key)
        return Response(content="success", media_type="text/plain")

    background_tasks.add_task(
        process_text_message_async,
        from_user=from_user,
        msg_id=msg_id or "unknown",
        user_message=user_message,
    )
    logger.info("scheduled async processing msg_id=%s from_user=%s", msg_id, from_user)
    return Response(content="success", media_type="text/plain")


@app.get("/")
def index() -> dict[str, str]:
    return {
        "service": "wecom-callback",
        "callback_path": "/wecom/callback",
        "timestamp": str(int(time.time())),
    }
