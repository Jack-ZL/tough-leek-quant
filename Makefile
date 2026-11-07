.PHONY: dev install install-backend install-frontend clean backend frontend help

SHELL := /bin/bash
ROOT  := $(shell cd "$$(dirname "$$(realpath "$(lastword $(MAKEFILE_LIST))")")" && pwd)
BACKEND_DIR  := $(ROOT)/backend
FRONTEND_DIR := $(ROOT)/frontend

BACKEND_PORT  ?= 3018
FRONTEND_PORT ?= 3011
BACKEND_HOST  ?= 0.0.0.0

define read_env
$(shell awk -v key='$(1)' '$$0~"^[[:space:]]*"key"[[:space:]]*="{sub(/^[^=]*=/,"");sub(/[[:space:]]+#.*/,"");gsub(/^[[:space:]]+|[[:space:]]+$$/,"");gsub(/^"|\x27|"$$|\x27$$/,"");print;exit}' $(ROOT)/.env 2>/dev/null)
endef

HOST_ORIGIN := $(origin HOST)
ifeq ($(filter undefined file,$(HOST_ORIGIN)),)
else
  	_ENV_HOST := $(call read_env,HOST)
	ifneq ($(_ENV_HOST),)
	BACKEND_HOST := $(_ENV_HOST)
	endif
endif
PORT_ORIGIN := $(origin PORT)
ifeq ($(filter undefined file,$(PORT_ORIGIN)),)
else
  	_ENV_PORT := $(call read_env,PORT)
	ifneq ($(_ENV_PORT),)
	BACKEND_PORT := $(_ENV_PORT)
	endif
endif

DISPLAY_HOST := $(BACKEND_HOST)
ifeq ($(DISPLAY_HOST),0.0.0.0)
  	DISPLAY_HOST := localhost
endif
ifeq ($(DISPLAY_HOST),::)
	DISPLAY_HOST := localhost
endif

C_BLUE   := \033[0;34m
C_GREEN  := \033[0;32m
C_RED    := \033[0;31m
C_YELLOW := \033[0;33m
C_GRAY   := \033[0;90m
C_NC     := \033[0m

dev: install
	@# 清理占用后端端口的旧进程
	@if lsof -ti :$(BACKEND_PORT) >/dev/null 2>&1; then \
		echo -e "$(C_YELLOW)[make]$(C_NC) killing existing process on port $(BACKEND_PORT)..."; \
		lsof -ti :$(BACKEND_PORT) | xargs kill -9 2>/dev/null || true; \
		sleep 1; \
	fi
	@echo -e "$(C_BLUE)================================================$(C_NC)"
	@echo -e "$(C_BLUE)  $(C_GREEN)tough-leek-quant$(C_NC)"
	@echo -e "$(C_BLUE)  backend   $(C_YELLOW)http://$(DISPLAY_HOST):$(BACKEND_PORT)$(C_NC)"
	@echo -e "$(C_BLUE)  frontend  $(C_YELLOW)http://$(DISPLAY_HOST):$(FRONTEND_PORT)$(C_NC)"
	@echo -e "$(C_BLUE)  Ctrl-C    $(C_NC)"
	@echo -e "$(C_BLUE)================================================$(C_NC)"
	@echo ""
	@trap 'kill 0; exit 0' INT TERM; \
	trap 'kill 0' EXIT; \
	( \
		cd $(BACKEND_DIR) && \
		uv run --no-sync python -m uvicorn app.main:app \
			--reload --host $(BACKEND_HOST) --port $(BACKEND_PORT) 2>&1 \
		| while IFS= read -r line; do echo -e "$(C_BLUE)[backend ]$(C_NC) $$line"; done \
	) & \
	BACKEND_PID=$$!; \
	( \
		cd $(FRONTEND_DIR) && \
		BACKEND_HOST=$(BACKEND_HOST) BACKEND_PORT=$(BACKEND_PORT) \
		pnpm dev --host $(BACKEND_HOST) --port $(FRONTEND_PORT) 2>&1 \
		| while IFS= read -r line; do echo -e "$(C_GREEN)[frontend]$(C_NC) $$line"; done \
	) & \
	FRONTEND_PID=$$!; \
	wait -n 2>/dev/null || wait; \
	kill $$BACKEND_PID $$FRONTEND_PID 2>/dev/null || true

backend: install-backend
	@echo -e "$(C_GRAY)[make]$(C_NC) backend http://$(DISPLAY_HOST):$(BACKEND_PORT)"
	@cd $(BACKEND_DIR) && uv run --no-sync python -m uvicorn app.main:app \
		--reload --host $(BACKEND_HOST) --port $(BACKEND_PORT)

frontend: install-frontend
	@echo -e "$(C_GRAY)[make]$(C_NC) frontend http://$(DISPLAY_HOST):$(FRONTEND_PORT)"
	@cd $(FRONTEND_DIR) && BACKEND_HOST=$(BACKEND_HOST) BACKEND_PORT=$(BACKEND_PORT) \
		pnpm dev --host $(BACKEND_HOST) --port $(FRONTEND_PORT)

install: install-backend install-frontend
	@echo -e "$(C_GREEN)[make]$(C_NC) all deps installed"

install-backend:
	@if [ ! -d "$(BACKEND_DIR)/.venv" ]; then \
		echo -e "$(C_GRAY)[make]$(C_NC) installing backend deps..."; \
		cd $(BACKEND_DIR) && uv sync --frozen; \
		echo -e "$(C_GREEN)[make]$(C_NC) backend deps done"; \
	else \
		echo -e "$(C_GRAY)[make]$(C_NC) backend deps already exist"; \
	fi

install-frontend:
	@if [ ! -d "$(FRONTEND_DIR)/node_modules" ]; then \
		echo -e "$(C_GRAY)[make]$(C_NC) installing frontend deps..."; \
		cd $(FRONTEND_DIR) && pnpm install; \
		echo -e "$(C_GREEN)[make]$(C_NC) frontend deps done"; \
	else \
		echo -e "$(C_GRAY)[make]$(C_NC) frontend deps already exist"; \
	fi

clean:
	@echo -e "$(C_GRAY)[make]$(C_NC) cleaning ports $(BACKEND_PORT) $(FRONTEND_PORT)..."
	@for port in $(BACKEND_PORT) $(FRONTEND_PORT); do \
		pids=$$(lsof -nP -tiTCP:$$port -sTCP:LISTEN 2>/dev/null || true); \
		if [ -n "$$pids" ]; then \
			echo -e "$(C_YELLOW)[make]$(C_NC) port $$port occupied, killing PID: $$(echo $$pids | xargs)"; \
			echo $$pids | xargs kill 2>/dev/null || true; \
			sleep 1; \
			pids=$$(lsof -nP -tiTCP:$$port -sTCP:LISTEN 2>/dev/null || true); \
			if [ -n "$$pids" ]; then \
				echo $$pids | xargs kill -9 2>/dev/null || true; \
			fi; \
		else \
			echo -e "$(C_GRAY)[make]$(C_NC) port $$port is free"; \
		fi; \
	done
	@echo -e "$(C_GREEN)[make]$(C_NC) clean done"

help:
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@echo "  dev              Start backend and frontend (default)"
	@echo "  backend          Start backend only"
	@echo "  frontend         Start frontend only"
	@echo "  install          Install all dependencies"
	@echo "  install-backend  Install backend dependencies"
	@echo "  install-frontend Install frontend dependencies"
	@echo "  clean            Kill processes on ports"
	@echo "  help             Show this help"
	@echo ""
	@echo "Env vars:"
	@echo "  BACKEND_PORT=$(BACKEND_PORT)"
	@echo "  FRONTEND_PORT=$(FRONTEND_PORT)"
	@echo "  BACKEND_HOST=$(BACKEND_HOST)"
	@echo ""
	@echo "Examples:"
	@echo "  make"
	@echo "  BACKEND_PORT=8000 make"
	@echo "  make clean"
