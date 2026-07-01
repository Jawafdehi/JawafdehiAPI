#!/bin/bash
set -e

# Format and lint Django project

if [ "$1" = "--check" ]; then
    echo "Checking code formatting..."
    poetry run black --check .
    poetry run ruff check .
else
    echo "Formatting code..."
    poetry run black .
    poetry run ruff check --fix .
fi
