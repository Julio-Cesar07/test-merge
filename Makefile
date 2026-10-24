# Fluxo de migrations em equipe. Detalhes: scripts/migrations.py e README.md.
PY ?= python3
MIG = $(PY) scripts/migrations.py

.PHONY: install db-up db-down migrate migration migration-pr release migration-rechain db-dev-realign migration-check

install:            ## venv + dependências
	$(PY) -m venv .venv && .venv/bin/pip install -q -r requirements.txt
	@echo "ative com: source .venv/bin/activate"

db-up:              ## sobe o Postgres local (porta 5433)
	docker compose up -d postgres

db-down:
	docker compose down

migrate:            ## aplica as migrations no banco do DATABASE_URL (padrão: local)
	alembic upgrade head

migration:          ## make migration m="add price" [EMPTY=1]
	@test -n "$(m)" || (echo 'uso: make migration m="mensagem"' && exit 1)
	$(MIG) new -m "$(m)" $(if $(EMPTY),--empty,)

migration-pr:       ## abre o PR só com as migrations novas da branch atual para a dev
	$(MIG) pr $(if $(FORCE),--force,)

release:            ## make release  (lista)  |  make release PRS="12 13"
	$(MIG) release $(PRS)

migration-rechain:  ## na dev local, depois do rebase sobre a main
	$(MIG) rechain

db-dev-realign:     ## stamp + upgrade no banco do DATABASE_URL, antes do push da dev [OLD_REF=sha]
	$(MIG) realign $(if $(OLD_REF),--old-ref $(OLD_REF),)

migration-check:    ## corrente única (sem banco)
	$(MIG) check
