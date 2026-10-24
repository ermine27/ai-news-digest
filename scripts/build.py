"""AIニュースの取得・要約・暗号化スクリプト"""
import base64
import hashlib
import json
import os
import re
import socket
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import yaml
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from google import genai
from google.genai import types

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yml"
DATA_PATH = ROOT / "docs" / "data.enc.json"
PBKDF2_ITERATIONS = 600_000  # index.html 側は data.enc.json の iter を参照する

PROMPT = """あなたはAI分野のニュース編集者です。
以下のJSON配列の各記事について、日本語で次の項目を持つJSON配列だけを返してください。
- id: 入力のidをそのまま返す
- title_ja: 日本語タイトル（40字以内）
- summary_ja: 日本語要約（2〜3文、140字以内。固有名詞は原語のまま。入力にない情報は書かない）
- stars: 注目度（1〜5の整数）

注目度の基準:
5=業界全体に影響する発表（主要モデル・規制・大型提携）
4=主要企業の新製品・重要な研究成果
3=実務に役立つ技術情報・ツール更新
2=小規模な更新・解説記事
1=宣伝色が強い・重要度が低い

入力:
__ARTICLES__
"""


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text)


def derive_key(passcode: str, salt: bytes, iterations: int) -> bytes:
    """パスコードから AES-256 鍵を導出する"""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations
    )
    return kdf.derive(passcode.encode("utf-8"))


def encrypt(obj: dict, passcode: str) -> dict:
    """辞書を AES-GCM で暗号化する（ct は 暗号文+タグ。Web Crypto と互換）"""
    salt = os.urandom(16)
    iv = os.urandom(12)
    key = derive_key(passcode, salt, PBKDF2_ITERATIONS)
    plain = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    ct = AESGCM(key).encrypt(iv, plain, None)
    return {
        "v": 1,
        "iter": PBKDF2_ITERATIONS,
        "salt": b64e(salt),
        "iv": b64e(iv),
        "ct": b64e(ct),
    }


def decrypt(payload: dict, passcode: str) -> dict:
    key = derive_key(passcode, b64d(payload["salt"]), int(payload["iter"]))
    plain = AESGCM(key).decrypt(b64d(payload["iv"]), b64d(payload["ct"]), None)
    return json.loads(plain.decode("utf-8"))


def load_state(passcode: str) -> dict:
    """既存の暗号化データを復号して読み込む（初回は空）"""
    if not DATA_PATH.exists():
        return {"articles": []}
    try:
        payload = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        return decrypt(payload, passcode)
    except InvalidTag:
        sys.exit(
            "復号に失敗しました。APP_PASSCODE を変更した場合は "
            "docs/data.enc.json を削除して再実行してください。"
        )


def fetch_new_articles(cfg: dict, seen_ids: set) -> list:
    """RSS から未取得の記事を集める"""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=cfg.get("max_age_hours", 48))
    found = {}
    socket.setdefaulttimeout(30)  # 応答しないフィードで固まらないようにする
    for feed in cfg["feeds"]:
        print(f"取得中: {feed['name']}", flush=True)
        parsed = feedparser.parse(feed["url"], agent="ai-news-bot/1.0")
        if not parsed.entries:
            print(f"[警告] 記事なし/取得失敗: {feed['name']}")
            continue
        for entry in parsed.entries[: cfg.get("per_feed_limit", 10)]:
            url = entry.get("link")
            if not url:
                continue
            aid = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
            if aid in seen_ids or aid in found:
                continue
            ts = entry.get("published_parsed") or entry.get("updated_parsed")
            published = datetime(*ts[:6], tzinfo=timezone.utc) if ts else now
            if published < cutoff:
                continue
            text = re.sub(r"<[^>]+>", " ", entry.get("summary", ""))
            text = re.sub(r"\s+", " ", text).strip()[:1000]
            found[aid] = {
                "id": aid,
                "url": url,
                "source": feed["name"],
                "title": entry.get("title", "(no title)"),
                "text": text,
                "published": published.isoformat(),
            }
    articles = sorted(found.values(), key=lambda a: a["published"], reverse=True)
    return articles[: cfg.get("max_new_per_run", 30)]


def summarize_batch(client, model: str, batch: list) -> dict:
    """記事のまとまりを 1 リクエストで要約・翻訳・採点する。失敗時は空辞書"""
    payload = [
        {"id": a["id"], "title": a["title"], "text": a["text"], "source": a["source"]}
        for a in batch
    ]
    prompt = PROMPT.replace("__ARTICLES__", json.dumps(payload, ensure_ascii=False))
    for attempt in range(4):
        try:
            resp = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", temperature=0.2
                ),
            )
            items = json.loads(resp.text)
            return {i["id"]: i for i in items if isinstance(i, dict) and "id" in i}
        except Exception as exc:  # 429・JSON不正などを含めて再試行する
            if getattr(exc, "code", None) in (400, 401, 403, 404):
                sys.exit(f"[エラー] 再試行しても解消しない失敗です（モデルID・APIキーを確認）: {exc}")
            wait = 20 * (attempt + 1)
            print(f"[警告] 要約失敗({attempt + 1}/4): {exc} → {wait}秒待機")
            time.sleep(wait)
    return {}


def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    passcode = os.environ.get("APP_PASSCODE")
    if not api_key or not passcode:
        sys.exit("環境変数 GEMINI_API_KEY / APP_PASSCODE が未設定です。")

    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    state = load_state(passcode)
    articles = state.get("articles", [])
    now = datetime.now(timezone.utc)
    keep_from = (now - timedelta(days=cfg.get("retention_days", 14))).isoformat()
    # 要約に失敗した記事の試行回数（記事本体は保存しない）。保持期間を過ぎたら捨てる
    failures = {
        k: v for k, v in state.get("failures", {}).items() if v["last"] >= keep_from
    }
    failures_before = json.dumps(failures, sort_keys=True)
    max_attempts = cfg.get("max_summary_attempts", 3)
    given_up = {k for k, v in failures.items() if v["count"] >= max_attempts}
    if given_up:
        print(f"試行上限に達して見送り中: {len(given_up)} 件")
    seen = {a["id"] for a in articles} | given_up

    new_items = fetch_new_articles(cfg, seen)
    print(f"新規候補: {len(new_items)} 件")
    if not new_items:
        print("新規記事なし。data.enc.json は更新しません。")
        return

    client = genai.Client(
        api_key=api_key, http_options=types.HttpOptions(timeout=60_000)  # ミリ秒
    )
    size = cfg.get("batch_size", 10)
    added = 0
    failed = 0
    for i in range(0, len(new_items), size):
        chunk = new_items[i : i + size]
        print(f"要約中: {i + 1}〜{i + len(chunk)} 件目", flush=True)
        result = summarize_batch(client, cfg["model"], chunk)
        for a in chunk:
            r = result.get(a["id"])
            if not r:
                # 記事は保存しない → 次回実行で再試行される（回数は上限まで数える）
                n = failures.get(a["id"], {}).get("count", 0) + 1
                failures[a["id"]] = {"count": n, "last": now.isoformat()}
                failed += 1
                continue
            failures.pop(a["id"], None)
            try:
                stars = max(1, min(5, int(r.get("stars", 3))))
            except (TypeError, ValueError):
                stars = 3
            articles.append(
                {
                    "id": a["id"],
                    "url": a["url"],
                    "source": a["source"],
                    "title": str(r.get("title_ja") or a["title"]),
                    "summary": str(r.get("summary_ja") or ""),
                    "stars": stars,
                    "published": a["published"],
                }
            )
            added += 1
        time.sleep(5)  # RPM 対策の間隔

    print(f"要約結果: 成功 {added} 件 / 失敗 {failed} 件", flush=True)
    for aid, f in failures.items():
        if f["count"] >= max_attempts and aid not in given_up:
            print(f"[警告] 試行上限に達したため今後は見送ります: {aid}")

    if added == 0 and json.dumps(failures, sort_keys=True) == failures_before:
        print("要約に成功した記事がなく、失敗記録も変わりません。data.enc.json は更新しません。")
        return

    articles = [a for a in articles if a["published"] >= keep_from]
    articles.sort(key=lambda a: a["published"], reverse=True)

    out = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "articles": articles,
        "failures": failures,
    }
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(encrypt(out, passcode)), encoding="utf-8")
    print(f"追加: {added} 件 / 保持合計: {len(articles)} 件")


if __name__ == "__main__":
    main()
