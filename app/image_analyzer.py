import base64
import logging
import os
from typing import Any

import httpx

from app.openai_compat import request_multimodal_text


logger = logging.getLogger("image-analyzer")


class ImageAnalyzerError(Exception):
    pass


class ImageAnalyzer:
    def __init__(self) -> None:
        base_url = self._get_required_env("OPENAI_BASE_URL")
        api_key = self._get_required_env("OPENAI_API_KEY")
        self.model = self._get_required_env("OPENAI_MODEL")
        self.timeout = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "20"))
        self.download_timeout = float(os.getenv("IMAGE_DOWNLOAD_TIMEOUT_SECONDS", "15"))
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("openai sdk not installed") from exc
        self.client: Any = OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=self.timeout,
        )
        logger.info(
            "image analyzer initialized base_url=%s model=%s timeout=%s download_timeout=%s",
            base_url,
            self.model,
            self.timeout,
            self.download_timeout,
        )

    @staticmethod
    def _get_required_env(name: str) -> str:
        value = os.getenv(name)
        if not value:
            raise RuntimeError(f"missing required environment variable: {name}")
        return value

    def describe(self, image_url: str) -> str:
        prompt = (
            "请识别这张图片并用简洁中文描述。"
            "重点说明：1. 画面里有哪些主要元素；"
            "2. 人物、动物或物体分别在做什么；"
            "3. 场景环境与大致氛围；"
            "4. 如果有明显文字、品牌、颜色、食物、交通工具或屏幕内容，也提一下。"
            "如果细节不确定，请明确说“看起来像”或“可能是”，不要编造。"
            "输出 3 到 6 句话，不要使用 markdown。"
        )
        image_input_url = self._build_data_url(image_url)
        try:
            text = request_multimodal_text(
                self.client,
                model=self.model,
                instructions="你是一个认真、客观的图像内容描述助手。",
                input_text=prompt,
                image_url=image_input_url,
            )
        except Exception as exc:
            logger.exception("image analysis request failed")
            raise ImageAnalyzerError(str(exc)) from exc
        if not text:
            raise ImageAnalyzerError("empty image analysis response")
        return text.strip()

    def _build_data_url(self, image_url: str) -> str:
        try:
            with httpx.Client(timeout=self.download_timeout, follow_redirects=True) as client:
                response = client.get(image_url)
                response.raise_for_status()
        except Exception as exc:
            logger.warning("image download failed, fallback to remote url image_url=%s error=%s", image_url, exc)
            return image_url

        content_type = response.headers.get("content-type", "").split(";")[0].strip() or "image/jpeg"
        encoded = base64.b64encode(response.content).decode("ascii")
        return f"data:{content_type};base64,{encoded}"


def is_image_analyzer_configured() -> bool:
    required_envs = ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL")
    return all(os.getenv(name) for name in required_envs)
