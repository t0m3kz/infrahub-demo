#!/bin/bash
uv sync --quiet
uv run invoke start
./scripts/bootsttrap.sh
