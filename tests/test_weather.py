import unittest
from unittest.mock import patch

from app.weather import WeatherError, is_weather_question, weather_reply


PAYLOAD = {
    "current": {
        "temperature_2m": 23,
        "apparent_temperature": 22,
        "relative_humidity_2m": 42,
        "precipitation": 0,
        "weather_code": 0,
        "wind_speed_10m": 10,
    },
    "daily": {
        "temperature_2m_max": [23, 20],
        "temperature_2m_min": [13, 10],
        "precipitation_probability_max": [0, 30],
        "weather_code": [45, 61],
    },
}


class WeatherTests(unittest.TestCase):
    def test_weather_intent(self):
        self.assertTrue(is_weather_question("今天天气怎么样"))
        self.assertTrue(is_weather_question("会下雨吗"))
        self.assertTrue(is_weather_question("沧州天气怎么样"))
        self.assertFalse(is_weather_question("今天心情不错"))

    def test_explanations_and_stories_do_not_trigger_forecast_lookup(self):
        for text in ['用三句话解释一下为什么会下雨','为什么天空会下雨','降雨是怎么形成的',
                     '什么是气温','降雨概率是什么意思','气温和温度有什么区别',
                     '下雨和湿度有什么关系','如何测量温度','讲一个下雨天的小故事','写一首关于天气的诗歌']:
            with self.subTest(text=text):self.assertFalse(is_weather_question(text))

    def test_current_and_future_forecasts_still_use_weather_lookup(self):
        for text in ['北京今天天气怎么样','明天会不会下雨','今天需要带伞吗','上海现在气温多少',
                     '明天降雨概率是多少','沧州今天冷不冷','查一下明天的天气预报']:
            with self.subTest(text=text):self.assertTrue(is_weather_question(text))

    @patch("app.weather._weather", return_value=PAYLOAD)
    def test_cangzhou_today_reply(self, _weather):
        text = weather_reply("今天天气怎么样")
        self.assertTrue(text.startswith("沧州："))
        self.assertIn("现在23°C", text)
        self.assertIn("晴", text)
        self.assertNotIn("湿度", text)
        self.assertNotIn("风速", text)

    @patch("app.weather._weather", return_value=PAYLOAD)
    def test_tomorrow_uses_second_daily_index(self, _weather):
        text = weather_reply("明天天气怎么样")
        self.assertIn("沧州：明天小雨，10到20°C", text)
        self.assertNotIn("降雨概率", text)

    @patch("app.weather._fetch_json", side_effect=WeatherError("WEATHER_UNAVAILABLE"))
    def test_network_failure_is_not_silent_success(self, _fetch):
        with self.assertRaises(WeatherError):
            weather_reply("今天天气怎么样")


if __name__ == "__main__":
    unittest.main()
