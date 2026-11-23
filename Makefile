# Governed AI gateway prototype. Works with GNU make on Linux/macOS and on Windows (cmd shell).
# Targets only call Python and docker, so they are shell-agnostic.

ifeq ($(OS),Windows_NT)
  VENV_PY := .venv/Scripts/python.exe
  BOOT_PY ?= python
else
  VENV_PY := .venv/bin/python
  BOOT_PY ?= python3.12
endif
PY := $(VENV_PY)
COMPOSE := docker compose

.PHONY: codeocean-emulate codeocean-packages help venv setup lock keys approve-pairings register compile up wait down fresh test-unit test \
        bench report verify-evidence env warehouse all ci

help:
	@$(BOOT_PY) -c "print('targets: setup compile up test bench report verify-evidence all down')"

$(VENV_PY):
	$(BOOT_PY) -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.lock

venv: $(VENV_PY)

setup: venv
	$(PY) scripts/get_opa.py
	$(PY) -m govlib.keys
	$(PY) -m cp_compile.approve_pairings

lock:
	$(PY) -m pip install -r requirements.in
	$(PY) scripts/lock.py

keys:
	$(PY) -m govlib.keys

approve-pairings:
	$(PY) -m cp_compile.approve_pairings

# Re-register MCP tools and A2A agents from the running mocks (updates tools.yaml / agents.yaml).
register:
	$(PY) -m cp_compile.register --mcp-url-override mcp-docs=http://localhost:8200/mcp \
	  --agent-base-override agent-b=http://localhost:8301 --agent-base-override agent-c=http://localhost:8302

compile:
	$(PY) -m cp_compile.compile

up:
	$(COMPOSE) up -d --build
	$(PY) scripts/wait_stack.py

wait:
	$(PY) scripts/wait_stack.py

down:
	$(COMPOSE) down -v

# Fresh run state: stop the stack and clear generated telemetry/evidence (keys and config are kept).
fresh:
	$(COMPOSE) down -v
	$(PY) scripts/clean_run.py

test-unit:
	$(PY) -m pytest tests/unit -q

test:
	$(PY) -m pytest tests -q -p no:cacheprovider

# Runs inside the compose network (see the bench service in docker-compose.yml).
bench:
	$(COMPOSE) --profile bench run --rm bench

warehouse:
	$(PY) -m telemetry.warehouse.build

env:
	$(PY) scripts/environment.py

report: warehouse env
	$(PY) scripts/report.py

verify-evidence:
	$(PY) -m telemetry.evidence_store.verify evidence

all: setup fresh compile up test bench report verify-evidence

ci: setup compile test-unit

# Code Ocean: regenerate the pip list for the capsule, and emulate a capsule run locally
# (code mounted read-only at /code, outputs in codeocean-results/).
codeocean-packages:
	$(PY) scripts/codeocean_packages.py

codeocean-emulate:
	docker build -f codeocean/emulate.Dockerfile -t gaigw-codeocean:local .
	docker run --rm -v "$(CURDIR):/code:ro" -v "$(CURDIR)/codeocean-results:/results" gaigw-codeocean:local bash /code/codeocean/run
