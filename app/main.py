import logging
import os
import time
import xml.etree.ElementTree as ET

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request, Response

from app.agent import AgentError, OpenAIAgent, is_agent_configured
from app.crypto import WeComCrypto, WeComCryptoError
from app.dedupe import TTLMessageDeduper
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

app = FastAPI(title="WeCom Callback Service")

logger.info(
    "service startup corp_id=%s agent_configured=%s wecom_api_configured=%s log_level=%s dedupe_ttl_seconds=%s",
    CORP_ID,
    bool(agent),
    bool(wecom_api),
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


def process_text_message_async(*, from_user: str, msg_id: str, user_message: str) -> None:
    logger.info(
        "async processing start msg_id=%s from_user=%s user_message_len=%s",
        msg_id,
        from_user,
        len(user_message),
    )
    try:
        if agent is None:
            raise AgentError("agent is not configured")
        agent_reply = agent.reply(user_message=user_message, user_id=from_user)
        logger.info(
            "async agent reply ready msg_id=%s from_user=%s reply_len=%s reply_preview=%r",
            msg_id,
            from_user,
            len(agent_reply),
            agent_reply[:300],
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


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {
        "status": "ok",
        "agent_configured": "true" if agent else "false",
        "wecom_api_configured": "true" if wecom_api else "false",
    }


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

    if msg_type != "text":
        logger.info("skip non-text callback msg_type=%s event_type=%s", msg_type, event_type)
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
