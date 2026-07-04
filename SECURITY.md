# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.2.x   | ✓         |
| 0.1.x   | ✗         |

## Reporting a Vulnerability

脆弱性は公開Issueではなく **GitHub Private Vulnerability Reporting** で報告してください。
対応目標: 初回応答 72時間以内、修正リリース 30日以内。

## Design Notes

- サーバは 127.0.0.1 のみにバインド (コードで強制)。LAN公開は意図的に非サポート
- 外部送信なし: LLM/埋め込みはユーザー指定のローカルエンドポイントのみ
- URL取込はSSRF防御あり (プライベート/ループバック/リンクローカルIP遮断、各リダイレクトで再検証)
- 取込文書内の指示には従わないようシステムプロンプトで防御 (間接プロンプトインジェクション対策)
