import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_filename(user_id: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", user_id).strip("-")
    return sanitized or "unknown-user"


@dataclass
class IdentityFact:
    label: str
    value: str
    source_message: str
    updated_at: str


class UserIdentityStore:
    def __init__(self, base_dir: str | None = None) -> None:
        root = base_dir or os.getenv("IDENTITY_DIR", "/app/identities")
        self.base_dir = Path(root)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def get_identity_file_path(self, user_id: str) -> Path:
        return self.base_dir / f"{_safe_filename(user_id)}-Identity.md"

    def ensure_file(self, user_id: str) -> Path:
        path = self.get_identity_file_path(user_id)
        if not path.exists():
            created_at = _utc_now()
            content = self._render(user_id=user_id, created_at=created_at, updated_at=created_at, facts=[])
            path.write_text(content, encoding="utf-8")
        return path

    def load_markdown(self, user_id: str) -> str:
        path = self.ensure_file(user_id)
        return path.read_text(encoding="utf-8").strip()

    def update_from_message(self, user_id: str, message: str) -> list[IdentityFact]:
        path = self.ensure_file(user_id)
        facts = self._parse_facts(path.read_text(encoding="utf-8"))
        extracted = self._extract_facts(message)
        if not extracted:
            return []

        now = _utc_now()
        existing = {fact.label: fact for fact in facts}
        changed: list[IdentityFact] = []
        for label, value in extracted.items():
            current = existing.get(label)
            if current and current.value == value:
                continue
            fact = IdentityFact(
                label=label,
                value=value,
                source_message=message,
                updated_at=now,
            )
            existing[label] = fact
            changed.append(fact)

        if changed:
            created_at = self._extract_created_at(path.read_text(encoding="utf-8")) or now
            ordered_facts = [existing[key] for key in sorted(existing.keys())]
            path.write_text(
                self._render(
                    user_id=user_id,
                    created_at=created_at,
                    updated_at=now,
                    facts=ordered_facts,
                ),
                encoding="utf-8",
            )
        return changed

    @staticmethod
    def _extract_created_at(content: str) -> str | None:
        match = re.search(r"^- Created At: (.+)$", content, flags=re.MULTILINE)
        return match.group(1).strip() if match else None

    @staticmethod
    def _parse_facts(content: str) -> list[IdentityFact]:
        pattern = re.compile(
            r"^- (?P<label>[^:]+): (?P<value>.+?) \(source: (?P<source>.+?), updated: (?P<updated>.+?)\)$",
            flags=re.MULTILINE,
        )
        facts = []
        for match in pattern.finditer(content):
            facts.append(
                IdentityFact(
                    label=match.group("label").strip(),
                    value=match.group("value").strip(),
                    source_message=match.group("source").strip(),
                    updated_at=match.group("updated").strip(),
                )
            )
        return facts

    @staticmethod
    def _extract_facts(message: str) -> dict[str, str]:
        text = message.strip()
        patterns = [
            ("姓名", [r"我叫([^\n，。,；;！!？?]{1,20})", r"我的名字是([^\n，。,；;！!？?]{1,20})"]),
            ("英文名", [r"我的英文名是([A-Za-z][A-Za-z .'-]{0,30})"]),
            ("公司", [r"我在([^，。,；;！!？?\n]{2,30})工作", r"我是([^，。,；;！!？?\n]{2,30})的员工"]),
            ("职位", [r"我是([^，。,；;！!？?\n]{2,20}(?:工程师|产品经理|设计师|老师|律师|医生|运营|顾问|销售|学生|开发))"]),
            ("城市", [r"我现在在([^，。,；;！!？?\n]{2,20})", r"我住在([^，。,；;！!？?\n]{2,20})", r"我是([^，。,；;！!？?\n]{2,20})人"]),
            ("学校", [r"我在([^，。,；;！!？?\n]{2,30}(?:大学|学院|学校))"]),
        ]
        extracted: dict[str, str] = {}
        for label, regex_list in patterns:
            for regex in regex_list:
                match = re.search(regex, text)
                if match:
                    extracted[label] = match.group(1).strip()
                    break
        return extracted

    @staticmethod
    def _render(user_id: str, created_at: str, updated_at: str, facts: list[IdentityFact]) -> str:
        lines = [
            f"# {user_id} Identity",
            "",
            "## Metadata",
            f"- User ID: {user_id}",
            f"- Created At: {created_at}",
            f"- Updated At: {updated_at}",
            "",
            "## Confirmed Facts",
        ]
        if facts:
            for fact in facts:
                lines.append(
                    f"- {fact.label}: {fact.value} "
                    f"(source: {fact.source_message[:80]}, updated: {fact.updated_at})"
                )
        else:
            lines.append("- None yet. Only explicit self-declared facts are stored.")
        lines.extend(
            [
                "",
                "## Notes",
                "- This file is updated automatically from explicit user self-descriptions.",
                "- Ambiguous or inferred facts should not be written here.",
            ]
        )
        return "\n".join(lines) + "\n"
