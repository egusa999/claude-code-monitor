"""使用量APIの呼び出し間隔と、HTTP 429時の待ち(バックオフ)の確認。"""
import unittest
from unittest import mock

import claude_monitor as cm

OK = {"session": {"pct": 2.0, "resets_at": None, "window_sec": 18000}}


class UsageBackoffTest(unittest.TestCase):
    def setUp(self):
        cm._usage_cache, cm._usage_cache_at = {}, 0.0
        cm._usage_next_at, cm._usage_backoff_sec = 0.0, 0.0
        self.now = 1_000_000.0
        p = mock.patch.object(cm.time, "time", lambda: self.now)
        p.start()
        self.addCleanup(p.stop)
        self.calls = 0
        self.responses = []

    def fake_fetch(self):
        self.calls += 1
        return dict(self.responses.pop(0))

    def fetch(self):
        with mock.patch.object(cm, "_fetch_plan_usage_uncached", self.fake_fetch):
            return cm.fetch_plan_usage()

    def test_success_is_cached_for_normal_interval(self):
        self.responses = [OK, OK]
        self.fetch()
        self.now += cm.USAGE_CACHE_SEC - 1
        self.fetch()
        self.assertEqual(self.calls, 1)
        self.now += 2
        self.fetch()
        self.assertEqual(self.calls, 2)

    def test_429_keeps_previous_values_and_backs_off(self):
        busy = {"error": cm.USAGE_BUSY_ERROR, "_rate_limited": True, "_retry_after": None}
        self.responses = [OK, busy, busy, OK]
        self.fetch()
        self.now += cm.USAGE_CACHE_SEC
        got = self.fetch()
        self.assertEqual(got["error"], cm.USAGE_BUSY_ERROR)
        self.assertEqual(got["session"]["pct"], 2.0)  # 前回値を残す
        self.assertNotIn("_rate_limited", got)
        self.assertEqual(cm._usage_backoff_sec, cm.USAGE_BACKOFF_MIN_SEC)
        self.now += cm.USAGE_BACKOFF_MIN_SEC
        self.fetch()
        self.assertEqual(cm._usage_backoff_sec, cm.USAGE_BACKOFF_MIN_SEC * 2)  # 倍に延びる
        self.now += cm.USAGE_BACKOFF_MIN_SEC * 2 - 1
        self.fetch()
        self.assertEqual(self.calls, 3)  # 待ちの間は呼ばない
        self.now += 1
        got = self.fetch()
        self.assertNotIn("error", got)
        self.assertEqual(cm._usage_backoff_sec, 0.0)  # 成功で通常に戻る

    def test_backoff_rules(self):
        self.assertEqual(cm.next_usage_backoff(0, None), cm.USAGE_BACKOFF_MIN_SEC)
        self.assertEqual(cm.next_usage_backoff(1200, None), cm.USAGE_BACKOFF_MAX_SEC)  # 上限
        self.assertEqual(cm.next_usage_backoff(0, 900), 900)  # Retry-After優先
        self.assertEqual(cm.next_usage_backoff(0, 3600), 3600)  # 上限より長いRetry-Afterにも従う
        self.assertEqual(cm.next_usage_backoff(0, 10), cm.USAGE_BACKOFF_MIN_SEC)  # 短すぎる値は最小値

    def test_parse_retry_after(self):
        self.assertEqual(cm.parse_retry_after("120"), 120.0)
        self.assertEqual(cm.parse_retry_after(" 5 "), 5.0)
        for bad in (None, "", "abc", "-1", "nan", "inf", "Wed, 21 Oct 2026 07:28:00 GMT"):
            self.assertIsNone(cm.parse_retry_after(bad), bad)


if __name__ == "__main__":
    unittest.main()
