"""Small, no-key weather lookup for the companion.

Open-Meteo is used only for a current condition and a one/two-day summary.
No API key, no owner data, no raw audio. Network failure is reported as a
short local message; it must never be presented as a successful lookup.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request

DEFAULT_CITY = "沧州"
DEFAULT_LAT = 38.31124
DEFAULT_LON = 116.85334

CITY_RE = re.compile(
    r"(北京|上海|沧州|广州|深圳|杭州|南京|成都|重庆|武汉|西安|苏州|天津|长沙|郑州|"
    r"青岛|大连|厦门|福州|济南|合肥|昆明|宁波|无锡|佛山|东莞|沈阳|哈尔滨|长春|"
    r"石家庄|太原|南昌|贵阳|南宁|海口|兰州|银川|西宁|乌鲁木齐|呼和浩特|拉萨|"
    r"香港|澳门|台北)"
)
WEATHER_RE = re.compile(r"天气|气温|温度|下雨|降雨|带伞|冷不冷|热不热")
WEATHER_EXPLANATION_RE = re.compile(
    r"为什么|为何|原理|成因|形成|科普|含义|什么是|什么叫|是什么意思|有什么(?:区别|影响|关系)|"
    r"(?:怎么|如何)(?:会|产生|测量|使用)|(?:讲|写|编).*(?:故事|童话|小说|诗歌|作文)"
)

WMO = {
    0: "晴", 1: "大致晴朗", 2: "局部多云", 3: "阴",
    45: "有雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "强毛毛雨",
    56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨",
    85: "小阵雪", 86: "强阵雪",
    95: "雷暴", 96: "雷暴伴小冰雹", 99: "雷暴伴大冰雹",
}


class WeatherError(Exception):
    pass


def is_weather_question(text: str) -> bool:
    text=text or ""
    # Forecast lookup cannot answer causes, definitions or creative requests.
    # Those keep the normal conversation path, even when weather words occur.
    return bool(WEATHER_RE.search(text)) and not WEATHER_EXPLANATION_RE.search(text)


def _fetch_json(url: str, timeout: float = 8.0) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "XiaoNuo-Weather/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status != 200:
                raise WeatherError("WEATHER_HTTP")
            return json.loads(response.read().decode("utf-8"))
    except WeatherError:
        raise
    except Exception as exc:
        raise WeatherError("WEATHER_UNAVAILABLE") from exc


def _geocode(city: str, timeout: float = 8.0) -> tuple[float, float, str]:
    query = urllib.parse.urlencode(
        {"name": city, "count": 1, "language": "zh", "format": "json"}
    )
    payload = _fetch_json(f"https://geocoding-api.open-meteo.com/v1/search?{query}", timeout)
    results = payload.get("results") or []
    if not results:
        raise WeatherError("WEATHER_CITY_NOT_FOUND")
    item = results[0]
    return float(item["latitude"]), float(item["longitude"]), str(item.get("name") or city)


def _weather(lat: float, lon: float, days: int = 2, timeout: float = 8.0) -> dict:
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "Asia/Shanghai",
        "forecast_days": days,
    }
    return _fetch_json(
        "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params), timeout
    )


def _description(code: int | None) -> str:
    return WMO.get(int(code) if code is not None else -1, "天气状况未知")


def weather_reply(text: str, *, city: str | None = None, timeout: float = 8.0) -> str:
    """Return one short spoken sentence; raise WeatherError on any failure."""
    match = CITY_RE.search(text or "")
    selected = match.group(1) if match else (city or DEFAULT_CITY)
    if selected == DEFAULT_CITY:
        lat, lon, name = DEFAULT_LAT, DEFAULT_LON, DEFAULT_CITY
    else:
        lat, lon, name = _geocode(selected, timeout)
    payload = _weather(lat, lon, 2, timeout)
    current = payload.get("current") or {}
    daily = payload.get("daily") or {}
    if not current or not daily:
        raise WeatherError("WEATHER_INVALID_PAYLOAD")

    temp = current.get("temperature_2m")
    feels = current.get("apparent_temperature")
    humidity = current.get("relative_humidity_2m")
    wind = current.get("wind_speed_10m")
    condition = _description(current.get("weather_code"))
    if temp is None:
        raise WeatherError("WEATHER_INVALID_PAYLOAD")

    tomorrow = "明天" in (text or "")
    index = 1 if tomorrow else 0
    try:
        high = daily["temperature_2m_max"][index]
        low = daily["temperature_2m_min"][index]
        rain = daily["precipitation_probability_max"][index]
        daily_desc = _description(daily["weather_code"][index])
    except (KeyError, IndexError, TypeError) as exc:
        raise WeatherError("WEATHER_INVALID_PAYLOAD") from exc

    # Keep the spoken answer short: the relay is stable for short replies, and
    # long TTS segments are the ones that lose the connection on this link.
    if tomorrow:
        return f"{name}：明天{daily_desc}，{round(float(low))}到{round(float(high))}°C。"
    return f"{name}：现在{round(float(temp))}°C，{condition}。"
