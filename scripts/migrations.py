#!/usr/bin/env python3
"""Fluxo de migrations em equipe (alembic + git).

Regras do fluxo:
  * O banco de dev só recebe migration pelo deploy da `dev`. Ninguém roda `alembic upgrade`
    contra ele da própria máquina.
  * Migration nova sai num PR só dela para a `dev` (`pr`), já encadeada na cabeça da `dev`.
  * Release parcial (`release`) leva só os PRs escolhidos para a `main`; a migration deles é
    reencadeada na cabeça da `main`. Depois, a `dev` e o banco de dev precisam de ajuste
    (`rechain` + `realign`); o próprio `release` imprime o passo a passo.

Comandos (normalmente via Makefile):
  new -m "mensagem" [--empty]   gera a migration com o próximo número a partir da origin/dev
  pr                            abre o PR só com as migrations novas da branch atual para a dev
  release [PR ...]              sem PRs, lista o que está na dev e não na main; com PRs, cria a release
  rechain                       reencadeia as migrations que estão só na dev sobre a cabeça da main
  realign [--old-ref REF]       alinha o alembic_version do banco à corrente atual (stamp + upgrade)
  check                         falha se as migrations da árvore atual não formam uma corrente única
                                (usado pelo sync e pelo CI; não precisa de banco nem de rede)

As revisões são sequenciais ("0001", "0002", ...) e o arquivo é "<revisão>_<slug>.py".
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = "alembic/versions"
REMOTE = "origin"
DEV = "dev"
MAIN = "main"
MIGRATION_PREFIX = "migration:"

DESTRUCTIVE = re.compile(
    r"op\.(drop_table|drop_column|drop_constraint|drop_index|rename_table)\b|new_column_name\s*="
)
REV_RE = re.compile(r"^revision(?:\s*:\s*[^=]+)?\s*=\s*['\"]([^'\"]+)['\"]", re.M)
DOWN_RE = re.compile(r"^(down_revision(?:\s*:\s*[^=]+)?\s*=\s*)(.+)$", re.M)
REVISES_RE = re.compile(r"^Revises:.*$", re.M)


# ---------------------------------------------------------------------------
# utilitários
# ---------------------------------------------------------------------------


class Abort(Exception):
    pass


def run(*cmd: str, cwd: Path = ROOT, check: bool = True, capture: bool = True) -> str:
    result = subprocess.run(cmd, cwd=cwd, text=True, capture_output=capture)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() if capture else ""
        raise Abort(f"falhou: {' '.join(cmd)}\n{detail}")
    return (result.stdout or "").strip() if capture else ""


def git(*args: str, cwd: Path = ROOT, check: bool = True) -> str:
    return run("git", *args, cwd=cwd, check=check)


def ok(msg: str) -> None:
    print(f"✓ {msg}")


def warn(msg: str) -> None:
    print(f"! {msg}")


def confirm(question: str) -> bool:
    return input(f"{question} [s/N] ").strip().lower() in {"s", "sim", "y", "yes"}


def current_branch() -> str:
    return git("rev-parse", "--abbrev-ref", "HEAD")


def require_clean() -> None:
    if git("status", "--porcelain", "--untracked-files=no"):
        raise Abort("há mudanças não commitadas — commite ou guarde (git stash) antes.")


def fetch() -> None:
    git("fetch", REMOTE, DEV, MAIN)


def is_ancestor(a: str, b: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=ROOT).returncode == 0


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "migration"


# ---------------------------------------------------------------------------
# leitura da corrente de migrations (num ref do git ou na árvore de trabalho)
# ---------------------------------------------------------------------------


@dataclass
class Migration:
    rev: str
    down: str | None
    path: str  # relativo à raiz do repo
    text: str


def parse(path: str, text: str) -> Migration:
    rev = REV_RE.search(text)
    down = DOWN_RE.search(text)
    if not rev or not down:
        raise Abort(f"não consegui ler revision/down_revision de {path}")
    raw = down.group(2).strip()
    if raw == "None":
        down_rev = None
    else:
        m = re.fullmatch(r"['\"]([^'\"]+)['\"]", raw)
        if not m:
            raise Abort(f"{path}: down_revision com mais de um pai (merge) não é suportado: {raw}")
        down_rev = m.group(1)
    return Migration(rev.group(1), down_rev, path, text)


def migrations_at(ref: str | None) -> dict[str, Migration]:
    """Migrations num ref do git; ref=None lê a árvore de trabalho."""
    found: dict[str, Migration] = {}
    if ref is None:
        files = [(f"{VERSIONS}/{p.name}", p.read_text()) for p in sorted((ROOT / VERSIONS).glob("*.py"))]
    else:
        names = git("ls-tree", "--name-only", f"{ref}:{VERSIONS}", check=False).splitlines()
        files = [(f"{VERSIONS}/{n}", git("show", f"{ref}:{VERSIONS}/{n}")) for n in names if n.endswith(".py")]
    for path, text in files:
        mig = parse(path, text)
        if mig.rev in found:
            raise Abort(f"revisão {mig.rev} duplicada: {found[mig.rev].path} e {path}")
        found[mig.rev] = mig
    return found


def heads(migs: dict[str, Migration]) -> list[str]:
    parents = {m.down for m in migs.values()}
    return sorted(r for r in migs if r not in parents)


def single_head(migs: dict[str, Migration], where: str) -> str:
    hs = heads(migs)
    if len(hs) != 1:
        raise Abort(f"{where} tem {len(hs)} cabeças de migration ({', '.join(hs) or 'nenhuma'}); esperado 1.")
    return hs[0]


def chain(migs: dict[str, Migration], head: str) -> list[str]:
    """Revisões da base até a cabeça."""
    order, rev = [], head
    while rev is not None:
        if rev not in migs:
            raise Abort(f"corrente quebrada: {rev} não existe")
        order.append(rev)
        rev = migs[rev].down
    return list(reversed(order))


def next_number(*sets: dict[str, Migration]) -> int:
    nums = [int(r) for s in sets for r in s if r.isdigit()]
    return max(nums, default=0) + 1


def rewrite(mig: Migration, new_rev: str, new_down: str | None) -> tuple[str, str]:
    """Novo (caminho, texto) da migration com revisão e pai trocados."""
    text = REV_RE.sub(lambda m: m.group(0).replace(m.group(1), new_rev), mig.text, count=1)
    text = DOWN_RE.sub(lambda m: m.group(1) + (repr(new_down) if new_down else "None"), text, count=1)
    text = REVISES_RE.sub(f"Revises: {new_down or ''}", text, count=1)
    text = re.sub(r"^Revision ID:.*$", f"Revision ID: {new_rev}", text, count=1, flags=re.M)
    name = Path(mig.path).name
    if name.startswith(f"{mig.rev}_"):
        name = f"{new_rev}_{name[len(mig.rev) + 1:]}"
    return f"{VERSIONS}/{name}", text


def added_in(base: str, ref: str = "HEAD") -> list[str]:
    """Arquivos de migration adicionados em base..ref, na ordem dos commits."""
    out = git("log", "--reverse", "--diff-filter=A", "--format=", "--name-only", f"{base}..{ref}", "--", VERSIONS)
    seen: list[str] = []
    for line in out.splitlines():
        if line.endswith(".py") and line not in seen:
            seen.append(line)
    return seen


def destructive(text: str) -> bool:
    """Só olha o upgrade(): o downgrade normalmente desfaz com drop, e isso é esperado."""
    m = re.search(r"^def upgrade\(.*?(?=^def |\Z)", text, re.M | re.S)
    return bool(DESTRUCTIVE.search(m.group(0) if m else text))


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


def repo_slug() -> str | None:
    url = git("remote", "get-url", REMOTE)
    m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
    return m.group(1) if m else None


def open_pr(head: str, base: str, title: str, body: str) -> str:
    slug = repo_slug()
    if slug is None:
        return f"(remote não é GitHub: abra o PR de {head} para {base} manualmente)"
    if shutil.which("gh"):
        existing = run("gh", "pr", "view", head, "--json", "url,state", "--jq",
                       'select(.state == "OPEN") | .url', check=False)
        if existing:
            return existing
        return run("gh", "pr", "create", "--base", base, "--head", head, "--title", title, "--body", body)
    warn("gh não instalado: abra o PR pelo link abaixo")
    return f"https://github.com/{slug}/compare/{base}...{head}?expand=1"


# ---------------------------------------------------------------------------
# comandos
# ---------------------------------------------------------------------------


def cmd_new(args: argparse.Namespace) -> None:
    fetch()
    if not is_ancestor(f"{REMOTE}/{DEV}", "HEAD"):
        raise Abort(f"sua branch não está atualizada com {REMOTE}/{DEV}: rode `git rebase {REMOTE}/{DEV}` antes.")
    local = migrations_at(None)
    head = single_head(local, "sua branch")
    rev = f"{next_number(local, migrations_at(f'{REMOTE}/{DEV}')):04d}"
    cmd = ["alembic", "revision", "-m", args.message, "--rev-id", rev, "--head", head]
    if not args.empty:
        cmd.insert(2, "--autogenerate")
    print(run(*cmd))
    ok(f"migration {rev} criada (pai: {head}). Revise o arquivo, commite e rode `make migration-pr`.")


def cmd_pr(args: argparse.Namespace) -> None:
    require_clean()
    fetch()
    branch = current_branch()
    if branch in {DEV, MAIN} or branch.startswith("migration/"):
        raise Abort(f"rode na branch da sua feature, não em {branch}.")

    dev_ref = f"{REMOTE}/{DEV}"
    dev = migrations_at(dev_ref)
    dev_head = single_head(dev, DEV)
    dev_paths = {m.path for m in dev.values()}
    pending = [parse(p, (ROOT / p).read_text()) for p in added_in(dev_ref)
               if (ROOT / p).exists() and p not in dev_paths]
    if not pending:
        raise Abort(f"nenhuma migration nova nesta branch em relação à {DEV}.")

    # ordena as pendentes pela corrente entre elas (o número pode colidir com um da dev)
    by_rev = {m.rev: m for m in pending}
    if len(by_rev) != len(pending):
        raise Abort("duas migrations novas com a mesma revisão nesta branch.")
    roots = [m for m in pending if m.down not in by_rev]
    if len(roots) != 1:
        raise Abort("as migrations novas desta branch não formam uma corrente única.")
    order, cur = [], roots[0]
    while cur is not None:
        order.append(cur)
        cur = next((m for m in pending if m.down == cur.rev), None)
    if len(order) != len(pending):
        raise Abort("as migrations novas desta branch não formam uma corrente única.")

    for mig in order:
        if destructive(mig.text):
            warn(f"{mig.path} é destrutiva (drop/rename). Ela não pode subir antes da feature.")
            if not args.force and not confirm("Abrir o PR mesmo assim?"):
                raise Abort("cancelado.")

    # reencadeia sobre a cabeça da dev, renumerando a partir do próximo número livre
    num = next_number(dev)
    parent = dev_head
    rewritten: list[tuple[Migration, str, str, str]] = []  # (orig, novo path, texto, nova rev)
    for mig in order:
        new_rev = f"{num:04d}"
        path, text = rewrite(mig, new_rev, parent)
        rewritten.append((mig, path, text, new_rev))
        if (mig.rev, mig.down) != (new_rev, parent):
            ok(f"{mig.rev} → {new_rev} (pai: {parent})")
        parent, num = new_rev, num + 1

    first = rewritten[0]
    title_msg = first[2].splitlines()[0].strip('"') or Path(first[1]).stem
    revs = ", ".join(r[3] for r in rewritten)
    pr_branch = f"migration/{slugify(Path(order[0].path).stem.split('_', 1)[-1])}"

    # branch do PR = origin/dev + só as migrations (worktree temporário)
    # Se a branch do PR já existe no remoto (rodada anterior, ou "Update branch" no GitHub),
    # atualiza a referência local: o push abaixo recria a branch e precisa do lease em dia.
    git("fetch", REMOTE, f"+refs/heads/{pr_branch}:refs/remotes/{REMOTE}/{pr_branch}", check=False)
    tmp = Path(tempfile.mkdtemp(prefix="migration-pr-"))
    try:
        git("worktree", "add", "-B", pr_branch, str(tmp), dev_ref)
        for _, path, text, _ in rewritten:
            (tmp / path).write_text(text)
            git("add", path, cwd=tmp)
        git("commit", "-m", f"{MIGRATION_PREFIX} {revs} {title_msg}", cwd=tmp)
        git("push", "--force-with-lease", REMOTE, f"{pr_branch}:{pr_branch}", cwd=tmp)
    finally:
        git("worktree", "remove", "--force", str(tmp), check=False)
    ok(f"branch {pr_branch} criada a partir da {DEV}")

    # deixa a branch da feature com os arquivos idênticos aos do PR
    changed = False
    for orig, path, text, _ in rewritten:
        if orig.path != path:
            git("mv", orig.path, path)
            changed = True
        if (ROOT / path).read_text() != text:
            (ROOT / path).write_text(text)
            changed = True
        git("add", path)
    if changed:
        git("commit", "-m", f"{MIGRATION_PREFIX} alinha {revs} com o PR para a {DEV}")
        ok(f"branch {branch} ajustada (mesmos arquivos do PR)")

    url = open_pr(pr_branch, DEV, f"{MIGRATION_PREFIX} {revs} {title_msg}",
                  f"Migration da branch `{branch}`.\n\nMergear com **Squash and merge**. "
                  f"O deploy da `{DEV}` aplica no banco de dev.")
    ok(f"PR: {url}")
    print(f"  → mande o link para alguém aprovar e mergear (squash)\n"
          f"  → depois do merge: git fetch && git rebase {REMOTE}/{DEV}")


def pending_commits() -> list[tuple[str, str, str | None, bool]]:
    """Commits na dev e não na main: (sha, título, nº do PR, só-migration?)."""
    out = git("log", "--reverse", "--format=%H%x09%s", f"{REMOTE}/{MAIN}..{REMOTE}/{DEV}")
    result = []
    for line in out.splitlines():
        sha, subject = line.split("\t", 1)
        pr = re.search(r"\(#(\d+)\)\s*$", subject)
        files = git("show", "--format=", "--name-only", sha).splitlines()
        only_mig = bool(files) and all(f.startswith(VERSIONS + "/") for f in files)
        result.append((sha, subject, pr.group(1) if pr else None, only_mig))
    return result


def cmd_release(args: argparse.Namespace) -> None:
    require_clean()
    fetch()
    commits = pending_commits()
    if not commits:
        raise Abort(f"nada na {DEV} que não esteja na {MAIN}.")
    if not args.prs:
        print(f"Na {DEV} e não na {MAIN} (mais antigo primeiro):")
        for sha, subject, pr, only_mig in commits:
            print(f"  {'#' + pr if pr else '   -':>6}  {sha[:7]}  {'[migration] ' if only_mig else ''}{subject}")
        print("\nUse: make release PRS=\"<números>\"  (inclua os PRs das migrations da feature)")
        return

    wanted = set(args.prs)
    chosen = [c for c in commits if c[2] in wanted]
    missing = wanted - {c[2] for c in chosen}
    if missing:
        raise Abort(f"PR(s) não encontrados entre os commits pendentes: {', '.join(sorted(missing))}")

    main_ref = f"{REMOTE}/{MAIN}"
    main_migs = migrations_at(main_ref)
    main_head = single_head(main_migs, MAIN)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    rel_branch = f"release/{stamp}-pr{'-'.join(sorted(wanted, key=int))}"

    tmp = Path(tempfile.mkdtemp(prefix="release-"))
    try:
        git("worktree", "add", "-B", rel_branch, str(tmp), main_ref)
        for sha, subject, _, _ in chosen:
            if subprocess.run(["git", "cherry-pick", sha], cwd=tmp, capture_output=True).returncode:
                git("cherry-pick", "--abort", cwd=tmp, check=False)
                raise Abort(f"conflito ao levar \"{subject}\": provavelmente depende de algo que fica na {DEV}. "
                            "Inclua o PR de que ele depende ou espere.")
            ok(f"levando: {subject}")

        # reencadeia as migrations levadas sobre a cabeça da main (mantém os números)
        parent, relinked = main_head, []
        for path in added_in(main_ref, rel_branch):
            mig = parse(path, (tmp / path).read_text())
            if mig.down != parent:
                new_path, text = rewrite(mig, mig.rev, parent)
                (tmp / new_path).write_text(text)
                git("add", new_path, cwd=tmp)
                relinked.append(f"{mig.rev} (pai: {mig.down} → {parent})")
            parent = mig.rev
        if relinked:
            git("commit", "-m", f"{MIGRATION_PREFIX} reencadeia sobre a {MAIN}: {', '.join(relinked)}", cwd=tmp)
            for r in relinked:
                ok(f"reencadeada {r}")

        # confere a corrente da release
        names = git("ls-tree", "--name-only", f"HEAD:{VERSIONS}", cwd=tmp).splitlines()
        rel_all = {}
        for n in names:
            if n.endswith(".py"):
                m = parse(f"{VERSIONS}/{n}", (tmp / VERSIONS / n).read_text())
                rel_all[m.rev] = m
        single_head(rel_all, "a release")
        ok("corrente de migrations ok (uma cabeça só)")
        git("push", "--force-with-lease", REMOTE, f"{rel_branch}:{rel_branch}", cwd=tmp)
    finally:
        git("worktree", "remove", "--force", str(tmp), check=False)

    titles = "\n".join(f"- {c[1]}" for c in chosen)
    url = open_pr(rel_branch, MAIN, f"release: PR {', '.join('#' + p for p in sorted(wanted, key=int))}",
                  f"Release parcial:\n\n{titles}\n\nMergear com **Rebase and merge**.")
    ok(f"PR: {url}")
    print(f"""
ATENÇÃO — depois do merge na {MAIN}, a {DEV} e o banco de dev precisam de ajuste.
O sync vai parar sem dar push (migrations com duas cabeças) — esperado. Quem fez a release:

  1. git fetch {REMOTE}
     git checkout {DEV} && git reset --hard {REMOTE}/{DEV}
     git rebase {REMOTE}/{MAIN}
       - normalmente passa limpo; se der conflito numa migration levada, fique com a versão da {MAIN}
         (git checkout --ours <arquivo> && git add <arquivo>), depois git rebase --continue
         (se o commit ficar vazio: git rebase --skip)
  2. make migration-rechain      # reencadeia o que ficou só na {DEV}
  3. make db-dev-realign         # stamp + upgrade no banco de dev — ANTES do push
  4. git push --force-with-lease {REMOTE} {DEV}
""")


def cmd_rechain(args: argparse.Namespace) -> None:
    require_clean()
    fetch()
    if current_branch() != DEV:
        raise Abort(f"rode na {DEV} local, depois do rebase sobre a {MAIN}.")
    main_ref = f"{REMOTE}/{MAIN}"
    if not is_ancestor(main_ref, "HEAD"):
        raise Abort(f"a {DEV} local não contém a {MAIN}: rode `git rebase {main_ref}` primeiro.")
    main_head = single_head(migrations_at(main_ref), MAIN)
    local = migrations_at(None)
    parent, changed = main_head, []
    for path in added_in(main_ref):
        mig = next((m for m in local.values() if m.path == path), None)
        if mig is None:
            continue
        if mig.down != parent:
            new_path, text = rewrite(mig, mig.rev, parent)
            (ROOT / new_path).write_text(text)
            git("add", new_path)
            changed.append(f"{mig.rev} (pai: {mig.down} → {parent})")
        parent = mig.rev
    if not changed:
        single_head(migrations_at(None), f"a {DEV} local")
        ok("nada a reencadear: corrente já linear")
        return
    git("commit", "-m", f"{MIGRATION_PREFIX} reencadeia a {DEV} sobre a {MAIN}: {', '.join(changed)}")
    for c in changed:
        ok(f"reencadeada {c}")
    head = single_head(migrations_at(None), f"a {DEV} local")
    ok(f"corrente: {' → '.join(chain(migrations_at(None), head))}")
    print("  → agora: make db-dev-realign, e só depois git push --force-with-lease origin dev")


def cmd_check(args: argparse.Namespace) -> None:
    migs = migrations_at(None)
    head = single_head(migs, "a árvore atual")
    ok(f"corrente única: {' → '.join(chain(migs, head))}")


def cmd_realign(args: argparse.Namespace) -> None:
    fetch()
    out = run("alembic", "current")
    current = next((ln.split()[0] for ln in out.splitlines() if ln.strip()), None)
    if current is None:
        raise Abort("o banco não tem alembic_version (vazio?): use `alembic upgrade head`.")

    new = migrations_at(None)
    new_order = chain(new, single_head(new, "a corrente local"))
    # O que o banco já tem = a corrente ANTIGA (antes do ajuste) até a revisão atual do banco.
    old_ref = args.old_ref or f"{REMOTE}/{DEV}"
    old = migrations_at(old_ref)
    old_heads = heads(old)
    if len(old_heads) != 1:
        raise Abort(f"{old_ref} tem {len(old_heads)} cabeças ({', '.join(old_heads)}), então não serve de referência "
                    "do que o banco já tem. Passe --old-ref <sha da dev antes da release> "
                    f"(veja em `git reflog show {REMOTE}/{DEV}`).")
    if current not in old:
        raise Abort(f"o banco está em {current}, que não existe em {old_ref} — não sei o que já foi aplicado.")
    applied = set(chain(old, current))
    if current != old_heads[0]:
        warn(f"o banco está em {current}, mas a cabeça de {old_ref} é {old_heads[0]}.")

    # maior prefixo da corrente nova que é exatamente o que o banco já tem
    target = None
    for i in range(len(new_order), 0, -1):
        if set(new_order[:i]) == applied:
            target = new_order[i - 1]
            break
    if target is None:
        extra = applied - set(new_order)
        detail = f" Revisões no banco que não existem na corrente nova: {', '.join(sorted(extra))}." if extra else ""
        raise Abort("a ordem nova não começa exatamente pelas revisões que o banco já tem; "
                    f"não dá para alinhar com stamp.{detail}")

    pending = new_order[new_order.index(target) + 1:]
    print(f"Banco em:      {current}")
    print(f"Corrente nova: {' → '.join(new_order)}")
    print(f"Stamp em:      {target}" + (f"  (depois aplica: {', '.join(pending)})" if pending else "  (nada a aplicar)"))
    url_hint = os.environ.get("DATABASE_URL", "(DATABASE_URL não definido — usa o padrão do env.py)")
    print(f"Banco:         {re.sub(r'://[^@]*@', '://***@', url_hint)}")
    if target == current and not pending:
        ok("já alinhado")
        return
    if not args.yes and not confirm("Aplicar?"):
        raise Abort("cancelado.")
    print(run("alembic", "stamp", "--purge", target))
    print(run("alembic", "upgrade", "head"))
    ok("banco alinhado")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new")
    p.add_argument("-m", "--message", required=True)
    p.add_argument("--empty", action="store_true", help="sem autogenerate")
    p.set_defaults(func=cmd_new)
    p = sub.add_parser("pr")
    p.add_argument("--force", action="store_true", help="não pergunta sobre migration destrutiva")
    p.set_defaults(func=cmd_pr)
    p = sub.add_parser("release")
    p.add_argument("prs", nargs="*")
    p.set_defaults(func=cmd_release)
    sub.add_parser("rechain").set_defaults(func=cmd_rechain)
    sub.add_parser("check").set_defaults(func=cmd_check)
    p = sub.add_parser("realign")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--old-ref", help=f"ref com a corrente que o banco seguia (padrão: {REMOTE}/{DEV})")
    p.set_defaults(func=cmd_realign)
    args = parser.parse_args()
    try:
        args.func(args)
    except Abort as exc:
        print(f"erro: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
