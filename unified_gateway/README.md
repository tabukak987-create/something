# Folder Overview

## Purpose
The `unified_gateway` folder provides a high-level entry or legacy routing mechanism that wraps or links to the main `backend` services.

## Key Files
- `main.py` → Entry point for gateway-specific operations.

## Structure
- Flat structure, acts as a thin wrapper or namespace.

## Rules
- Avoid duplicating logic that already exists in `backend/services`.

## Dependencies
- Operates on top of or alongside the primary `backend` module.
