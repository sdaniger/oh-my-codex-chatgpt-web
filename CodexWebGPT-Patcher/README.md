# Codex Web GPT パッチャー

macOS Apple Silicon 用のバニラ版 Codex Web GPT **6.1.14** から、これまでの修正を含むアプリのコピーを作成します。10件の同時ブラウザー処理、セッション保護、接続復旧、更新保護、性能表示を含みます。

```bash
python3 patcher.py --app '/Applications/Codex Web GPT.app' --output '/Applications/Codex Web GPT Patched.app'
```

入力と出力の省略時も上記のパスです。出力先が既にある場合や、入力が対応するバニラ版と一致しない場合は処理を止めます。元のアプリを上書きせず、既存のログイン情報やチャットも変更しません。

完成したアプリはコピーとして作成されます。切り替える前に、元アプリと実行中の処理を終了してください。両方のアプリは同じアプリID・ユーザーデータを使うため、同時起動は避けてください。署名はローカルのad-hoc署名です。更新保護のため、必要な検証済みファイルは初回実行時に `~/.codex-chatgpt-web/` 内へ保存されます。

このパッケージは6.1.14専用です。他のバージョンには適用せず、新版を検証するまで自動更新を拒否します。既存の別の更新保護プログラムが同じアプリを自動変更する環境では、その設定を確認してください。

## Pi / OpenCode / Hermes でモデルを利用する

パッチ適用後に以下を実行します。

```bash
python3 install_models.py
```

`--clients pi,opencode,hermes` と `--models chatgpt-web/gpt-6-sol` で対象を変更できます。Codex Web GPTの `~/.codex-chatgpt-web/config.json` に設定されたローカルポートを使い、他のプロバイダーと既存の選択モデルを維持します。変更前の設定には `.bak-日時` のバックアップを作ります。OpenCodeのJSONCは編集時に標準JSONに整形されます。

モデルのダウンロード機能ではなく、既存のローカルResponses APIへの接続登録です。利用時はCodex Web GPTが起動しログイン済みである必要があります。Hermesは `codex_app_server` 対応版を前提とします。
