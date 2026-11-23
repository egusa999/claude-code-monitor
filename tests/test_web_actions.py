"""PWA向け操作API(作成・終了・信頼確認の承認)のテスト。claude-session-ctlは偽物に差し替える。"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import claude_monitor as cm  # noqa: E402
import claude_monitor_web as web  # noqa: E402

# 実際の画面(claude 2.1.x、2026-11-21に確認): 「No, exit」が上でカーソルの初期位置、「Yes」が下
TRUST_HEAD = (" Accessing workspace:\n /root/onedrive/ClaudeCode/demo\n"
              " Quick safety check: Is this a project you created or one you trust?\n Security guide\n")
TRUST_NO = TRUST_HEAD + " ❯ No, exit\n   Yes, I trust this folder\n Enter to confirm · Esc to cancel\n"
TRUST_YES = TRUST_HEAD + "   No, exit\n ❯ Yes, I trust this folder\n Enter to confirm · Esc to cancel\n"
# 並びが逆・番号付きの版でも動くこと
REV_NO = TRUST_HEAD + "   1. Yes, I trust this folder\n ❯ 2. No, exit\n"
REV_YES = TRUST_HEAD + " ❯ 1. Yes, I trust this folder\n   2. No, exit\n"
INPUT_BOX = "─────\n❯ \n─────\n"


class FakeCtl:
    """captureで返す画面を順に切り替えられる偽ctl。Upでカーソルが動く。"""

    def __init__(self, panes=None, cursor_moves=True):
        self.calls: list[tuple] = []
        self.panes = list(panes or [])
        self.current = self.panes.pop(0) if self.panes else INPUT_BOX
        self.cursor_moves = cursor_moves
        self.fail: dict[str, str] = {}

    def __call__(self, cmd, **params):
        self.calls.append((cmd, params))
        if cmd in self.fail:
            raise cm.SessionCtlError(self.fail.pop(cmd))
        if cmd == cm.SESSION_CTL_CAPTURE:
            pane = self.current
            if self.panes:
                self.current = self.panes.pop(0)
            return pane
        if cmd == cm.SESSION_CTL_SEND_KEY and self.cursor_moves:
            moves = {("Down", TRUST_NO): TRUST_YES, ("Up", TRUST_YES): TRUST_NO,
                     ("Up", REV_NO): REV_YES, ("Down", REV_YES): REV_NO}
            self.current = moves.get((params.get("key"), self.current), self.current)
        if cmd == cm.SESSION_CTL_LISTDIR:
            return ["alpha", "beta", ""]
        if cmd == cm.SESSION_CTL_MD_STATUS:
            return {"rules.md": False}
        return None

    def keys(self):
        return [p["key"] for c, p in self.calls if c == cm.SESSION_CTL_SEND_KEY]


def session(sid="s1", name="demo", working=False, tokens=100):
    return cm.SessionInfo(pid=1, session_id=sid, name=name, cwd="/x", started_at=0,
                          context_tokens=tokens, working=working, attach_cmd=f"tmux attach -t ={name}")


class TrustTests(unittest.TestCase):
    def test_choice(self):
        self.assertEqual(cm.trust_cursor_choice(TRUST_YES), "yes")
        self.assertEqual(cm.trust_cursor_choice(TRUST_NO), "other")
        self.assertEqual(cm.trust_cursor_choice(REV_YES), "yes")
        self.assertEqual(cm.trust_cursor_choice(REV_NO), "other")
        self.assertIsNone(cm.trust_cursor_choice(INPUT_BOX))
        self.assertIsNone(cm.trust_cursor_choice(""))
        # カーソルが2つ見える(判定不能)ならNone
        self.assertIsNone(cm.trust_cursor_choice(TRUST_YES + " ❯ 2. No, exit\n"))
        # Yesの選択肢が見えない画面は判定不能
        self.assertIsNone(cm.trust_cursor_choice(TRUST_HEAD + " ❯ No, exit\n"))

    def test_moves_cursor_to_yes_then_enter(self):
        ctl = FakeCtl([TRUST_NO])
        cm.approve_trust("demo", ctl, sleep=lambda s: None)
        self.assertEqual(ctl.keys(), ["Down", "Enter"])

    def test_reversed_order_moves_up(self):
        ctl = FakeCtl([REV_NO])
        cm.approve_trust("demo", ctl, sleep=lambda s: None)
        self.assertEqual(ctl.keys(), ["Up", "Enter"])

    def test_already_on_yes(self):
        ctl = FakeCtl([TRUST_YES])
        cm.approve_trust("demo", ctl, sleep=lambda s: None)
        self.assertEqual(ctl.keys(), ["Enter"])

    def test_never_enter_when_cursor_stays_on_no(self):
        ctl = FakeCtl([TRUST_NO], cursor_moves=False)
        with self.assertRaises(cm.SessionCtlError):
            cm.approve_trust("demo", ctl, sleep=lambda s: None)
        self.assertNotIn("Enter", ctl.keys())

    def test_no_trust_screen_sends_nothing(self):
        ctl = FakeCtl([INPUT_BOX])
        with self.assertRaises(cm.SessionCtlError):
            cm.approve_trust("demo", ctl, sleep=lambda s: None)
        self.assertEqual(ctl.keys(), [])


class LocalCtlTests(unittest.TestCase):
    def _runner(self, stdout="", rc=0, exc=None):
        def run(argv, **kw):
            self.argv, self.input = argv, kw.get("input")
            if exc:
                raise exc
            return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr="boom")
        return run

    def test_ok(self):
        r = cm.call_session_ctl_local("listdir", runner=self._runner('{"ok": true, "result": [1]}'), base="/b")
        self.assertEqual(r, [1])
        self.assertEqual(self.argv, [cm.SESSION_CTL_COMMAND])
        self.assertEqual(json.loads(self.input), {"cmd": "listdir", "base": "/b"})

    def test_errors(self):
        for runner in (self._runner("not json", rc=1), self._runner('{"ok": false, "error": "x"}'),
                       self._runner("[1]"), self._runner(exc=subprocess.TimeoutExpired("c", 1)),
                       self._runner(exc=FileNotFoundError("c"))):
            with self.assertRaises(cm.SessionCtlError):
                cm.call_session_ctl_local("list", runner=runner)

    def test_ssh_version_unchanged(self):
        r = cm.call_session_ctl("root@h", "list", runner=self._runner('{"ok": true, "result": 5}'))
        self.assertEqual(r, 5)
        self.assertEqual(self.argv[0], "ssh")
        with self.assertRaises(cm.SessionCtlError):
            cm.call_session_ctl("-oProxy=x", "list", runner=self._runner("{}"))


def make_jobs(ctl, sessions):
    threads: list[threading.Thread] = []

    def spawn(fn):
        t = threading.Thread(target=fn, daemon=True)
        threads.append(t)
        t.start()
    jobs = web.ActionJobs(ctl=ctl, sessions=lambda: list(sessions), sleep=lambda s: time.sleep(0.001), spawn=spawn)
    return jobs, threads


def wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


class JobTests(unittest.TestCase):
    def test_create_trusted_folder_no_init(self):
        ctl = FakeCtl([INPUT_BOX])
        jobs, threads = make_jobs(ctl, [])
        job = jobs.create(cm.DEFAULT_CREATE_BASES[0], "alpha", "alpha", False)
        threads[0].join(5)
        self.assertEqual(job.stage, "done", job.error)
        start = [p for c, p in ctl.calls if c == cm.SESSION_CTL_START][0]
        self.assertEqual(start, {"name": "alpha", "dir": cm.DEFAULT_CREATE_BASES[0] + "/alpha"})
        self.assertEqual(ctl.keys(), [])

    def test_create_trust_then_approve(self):
        ctl = FakeCtl([TRUST_NO])
        jobs, threads = make_jobs(ctl, [])
        job = jobs.create(cm.DEFAULT_CREATE_BASES[0], "", "base1", False)  # 作成元自体
        self.assertTrue(wait_until(lambda: job.stage == "trust"))
        self.assertTrue(job.to_dict()["trust_pane"])
        self.assertEqual(ctl.keys(), [])  # 承認前は何も送らない
        jobs.answer_trust(job.id, True)
        threads[0].join(5)
        self.assertEqual(job.stage, "done", job.error)
        self.assertEqual(ctl.keys(), ["Down", "Enter"])
        with self.assertRaises(web.ApiError):  # 二重回答は拒否
            jobs.answer_trust(job.id, True)

    def test_create_trust_reject_stops(self):
        ctl = FakeCtl([TRUST_NO])
        jobs, threads = make_jobs(ctl, [])
        job = jobs.create(cm.DEFAULT_CREATE_BASES[0], "alpha", "alpha", True)
        self.assertTrue(wait_until(lambda: job.stage == "trust"))
        jobs.answer_trust(job.id, False)
        threads[0].join(5)
        self.assertEqual(job.stage, "done")
        self.assertIn((cm.SESSION_CTL_STOP, {"name": "alpha"}), ctl.calls)
        self.assertNotIn("Enter", ctl.keys())

    def test_create_start_error(self):
        ctl = FakeCtl()
        ctl.fail[cm.SESSION_CTL_START] = "作業フォルダが許可範囲外"
        jobs, threads = make_jobs(ctl, [])
        job = jobs.create(cm.DEFAULT_CREATE_BASES[0], "alpha", "alpha", False)
        threads[0].join(5)
        self.assertEqual(job.stage, "error")
        self.assertIn("許可範囲外", job.error)

    def test_create_validation_and_duplicates(self):
        jobs, _ = make_jobs(FakeCtl([TRUST_NO]), [])
        base = cm.DEFAULT_CREATE_BASES[0]
        for args in (("/etc", "x", "x"), (base, "../x", "x"), (base, "a/b", "x"), (base, "..", "x"),
                     (base, 5, "x"), (base, "x", "日本語"), (base, "x", ""), (base, "x", None), (base, "x", "-a")):
            with self.assertRaises(web.ApiError, msg=args):
                jobs.create(*args, False)
        jobs.create(base, "alpha", "dup", False)
        self.assertTrue(wait_until(lambda: jobs.list()[0]["stage"] == "trust"))
        with self.assertRaises(web.ApiError) as cx:
            jobs.create(base, "alpha", "dup", False)
        self.assertEqual(cx.exception.status, 409)

    def test_end_checks(self):
        jobs, _ = make_jobs(FakeCtl(), [session(working=True), session("s2", "bg1")])
        with self.assertRaises(web.ApiError) as cx:
            jobs.end("s1")
        self.assertEqual(cx.exception.status, 409)
        with self.assertRaises(web.ApiError) as cx:
            jobs.end("missing")
        self.assertEqual(cx.exception.status, 404)
        jobs2, _ = make_jobs(FakeCtl(), [cm.SessionInfo(1, "s3", "b", "/", 0, attach_cmd="claude attach j1")])
        with self.assertRaises(web.ApiError) as cx:
            jobs2.end("s3")
        self.assertEqual(cx.exception.status, 400)

    def test_end_context_zero_just_stops(self):
        ctl = FakeCtl()
        jobs, threads = make_jobs(ctl, [session(tokens=0)])
        job = jobs.end("s1")
        threads[0].join(5)
        self.assertEqual(job.stage, "done", job.error)
        self.assertEqual([c for c, _ in ctl.calls], [cm.SESSION_CTL_STOP])

    def test_unknown_job(self):
        jobs, _ = make_jobs(FakeCtl(), [])
        with self.assertRaises(web.ApiError) as cx:
            jobs.get("nope")
        self.assertEqual(cx.exception.status, 404)


class FakeClaude:
    """/session-end と /clear に反応する偽のセッション(偽ctl+一覧+時計)。
    /session-end: 送信後しばらく作業中→待機。/clear: コンテキスト0+新しいsession_id(Claudeの/clearと同じ)。"""

    def __init__(self, tokens=100, end_never_starts=False, clear_never_empties=False):
        self.now = 0.0
        self.info = session(tokens=tokens)
        self.ctl = FakeCtl()
        self.end_never_starts = end_never_starts
        self.clear_never_empties = clear_never_empties
        self.working_until = None
        self.last_text = None
        orig = self.ctl.__call__

        def ctl(cmd, **params):
            r = orig(cmd, **params)
            if cmd == cm.SESSION_CTL_SEND_TEXT:
                self.last_text = params["text"]
            if cmd == cm.SESSION_CTL_SEND_KEY and params.get("key") == "Enter":
                if self.last_text == cm.SESSION_END_COMMAND and not self.end_never_starts:
                    self.working_until = self.now + 5
                elif self.last_text == cm.CLEAR_COMMAND and not self.clear_never_empties:
                    self.info = cm.SessionInfo(pid=1, session_id="s1-new", name="demo", cwd="/x", started_at=0,
                                               context_tokens=0, attach_cmd="tmux attach -t =demo")
            return r
        self.call = ctl

    def sleep(self, sec):
        self.now += sec

    def clock(self):
        return self.now

    def find(self):
        self.info.working = self.working_until is not None and self.now < self.working_until
        return self.info

    def texts(self):
        return [p["text"] for c, p in self.ctl.calls if c == cm.SESSION_CTL_SEND_TEXT]


class ClearWorkflowTests(unittest.TestCase):
    def run_clear(self, fake):
        return cm.clear_session_workflow("demo", "s1", fake.call, fake.find, fake.sleep, fake.clock)

    def test_session_end_then_clear(self):
        fake = FakeClaude()
        self.assertTrue(self.run_clear(fake))
        self.assertEqual(fake.texts(), [cm.SESSION_END_COMMAND, cm.CLEAR_COMMAND])
        self.assertNotIn(cm.SESSION_CTL_STOP, [c for c, _ in fake.ctl.calls])  # セッションは削除しない
        self.assertEqual(fake.info.context_tokens, 0)

    def test_context_zero_sends_nothing(self):
        fake = FakeClaude(tokens=0)
        self.assertFalse(self.run_clear(fake))
        self.assertEqual(fake.ctl.calls, [])

    def test_missing_session(self):
        fake = FakeClaude()
        with self.assertRaises(cm.SessionCtlError):
            cm.clear_session_workflow("demo", "s1", fake.call, lambda: None, fake.sleep, fake.clock)
        self.assertEqual(fake.ctl.calls, [])

    def test_no_clear_when_session_end_does_not_finish(self):
        fake = FakeClaude(end_never_starts=True)
        with self.assertRaises(cm.SessionCtlError) as cx:
            self.run_clear(fake)
        self.assertIn(cm.CLEAR_COMMAND, str(cx.exception))
        self.assertEqual(fake.texts(), [cm.SESSION_END_COMMAND])  # /clear は送らない

    def test_pending_input_blocks_everything(self):
        fake = FakeClaude()
        fake.ctl.current = "─────\n❯ typing\n─────\n"
        with self.assertRaises(cm.SessionCtlError):
            self.run_clear(fake)
        self.assertEqual(fake.texts(), [])

    def test_clear_timeout(self):
        fake = FakeClaude(clear_never_empties=True)
        with self.assertRaises(cm.SessionCtlError):
            self.run_clear(fake)
        self.assertEqual(fake.texts(), [cm.SESSION_END_COMMAND, cm.CLEAR_COMMAND])


class ClearJobTests(unittest.TestCase):
    def test_clear_job(self):
        fake = FakeClaude()
        jobs = web.ActionJobs(ctl=fake.call, sessions=lambda: [fake.find()], sleep=fake.sleep,
                              clock=fake.clock, spawn=lambda fn: fn())
        job = jobs.clear("s1")
        self.assertEqual((job.kind, job.stage), ("clear", "done"), job.error)
        self.assertIn("クリアしました", job.message)
        self.assertEqual(fake.texts(), [cm.SESSION_END_COMMAND, cm.CLEAR_COMMAND])

    def test_clear_checks(self):
        jobs, _ = make_jobs(FakeCtl(), [session(working=True), session("s2", "empty", tokens=0),
                                        cm.SessionInfo(1, "s3", "b", "/", 0, context_tokens=5,
                                                       attach_cmd="claude attach j1")])
        for sid, status in (("s1", 409), ("s2", 409), ("s3", 400), ("missing", 404)):
            with self.assertRaises(web.ApiError, msg=sid) as cx:
                jobs.clear(sid)
            self.assertEqual(cx.exception.status, status, sid)
        self.assertEqual(jobs.list(), [])

    def test_clear_and_end_are_exclusive(self):
        ctl = FakeCtl()
        jobs = web.ActionJobs(ctl=ctl, sessions=lambda: [session()], spawn=lambda fn: None)
        jobs.clear("s1")
        with self.assertRaises(web.ApiError) as cx:
            jobs.end("s1")
        self.assertEqual(cx.exception.status, 409)


class HttpTests(unittest.TestCase):
    TOKEN = "t" * 40

    @classmethod
    def setUpClass(cls):
        cls.ctl = FakeCtl([TRUST_NO])
        web.MonitorHandler.token = cls.TOKEN
        web.MonitorHandler.jobs, _ = make_jobs(cls.ctl, [])
        cls.server = web.MonitorServer(("127.0.0.1", 0), web.MonitorHandler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def req(self, path, body=None, token=TOKEN, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data)
        if token:
            r.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_auth_required(self):
        for path, body in (("/api/create/bases", None), ("/api/jobs", None), ("/api/create", {}), ("/api/end", {}),
                           ("/api/clear", {})):
            self.assertEqual(self.req(path, body, token=None)[0], 401)
            self.assertEqual(self.req(path, body, token="wrong" * 10)[0], 401)

    def test_bases_listdir_md(self):
        status, body = self.req("/api/create/bases")
        self.assertEqual(status, 200)
        self.assertEqual([b["path"] for b in body["bases"]], list(cm.DEFAULT_CREATE_BASES))
        status, body = self.req("/api/create/listdir", {"base": cm.DEFAULT_CREATE_BASES[0]})
        self.assertEqual((status, body["folders"]), (200, ["alpha", "beta"]))
        self.assertEqual(self.req("/api/create/listdir", {"base": "/root"})[0], 400)
        status, body = self.req("/api/create/md_status", {"base": cm.DEFAULT_CREATE_BASES[0], "folder": "alpha"})
        self.assertEqual((status, body["md"]), (200, {"rules.md": False}))

    def test_bad_bodies(self):
        self.assertEqual(self.req("/api/create", raw=b"{nope")[0], 400)
        self.assertEqual(self.req("/api/create", raw=b"[1,2]")[0], 400)
        self.assertEqual(self.req("/api/create", raw=b"x" * (web.MAX_BODY_BYTES + 1))[0], 413)
        self.assertEqual(self.req("/api/end", {"session_id": ""})[0], 400)
        self.assertEqual(self.req("/api/clear", {"session_id": ""})[0], 400)
        self.assertEqual(self.req("/api/clear", {"session_id": 5})[0], 400)
        self.assertEqual(self.req("/api/clear", {"session_id": "missing"})[0], 404)
        self.assertEqual(self.req("/api/jobs/zzz")[0], 404)
        self.assertEqual(self.req("/api/jobs/zzz/trust", {"approve": True})[0], 404)

    def test_create_flow_over_http(self):
        status, body = self.req("/api/create", {"base": cm.DEFAULT_CREATE_BASES[0], "folder": "beta",
                                                "name": "httpjob", "init": False})
        self.assertEqual(status, 200, body)
        jid = body["job"]["id"]
        self.assertTrue(wait_until(lambda: self.req(f"/api/jobs/{jid}")[1]["job"]["stage"] == "trust"))
        status, body = self.req(f"/api/jobs/{jid}/trust", {"approve": True})
        self.assertEqual(status, 200, body)
        self.assertTrue(wait_until(lambda: self.req(f"/api/jobs/{jid}")[1]["job"]["done"]))
        self.assertIn(jid, [j["id"] for j in self.req("/api/jobs")[1]["jobs"]])


if __name__ == "__main__":
    unittest.main()
