# CI ワークフロー

`ci.yml` は GitHub Actions のワークフロー定義 (lint → type → test → coverage → secret scan → SBOM)。

このリポジトリへの自動プッシュでは GitHub App の `workflows` 権限制約により
`.github/workflows/` 配下を更新できないため、ここに置いている。
有効化するには、リポジトリ管理者が手動で移動する:

```bash
mkdir -p .github/workflows
git mv ci/ci.yml .github/workflows/ci.yml
```
