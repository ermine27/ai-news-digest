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
