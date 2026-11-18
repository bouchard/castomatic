#!/bin/sh
# Run castomatic from this checkout with uv, from any directory.
exec uv run --quiet --project "$(dirname "$0")" castomatic "$@"
