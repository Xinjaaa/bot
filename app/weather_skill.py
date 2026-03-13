import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx


logger = logging.getLogger("wecom-weather")


class WeatherSkillError(Exception):
    pass


@dataclass
class WeatherQueryParams:
    city: str
    day_offset: int
    day_count: int
    rain_only: bool


class WeatherSkill:
    def __init__(self) -> None:
        self.base_dir = Path(os.getenv("WEATHER_SKILL_DIR", "/app/skills/weather-cn"))
        self.script_path = self.base_dir / "weather-cn.sh"
        self.codes_path = self.base_dir / "weather_codes.txt"
        self.default_city = os.getenv("WEATHER_DEFAULT_CITY", "北京")
        self.timeout = float(os.getenv("WEATHER_SKILL_TIMEOUT_SECONDS", "12"))
        self.api_base_url = os.getenv("WEATHER_API_BASE_URL", "https://api.open-meteo.com/v1/forecast")
        self.archive_api_base_url = os.getenv("WEATHER_ARCHIVE_API_BASE_URL", "https://archive-api.open-meteo.com/v1/archive")
        self.geo_base_url = os.getenv("WEATHER_GEO_BASE_URL", "https://geocoding-api.open-meteo.com/v1/search")
        self.timezone = os.getenv("APP_TIMEZONE", "Asia/Shanghai")
        self.model = os.getenv("OPENAI_MODEL", "")
        self._client: Any | None = None
        if all(os.getenv(name) for name in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_MODEL")):
            try:
                from openai import OpenAI
            except ImportError:
                logger.warning("openai sdk not installed locally, weather query model parser disabled")
            else:
                self._client = OpenAI(
                    base_url=os.getenv("OPENAI_BASE_URL"),
                    api_key=os.getenv("OPENAI_API_KEY"),
                    timeout=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "20")),
                )
        self.cities = self._load_cities()
        logger.info(
            "weather skill initialized base_dir=%s default_city=%s timeout=%s city_count=%s timezone=%s model_parser_enabled=%s",
            self.base_dir,
            self.default_city,
            self.timeout,
            len(self.cities),
            self.timezone,
            bool(self._client),
        )

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
            return self._normalize_city_candidate(suffix_match.group(1))

        prefix_match = re.search(r"(?:查询|看看|查一下|帮我查一下|帮我查|告诉我)([一-龥]{2,10})天气", text)
        if prefix_match:
            return self._normalize_city_candidate(prefix_match.group(1))

        return self.default_city

    def query(self, message: str) -> str:
        params = self._parse_query_params(message)
        city = params.city
        day_offset = params.day_offset
        day_count = params.day_count
        rain_only = params.rain_only
        logger.info(
            "weather skill query detected city=%s day_offset=%s day_count=%s rain_only=%s message_preview=%r",
            city,
            day_offset,
            day_count,
            rain_only,
            message[:200],
        )
        try:
            if day_offset >= -2:
                weather_data = self._query_forecast(city, day_offset, day_count)
            else:
                weather_data = self._query_archive(city, day_offset, day_count)
            response = self._format_forecast_response(city, weather_data, day_offset, day_count, rain_only)
            logger.info(
                "weather skill forecast success city=%s day_offset=%s day_count=%s response_preview=%r",
                city,
                day_offset,
                day_count,
                response[:300],
            )
            return response
        except WeatherSkillError as exc:
            logger.warning("weather forecast query failed city=%s day_offset=%s day_count=%s error=%s", city, day_offset, day_count, exc)

        if day_offset != 0 or day_count != 1:
            raise WeatherSkillError(f"无法获取{city}对应日期的天气预报，请稍后再试")

        if not self.script_path.exists():
            raise WeatherSkillError(f"weather script not found: {self.script_path}")

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
            "weather skill script fallback result city=%s returncode=%s stdout_preview=%r stderr_preview=%r",
            city,
            result.returncode,
            stdout[:300],
            stderr[:300],
        )
        if result.returncode != 0 or "错误:" in stderr or "温度：未知" in stdout:
            raise WeatherSkillError(stderr or stdout or f"weather script failed for city={city}")
        return self._format_script_response(city, stdout.strip())

    def _parse_query_params(self, message: str) -> WeatherQueryParams:
        if self._client:
            try:
                parsed = self._parse_query_params_by_model(message)
                if parsed:
                    return parsed
            except Exception:
                logger.exception("weather query model parser failed, fallback to rule parser")
        return self._parse_query_params_by_rule(message)

    def _parse_query_params_by_model(self, message: str) -> WeatherQueryParams | None:
        now = datetime.now(ZoneInfo(self.timezone)).strftime("%Y-%m-%d %H:%M:%S %Z")
        instructions = (
            "你是天气查询参数解析器。"
            "从用户消息中提取天气查询参数。"
            "只输出 JSON，不要输出 markdown。"
            "字段必须是: city, day_offset, day_count, rain_only。"
            "city 可以是字符串或 null。未明确提到城市时输出 null。"
            "day_offset 是相对今天的整数天偏移，过去是负数，今天是 0，未来是正数。"
            "day_count 是 1 到 7 的整数。"
            "rain_only 只有在用户主要关心下雨/降水时才设为 true，否则为 false。"
        )
        payload = (
            f"当前时间: {now}\n"
            f"默认城市: {self.default_city}\n"
            f"常见城市列表: {json.dumps(self.cities[:200], ensure_ascii=False)}\n"
            f"用户消息: {message}\n"
        )
        response = self._client.responses.create(
            model=self.model,
            instructions=instructions,
            input=payload,
        )
        text = (response.output_text or "").strip()
        if not text:
            return None
        data = json.loads(text)
        city = data.get("city")
        if city is not None:
            city = self._normalize_city_candidate(str(city))
            city = city or None
        try:
            day_offset = int(data.get("day_offset", 0))
        except (TypeError, ValueError):
            day_offset = 0
        try:
            day_count = int(data.get("day_count", 1))
        except (TypeError, ValueError):
            day_count = 1
        day_count = max(1, min(day_count, 7))
        rain_only = bool(data.get("rain_only", False))
        return WeatherQueryParams(
            city=city or self.default_city,
            day_offset=day_offset,
            day_count=day_count,
            rain_only=rain_only,
        )

    def _parse_query_params_by_rule(self, message: str) -> WeatherQueryParams:
        return WeatherQueryParams(
            city=self.detect_city(message),
            day_offset=self._detect_day_offset(message),
            day_count=self._detect_day_count(message),
            rain_only=self._is_rain_question(message),
        )

    def _normalize_city_candidate(self, city: str) -> str:
        normalized = city.strip()
        for keyword in self._relative_day_offsets():
            if normalized.startswith(keyword):
                normalized = normalized[len(keyword) :].strip()
            if normalized.endswith(keyword):
                normalized = normalized[: -len(keyword)].strip()
        for prefix in ("今天", "明天", "后天", "昨天", "前天", "大前天", "大后天", "现在", "当前"):
            if normalized.startswith(prefix):
                normalized = normalized[len(prefix) :].strip()
        return normalized or self.default_city

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

    def _detect_day_offset(self, message: str) -> int:
        for keyword, offset in self._relative_day_offsets().items():
            if keyword in message:
                return offset
        return 0

    @staticmethod
    def _detect_day_count(message: str) -> int:
        text = message.strip()
        multi_day_patterns = [
            (r"(最近|未来|接下来)(\d+)天", 2),
            (r"(\d+)天天气", 1),
            (r"(\d+)天的天气", 1),
        ]
        for pattern, group_idx in multi_day_patterns:
            match = re.search(pattern, text)
            if match:
                try:
                    return max(1, min(int(match.group(group_idx)), 7))
                except ValueError:
                    return 1
        chinese_number_map = {"三天": 3, "两天": 2, "三日": 3, "两日": 2}
        for key, value in chinese_number_map.items():
            if key in text and any(prefix in text for prefix in ("最近", "未来", "接下来")):
                return value
        return 1

    @staticmethod
    def _is_rain_question(message: str) -> bool:
        rain_keywords = ("下雨", "会不会下雨", "有雨", "降雨", "雨吗")
        return any(keyword in message for keyword in rain_keywords)

    @staticmethod
    def _relative_day_offsets() -> dict[str, int]:
        return {
            "大前天": -3,
            "前天": -2,
            "昨天": -1,
            "今天": 0,
            "明天": 1,
            "后天": 2,
            "大后天": 3,
        }

    @staticmethod
    def _extract_field(pattern: str, text: str) -> str:
        match = re.search(pattern, text, flags=re.MULTILINE)
        return match.group(1).strip() if match else "未知"

    def _format_script_response(self, city: str, raw_output: str) -> str:
        date_text = self._extract_field(r"今日天气（([^)]+)）", raw_output)
        weather = self._extract_field(r"[☀️⛅☁️🌧️❄️🌤️]\s*(.*?)\s*\|\s*温度：", raw_output)
        weather = weather.lstrip("️ ").strip()
        temperature = self._extract_field(r"温度：([^\n]+)", raw_output)
        cold_index = self._extract_field(r"感冒：([^\n]+)", raw_output)
        sport_index = self._extract_field(r"运动：([^\n]+)", raw_output)
        dress_index = self._extract_field(r"穿衣：([^\n]+)", raw_output)
        wash_index = self._extract_field(r"洗车：([^\n]+)", raw_output)
        uv_index = self._extract_field(r"紫外线：([^\n]+)", raw_output)

        tips = []
        if "雨" in weather:
            tips.append("出门记得带伞呀")
        if dress_index in {"较冷", "冷"}:
            tips.append("记得多穿一点，别着凉啦")
        if uv_index == "强":
            tips.append("紫外线有点强，出门注意防晒哒")
        if not tips:
            tips.append("整体天气还算平稳，可以按计划安排出行嘿嘿")

        return (
            f"帮你看好啦，{city} {date_text} 的天气是 {weather}，温度 {temperature}。\n"
            f"生活指数这边：感冒 {cold_index}，运动 {sport_index}，穿衣 {dress_index}，洗车 {wash_index}，紫外线 {uv_index}。\n"
            f"小提醒：{'；'.join(tips)}"
        )

    def _query_forecast(self, city: str, day_offset: int, day_count: int) -> list[dict]:
        geo = self._geocode_city(city)
        past_days = max(0, -day_offset)
        forecast_days = max(1, day_offset + day_count)
        params = {
            "latitude": geo["latitude"],
            "longitude": geo["longitude"],
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum",
            "timezone": self.timezone,
            "past_days": past_days,
            "forecast_days": forecast_days,
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(self.api_base_url, params=params)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            raise WeatherSkillError(f"forecast api request failed for city={city}") from exc

        daily = data.get("daily") or {}
        times = daily.get("time") or []
        start_idx = past_days + day_offset
        end_idx = start_idx + day_count
        if start_idx < 0 or len(times) < end_idx:
            raise WeatherSkillError(f"forecast data missing for city={city} day_offset={day_offset} day_count={day_count}")
        items = []
        for idx in range(start_idx, end_idx):
            items.append(
                {
                    "date": times[idx],
                    "weather_code": (daily.get("weather_code") or [None])[idx],
                    "temp_max": (daily.get("temperature_2m_max") or [None])[idx],
                    "temp_min": (daily.get("temperature_2m_min") or [None])[idx],
                    "precip_probability": (daily.get("precipitation_probability_max") or [None])[idx],
                    "precip_sum": (daily.get("precipitation_sum") or [None])[idx],
                }
            )
        return items

    def _query_archive(self, city: str, day_offset: int, day_count: int) -> list[dict]:
        geo = self._geocode_city(city)
        today = datetime.now(ZoneInfo(self.timezone)).date()
        start_date = today + timedelta(days=day_offset)
        end_date = start_date + timedelta(days=day_count - 1)
        params = {
            "latitude": geo["latitude"],
            "longitude": geo["longitude"],
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum",
            "timezone": self.timezone,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(self.archive_api_base_url, params=params)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            raise WeatherSkillError(f"archive api request failed for city={city}") from exc

        daily = data.get("daily") or {}
        times = daily.get("time") or []
        if len(times) < day_count:
            raise WeatherSkillError(f"archive data missing for city={city} day_offset={day_offset} day_count={day_count}")

        items = []
        for idx in range(day_count):
            items.append(
                {
                    "date": times[idx],
                    "weather_code": (daily.get("weather_code") or [None])[idx],
                    "temp_max": (daily.get("temperature_2m_max") or [None])[idx],
                    "temp_min": (daily.get("temperature_2m_min") or [None])[idx],
                    "precip_probability": None,
                    "precip_sum": (daily.get("precipitation_sum") or [None])[idx],
                }
            )
        return items

    def _geocode_city(self, city: str) -> dict:
        params = {
            "name": city,
            "count": 1,
            "language": "zh",
            "format": "json",
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                response = client.get(self.geo_base_url, params=params)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            raise WeatherSkillError(f"geocoding request failed for city={city}") from exc
        results = data.get("results") or []
        if not results:
            raise WeatherSkillError(f"city geocoding not found for city={city}")
        return results[0]

    def _format_forecast_response(self, city: str, data: list[dict], day_offset: int, day_count: int, rain_only: bool) -> str:
        primary = data[0]
        date_label = self._date_label(day_offset, primary["date"])
        weather_text = self._weather_code_to_text(primary.get("weather_code"))
        temp_min = self._fmt_number(primary.get("temp_min"))
        temp_max = self._fmt_number(primary.get("temp_max"))
        precip_probability = self._fmt_number(primary.get("precip_probability"), suffix="%")
        precip_sum = self._fmt_number(primary.get("precip_sum"), suffix="mm")
        is_historical = day_offset < 0

        if rain_only and day_count == 1:
            rain_assessment = self._rain_assessment(primary.get("weather_code"), primary.get("precip_probability"), primary.get("precip_sum"))
            details = (
                f"天气大致是 {weather_text}，气温 {temp_min} 到 {temp_max}，降水概率大约 {precip_probability}。"
                if primary.get("precip_probability") is not None
                else f"天气大致是 {weather_text}，气温 {temp_min} 到 {temp_max}，记录到的降水量大约 {precip_sum}。"
            )
            return (
                f"让我帮你看了一下，{date_label}{city}"
                f"{rain_assessment}。\n"
                f"{details}"
            )

        if day_count > 1:
            lines = [f"帮你整理好啦，{city}{'最近' if day_offset < 0 else '接下来'} {day_count} 天的天气大致是这样的："]
            for idx, item in enumerate(data):
                item_label = self._date_label(day_offset + idx, item["date"])
                item_weather = self._weather_code_to_text(item.get("weather_code"))
                item_temp_min = self._fmt_number(item.get("temp_min"))
                item_temp_max = self._fmt_number(item.get("temp_max"))
                item_precip_probability = self._fmt_number(item.get("precip_probability"), suffix="%")
                if item.get("precip_probability") is not None:
                    line = f"{item_label} {item['date']}：{item_weather}，{item_temp_min} 到 {item_temp_max}，降水概率 {item_precip_probability}"
                else:
                    line = f"{item_label} {item['date']}：{item_weather}，{item_temp_min} 到 {item_temp_max}，降水量 {self._fmt_number(item.get('precip_sum'), suffix='mm')}"
                lines.append(line)
            max_precip = max((item.get("precip_probability") or 0) for item in data)
            max_precip_sum = max((item.get("precip_sum") or 0) for item in data)
            if max_precip >= 50 or max_precip_sum >= 1:
                lines.append("小提醒：这几天里有下雨概率偏高的时段，出门带伞会更稳妥一点呀。")
            else:
                lines.append("小提醒：这几天整体还算平稳，可以比较安心地安排出行嘿嘿。")
            return "\n".join(lines)

        tips = []
        if primary.get("precip_probability") and primary["precip_probability"] >= 50:
            tips.append("出门带伞会更稳妥一点呀")
        if primary.get("temp_min") is not None and primary["temp_min"] <= 10:
            tips.append("早晚会偏凉，记得多穿一点")
        if not tips:
            tips.append("整体看起来还比较平稳，可以安心安排出门计划嘿嘿")

        if is_historical and primary.get("precip_probability") is None:
            summary_line = f"记录到的降水量大约 {precip_sum}。"
        else:
            summary_line = f"降水概率大约 {precip_probability}，预计降水量 {precip_sum}。"

        return (
            f"帮你看好啦，{city}{date_label} {primary['date']} 的天气大致是 {weather_text}，"
            f"气温 {temp_min} 到 {temp_max}。\n"
            f"{summary_line}\n"
            f"小提醒：{'；'.join(tips)}"
        )

    @classmethod
    def _date_label(cls, day_offset: int, fallback: str) -> str:
        for keyword, offset in cls._relative_day_offsets().items():
            if offset == day_offset:
                return keyword
        return fallback

    @staticmethod
    def _weather_code_to_text(code: int | None) -> str:
        mapping = {
            0: "晴",
            1: "大体晴朗",
            2: "局部多云",
            3: "阴天",
            45: "有雾",
            48: "雾凇",
            51: "毛毛雨",
            53: "小雨",
            55: "中雨",
            61: "小雨",
            63: "中雨",
            65: "大雨",
            71: "小雪",
            73: "中雪",
            75: "大雪",
            80: "阵雨",
            81: "较强阵雨",
            82: "强阵雨",
            95: "雷阵雨",
        }
        return mapping.get(code, "天气多变")

    @staticmethod
    def _rain_assessment(weather_code: int | None, precip_probability: float | None, precip_sum: float | None) -> str:
        rainy_codes = {51, 53, 55, 61, 63, 65, 80, 81, 82, 95}
        probability = precip_probability or 0
        amount = precip_sum or 0
        if weather_code in rainy_codes or probability >= 60 or amount >= 1:
            return "大概率会下雨"
        if probability >= 30:
            return "有一定概率会下雨"
        return "看起来下雨概率不高"

    @staticmethod
    def _fmt_number(value: float | None, suffix: str = "℃") -> str:
        if value is None:
            return "未知"
        if isinstance(value, float):
            if value.is_integer():
                return f"{int(value)}{suffix}"
            return f"{value:.1f}{suffix}"
        return f"{value}{suffix}"


def is_weather_skill_configured() -> bool:
    base_dir = Path(os.getenv("WEATHER_SKILL_DIR", "/app/skills/weather-cn"))
    return (base_dir / "weather-cn.sh").exists() and (base_dir / "weather_codes.txt").exists()
