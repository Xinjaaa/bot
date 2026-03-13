import logging
import os
import re
import subprocess
from pathlib import Path


logger = logging.getLogger("wecom-weather")


class WeatherSkillError(Exception):
    pass


class WeatherSkill:
    def __init__(self) -> None:
        self.base_dir = Path(os.getenv("WEATHER_SKILL_DIR", "/app/skills/weather-cn"))
        self.script_path = self.base_dir / "weather-cn.sh"
        self.codes_path = self.base_dir / "weather_codes.txt"
        self.default_city = os.getenv("WEATHER_DEFAULT_CITY", "北京")
        self.timeout = float(os.getenv("WEATHER_SKILL_TIMEOUT_SECONDS", "12"))
        self.cities = self._load_cities()
        logger.info(
            "weather skill initialized base_dir=%s default_city=%s timeout=%s city_count=%s",
            self.base_dir,
            self.default_city,
            self.timeout,
            len(self.cities),
        )

    def is_weather_query(self, message: str) -> bool:
        text = message.strip()
        keywords = (
            "天气",
            "气温",
            "温度",
            "下雨",
            "降雨",
            "降雪",
            "会不会下雨",
            "冷不冷",
            "热不热",
            "天气预报",
        )
        return any(keyword in text for keyword in keywords)

    def detect_city(self, message: str) -> str:
        text = message.strip()
        for city in sorted(self.cities, key=len, reverse=True):
            if city in text:
                return city

        normalized = text
        for prefix in ("帮我查一下", "帮我查", "查一下", "查查", "看看", "查询", "告诉我"):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :]
                break

        suffix_match = re.search(r"([一-龥]{2,10})(?:今天天气|明天天气|后天天气|天气怎么样|天气如何|天气预报|天气)", normalized)
        if suffix_match:
            return suffix_match.group(1)

        prefix_match = re.search(r"(?:查询|看看|查一下|帮我查一下|帮我查|告诉我)([一-龥]{2,10})天气", text)
        if prefix_match:
            return prefix_match.group(1)

        return self.default_city

    def query(self, message: str) -> str:
        if not self.script_path.exists():
            raise WeatherSkillError(f"weather script not found: {self.script_path}")
        city = self.detect_city(message)
        logger.info("weather skill query detected city=%s message_preview=%r", city, message[:200])
        try:
            result = subprocess.run(
                ["bash", str(self.script_path), city],
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise WeatherSkillError(f"weather query timeout for city={city}") from exc

        stdout = self._sanitize_output(result.stdout)
        stderr = self._sanitize_output(result.stderr)
        logger.info(
            "weather skill result city=%s returncode=%s stdout_preview=%r stderr_preview=%r",
            city,
            result.returncode,
            stdout[:300],
            stderr[:300],
        )
        if result.returncode != 0:
            raise WeatherSkillError(stderr or stdout or f"weather script failed for city={city}")
        return stdout.strip()

    def _load_cities(self) -> list[str]:
        if not self.codes_path.exists():
            logger.warning("weather city code file not found path=%s", self.codes_path)
            return []
        cities = []
        for line in self.codes_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "," not in line:
                continue
            city, _ = line.split(",", 1)
            cities.append(city.strip())
        return cities

    @staticmethod
    def _sanitize_output(text: str) -> str:
        return re.sub(r"\x1b\[[0-9;]*m", "", text or "")


def is_weather_skill_configured() -> bool:
    base_dir = Path(os.getenv("WEATHER_SKILL_DIR", "/app/skills/weather-cn"))
    return (base_dir / "weather-cn.sh").exists() and (base_dir / "weather_codes.txt").exists()
