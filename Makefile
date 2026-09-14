PY  := .venv/bin/python
PIP := .venv/bin/pip

.DEFAULT_GOAL := help
.PHONY: help setup build check app admin clean refresh

help:  ## show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk -F':.*?## ' '{printf "  \033[1m%-10s\033[0m %s\n", $$1, $$2}'

setup:  ## create the venv, install dependencies, enable the pre-commit gate (in a git repo)
	python3 -m venv .venv
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@if git rev-parse --git-dir >/dev/null 2>&1; then \
	  git config core.hooksPath .githooks && echo "pre-commit gate enabled"; \
	else echo "not a git repo yet: run 'git init' then 'make setup' to enable the pre-commit gate"; fi

## Stage a new snapshot and rebuild:
##   make refresh FILE="~/Downloads/Report True Friend V3.1 18-09-2026.xlsx"
refresh:  ## copy FILE into data/raw and rebuild (fails if validation fails)
ifndef FILE
	$(error Usage: make refresh FILE="/path/to/Report True Friend V3.1 DD-MM-YYYY[-Final].xlsx")
endif
	@cp "$(FILE)" data/raw/
	@echo "staged $$(basename "$(FILE)")"
	@$(MAKE) build

build:  ## rebuild parquet from the published snapshots in data/raw
	$(PY) -W ignore -m src.build

check:  ## rebuild EVERY snapshot in data/raw one at a time -- the drift regression
	@for f in data/raw/*.xlsx; do \
	  echo "───────── $$f"; $(PY) -W ignore -m src.build "$$f" || exit 1; \
	done
	@echo "note: data/processed now holds the LAST file checked -- run 'make build' before serving"

app:  ## run the dashboard locally
	.venv/bin/streamlit run app.py

admin:  ## local-only tool: upload a snapshot, rebuild, commit, push
	.venv/bin/streamlit run admin.py --server.port 8502

clean:  ## remove generated parquet
	rm -f data/processed/*.parquet data/processed/meta.json
