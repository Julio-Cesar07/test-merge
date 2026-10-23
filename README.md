# test-merge

Repo de teste da estratégia de merge: feature → `dev` só squash, `dev` → `main` só rebase,
e `dev` realinhada automaticamente depois de qualquer mudança na `main`.

- `.github/workflows/sync-dev-after-main-merge.yml`: cópia do workflow (a referência fica em `infra/scripts/github/`)
- `scripts/setup-merge-strategy.sh`: cópia do script de configuração
- `app.txt`: arquivo mexido nos cenários de teste

## Preparação

Requer `gh` logado (`gh auth login`) e `jq`.

> Rulesets em repo **privado** de conta pessoal exigem GitHub Pro. Se o repo for privado
> e a conta for Free, deixe o repo **público** durante o teste.

1. **Primeiro push** (`main` e `dev` no mesmo commit):
   ```bash
   git add -A && git commit -m "chore: setup"
   git push -u origin main
   git push origin main:dev
   ```
2. **GitHub App** (na conta pessoal): **Settings → Developer settings → GitHub Apps → New GitHub App**
   - nome: `rd-branch-sync-test` (o nome é global no GitHub; `rd-branch-sync` fica para a org)
   - Homepage URL: `https://github.com/Julio-Cesar07`
   - Webhook: desmarcar **Active**
   - Repository permissions: **Contents: Read and write**, **Workflows: Read and write**
   - Only on this account → **Create**
   - anotar o **App ID**, **Generate a private key** (.pem)
   - **Install App** → Only select repositories → `test-merge`
3. **Script**: com `BYPASS_REPO_ADMINS=false`, porque você é admin do repo e, no bypass,
   furaria todas as regras e o teste não provaria nada. Só o App fica no bypass.
   ```bash
   export BYPASS_REPO_ADMINS=false
   ./scripts/setup-merge-strategy.sh --owner Julio-Cesar07 --repo test-merge \
     --app-id <APP_ID> --app-key-file ~/Downloads/<arquivo>.pem          # dry-run
   ./scripts/setup-merge-strategy.sh --owner Julio-Cesar07 --repo test-merge \
     --app-id <APP_ID> --app-key-file ~/Downloads/<arquivo>.pem --apply
   ```
   Em conta pessoal, a variable `BRANCH_SYNC_APP_ID` e o secret `BRANCH_SYNC_APP_PRIVATE_KEY`
   vão para o próprio repo (Settings → Secrets and variables → Actions).

## Cenários

Em cada um, confira o resultado com:
```bash
git fetch origin && git log --oneline --graph origin/main origin/dev -10
```
e o job em **Actions → "sync dev after main merge"**.

**1. Squash na dev.** Branch `feat/a` com 3 commits em `app.txt`, PR para `dev`.
- Esperado: o botão só oferece **Squash and merge**; a `dev` ganha 1 commit com o título do PR;
  a branch `feat/a` é apagada.

**2. Rebase da dev na main.** Repita o 1 com `feat/b`. PR `dev → main`.
- Esperado: só **Rebase and merge**; a `main` ganha 2 commits (um por feature); o sync roda e
  a `dev` fica **no mesmo SHA** da `main`.

**3. Hotfix na main com feature pendente na dev.** Mergeie `feat/c` (arquivo novo) na `dev`.
Depois crie `hotfix/x` a partir da `main`, mexendo em outro arquivo, e mergeie por PR na `main`
(Rebase and merge).
- Esperado: o sync rebaseia a `dev`: ela fica = `main` + `feat/c`.

**4. Conflito.** Mergeie na `dev` uma feature que muda a linha 1 de `app.txt`; mergeie na `main`
um `hotfix/y` mudando a mesma linha.
- Esperado: o job **falha** com a mensagem de conflito e a `dev` não muda. Resolva à mão:
  ```bash
  git checkout dev && git reset --hard origin/dev
  git rebase origin/main   # resolver conflito
  git push --force-with-lease origin dev
  ```

  O `push --force-with-lease` na `dev` vai ser **rejeitado** (você não está no bypass). Para
  destravar no teste: rode o script de novo sem `BYPASS_REPO_ADMINS=false` (`--apply`), faça o
  push e volte a configuração. Na org isso é exatamente o caso em que um admin resolve.

**5. Push direto bloqueado.** `git commit` local e `git push origin dev` (e `main`).
- Esperado: rejeitado (PR obrigatório / sem force push). Na UI, não aparece "Create a merge commit".

**6. Idempotência do script.** Rode o script de novo (dry-run): deve dizer "merge settings: ok" e
"atualizaria ruleset", sem criar duplicados.
