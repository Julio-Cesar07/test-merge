#!/usr/bin/env bash
#
# Configura a estratégia de merge nos repos de back/front:
#   feature → dev  : só "Squash and merge"
#   dev     → main : só "Rebase and merge"
#
# O que faz, por repo:
#   1. Settings → General: desliga merge commit, liga squash e rebase, squash usa o
#      título do PR; desliga a exclusão automática da branch (apagaria a `dev`).
#   2. Rulesets `dev` e `main`: PR obrigatório com o método de merge permitido,
#      histórico linear, sem force push, sem apagar a branch. Os status checks exigidos
#      pela branch protection clássica (se houver) são copiados para o ruleset.
#   3. Confere se a branch protection clássica ainda existe (não mexe nela) e se o
#      workflow de sync está na `main`.
# E cria/atualiza a variable e o secret do GitHub App `rd-branch-sync` (só quando
# --app-key-file é passado): na org, se o dono for uma org; em cada repo, se for uma
# conta pessoal (útil para testar num repo próprio, ex.: --owner fulano --repo test-merge).
#
# Roda em dry-run por padrão: só mostra o que mudaria. Para aplicar, --apply.
# Idempotente: rodar de novo só atualiza o que estiver diferente.
#
# Requer: gh (logado com uma conta admin do dono dos repos) e jq.
# Passo a passo completo em README.md, nesta pasta.

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

OWNER="RD-Medicine"

REPOS=(
  rd-commerce-back
  rd-commerce-front
  tep-back
  rd-residencia-back
  rd-residia-back
  plataforma-unificada-back-rd
)

APP_ID_VAR="BRANCH_SYNC_APP_ID"
APP_KEY_SECRET="BRANCH_SYNC_APP_PRIVATE_KEY"

# Quem pode furar os rulesets (push direto, force push, merge com qualquer método) em
# `dev` e `main`. Padrão: todo mundo com escrita. No PR, furar exige marcar a caixa
# "bypass rules" de propósito; no `git push` passa direto (só aparece um aviso) — quem dá
# --force assume o risco. O merge commit continua impossível para todos (Settings → General).
# O App de sync entra sempre no bypass da `dev`, independente disso.
# Dá para sobrescrever por variável de ambiente (ex.: BYPASS_REPO_ROLES="" para testar as
# regras sendo admin do repo).
BYPASS_ORG_ADMINS=${BYPASS_ORG_ADMINS:-true}
BYPASS_REPO_ROLES=${BYPASS_REPO_ROLES-admin maintain write} # roles do repo: admin maintain write
BYPASS_TEAMS=() # slugs de times da org, ex.: (tech-leads); também via --bypass-team

# Mensagem do commit no squash: título do PR + descrição do PR.
SQUASH_TITLE="PR_TITLE"
SQUASH_MESSAGE="PR_BODY"

RULESET_DEV_NAME="merge-strategy: dev (squash)"
RULESET_MAIN_NAME="merge-strategy: main (rebase)"

SYNC_WORKFLOW_PATH=".github/workflows/sync-dev-after-main-merge.yml"

# Check de migrations (corrente única, sobe do zero). Quando o workflow existe na `dev` do
# repo, vira obrigatório no ruleset da `dev` com "branch atualizada antes do merge": se a
# `dev` andou (ex.: outro PR mergeou uma migration), o PR precisa de "Update branch", o que
# roda o check de novo sobre a `dev` nova. Sem o workflow, não exige (travaria todo PR).
MIGRATIONS_WORKFLOW_PATH=".github/workflows/migrations-check.yml"
MIGRATIONS_CHECK_CONTEXT="migrations" # nome do job no workflow

# ---------------------------------------------------------------------------

APPLY=false
APP_ID=""
APP_KEY_FILE=""
ONLY_REPOS=()
WARNINGS=0
FAILED=()

usage() {
  cat <<EOF
Uso: $(basename "$0") [--apply] [--owner DONO] [--repo NOME]... [--bypass-team SLUG]...
       [--app-id ID --app-key-file ARQUIVO.pem]

  --apply             aplica as mudanças (sem isso, só mostra o que faria)
  --owner DONO        org ou usuário dono dos repos (padrão: $OWNER)
  --repo NOME         em vez da lista REPOS, só este repo (pode repetir)
  --bypass-team SLUG  time da org a mais no bypass dos rulesets (normalmente não precisa:
                      quem tem escrita já está); pode repetir
  --app-id ID         App ID do rd-branch-sync (padrão: lê a variable $APP_ID_VAR)
  --app-key-file ARQ  private key (.pem) do App; grava variable + secret
EOF
}

die() { echo "erro: $*" >&2; exit 1; }
info() { echo "  $*"; }
warn() { echo "  ! $*"; WARNINGS=$((WARNINGS + 1)); }

while [[ $# -gt 0 ]]; do
  case $1 in
    --apply) APPLY=true ;;
    --owner) OWNER="${2:?--owner precisa de um nome}"; shift ;;
    --bypass-team) BYPASS_TEAMS+=("${2:?--bypass-team precisa de um slug}"); shift ;;
    --repo) ONLY_REPOS+=("${2:?--repo precisa de um nome}"); shift ;;
    --app-id) APP_ID="${2:?--app-id precisa de um valor}"; shift ;;
    --app-key-file) APP_KEY_FILE="${2:?--app-key-file precisa de um caminho}"; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; die "argumento desconhecido: $1" ;;
  esac
  shift
done

command -v gh >/dev/null || die "gh não encontrado (https://cli.github.com)"
command -v jq >/dev/null || die "jq não encontrado (apt install jq / brew install jq)"
gh auth status >/dev/null 2>&1 || die "gh não está logado (gh auth login)"

OWNER_TYPE=$(gh api "users/$OWNER" --jq .type 2>/dev/null) || die "dono não encontrado: $OWNER"
IS_ORG=false
[[ $OWNER_TYPE == Organization ]] && IS_ORG=true

TARGET_REPOS=("${REPOS[@]}")
if [[ ${#ONLY_REPOS[@]} -gt 0 ]]; then
  TARGET_REPOS=("${ONLY_REPOS[@]}")
elif ! $IS_ORG; then
  die "$OWNER é uma conta pessoal: diga quais repos com --repo"
fi

# ---------------------------------------------------------------------------
# Credenciais do App: na org (dono = org) ou em cada repo (dono = conta pessoal)
# ---------------------------------------------------------------------------

setup_app_credentials() {
  local r
  if $IS_ORG; then
    echo "== org $OWNER: credenciais do App"
  else
    echo "== conta pessoal $OWNER: credenciais do App em cada repo"
  fi

  if [[ -n $APP_KEY_FILE ]]; then
    [[ -f $APP_KEY_FILE ]] || die "arquivo não encontrado: $APP_KEY_FILE"
    [[ -n $APP_ID ]] || die "--app-key-file exige --app-id"
    if $IS_ORG; then
      # Lista completa (REPOS + --repo): --repos substitui o acesso, não soma.
      local repos_csv
      repos_csv=$(printf '%s\n' "${REPOS[@]}" "${TARGET_REPOS[@]}" | sort -u | paste -sd, -)
      if $APPLY; then
        gh variable set "$APP_ID_VAR" --org "$OWNER" --visibility selected --repos "$repos_csv" --body "$APP_ID"
        gh secret set "$APP_KEY_SECRET" --org "$OWNER" --visibility selected --repos "$repos_csv" <"$APP_KEY_FILE"
        info "variable $APP_ID_VAR e secret $APP_KEY_SECRET gravados (repos: $repos_csv)"
      else
        info "[dry-run] gravaria variable $APP_ID_VAR=$APP_ID e secret $APP_KEY_SECRET na org (repos: $repos_csv)"
      fi
    else
      for r in "${TARGET_REPOS[@]}"; do
        if $APPLY; then
          gh variable set "$APP_ID_VAR" --repo "$OWNER/$r" --body "$APP_ID"
          gh secret set "$APP_KEY_SECRET" --repo "$OWNER/$r" <"$APP_KEY_FILE"
          info "$r: variable $APP_ID_VAR e secret $APP_KEY_SECRET gravados"
        else
          info "[dry-run] $r: gravaria variable $APP_ID_VAR=$APP_ID e secret $APP_KEY_SECRET"
        fi
      done
    fi
    return
  fi

  if [[ -z $APP_ID ]]; then
    if $IS_ORG; then
      APP_ID=$(gh variable get "$APP_ID_VAR" --org "$OWNER" 2>/dev/null || true)
    else
      APP_ID=$(gh variable get "$APP_ID_VAR" --repo "$OWNER/${TARGET_REPOS[0]}" 2>/dev/null || true)
    fi
  fi
  if [[ -z $APP_ID ]]; then
    $APPLY && die "App ID não encontrado: crie o App rd-branch-sync (README, passo 1) e passe --app-id ID --app-key-file ARQUIVO.pem"
    warn "App ID não encontrado (o App ainda não foi criado?); o dry-run segue com um ID fictício"
    APP_ID=0
  fi
  info "App ID: $APP_ID"

  if $IS_ORG; then
    gh secret list --org "$OWNER" --json name --jq '.[].name' 2>/dev/null | grep -qx "$APP_KEY_SECRET" \
      && info "secret $APP_KEY_SECRET: existe" \
      || warn "secret $APP_KEY_SECRET não existe na org: o workflow de sync vai falhar (passe --app-key-file)"
  else
    for r in "${TARGET_REPOS[@]}"; do
      gh secret list --repo "$OWNER/$r" --json name --jq '.[].name' 2>/dev/null | grep -qx "$APP_KEY_SECRET" \
        && info "$r: secret $APP_KEY_SECRET existe" \
        || warn "$r: secret $APP_KEY_SECRET não existe: o workflow de sync vai falhar (passe --app-key-file)"
    done
  fi
}

# ---------------------------------------------------------------------------
# Bypass dos rulesets
# ---------------------------------------------------------------------------

build_bypass() {
  local include_app=$1 actors='[]' slug team_id role role_id
  # Conta pessoal não tem admin de org nem times.
  if $IS_ORG && $BYPASS_ORG_ADMINS; then
    actors=$(jq '. + [{actor_id: 1, actor_type: "OrganizationAdmin", bypass_mode: "always"}]' <<<"$actors")
  fi
  for role in $BYPASS_REPO_ROLES; do
    # IDs fixos das roles de repositório no GitHub.
    case $role in
      admin) role_id=5 ;;
      maintain) role_id=2 ;;
      write) role_id=4 ;;
      *) die "role desconhecida em BYPASS_REPO_ROLES: $role (use admin, maintain, write)" ;;
    esac
    actors=$(jq --argjson id "$role_id" '. + [{actor_id: $id, actor_type: "RepositoryRole", bypass_mode: "always"}]' <<<"$actors")
  done
  $IS_ORG || [[ ${#BYPASS_TEAMS[@]} -eq 0 ]] || die "BYPASS_TEAMS só vale para org"
  for slug in "${BYPASS_TEAMS[@]+"${BYPASS_TEAMS[@]}"}"; do
    team_id=$(gh api "orgs/$OWNER/teams/$slug" --jq .id 2>/dev/null) || die "time não encontrado na org: $slug"
    actors=$(jq --argjson id "$team_id" '. + [{actor_id: $id, actor_type: "Team", bypass_mode: "always"}]' <<<"$actors")
  done
  if [[ $include_app == true ]]; then
    actors=$(jq --argjson id "$APP_ID" '. + [{actor_id: $id, actor_type: "Integration", bypass_mode: "always"}]' <<<"$actors")
  fi
  echo "$actors"
}

# ---------------------------------------------------------------------------
# Por repo
# ---------------------------------------------------------------------------

check_branches() {
  local repo=$1 b
  for b in dev main; do
    gh api "repos/$OWNER/$repo/branches/$b" >/dev/null 2>&1 || warn "branch $b não existe"
  done
}

configure_merge_settings() {
  local repo=$1 meta=$2 desired current diff
  desired=$(jq -n --arg t "$SQUASH_TITLE" --arg m "$SQUASH_MESSAGE" '{
    allow_merge_commit: false,
    allow_squash_merge: true,
    allow_rebase_merge: true,
    # false: a opção nativa apagaria a própria `dev` no PR dev → main (quem mergeia está
    # no bypass do ruleset, que libera a exclusão). Branches de feature não são apagadas.
    delete_branch_on_merge: false,
    squash_merge_commit_title: $t,
    squash_merge_commit_message: $m
  }')
  current=$(jq '{allow_merge_commit, allow_squash_merge, allow_rebase_merge, delete_branch_on_merge,
                 squash_merge_commit_title, squash_merge_commit_message}' <<<"$meta")
  diff=$(jq -rn --argjson d "$desired" --argjson c "$current" \
    '$d | to_entries[] | select($c[.key] != .value) | "\(.key): \($c[.key]) → \(.value)"')

  if [[ -z $diff ]]; then
    info "merge settings: ok"
    return
  fi
  info "merge settings:"
  sed 's/^/      /' <<<"$diff"
  if $APPLY; then
    gh api -X PATCH "repos/$OWNER/$repo" --input - <<<"$desired" >/dev/null || {
      warn "falhou ao gravar os merge settings"
      FAILED+=("$repo")
      return
    }
    info "→ merge settings aplicados"
  fi
}

# Lê a branch protection clássica: avisa que ela continua valendo e guarda em
# CLASSIC_CHECKS os status checks exigidos, para o ruleset copiar.
CLASSIC_CHECKS='[]'
read_classic_protection() {
  local repo=$1 branch=$2 is_admin=$3 p summary
  CLASSIC_CHECKS='[]'
  if [[ $is_admin != true ]]; then
    info "proteção clássica em $branch: não dá para ler sem admin"
    return
  fi
  if ! p=$(gh api "repos/$OWNER/$repo/branches/$branch/protection" 2>/dev/null); then
    info "proteção clássica em $branch: nenhuma"
    return
  fi
  summary=$(jq -r '[
      "checks=" + ((.required_status_checks.contexts // []) | join(",")),
      "aprovações=" + ((.required_pull_request_reviews.required_approving_review_count // "não") | tostring),
      "force_push=" + (.allow_force_pushes.enabled | tostring),
      "restrições=" + (if .restrictions then "sim" else "não" end)
    ] | join(" ")' <<<"$p")
  warn "$branch tem branch protection clássica ($summary)."
  info "  ela continua valendo junto com o ruleset; os checks são copiados para o ruleset — depois de conferir, apague a regra clássica em Settings → Branches."
  if [[ $branch == dev ]]; then
    info "  na dev, enquanto a regra clássica existir o App pode ser barrado (ela não aceita o bypass do ruleset)."
  fi
  CLASSIC_CHECKS=$(jq '[(.required_status_checks.checks // [])[] | {context} + (if (.app_id // 0) > 0 then {integration_id: .app_id} else {} end)]' <<<"$p")
}

ruleset_json() {
  local name=$1 branch=$2 method=$3 bypass=$4 checks=$5 strict=${6:-false}
  jq -n --arg name "$name" --arg ref "refs/heads/$branch" --arg method "$method" \
    --argjson bypass "$bypass" --argjson checks "$checks" --argjson strict "$strict" '{
    name: $name,
    target: "branch",
    enforcement: "active",
    conditions: {ref_name: {include: [$ref], exclude: []}},
    bypass_actors: $bypass,
    rules: ([
      {type: "deletion"},
      {type: "non_fast_forward"},
      {type: "required_linear_history"},
      {type: "pull_request", parameters: {
        required_approving_review_count: 0,
        dismiss_stale_reviews_on_push: false,
        require_code_owner_review: false,
        require_last_push_approval: false,
        required_review_thread_resolution: false,
        allowed_merge_methods: [$method]
      }}
    ] + (if ($checks | length) > 0 then [{type: "required_status_checks", parameters: {
        strict_required_status_checks_policy: $strict,
        required_status_checks: $checks
      }}] else [] end))
  }'
}

upsert_ruleset() {
  local repo=$1 name=$2 branch=$3 method=$4 bypass=$5 checks=$6 strict=${7:-false} body existing_id
  body=$(ruleset_json "$name" "$branch" "$method" "$bypass" "$checks" "$strict")
  existing_id=$(gh api "repos/$OWNER/$repo/rulesets?includes_parents=false" \
    --jq ".[] | select(.name == \"$name\") | .id" 2>/dev/null || true)

  local verb="criaria" checks_txt
  [[ -n $existing_id ]] && verb="atualizaria"
  checks_txt=$(jq -r 'map(.context) | if length > 0 then join(",") else "nenhum" end' <<<"$checks")
  if ! $APPLY; then
    info "[dry-run] $verb ruleset \"$name\": só $method, checks=$checks_txt$($strict && echo " (branch atualizada obrigatória)"), bypass=$(jq -r 'map(.actor_type) | join(",")' <<<"$bypass")"
    return
  fi
  if [[ -n $existing_id ]]; then
    gh api -X PUT "repos/$OWNER/$repo/rulesets/$existing_id" --input - <<<"$body" >/dev/null || return 1
    info "→ ruleset \"$name\" atualizado"
  else
    gh api -X POST "repos/$OWNER/$repo/rulesets" --input - <<<"$body" >/dev/null || return 1
    info "→ ruleset \"$name\" criado"
  fi
}

# Se o workflow de migrations existe na `dev`, acrescenta o check aos de DEV_CHECKS e liga
# MIGRATIONS_STRICT (branch atualizada antes do merge).
DEV_CHECKS='[]'
MIGRATIONS_STRICT=false
add_migrations_check() {
  local repo=$1
  MIGRATIONS_STRICT=false
  if ! gh api "repos/$OWNER/$repo/contents/$MIGRATIONS_WORKFLOW_PATH?ref=dev" >/dev/null 2>&1; then
    info "check de migrations: $MIGRATIONS_WORKFLOW_PATH não está na dev — não exigido"
    return
  fi
  MIGRATIONS_STRICT=true
  info "check de migrations: obrigatório na dev, com branch atualizada antes do merge"
  DEV_CHECKS=$(jq --arg c "$MIGRATIONS_CHECK_CONTEXT" \
    'if any(.[]; .context == $c) then . else . + [{context: $c}] end' <<<"$DEV_CHECKS")
}

check_sync_workflow() {
  local repo=$1 content
  if ! content=$(gh api "repos/$OWNER/$repo/contents/$SYNC_WORKFLOW_PATH?ref=main" --jq .content 2>/dev/null); then
    warn "workflow de sync ainda não está na main ($SYNC_WORKFLOW_PATH)"
    return
  fi
  if base64 -d <<<"$content" 2>/dev/null | grep -q "create-github-app-token"; then
    info "workflow de sync: versão nova na main"
  else
    warn "workflow de sync na main é a versão antiga (sem token do App)"
  fi
}

process_repo() {
  local repo=$1 meta is_admin dev_checks main_checks
  echo
  echo "== $OWNER/$repo"
  if ! meta=$(gh api "repos/$OWNER/$repo" 2>/dev/null); then
    warn "repo não encontrado ou sem acesso"
    FAILED+=("$repo")
    return
  fi
  is_admin=$(jq -r '.permissions.admin // false' <<<"$meta")
  if [[ $is_admin != true ]]; then
    if $APPLY; then
      warn "sem permissão de admin — pulando"
      FAILED+=("$repo")
      return
    fi
    warn "sem permissão de admin: o dry-run mostra só o que dá para ler"
  fi

  check_branches "$repo"
  configure_merge_settings "$repo" "$meta"
  read_classic_protection "$repo" dev "$is_admin"
  DEV_CHECKS=$CLASSIC_CHECKS
  add_migrations_check "$repo"
  dev_checks=$DEV_CHECKS
  read_classic_protection "$repo" main "$is_admin"
  main_checks=$CLASSIC_CHECKS
  upsert_ruleset "$repo" "$RULESET_DEV_NAME" dev squash "$BYPASS_DEV" "$dev_checks" "$MIGRATIONS_STRICT" || {
    warn "falhou ao gravar o ruleset da dev (o App está instalado neste repo?)"
    FAILED+=("$repo")
  }
  upsert_ruleset "$repo" "$RULESET_MAIN_NAME" main rebase "$BYPASS_MAIN" "$main_checks" || {
    warn "falhou ao gravar o ruleset da main"
    FAILED+=("$repo")
  }
  check_sync_workflow "$repo"
}

# ---------------------------------------------------------------------------

$APPLY && echo "Modo: APPLY" || echo "Modo: dry-run (nada é alterado; use --apply)"
echo

setup_app_credentials
BYPASS_DEV=$(build_bypass true)
BYPASS_MAIN=$(build_bypass false)

for repo in "${TARGET_REPOS[@]}"; do
  process_repo "$repo"
done

echo
echo "== resumo"
echo "  avisos: $WARNINGS"
if [[ ${#FAILED[@]} -gt 0 ]]; then
  echo "  com falha: $(printf '%s\n' "${FAILED[@]}" | sort -u | tr '\n' ' ')"
  exit 1
fi
$APPLY || echo "  dry-run concluído; rode de novo com --apply para aplicar."
