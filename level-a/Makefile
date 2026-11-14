# All Clear: thin wrappers over the command line. Run from level-a/.
# Every target is one CLI call (python -m levela ..., python scripts/...), so the
# RUNBOOK's raw commands and these targets are the same thing.
#
#   make help                     list the targets
#   make PYTHON=.venv/bin/python api
#   make worker WORKERS=8 RECALL=RCL-2026-020
#   make demo SPEED=15            the finals clock: 15 s per simulated hour

PYTHON ?= python
HOST ?= 127.0.0.1
PORT ?= 8000
WORKERS ?= 4
RECALL ?=
SPEED ?=
MIN_S ?= 120
MAX_S ?= 300

RECALL_OPT := $(if $(RECALL),--recall $(RECALL))
RECALL_ID_OPT := $(if $(RECALL),--recall-id $(RECALL))
SPEED_OPT := $(if $(SPEED),--speed $(SPEED))

.PHONY: help setup-db seed api worker order systems supervisor test test-slow reset demo demo-4 \
        dryrun preflight provision kill-demo long-run status

help: ## list the targets
	@awk 'BEGIN { FS = ":.*## " } /^[a-z0-9-]+:.*## / { printf "  %-11s %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

setup-db: ## create the collections, validators and indexes (idempotent)
	$(PYTHON) -m levela setup-db

seed: ## load the 16 synthetic past recalls and the vector index (once)
	$(PYTHON) -m levela load-history

api: ## the API server: tools, webhooks, form, admin, wall, scheduler
	$(PYTHON) -m levela api --host $(HOST) --port $(PORT)

worker: ## the worker pool on its own (no random kills)
	$(PYTHON) -m levela worker --workers $(WORKERS) $(RECALL_OPT)

order systems: ## the two mock distributor order systems (north 8101, east 8102)
	PYTHON="$(PYTHON)" bash scripts/start_order_systems.sh

supervisor: ## the worker under random kills (2 to 5 real minutes)
	$(PYTHON) scripts/supervisor.py --workers $(WORKERS) --min-s $(MIN_S) --max-s $(MAX_S) $(RECALL_OPT)

test: ## the fast tests
	$(PYTHON) -m pytest -q -m "not slow"

test-slow: ## the slow tests (long run, random kills, eight workers)
	$(PYTHON) -m pytest -q -m slow

reset: ## remove every run-time recall; the seeded history stays
	$(PYTHON) -m levela reset

demo: ## start the long run in demo mode: slice and order system change held (API up)
	$(PYTHON) scripts/demo.py $(RECALL_ID_OPT) start --scenario recall_long --mode demo $(SPEED_OPT)

demo-4: ## start the four-party slice alone (recall_1) in demo mode (API up)
	$(PYTHON) scripts/demo.py start --scenario recall_1 --mode demo

dryrun: ## the seed kit's dry run in a scratch database
	$(PYTHON) -m seedkit dryrun

preflight: ## check Atlas, the schema, the history and the vector index
	$(PYTHON) -m levela preflight

provision: ## create or update the ElevenLabs tools, webhook and agent
	$(PYTHON) scripts/provision_agent.py

kill-demo: ## the kill beat: arm the failpoint, run the worker, restart on Enter
	PYTHON="$(PYTHON)" bash scripts/kill_demo.sh --workers $(WORKERS) $(RECALL_ID_OPT)

long-run: ## the whole run unattended: recall_long in sim mode under random kills
	$(PYTHON) scripts/demo.py start --scenario recall_long --mode sim $(SPEED_OPT)
	$(PYTHON) scripts/supervisor.py --workers $(WORKERS) --min-s $(MIN_S) --max-s $(MAX_S)

status: ## the wall's numbers on one screen
	$(PYTHON) scripts/demo.py $(RECALL_ID_OPT) status
