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

**対応バージョンは6.1.14のみです。** 対応していない更新や改変済みの元アプリへの適用は拒否します。署名はローカルのad-hoc署名になります。

## ソースと検証

`CodexWebGPT-Patcher/` にパッチャー、更新ブリッジ、差分パッチと変更したランチャーのコードを置いています。実行に必要なBun、ランタイム、依存ファイルは配布ZIPに同梱されています（Git上のソース閲覧用ディレクトリには含めていません）。

macOS Apple Silicon の Codex Web GPT 6.1.14 からコピーを作成する統合テストで、署名、ランチャー起動、差分適用、再適用拒否、改変された元アプリの拒否を確認しています。ほかのOS・バージョンの動作は未検証です。
