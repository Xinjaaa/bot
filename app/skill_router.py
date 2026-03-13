import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.openai_compat import request_text


logger = logging.getLogger("skill-router")


@dataclass
class SkillDefinition:
    name: str
    description: str
    path: Path


class SkillRouter:
    def __init__(self, skills_dir: str | None = None) -> None:
        self.skills_dir = Path(skills_dir or os.getenv("SKILLS_DIR", "/app/skills"))
        self.model = os.getenv("OPENAI_MODEL", "")
        self._client: Any | None = None
        if all(os.getenv(name) for name in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL")):
            try:
                from openai import OpenAI
            except ImportError:
                logger.warning("openai sdk not installed locally, skill router model selection disabled")
            else:
                self._client = OpenAI(
                    base_url=os.getenv("OPENAI_BASE_URL"),
                    api_key=os.getenv("OPENAI_API_KEY"),
                    timeout=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "20")),
                )
        self.skills = self._load_skills()
        logger.info("skill router initialized skills_dir=%s skills=%s", self.skills_dir, [skill.name for skill in self.skills])

    def select_skill(self, message: str) -> str | None:
        if not self.skills or not self._client:
            return None
        skill_payload = [{"name": skill.name, "description": skill.description} for skill in self.skills]
        instructions = (
            "你是一个 skills 路由器。"
            "根据用户消息，从给定 skills 中选择最合适的一个。"
            "只输出 JSON，格式为 {\"skill_name\": \"...\"}。"
            "如果没有任何 skill 明显匹配，输出 {\"skill_name\": null}。"
        )
        input_text = (
            f"可用 skills:\n{json.dumps(skill_payload, ensure_ascii=False)}\n\n"
            f"用户消息:\n{message}"
        )
        try:
            output = request_text(
                self._client,
                model=self.model,
                instructions=instructions,
                input_text=input_text,
            )
        except Exception:
            logger.exception("skill router request failed")
            return None
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            logger.warning("skill router returned invalid json output=%r", output)
            return None
        skill_name = data.get("skill_name")
        if any(skill.name == skill_name for skill in self.skills):
            logger.info("skill router selected skill=%s for message_preview=%r", skill_name, message[:200])
            return skill_name
        logger.info("skill router selected no skill for message_preview=%r", message[:200])
        return None

    def _load_skills(self) -> list[SkillDefinition]:
        if not self.skills_dir.exists():
            return []
        skills = []
        for skill_file in sorted(self.skills_dir.glob("*/SKILL.md")):
            parsed = self._parse_skill_file(skill_file)
            if parsed:
                skills.append(parsed)
        return skills

    @staticmethod
    def _parse_skill_file(path: Path) -> SkillDefinition | None:
        content = path.read_text(encoding="utf-8")
        frontmatter_match = re.match(r"^---\n(.*?)\n---\n", content, flags=re.DOTALL)
        if not frontmatter_match:
            return None
        frontmatter = frontmatter_match.group(1)
        name_match = re.search(r"^name:\s*\"?([^\n\"]+)\"?$", frontmatter, flags=re.MULTILINE)
        description_match = re.search(r"^description:\s*\"?([^\n\"]+)\"?$", frontmatter, flags=re.MULTILINE)
        if not name_match or not description_match:
            return None
        return SkillDefinition(
            name=name_match.group(1).strip(),
            description=description_match.group(1).strip(),
            path=path.parent,
        )
