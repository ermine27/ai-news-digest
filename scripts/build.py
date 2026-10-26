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

PROMPT = """あなたは技術ニュース（AI・セキュリティー・プログラミングなど）の編集者です。
以下のJSON配列の各記事について、日本語で次の項目を持つJSON配列だけを返してください。
- id: 入力のidをそのまま返す
- title_ja: 日本語タイトル（40字以内）
- summary_ja: 日本語要約（2〜3文、140字以内。固有名詞は原語のまま。入力にない情報は書かない）
- stars: 注目度（1〜5の整数）
- dup_of: 同じ出来事を扱う記事が「既出の記事」または入力の先頭側にあれば、その記事のid。なければ null（別の切り口・続報は重複としない）

注目度の基準（入力の category は分野の目安。どの分野でも同じ基準で採点する）:
5=業界・社会全体に影響する出来事（主要製品・規制・大型提携・広範囲に及ぶ重大インシデントや脆弱性）
4=主要企業の新製品・重要な研究成果・実害のある脆弱性や攻撃
3=実務に役立つ技術情報・ツール更新・実践的な解説
2=小規模な更新・一般的な解説記事
1=宣伝色が強い・重要度が低い
__INTERESTS__
既出の記事（dup_of の候補）:
__KNOWN__

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


def matches_filters(feed: dict, title: str, text: str) -> bool:
    """feed の include(いずれかを含む)と exclude(どれも含まない)で記事を絞る(大小文字は無視)"""
    haystack = (title + " " + text).lower()
    include = [str(k).lower() for k in feed.get("include") or []]
    exclude = [str(k).lower() for k in feed.get("exclude") or []]
    if include and not any(k in haystack for k in include):
        return False
    return not any(k in haystack for k in exclude)


def round_robin(lists: list, limit: int) -> list:
    """各リストの先頭から順に 1 件ずつ取り、limit 件まで集める(1 つのフィードが枠を独占しない)"""
    picked = []
    depth = 0
    while len(picked) < limit and any(depth < len(l) for l in lists):
        for l in lists:
            if depth < len(l) and len(picked) < limit:
                picked.append(l[depth])
        depth += 1
    return picked


def fetch_new_articles(
    cfg: dict,
    seen_ids: set,
    retry_ids: frozenset = frozenset(),
    empty_feeds: list | None = None,
) -> list:
    """RSS から未取得の記事を集める

    retry_ids(要約に失敗中の記事)は max_age_hours を超えていても、
    retention_days の範囲なら再び対象にする。
    記事を取れなかったフィード名は empty_feeds に追加する。
    フィードごとに include / exclude で絞り、limit 件(未指定は per_feed_limit)までに抑え、
    全体は max_new_per_run まで各フィードから均等に集める。
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=cfg.get("max_age_hours", 48))
    retry_cutoff = now - timedelta(days=cfg.get("retention_days", 14))
    found_ids = set()
    per_feed = []
    socket.setdefaulttimeout(30)  # 応答しないフィードで固まらないようにする
    for feed in cfg["feeds"]:
        print(f"取得中: {feed['name']}", flush=True)
        parsed = feedparser.parse(feed["url"], agent="ai-news-bot/1.0")
        if not parsed.entries:
            print(f"[警告] 記事なし/取得失敗: {feed['name']}")
            if empty_feeds is not None:
                empty_feeds.append(feed["name"])
            continue
        picked = []
        for entry in parsed.entries[: cfg.get("per_feed_limit", 10)]:
            url = entry.get("link")
            if not url:
                continue
            aid = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
            if aid in seen_ids or aid in found_ids:
                continue
            ts = entry.get("published_parsed") or entry.get("updated_parsed")
            published = datetime(*ts[:6], tzinfo=timezone.utc) if ts else now
            if published < cutoff and not (aid in retry_ids and published >= retry_cutoff):
                continue
            text = re.sub(r"<[^>]+>", " ", entry.get("summary", ""))
            text = re.sub(r"\s+", " ", text).strip()[:1000]
            title = entry.get("title", "(no title)")
            if not matches_filters(feed, title, text):
                continue
            found_ids.add(aid)
            picked.append(
                {
                    "id": aid,
                    "url": url,
                    "source": feed["name"],
                    "title": title,
                    "text": text,
                    "category": feed.get("category", ""),
                    "published": published.isoformat(),
                }
            )
        picked.sort(key=lambda a: a["published"], reverse=True)
        per_feed.append(picked[: feed.get("limit", cfg.get("per_feed_limit", 10))])
    articles = round_robin(per_feed, cfg.get("max_new_per_run", 30))
    return sorted(articles, key=lambda a: a["published"], reverse=True)


def format_interests(interests) -> str:
    """関心領域を文にする。リスト(全分野共通)とカテゴリー別の辞書の両方を受け付ける"""
    def clean(topics):
        return [str(t).strip() for t in (topics or []) if str(t).strip()]

    if isinstance(interests, dict):
        parts = [f"{cat}: " + "、".join(clean(ts)) for cat, ts in interests.items() if clean(ts)]
        return "\n".join(parts)
    return "、".join(clean(interests))


def build_prompt(payload: list, interests, known: list) -> str:
    """要約プロンプトを組み立てる。interests があれば採点の補正指示を加える"""
    topics = format_interests(interests)
    if topics:
        note = (
            "\n読者の関心領域（カテゴリー別の場合は、記事の category に対応する行を使う）:\n"
            + topics + "\n"
            "上の基準で採点したうえで、関心領域に直接関わる記事は1段階上げ、"
            "無関係な記事は1段階下げてください（結果は1〜5に収める）。\n"
        )
    else:
        note = ""
    return (
        PROMPT.replace("__INTERESTS__", note)
        .replace("__KNOWN__", json.dumps(known, ensure_ascii=False))
        .replace("__ARTICLES__", json.dumps(payload, ensure_ascii=False))
    )


def known_for_dedup(articles: list, window_hours: int, limit: int = 60) -> list:
    """重複判定の候補にする既出記事(代表記事のみ・新しい順)を id とタイトルだけで返す"""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    recent = [a for a in articles if not a.get("dup_of") and a["published"] >= cutoff]
    recent.sort(key=lambda a: a["published"], reverse=True)
    return [{"id": a["id"], "title": a["title"]} for a in recent[:limit]]


def resolve_dup_of(batch: list, result: dict, known_ids: set) -> dict:
    """各記事の代表記事 id を返す(なければ None)。

    Gemini が返した dup_of は、既出記事か入力の先頭側の記事を指す場合だけ採用し、
    指す先がさらに重複なら代表記事までたどる。自己参照・未知の id は無視する。
    """
    roots = {}
    for a in batch:
        target = (result.get(a["id"]) or {}).get("dup_of")
        if not isinstance(target, str) or target == a["id"]:
            roots[a["id"]] = None
        elif target in known_ids:
            roots[a["id"]] = target
        elif target in roots:  # 入力の先頭側(処理済み)の記事
            roots[a["id"]] = roots[target] or target
        else:
            roots[a["id"]] = None
    return roots


class PermanentError(Exception):
    """再試行しても解消しない API エラー(モデルID・APIキーの誤りなど)"""


def call_model(client, model: str, prompt: str, attempts: int) -> dict | None:
    """1 つのモデルを上限付きでリトライして呼ぶ。一時エラーで尽きたら None"""
    for attempt in range(attempts):
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
                raise PermanentError(str(exc)) from exc
            wait = 20 * (attempt + 1)
            print(f"[警告] 要約失敗 {model} ({attempt + 1}/{attempts}): {exc} → {wait}秒待機")
            time.sleep(wait)
    return None


def summarize_batch(
    client, model: str, batch: list, interests: list, known: list, fallback_models=()
) -> dict:
    """記事のまとまりを 1 リクエストで要約・翻訳・採点する。失敗時は空辞書

    主モデルが一時エラーで尽きたときだけ fallback_models を順に試す。
    主モデルの永続エラーは即失敗、フォールバック側の永続エラーは警告して次へ進む。
    """
    payload = [
        {
            "id": a["id"],
            "title": a["title"],
            "text": a["text"],
            "source": a["source"],
            "category": a.get("category", ""),
        }
        for a in batch
    ]
    prompt = build_prompt(payload, interests, known)
    try:
        result = call_model(client, model, prompt, 4)
    except PermanentError as exc:
        sys.exit(f"[エラー] 再試行しても解消しない失敗です（モデルID・APIキーを確認）: {exc}")
    if result is not None:
        return result
    for fb in fallback_models:
        print(f"[警告] {model} が失敗し続けたため {fb} で再試行します", flush=True)
        try:
            result = call_model(client, fb, prompt, 2)
        except PermanentError as exc:
            print(f"[警告] フォールバック {fb} は使えません（モデルIDを確認）: {exc}")
            continue
        if result is not None:
            return result
    return {}


def is_alert(stats: dict) -> bool:
    """要約が全件失敗、または全フィードが空のとき True"""
    all_failed = stats["failed"] > 0 and stats["added"] == 0
    all_feeds_empty = stats["feeds"] > 0 and len(stats["empty_feeds"]) == stats["feeds"]
    return all_failed or all_feeds_empty


def build_report(stats: dict) -> str:
    """ジョブサマリー(Markdown)を作る"""
    lines = [
        "## 実行結果",
        "",
        f"- 新規候補: {stats['candidates']} 件",
        f"- 要約: 成功 {stats['added']} 件 / 失敗 {stats['failed']} 件",
        f"- 試行上限で見送り中: {stats['given_up']} 件",
    ]
    if stats["empty_feeds"]:
        lines.append("- 記事を取得できなかったフィード: " + "、".join(stats["empty_feeds"]))
    if is_alert(stats):
        lines += ["", "**⚠ 要約が全件失敗、または全フィードが空でした。ログを確認してください。**"]
    return "\n".join(lines) + "\n"


def write_report(stats: dict) -> None:
    """GitHub Actions 上なら、ジョブサマリーと異常フラグを書き出す(ローカルでは何もしない)"""
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(build_report(stats))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as f:
            f.write(f"alert={'true' if is_alert(stats) else 'false'}\n")


def main() -> None:
    stats = {
        "feeds": 0, "empty_feeds": [], "candidates": 0,
        "added": 0, "failed": 0, "given_up": 0,
    }
    try:
        run(stats)
    finally:
        write_report(stats)


def run(stats: dict) -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    passcode = os.environ.get("APP_PASSCODE")
    if not api_key or not passcode:
        sys.exit("環境変数 GEMINI_API_KEY / APP_PASSCODE が未設定です。")

    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    stats["feeds"] = len(cfg["feeds"])
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
    stats["given_up"] = len(given_up)
    if given_up:
        print(f"試行上限に達して見送り中: {len(given_up)} 件")
    seen = {a["id"] for a in articles} | given_up

    retry_ids = frozenset(failures) - given_up
    new_items = fetch_new_articles(cfg, seen, retry_ids, stats["empty_feeds"])
    stats["candidates"] = len(new_items)
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
        known = known_for_dedup(articles, cfg.get("dedup_window_hours", 72))
        result = summarize_batch(
            client,
            cfg["model"],
            chunk,
            cfg.get("interests") or [],
            known,
            cfg.get("fallback_models") or [],
        )
        dups = resolve_dup_of(chunk, result, {k["id"] for k in known})
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
            item = {
                "id": a["id"],
                "url": a["url"],
                "source": a["source"],
                "title": str(r.get("title_ja") or a["title"]),
                "summary": str(r.get("summary_ja") or ""),
                "stars": stars,
                "published": a["published"],
            }
            if a.get("category"):
                item["category"] = a["category"]
            if dups.get(a["id"]):
                item["dup_of"] = dups[a["id"]]
            articles.append(item)
            added += 1
        time.sleep(5)  # RPM 対策の間隔

    stats["added"], stats["failed"] = added, failed
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
