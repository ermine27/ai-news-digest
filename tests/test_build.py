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
