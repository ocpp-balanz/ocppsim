# Makefile for ocpp-simulator project

# OS specific stuff. Support both Windows and Linux
ifeq ($(OS), Windows_NT)
	copy=copy
	FixPath=$(subst /,\,$1)
	cmdsep=&
	IS_POETRY := $(shell pip freeze | find "poetry==")
	PLATFORM=Windows
else
	copy=cp
	FixPath=$1
	cmdsep=;
	IS_POETRY := $(shell pip freeze | grep "poetry==")
	PLATFORM=Linux
endif

.install-poetry:
ifndef IS_POETRY
	@echo Installing Poetry...
	pip install poetry
endif

update: .install-poetry
	poetry update

install: .install-poetry
	poetry install

format: .install-poetry
	poetry run isort $(wildcard *.py)
	poetry run black --exclude .venv .
#	poetry run flake8 $(wildcard *.py)

docker:
	@docker build -t ocppsim .
