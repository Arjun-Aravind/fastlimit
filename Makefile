# FastLimit Development Makefile

.PHONY: help install dev test lint format clean docker-up docker-down docker-test benchmark

# Colors for terminal output
RED := \033[0;31m
GREEN := \033[0;32m
YELLOW := \033[1;33m
NC := \033[0m # No Color

help: ## Show this help message
	@echo "$(GREEN)FastLimit - Rate Limiting Library$(NC)"
	@echo ""
	@echo "Available commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  $(YELLOW)%-15s$(NC) %s\n", $$1, $$2}'

install: ## Install dependencies with Poetry
	@echo "$(GREEN)Installing dependencies...$(NC)"
	poetry install

dev: ## Start development environment (Redis only)
	@echo "$(GREEN)Starting development environment...$(NC)"
	docker-compose -f docker-compose.dev.yml up -d
	@echo "$(GREEN)Redis is running on localhost:6379$(NC)"
	@echo "$(GREEN)RedisInsight is available at http://localhost:8001$(NC)"

test: ## Run test suite
	@echo "$(GREEN)Running tests...$(NC)"
	poetry run pytest tests/ -v --tb=short

test-cov: ## Run tests with coverage
	@echo "$(GREEN)Running tests with coverage...$(NC)"
	poetry run pytest tests/ --cov=fastlimit --cov-report=html --cov-report=term

lint: ## Run linting checks
	@echo "$(GREEN)Running linting checks...$(NC)"
	poetry run ruff check fastlimit/ tests/ examples/
	poetry run mypy fastlimit/ --ignore-missing-imports

format: ## Format code with black
	@echo "$(GREEN)Formatting code...$(NC)"
	poetry run black fastlimit/ tests/ examples/
	poetry run ruff check --fix fastlimit/ tests/ examples/

clean: ## Clean up cache and build files
	@echo "$(GREEN)Cleaning up...$(NC)"
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	find . -type f -name ".coverage" -delete
	rm -rf .pytest_cache/
	rm -rf .mypy_cache/
	rm -rf htmlcov/
	rm -rf dist/
	rm -rf build/
	rm -rf *.egg-info

docker-up: ## Start all services with Docker Compose
	@echo "$(GREEN)Starting Docker services...$(NC)"
	docker-compose up -d
	@echo "$(GREEN)Services started:$(NC)"
	@echo "  - FastAPI Demo: http://localhost:8000"
	@echo "  - Multi-tenant Demo: http://localhost:8001"
	@echo "  - Redis: localhost:6379"

docker-down: ## Stop all Docker services
	@echo "$(YELLOW)Stopping Docker services...$(NC)"
	docker-compose down

docker-test: ## Run tests in Docker
	@echo "$(GREEN)Running tests in Docker...$(NC)"
	docker-compose --profile test run --rm tests

docker-build: ## Build Docker images
	@echo "$(GREEN)Building Docker images...$(NC)"
	docker-compose build

benchmark: ## Run performance benchmark
	@echo "$(GREEN)Running performance benchmark...$(NC)"
	docker-compose --profile benchmark run --rm benchmark

demo: ## Run the algorithms demo
	@echo "$(GREEN)Running algorithms demo...$(NC)"
	poetry run python examples/algorithms_demo.py

run-app: ## Run the example FastAPI app
	@echo "$(GREEN)Starting FastAPI demo app...$(NC)"
	poetry run uvicorn examples.fastapi_app:app --reload --port 8000

run-tenant: ## Run the multi-tenant example
	@echo "$(GREEN)Starting multi-tenant demo...$(NC)"
	poetry run uvicorn examples.multi_tenant:app --reload --port 8001

pre-commit: ## Install pre-commit hooks
	@echo "$(GREEN)Installing pre-commit hooks...$(NC)"
	poetry run pre-commit install

pre-commit-run: ## Run pre-commit on all files
	@echo "$(GREEN)Running pre-commit checks...$(NC)"
	poetry run pre-commit run --all-files

publish-test: ## Publish to TestPyPI
	@echo "$(YELLOW)Publishing to TestPyPI...$(NC)"
	poetry config repositories.testpypi https://test.pypi.org/legacy/
	poetry publish --build -r testpypi

publish: ## Publish to PyPI
	@echo "$(RED)Publishing to PyPI...$(NC)"
	@echo "$(RED)Are you sure? Press Ctrl+C to cancel$(NC)"
	@sleep 3
	poetry publish --build

.DEFAULT_GOAL := help
