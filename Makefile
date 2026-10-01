COMPOSE ?= docker compose
PY ?= python

.PHONY: up down logs build test lint fmt dev demo load

up:            ## поднять весь стек (RabbitMQ + TimescaleDB + сервисы + web)
	$(COMPOSE) up --build -d

down:          ## остановить стек
	$(COMPOSE) down

logs:          ## смотреть логи
	$(COMPOSE) logs -f

build:         ## пересобрать образы
	$(COMPOSE) build

dev:           ## локальный all-in-one (BUS=memory): http://localhost:8000
	BUS=memory $(PY) -m app.all

test:          ## тесты
	$(PY) -m pytest -q

lint:          ## статический анализ
	ruff check app tools tests

fmt:           ## форматирование
	ruff format app tools tests

demo:          ## headless-демо (BUS=memory)
	BUS=memory $(PY) -m tools.e2e_dispatcher

load:          ## нагрузочный тест
	$(PY) -m tools.stress
