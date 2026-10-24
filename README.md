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
3. **Script**: o padrão põe no bypass todo mundo com escrita (admin/maintain/write). Para os
   cenários 1, 2 e 5 testarem a regra "dura", rode com `BYPASS_REPO_ROLES=""` (só o App no
   bypass); para os cenários 4 e 7, rode sem a variável (configuração real da org).
   ```bash
   export BYPASS_REPO_ROLES=""
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

  Com `BYPASS_REPO_ROLES=""` o push é **rejeitado** (ninguém além do App no bypass). Rode o
  script de novo sem a variável (`unset BYPASS_REPO_ROLES`, `--apply`) e repita o push: passa,
  com um aviso de bypass no terminal. É o comportamento da org.

**5. Push direto bloqueado.** `git commit` local e `git push origin dev` (e `main`).
- Esperado: rejeitado (PR obrigatório / sem force push). Na UI, não aparece "Create a merge commit".

**6. Idempotência do script.** Rode o script de novo (dry-run): deve dizer "merge settings: ok" e
"atualizaria ruleset", sem criar duplicados.

**7. Bypass pela caixa (configuração real).** Com o script rodado sem `BYPASS_REPO_ROLES`, abra
um PR de feature para a `dev`.
- Esperado: o botão oferece só **Squash and merge**; aparece a caixa *"bypass rules"*. Marcando,
  surge também **Rebase and merge**, mas nunca **Create a merge commit**. O bypass aparece em
  Settings → Rules → Insights.

---

# Migrations em equipe (alembic)

Projeto mínimo: `app/models.py` (tabela `products`), `alembic/` com revisões sequenciais
(`0001_...`), Postgres local no `docker-compose.yml` (porta **5433**), comandos no `Makefile`
e a lógica em `scripts/migrations.py`. O CI (`migrations-check.yml`) roda em todo PR:
uma cabeça só, sobe do zero, desce e sobe de novo.

## Preparação

```bash
make install && source .venv/bin/activate
make db-up          # Postgres local; ele faz o papel do "banco de dev"
make migrate        # aplica a 0001
```

O banco vem de `DATABASE_URL` (padrão: o Postgres local do compose).

## Comandos

| Comando | O que faz |
|---|---|
| `make migration m="add price"` | gera a próxima revisão a partir da cabeça da `origin/dev` (autogenerate; `EMPTY=1` para vazia) |
| `make migration-pr` | PR só com as migrations novas da sua branch para a `dev`; renumera/reencadeia se a `dev` andou |
| `make release` | lista o que está na `dev` e não na `main`, com o nº do PR |
| `make release PRS="14 15"` | release parcial para a `main`; a migration da feature é reencadeada na cabeça da `main` |
| `make migration-rechain` | na `dev` local, depois do rebase: reencadeia o que ficou só na `dev` |
| `make db-dev-realign` | stamp + upgrade no banco, **antes** do push da `dev` (`OLD_REF=sha` se precisar) |
| `make migration-check` | corrente única, sem banco (o sync usa) |

## Roteiro de teste

**M1. Migration simples.** Branch `feat/price` a partir da `dev`; em `app/models.py` adicione
`price: Mapped[int | None] = mapped_column(Integer, nullable=True)`; `make migration m="add price"`;
revise, commite; `make migration-pr`.
- Esperado: branch `migration/add_price` com só o arquivo, PR aberto para a `dev`. Squash → o
  deploy (aqui: você roda `make migrate` com a `dev` atualizada) aplica.

**M2. Duas pessoas, mesma 0002.** Crie `feat/stock` a partir da `dev` **antes** de mergear o M1,
`make migration m="add stock" EMPTY=1` (vira 0002), escreva o `op.add_column`/`op.drop_column`,
commite. Mergeie o PR do M1. Depois rode `make migration-pr` na `feat/stock`.
- Esperado: `0002 → 0003 (pai: 0002)`; o arquivo é renomeado também na sua branch.
- Variante: abra o PR da `feat/stock` **antes** de mergear o M1 e mergeie o M1 depois → o PR
  da stock fica "out-of-date" e só mergeia depois do **Update branch** (precisa do script rodado
  com o `migrations-check.yml` já na `dev`); o check roda de novo e fica vermelho ("revisão 0002
  duplicada"); `make migration-pr` de novo resolve.

**M3. Migration destrutiva.** Uma migration com `op.drop_column` no `upgrade()` →
`make migration-pr` avisa e pergunta (`FORCE=1` pula).

**M4. Release parcial.** Com 3 features na `dev` (cada uma com PR de migration + PR de código),
`make release` para ver a lista e `make release PRS="<migration da C> <feature C>"`.
Mergeie o PR na `main` com **Rebase and merge**.
- Esperado: na `main`, a migration da C aponta para a cabeça antiga da `main`.
- O sync **falha sem dar push** ("mais de uma cabeça"); a `dev` fica como estava.
- Ajuste (o `make release` imprime os passos):
  ```bash
  git fetch origin && git checkout dev && git reset --hard origin/dev
  git rebase origin/main
  make migration-rechain     # ex.: 0001 → 0004 → 0002 → 0003
  make db-dev-realign        # banco em 0004 → stamp 0003, nada a aplicar
  git push --force-with-lease origin dev
  ```
- Esperado: `alembic current` = cabeça nova; `make migrate` não aplica nada.
