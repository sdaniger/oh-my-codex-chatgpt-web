# oh-my-codex-chatgpt-web

Codex Web GPT **6.1.14 (macOS Apple Silicon)** 用のパッチャーです。バニラ版アプリのコピーに、接続復旧、セッション保護、同時ブラウザー処理、更新保護、性能表示などの修正を適用します。

## 使い方

1. [配布ZIP](CodexWebGPT-Patcher-6.1.14-macos-arm64.zip) をダウンロードして展開します。
2. ターミナルで展開した `CodexWebGPT-Patcher` フォルダーへ移動します。
3. 次のコマンドを実行します。

   ```bash
   python3 patcher.py --app '/Applications/Codex Web GPT.app' --output '/Applications/Codex Web GPT Patched.app'
   ```

元アプリは上書きせず、別のアプリを作成します。既存の出力先がある場合は処理を中止します。元アプリとパッチ済みアプリは同じユーザーデータを使用するため、同時起動は避けてください。

## Pi・OpenCode・Hermesへのモデル登録

パッチ済みアプリのローカルAPIを、各コーディングエージェントに追加できます。パッチャーのフォルダー内で実行してください。

```bash
python3 install_models.py
```

`--clients pi,opencode` で対象を指定できます。標準では4モデル（`chatgpt-web/gpt-6-sol`、`chatgpt-web/gpt-6-sol-instant`、`chatgpt-web/gpt-5.6-sol`、`chatgpt-web/gpt-5.6-sol-instant`）を登録します。`--models chatgpt-web/gpt-6-sol` で登録モデルを絞れます。

- Pi: `~/.pi/agent/models.json` にResponses APIプロバイダーを追加。`/model` で選択。
- OpenCode: `~/.config/opencode/opencode.jsonc`（なければ `opencode.json`）にResponses APIプロバイダーを追加。`/models` で選択。
- Hermes: `~/.hermes/config.yaml` の `providers` に接続先を追加（既存の設定は維持）。`hermes model` で選択。

既存設定を変更する場合は `.bak-日時` を作成します。使用中のモデル・認証情報・既存のプロバイダーは切り替えません。OpenCodeのJSONC内にコメントがある場合、変更時に標準JSONへ整形されるため、必要なコメントはバックアップから参照してください。

この機能は**モデルの重みをダウンロードせず、ローカルCodex Web GPT APIのモデルを登録**します。利用時はCodex Web GPTを起動し、ChatGPTにログインしてください。Hermesの `codex_app_server` 転送方式は対応版のHermesが必要です。Pi/OpenCodeの実リクエストの互換性は環境とアプリのバージョンに依存します。

**対応バージョンは6.1.14のみです。** 対応していない更新や改変済みの元アプリへの適用は拒否します。署名はローカルのad-hoc署名になります。

## ソースと検証

`CodexWebGPT-Patcher/` にパッチャー、モデル登録スクリプト、更新ブリッジ、差分パッチと変更したランチャーのコードを置いています。実行に必要なBun、ランタイム、依存ファイルは配布ZIPに同梱されています（Git上のソース閲覧用ディレクトリには含めていません）。

macOS Apple Silicon の Codex Web GPT 6.1.14 からコピーを作成する統合テストで、署名、ランチャー起動、差分適用、再適用拒否、改変された元アプリの拒否を確認しています。ほかのOS・バージョンの動作は未検証です。
