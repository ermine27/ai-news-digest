"""build.py の暗号化往復と記事取得判定のテスト"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.exceptions import InvalidTag

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build  # noqa: E402


def test_encrypt_decrypt_roundtrip():
    obj = {"articles": [{"id": "a1", "title": "日本語タイトル"}]}
    payload = build.encrypt(obj, "pass-123")
    assert payload["iter"] == build.PBKDF2_ITERATIONS
    assert build.decrypt(payload, "pass-123") == obj


def test_decrypt_with_wrong_passcode_fails():
    payload = build.encrypt({"articles": []}, "right")
    with pytest.raises(InvalidTag):
        build.decrypt(payload, "wrong")


def test_encrypt_uses_fresh_salt_and_iv():
    a = build.encrypt({"x": 1}, "p")
    b = build.encrypt({"x": 1}, "p")
    assert a["salt"] != b["salt"]
    assert a["iv"] != b["iv"]


def _entry(link, published):
    return {
        "link": link,
        "title": "t " + link,
        "summary": "<p>body  text</p>",
        "published_parsed": published.utctimetuple(),
    }


@pytest.fixture
def fake_feed(monkeypatch):
    def install(entries):
        monkeypatch.setattr(
            build.feedparser, "parse",
            lambda url, agent=None: SimpleNamespace(entries=entries),
        )
    return install


CFG = {"feeds": [{"name": "F", "url": "http://example.invalid/feed"}]}


def test_fetch_skips_seen_and_expired_and_dedups(fake_feed):
    now = datetime.now(timezone.utc)
    fake_feed([
        _entry("http://x/new", now - timedelta(hours=1)),
        _entry("http://x/new", now - timedelta(hours=1)),  # 同一URLの重複
        _entry("http://x/old", now - timedelta(hours=100)),  # max_age_hours(48) 超過
        _entry("http://x/seen", now - timedelta(hours=1)),
    ])
    seen_id = build.hashlib.sha1(b"http://x/seen").hexdigest()[:16]
    items = build.fetch_new_articles(CFG, {seen_id})
    assert [a["url"] for a in items] == ["http://x/new"]
    assert items[0]["text"] == "body text"  # タグ除去と空白の正規化


def test_fetch_respects_max_new_per_run_newest_first(fake_feed):
    now = datetime.now(timezone.utc)
    fake_feed([_entry(f"http://x/{i}", now - timedelta(hours=i)) for i in range(5)])
    items = build.fetch_new_articles({**CFG, "max_new_per_run": 2}, set())
    assert [a["url"] for a in items] == ["http://x/0", "http://x/1"]


def _batch(*ids):
    return [{"id": i} for i in ids]


def test_resolve_dup_of_known_and_in_batch_chain():
    result = {
        "b": {"dup_of": "k1"},  # 既出記事に重複
        "c": {"dup_of": "b"},  # 先頭側の重複記事 → 代表 k1 までたどる
        "d": {"dup_of": "a"},  # 先頭側の代表記事
    }
    roots = build.resolve_dup_of(_batch("a", "b", "c", "d"), result, {"k1"})
    assert roots == {"a": None, "b": "k1", "c": "k1", "d": "a"}


def test_resolve_dup_of_ignores_invalid_targets():
    result = {
        "a": {"dup_of": "a"},  # 自己参照
        "b": {"dup_of": "zzz"},  # 未知のid
        "c": {"dup_of": "d"},  # 入力の後ろ側(循環の元)
        "d": {"dup_of": None},
        "e": {"dup_of": 123},  # 型不正
    }
    roots = build.resolve_dup_of(_batch("a", "b", "c", "d", "e"), result, set())
    assert all(v is None for v in roots.values())


def test_known_for_dedup_excludes_duplicates_and_old():
    now = datetime.now(timezone.utc)
    arts = [
        {"id": "p", "title": "代表", "published": (now - timedelta(hours=1)).isoformat()},
        {"id": "d", "title": "重複", "dup_of": "p",
         "published": (now - timedelta(hours=1)).isoformat()},
        {"id": "o", "title": "古い", "published": (now - timedelta(hours=200)).isoformat()},
    ]
    assert build.known_for_dedup(arts, 72) == [{"id": "p", "title": "代表"}]


def test_build_prompt_embeds_known_and_articles():
    p = build.build_prompt([{"id": "n1"}], [], [{"id": "k1", "title": "既出"}])
    assert "k1" in p and "n1" in p
    assert "__KNOWN__" not in p and "__ARTICLES__" not in p


def test_fetch_retries_failed_article_beyond_max_age(fake_feed):
    now = datetime.now(timezone.utc)
    fake_feed([
        _entry("http://x/failed", now - timedelta(hours=100)),  # 48h 超だが失敗中
        _entry("http://x/other-old", now - timedelta(hours=100)),
        _entry("http://x/too-old", now - timedelta(days=20)),  # retention_days(14) 超
    ])
    failed = build.hashlib.sha1(b"http://x/failed").hexdigest()[:16]
    too_old = build.hashlib.sha1(b"http://x/too-old").hexdigest()[:16]
    items = build.fetch_new_articles(CFG, set(), frozenset({failed, too_old}))
    assert [a["url"] for a in items] == ["http://x/failed"]


def _stats(**kw):
    base = {"feeds": 5, "empty_feeds": [], "candidates": 3,
            "added": 0, "failed": 0, "given_up": 0}
    return {**base, **kw}


def test_is_alert_when_all_summaries_failed():
    assert build.is_alert(_stats(failed=3, added=0))
    assert not build.is_alert(_stats(failed=2, added=1))  # 一部成功は異常にしない
    assert not build.is_alert(_stats(candidates=0))  # 新規なし


def test_is_alert_when_all_feeds_empty():
    names = ["a", "b", "c", "d", "e"]
    assert build.is_alert(_stats(empty_feeds=names, candidates=0))
    assert not build.is_alert(_stats(empty_feeds=names[:1], candidates=0))  # 1 本だけは警告止まり


def test_write_report_outputs_flag_and_summary(tmp_path, monkeypatch):
    summary, output = tmp_path / "summary.md", tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    build.write_report(_stats(failed=2, empty_feeds=["TechCrunch AI"]))
    assert "alert=true" in output.read_text(encoding="utf-8")
    text = summary.read_text(encoding="utf-8")
    assert "失敗 2 件" in text and "TechCrunch AI" in text


def test_write_report_is_noop_outside_actions(monkeypatch):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    build.write_report(_stats())  # 例外にならなければよい


def test_fetch_reports_empty_feeds(monkeypatch):
    monkeypatch.setattr(
        build.feedparser, "parse", lambda url, agent=None: SimpleNamespace(entries=[])
    )
    empty = []
    assert build.fetch_new_articles(CFG, set(), empty_feeds=empty) == []
    assert empty == ["F"]


# ---- summarize_batch / main() を偽の Gemini クライアントで確認する ----

class FakeErr(Exception):
    def __init__(self, code):
        super().__init__(f"fake error {code}")
        self.code = code


class FakeClient:
    """モデルごとに用意した応答を順に返す。Exception ならそのまま送出する"""

    def __init__(self, script):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls = []
        self.models = self

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        step = self.script[model].pop(0)
        if isinstance(step, Exception):
            raise step
        return SimpleNamespace(text=step)


def ok_json(*ids, dup_of=None):
    return build.json.dumps(
        [{"id": i, "title_ja": "T-" + i, "summary_ja": "S", "stars": 3,
          "dup_of": (dup_of or {}).get(i)} for i in ids],
        ensure_ascii=False,
    )


def aid(url):
    return build.hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


BATCH = [{"id": "a1", "title": "t", "text": "x", "source": "S"}]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(build.time, "sleep", lambda s: None)


def test_summarize_retries_transient_error_then_succeeds():
    client = FakeClient({"m": [FakeErr(504), ok_json("a1")]})
    result = build.summarize_batch(client, "m", BATCH, [], [])
    assert "a1" in result
    assert client.calls == ["m", "m"]


def test_summarize_retries_invalid_json():
    client = FakeClient({"m": ["これはJSONではない", ok_json("a1")]})
    assert "a1" in build.summarize_batch(client, "m", BATCH, [], [])


def test_summarize_fails_fast_on_permanent_error():
    client = FakeClient({"m": [FakeErr(404)]})
    with pytest.raises(SystemExit):
        build.summarize_batch(client, "m", BATCH, [], [])
    assert client.calls == ["m"]  # 再試行しない


def test_summarize_returns_empty_when_all_attempts_fail():
    client = FakeClient({"m": [FakeErr(503)] * 4})
    assert build.summarize_batch(client, "m", BATCH, [], []) == {}
    assert client.calls == ["m"] * 4


def test_summarize_uses_fallback_after_primary_exhausted():
    client = FakeClient({"m": [FakeErr(503)] * 4, "fb": [ok_json("a1")]})
    result = build.summarize_batch(client, "m", BATCH, [], [], ["fb"])
    assert "a1" in result
    assert client.calls == ["m"] * 4 + ["fb"]


def test_summarize_skips_unusable_fallback_and_tries_next():
    client = FakeClient({
        "m": [FakeErr(504)] * 4,
        "bad": [FakeErr(404)],  # 存在しないモデル ID: 警告して次へ
        "fb2": [ok_json("a1")],
    })
    result = build.summarize_batch(client, "m", BATCH, [], [], ["bad", "fb2"])
    assert "a1" in result
    assert client.calls == ["m"] * 4 + ["bad", "fb2"]


def test_summarize_does_not_use_fallback_for_primary_permanent_error():
    client = FakeClient({"m": [FakeErr(401)], "fb": [ok_json("a1")]})
    with pytest.raises(SystemExit):
        build.summarize_batch(client, "m", BATCH, [], [], ["fb"])
    assert client.calls == ["m"]


PASS = "pw-for-test"
URLS = ["http://x/1", "http://x/2"]


@pytest.fixture
def env(tmp_path, monkeypatch):
    """build.main() を tmp_path 上で動かす準備。run(client) で 1 回実行する"""
    cfg = {
        "model": "m",
        "feeds": [{"name": "F", "url": "http://example.invalid/feed"}],
        "batch_size": 5,
        "max_summary_attempts": 2,
    }
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(build.yaml.safe_dump(cfg), encoding="utf-8")
    data_path = tmp_path / "docs" / "data.enc.json"
    github_output = tmp_path / "output.txt"
    monkeypatch.setattr(build, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(build, "DATA_PATH", data_path)
    monkeypatch.setattr(build, "PBKDF2_ITERATIONS", 1000)  # テストを速くする
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("APP_PASSCODE", PASS)
    monkeypatch.setenv("GITHUB_OUTPUT", str(github_output))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    now = datetime.now(timezone.utc)
    entries = [_entry(u, now - timedelta(hours=1 + i)) for i, u in enumerate(URLS)]
    monkeypatch.setattr(
        build.feedparser, "parse",
        lambda url, agent=None: SimpleNamespace(entries=entries),
    )

    def run(client):
        monkeypatch.setattr(build.genai, "Client", lambda **kw: client)
        github_output.write_text("", encoding="utf-8")
        build.main()

    def state():
        return build.decrypt(build.json.loads(data_path.read_text(encoding="utf-8")), PASS)

    return SimpleNamespace(run=run, state=state, data_path=data_path, output=github_output)


def test_main_records_failures_and_raises_alert(env):
    env.run(FakeClient({"m": [FakeErr(504)] * 4}))
    st = env.state()
    assert st["articles"] == []  # 失敗した記事は保存しない
    assert {v["count"] for v in st["failures"].values()} == {1}
    assert set(st["failures"]) == {aid(u) for u in URLS}
    assert "alert=true" in env.output.read_text(encoding="utf-8")


def test_main_gives_up_after_max_attempts(env):
    for _ in range(2):  # max_summary_attempts = 2
        env.run(FakeClient({"m": [FakeErr(504)] * 4}))
    assert {v["count"] for v in env.state()["failures"].values()} == {2}
    before = env.data_path.read_bytes()
    client = FakeClient({"m": []})
    env.run(client)  # 見送り対象は候補から外れ、API も呼ばない
    assert client.calls == []
    assert env.data_path.read_bytes() == before


def test_main_success_clears_failures_and_stores_dup_of(env):
    env.run(FakeClient({"m": [FakeErr(504)] * 4}))  # まず失敗を記録
    ids = [aid(u) for u in URLS]
    # 新しい順なので ids[0] が先頭。ids[1] は ids[0] の重複として返す
    env.run(FakeClient({"m": [ok_json(*ids, dup_of={ids[1]: ids[0]})]}))
    st = env.state()
    assert st["failures"] == {}
    by_id = {a["id"]: a for a in st["articles"]}
    assert set(by_id) == set(ids)
    assert "dup_of" not in by_id[ids[0]]
    assert by_id[ids[1]]["dup_of"] == ids[0]
    assert "alert=false" in env.output.read_text(encoding="utf-8")


def test_main_partial_failure_is_not_alert(env):
    ids = [aid(u) for u in URLS]
    env.run(FakeClient({"m": [ok_json(ids[0])]}))  # 2 件目は応答に含まれず失敗扱い
    st = env.state()
    assert [a["id"] for a in st["articles"]] == [ids[0]]
    assert set(st["failures"]) == {ids[1]}
    assert "alert=false" in env.output.read_text(encoding="utf-8")


def test_matches_filters_include_exclude_case_insensitive():
    feed = {"include": ["LLM", "エージェント"], "exclude": ["PR"]}
    assert build.matches_filters(feed, "New llm released", "")
    assert build.matches_filters(feed, "t", "AIエージェント入門")
    assert not build.matches_filters(feed, "Weather", "sunny")  # include に当たらない
    assert not build.matches_filters(feed, "LLM sponsored", "PR記事")  # exclude に当たる
    assert build.matches_filters({}, "anything", "")  # 指定なしは全件通す


def test_round_robin_shares_quota_across_lists():
    lists = [["a1", "a2", "a3", "a4"], ["b1"], ["c1", "c2"]]
    assert build.round_robin(lists, 5) == ["a1", "b1", "c1", "a2", "c2"]
    assert build.round_robin(lists, 100) == ["a1", "b1", "c1", "a2", "c2", "a3", "a4"]
    assert build.round_robin([], 3) == []


def test_fetch_gives_each_feed_a_fair_share(monkeypatch):
    now = datetime.now(timezone.utc)
    noisy = [_entry(f"http://noisy/{i}", now - timedelta(minutes=i)) for i in range(8)]
    quiet = [_entry(f"http://quiet/{i}", now - timedelta(hours=5 + i)) for i in range(2)]
    feeds = {"http://noisy.invalid": noisy, "http://quiet.invalid": quiet}
    monkeypatch.setattr(
        build.feedparser, "parse",
        lambda url, agent=None: SimpleNamespace(entries=feeds[url]),
    )
    cfg = {
        "feeds": [
            {"name": "Noisy", "url": "http://noisy.invalid", "limit": 3},
            {"name": "Quiet", "url": "http://quiet.invalid"},
        ],
        "max_new_per_run": 4,
    }
    items = build.fetch_new_articles(cfg, set())
    by_source = sorted(a["source"] for a in items)
    assert by_source == ["Noisy", "Noisy", "Quiet", "Quiet"]  # 投稿が新しい Noisy が独占しない


def test_fetch_applies_feed_keyword_filters(fake_feed):
    now = datetime.now(timezone.utc)
    entries = [_entry("http://x/ai", now), _entry("http://x/other", now)]
    entries[0]["title"] = "LLM news"
    entries[1]["title"] = "cooking"
    fake_feed(entries)
    cfg = {"feeds": [{"name": "F", "url": "u", "include": ["llm"]}]}
    assert [a["url"] for a in build.fetch_new_articles(cfg, set())] == ["http://x/ai"]
