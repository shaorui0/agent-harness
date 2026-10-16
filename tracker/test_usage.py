"""usage.py / usage_alert.py 的单元测试：不调真实 claude、不发真实推送。"""
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import config
import usage
import usage_alert

TXT = """You are currently using your subscription to power your Claude Code usage

Current session: 20% used · resets Oct 16 at 9:50pm (Asia/Tokyo)
Current week (all models): 49% used · resets Oct 19 at 4am (Asia/Tokyo)
Current week (Fable): 0% used · resets Oct 19 at 4am (Asia/Tokyo)

What's contributing to your limits usage?
Last 24h · 595 requests · 13 sessions
"""
TOKYO = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 10, 16, 20, 33, tzinfo=TOKYO)


class ParseTest(unittest.TestCase):
    def test_parse_windows(self):
        ws = usage.parse_usage_text(TXT, NOW)
        self.assertEqual([w["id"] for w in ws], ["five_hour", "seven_day", "week_fable"])
        self.assertEqual(ws[0]["percent"], 20.0)
        self.assertEqual(ws[1]["percent"], 49.0)
        self.assertEqual(ws[0]["resets_at"], int(datetime(2026, 10, 16, 21, 50, tzinfo=TOKYO).timestamp()))
        self.assertEqual(ws[1]["resets_at"], int(datetime(2026, 10, 19, 4, 0, tzinfo=TOKYO).timestamp()))

    def test_time_only_rolls_to_tomorrow(self):
        ts = usage.parse_resets("9:50am (Asia/Tokyo)", NOW)
        self.assertEqual(ts, int(datetime(2026, 10, 17, 9, 50, tzinfo=TOKYO).timestamp()))

    def test_noon_midnight_and_garbage(self):
        self.assertEqual(usage.parse_resets("12am (Asia/Tokyo)", NOW), int(datetime(2026, 10, 17, 0, 0, tzinfo=TOKYO).timestamp()))
        self.assertIsNone(usage.parse_resets("whenever", NOW))
        self.assertEqual(usage.parse_usage_text("nothing here", NOW), [])


class TokensTest(unittest.TestCase):
    def test_sum_dedup_and_horizon(self):
        with tempfile.TemporaryDirectory() as d:
            now = time.time()
            iso = lambda age: datetime.fromtimestamp(now - age, ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")
            def line(mid, age, n):
                return json.dumps({"timestamp": iso(age), "message": {"id": mid, "usage": {"input_tokens": n, "output_tokens": 1}}})
            p = Path(d) / "proj" / "s.jsonl"
            p.parent.mkdir()
            p.write_text("\n".join([line("a", 60, 10), line("a", 60, 10), line("b", 6 * 3600, 100), "not json", "{}"]) + "\n")
            t = usage.local_tokens(now, Path(d))
            self.assertEqual(t["five_hour_tokens"], 11)      # a 只算一次 (10+1)
            self.assertEqual(t["seven_day_tokens"], 11 + 101)


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig = (config.STATE_DIR, config.SIDECAR_CWD, usage._run_claude_usage, usage.local_tokens)
        config.STATE_DIR = Path(self.tmp.name) / "state"
        config.SIDECAR_CWD = config.STATE_DIR / "sidecar"
        usage.local_tokens = lambda *a, **k: {"five_hour_tokens": 1, "seven_day_tokens": 2}
        self.calls = 0
        self.addCleanup(self._restore)

    def _restore(self):
        (config.STATE_DIR, config.SIDECAR_CWD, usage._run_claude_usage, usage.local_tokens) = self._orig

    def _ok(self):
        self.calls += 1
        return TXT

    def test_cache_hit_then_force(self):
        usage._run_claude_usage = self._ok
        self.assertTrue(usage.get()["ok"])
        usage.get()
        self.assertEqual(self.calls, 1)
        usage.get(force=True)
        self.assertEqual(self.calls, 2)

    def test_failure_falls_back_to_tokens_without_percent(self):
        def boom():
            raise RuntimeError("no claude")
        usage._run_claude_usage = boom
        r = usage.get()
        self.assertFalse(r["ok"])
        self.assertEqual(r["windows"], [])
        self.assertEqual(r["local"]["seven_day_tokens"], 2)

    def test_failure_keeps_last_good_as_stale(self):
        usage._run_claude_usage = self._ok
        usage.get()
        def boom():
            raise RuntimeError("x")
        usage._run_claude_usage = boom
        r = usage.get(force=True)
        self.assertTrue(r["ok"] and r["stale"])
        self.assertEqual(len(r["windows"]), 3)


def W(pct, ra=1000, wid="five_hour"):
    return {"id": wid, "label": "Current session", "percent": pct, "resets_text": "x", "resets_at": ra}


class EvaluateTest(unittest.TestCase):
    def test_once_per_threshold(self):
        a, st = usage_alert.evaluate([W(50)], {})
        self.assertEqual(a, [])
        a, st = usage_alert.evaluate([W(72)], st)
        self.assertEqual([x["threshold"] for x in a], [70])
        st["five_hour"]["fired"] = [70]                  # run() 发送成功后才记
        a, st = usage_alert.evaluate([W(75)], st)
        self.assertEqual(a, [])
        a, st = usage_alert.evaluate([W(91)], st)
        self.assertEqual([x["threshold"] for x in a], [90])

    def test_jump_straight_to_90_sends_one(self):
        a, _ = usage_alert.evaluate([W(95)], {})
        self.assertEqual(len(a), 1)
        self.assertEqual(a[0]["threshold"], 90)
        self.assertEqual(a[0]["_crossed"], [70, 90])

    def test_new_window_rearms(self):
        st = {"five_hour": {"resets_at": 1000, "fired": [70, 90]}}
        a, st = usage_alert.evaluate([W(10, ra=1000 + 5 * 3600)], st)
        self.assertEqual(st["five_hour"]["fired"], [])
        a, _ = usage_alert.evaluate([W(71, ra=1000 + 5 * 3600)], st)
        self.assertEqual(len(a), 1)

    def test_same_window_small_jitter_does_not_rearm(self):
        st = {"five_hour": {"resets_at": 1000, "fired": [70]}}
        a, _ = usage_alert.evaluate([W(80, ra=1030)], st)
        self.assertEqual(a, [])

    def test_windows_independent(self):
        a, _ = usage_alert.evaluate([W(80), W(10, wid="seven_day")], {})
        self.assertEqual([x["id"] for x in a], ["five_hour"])


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig = (config.STATE_DIR, usage_alert.notify)
        config.STATE_DIR = Path(self.tmp.name) / "state"
        self.sent = []
        usage_alert.notify = lambda t, b, cfg=None: (self.sent.append((t, b)) or ["fake"])
        self.addCleanup(lambda: (setattr(config, "STATE_DIR", self._orig[0]), setattr(usage_alert, "notify", self._orig[1])))
        self.cfg = {"usage_thresholds": [70, 90]}

    def res(self, pct):
        return {"ok": True, "windows": [W(pct)]}

    def test_run_dedups_across_invocations(self):
        usage_alert.run(cfg=self.cfg, res=self.res(72))
        usage_alert.run(cfg=self.cfg, res=self.res(74))
        self.assertEqual(len(self.sent), 1)
        usage_alert.run(cfg=self.cfg, res=self.res(92))
        self.assertEqual(len(self.sent), 2)
        usage_alert.run(cfg=self.cfg, res=self.res(93))
        self.assertEqual(len(self.sent), 2)

    def test_failed_delivery_retries_next_run(self):
        usage_alert.notify = lambda t, b, cfg=None: []
        usage_alert.run(cfg=self.cfg, res=self.res(80))
        usage_alert.notify = lambda t, b, cfg=None: (self.sent.append((t, b)) or ["fake"])
        usage_alert.run(cfg=self.cfg, res=self.res(80))
        self.assertEqual(len(self.sent), 1)

    def test_dry_run_sends_and_saves_nothing(self):
        usage_alert.run(dry_run=True, cfg=self.cfg, res=self.res(95))
        self.assertEqual(self.sent, [])
        self.assertFalse((config.STATE_DIR / "usage_alert.json").exists())

    def test_unavailable_data_does_not_alert(self):
        self.assertEqual(usage_alert.run(cfg=self.cfg, res={"ok": False, "windows": [], "error": "x"}), 1)
        self.assertEqual(self.sent, [])

    def test_format_has_no_secrets_just_label_and_percent(self):
        a, _ = usage_alert.evaluate([W(91)], {})
        t, b = usage_alert.format_alert(a[0])
        self.assertIn("90%", t)
        self.assertIn("91%", b)


if __name__ == "__main__":
    unittest.main()
