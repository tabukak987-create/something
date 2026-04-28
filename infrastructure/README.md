# Folder Overview

## Purpose
The `infrastructure` folder holds configurations and scripts required to deploy, migrate, and run the system in higher-level environments (e.g., Docker, staging, production).

## Key Files
- `docker-compose.yml` → Default container configuration for local/deployment.
- `docker-compose.staging.yml` → Environment-specific overrides for staging.
- `Dockerfile` → Container image definition for the backend application.
- `nginx.conf` & `nginx_waf.conf` → Reverse proxy, HTTPS, and Web Application Firewall configurations.
- `migrate.ps1` → Script for database migrations.

## Structure
- Contains mostly top-level configuration files and scripts.

## Rules
- All new external dependencies must be represented in the Dockerfile or docker-compose.
- Keep Nginx configuration synchronized with FastAPI's routing (`/api/v1/`).

## Dependencies
- Relies on Docker engine and Nginx.
