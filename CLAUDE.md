# AI News Digest

RSS を取得し、Gemini で日本語要約・採点し、AES-GCM で暗号化して GitHub Pages で配信する個人用アプリ。設計判断・構成図は [DeploymentGuide.md](DeploymentGuide.md)、使い方は [README.md](README.md) を参照。

## 構成

- `scripts/build.py`: 取得・要約・暗号化(GitHub Actions で実行)
- `docs/index.html`: 閲覧画面(単一ファイル。外部ライブラリーなし)
- `docs/data.enc.json`: 暗号化データ。**Actions が自動生成するため手で編集しない**
- `config.yml`: フィード・モデル・件数の設定
- `.github/workflows/update.yml`: 毎日 9:30 JST の実行とデプロイ

## 絶対に守ること(セキュリティー)

- このリポジトリーは **Public**。API キー、パスコード、平文の記事データをコミットしない。
- `GEMINI_API_KEY` と `APP_PASSCODE` は GitHub の Repository secrets のみで扱う。ログ・コード・コメントに値を出さない。
- `docs/` 配下は全て公開される。公開したくないファイルは `docs/` に置かない。
- 暗号方式(PBKDF2-SHA256 600,000 回 → AES-GCM)を変える場合は、`build.py` と `index.html` の両方を同時に直し、往復テストで確認する。`data.enc.json` の `iter` を `index.html` が参照している点に注意する。
- 記事由来の文字列は `textContent` で表示する。`innerHTML` を使わない。リンクは `http(s)` のみ許可する。

## 制約

- **完全無料**を維持する。有料サービス、サーバー、外部 DB を追加しない。Gemini の無料枠は 1 日数リクエスト前提で設計している。
- サーバーレス。状態は暗号化 JSON のコミットと、ブラウザーの localStorage だけで持つ。
- `index.html` は軽量に保つ(スマホ Chrome、ダークモード対応)。ビルド工程やフレームワークを入れない。
- `APP_PASSCODE` を変えると既存の `data.enc.json` が復号できなくなる。変更時は削除して再生成する(過去記事は消える)。

## 実装上の注意(これまでの実績から)

- **モデル ID は変わる。** `gemini-2.5-flash` は新規ユーザー向けに 404 になった。ID を決めるときは推測せず、API のモデル一覧か公式ドキュメントで確認する。
- Gemini は 504 / 429 を返すことがある。外部 API 呼び出しには必ずタイムアウトと上限付きリトライを付ける。永続的なエラー(400/401/403/404)はリトライせず即失敗させる。
- Actions のログは実行中は読めず、stdout はバッファされる。Python は `python -u` で動かし、進捗を `print(..., flush=True)` で出す。
- ワークフローの `git add` は、ファイルが存在しなくても失敗しない書き方にする(要約全滅時に exit 128 になった)。
- 要約に失敗した記事は保存しない。次回実行で再試行される設計を崩さない。

## 作業ルール

- 機能追加や修正などは、内容ごとに GitHub Issue を立て、Issue を分けて作業する。1 つの Issue に複数の内容を混ぜない。コミットや PR は対応する Issue 番号を参照する(例: `#12`)。
- Git のコミットメッセージは日本語で書く。
- 応答・コメントは日本語を基本とする(既存コードの流儀に合わせる)。
- コード変更後は、可能な範囲で検証してから報告する。検証できなかったことは、できなかったと明記する。
  - この PC には Python が未導入の場合がある。ローカル実行できないときは、Actions の手動実行(`workflow_dispatch`)で確認する。
- 手動実行や push で公開状態が変わる操作は、実行前に目的を伝える。
- 依存パッケージ、Actions のバージョン、外部サービスの仕様は、記憶で書かず公式情報で確認する。確認できないものは `[要確認]` と書く。
- 無関係なファイルの大規模な整形をしない。既存の挙動を変える修正は、理由を添えて報告する。
- 設計判断を変えたときは [DeploymentGuide.md](DeploymentGuide.md) と [README.md](README.md) の記述も合わせて更新する。

## 動作確認コマンド

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:GEMINI_API_KEY = "取得したキー"
$env:APP_PASSCODE = "決めたパスコード"
python -u scripts/build.py
python -m http.server 8000 --directory docs
```

Actions での手動実行: `gh workflow run update.yml`、状態確認: `gh run list --limit 3`
