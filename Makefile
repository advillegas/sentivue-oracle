# SentiVue Oracle — operator interface
# All targets are designed for the Mac Studio deployment target (macOS arm64).

SHELL        := /bin/bash
ROOT         := $(shell pwd)
# ENGINE: claude | opencode
ENGINE       ?= claude
HOURS        ?= 24
M            ?= conductor/missions/example.toml
PY           := uv run --project env python

.PHONY: help install models render serve stop status verify doctor harden \
        claude opencode mission report env skills ecc supabase-up supabase-down \
        dist uninstall clean

help:
	@echo "SentiVue Oracle    (guided setup: ./install)"
	@echo "  make install        one-time online setup (brew, engines, python, skills, ECC)"
	@echo "  make models         download model ensemble (~700 GB, resumable)"
	@echo "  make render         resolve model paths into final llama-swap config"
	@echo "  make serve          start llama-swap (launchd service)"
	@echo "  make stop           stop llama-swap"
	@echo "  make status         service + model + memory status"
	@echo "  make verify         offline end-to-end smoke test"
	@echo "  make doctor         full diagnostic with suggested fixes"
	@echo "  make harden         install optional pf firewall profile (offline enforcement)"
	@echo "  make dist           package a clean tarball for transfer to the Mac"
	@echo "  make uninstall      remove services/symlinks (add PURGE=1 to delete models)"
	@echo "  make claude         interactive session (Claude Code engine)"
	@echo "  make opencode       interactive session (OpenCode engine)"
	@echo "  make mission M=<toml> ENGINE=<claude|opencode> HOURS=<n>"
	@echo "  make supabase-up    start self-hosted Supabase stack (localhost only)"
	@echo "  make skills         re-sync skill packs into both engines"
	@echo "  make ecc            (re)install pinned ECC curated subset"

install:
	bash bootstrap/install.sh

models:
	bash bootstrap/download-models.sh

render:
	bash bootstrap/render-config.sh

serve: render
	bash serving/service.sh start

stop:
	bash serving/service.sh stop

status:
	bash serving/service.sh status

verify:
	bash bootstrap/verify-offline.sh

doctor:
	bash bootstrap/doctor.sh

dist:
	bash bootstrap/package.sh

uninstall:
	bash bootstrap/uninstall.sh $(if $(PURGE),--purge,)

harden:
	sudo bash bootstrap/harden-offline.sh

claude:
	bash engines/claude-code/launch.sh

opencode:
	bash engines/opencode/launch.sh

mission:
	$(PY) conductor/conductor.py run $(M) --engine $(ENGINE) --hours $(HOURS)

report:
	@ls -1t reports/ 2>/dev/null | head -5 || echo "no reports yet"

env:
	cd env && uv sync

skills:
	bash bootstrap/sync-skills.sh

ecc:
	bash harness/ecc/install-ecc.sh

supabase-up:
	cd connectors/supabase && docker compose up -d

supabase-down:
	cd connectors/supabase && docker compose down

clean:
	rm -rf .worktrees logs state
