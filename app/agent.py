import logging
import os
from pathlib import Path

from openai import OpenAI


logger = logging.getLogger("wecom-agent")


DEFAULT_SYSTEM_PROMPT = (
    "你是一个企业微信里的智能助理。"
    "请使用简洁、直接、专业的中文回复用户。"
    "如果用户的问题不明确，优先给出最有帮助的下一步。"
)
DEFAULT_SYSTEM_PROMPT_PATH = Path(
    os.getenv("OPENAI_SYSTEM_PROMPT_FILE", "/app/prompts/system_prompt.md")
)


class AgentError(Exception):
    pass


class OpenAIAgent:
    def __init__(self) -> None:
        base_url = self._get_required_env("OPENAI_BASE_URL")
        api_key = self._get_required_env("OPENAI_API_KEY")
        self.model = self._get_required_env("OPENAI_MODEL")
        self.system_prompt = self._load_system_prompt()
        self.timeout = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "20"))
        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=self.timeout,
        )
        logger.info(
            "agent initialized base_url=%s model=%s timeout=%s system_prompt_len=%s prompt_file=%s",
            base_url,
            self.model,
            self.timeout,
            len(self.system_prompt),
            os.getenv("OPENAI_SYSTEM_PROMPT_FILE", str(DEFAULT_SYSTEM_PROMPT_PATH)),
        )

    @staticmethod
    def _get_required_env(name: str) -> str:
        value = os.getenv(name)
        if not value:
            raise RuntimeError(f"missing required environment variable: {name}")
        return value

    @staticmethod
    def _load_system_prompt() -> str:
        inline_prompt = os.getenv("OPENAI_SYSTEM_PROMPT")
        if inline_prompt:
            logger.info("load system prompt from env OPENAI_SYSTEM_PROMPT")
            return inline_prompt

        prompt_path = Path(os.getenv("OPENAI_SYSTEM_PROMPT_FILE", str(DEFAULT_SYSTEM_PROMPT_PATH)))
        if prompt_path.exists():
            prompt = prompt_path.read_text(encoding="utf-8").strip()
            if prompt:
                logger.info("load system prompt from file path=%s", prompt_path)
                return prompt
            logger.warning("system prompt file is empty path=%s, fallback to default prompt", prompt_path)
        else:
            logger.warning("system prompt file not found path=%s, fallback to default prompt", prompt_path)
        return DEFAULT_SYSTEM_PROMPT

    def reply(self, user_message: str, user_id: str | None = None) -> str:
        logger.info(
            "agent request start user_id=%s model=%s user_message_len=%s user_message_preview=%r",
            user_id or "unknown",
            self.model,
            len(user_message),
            user_message[:200],
        )
        try:
            response = self.client.responses.create(
                model=self.model,
                instructions=self.system_prompt,
                input=user_message,
            )
        except Exception as exc:
            logger.exception("agent request failed")
            raise AgentError(str(exc)) from exc

        text = (response.output_text or "").strip()
        if not text:
            logger.warning("agent response empty user_id=%s raw_response=%s", user_id or "unknown", response)
            raise AgentError("empty model response")
        logger.info(
            "agent request success user_id=%s reply_len=%s reply_preview=%r response_id=%s",
            user_id or "unknown",
            len(text),
            text[:200],
            getattr(response, "id", None),
        )
        return text


def is_agent_configured() -> bool:
    required_envs = ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL")
    return all(os.getenv(name) for name in required_envs)
