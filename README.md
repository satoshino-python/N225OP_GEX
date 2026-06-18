# N225OP_GEX
日経225オプションのガンマエクスポージャーを作成
# 📋 JPXデータ自動取得システム 構築・運用手順書

手元のCodespaces（開発環境）で安全にテスト・デバッグを行い、GitHub Actions（本番環境）で毎日高速に自動実行させるための全手順です。

---

## 1. フォルダ・ファイル構成（リポジトリの正しい形）
リポジトリのトップ階層は、必ず以下の構造になっている必要があります。

```text
📂 N225OP_GEX
 ├── 📂 .github
 │    └── 📂 workflows
 │         └── 📄 jpx_scheduler.yml     # GitHub Actionsの設定ファイル
 ├── 📂 .vscode
 │    └── 📄 launch.json               # Codespacesでのデバッグ設定ファイル
 ├── 📄 main.py                         # Pythonのメインプログラム
 ├── 📄 requirements.txt               # 高速化（キャッシュ）用ライブラリ一覧
 └── 📄 README.md                       # 本手順書（このファイル）
