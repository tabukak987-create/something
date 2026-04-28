# Infrastructure Folder Summary

## Purpose
The `infrastructure` directory contains definitions for deployments, containerization, and reverse proxies. It is not part of the core Python runtime but wraps it for distribution.

## Key Files
- **[CRITICAL]** `docker-compose.yml`: Defines the local and primary Docker orchestration setup.
- **[CRITICAL]** `nginx.conf`: Directs incoming HTTP/HTTPS traffic to the FastAPI application and serves static frontend files.
- `Dockerfile`: Defines the system dependencies for the FastAPI Python image.

## Token Efficiency Notes
- There are no large source code files here.
- Usually, you only need to modify `docker-compose.yml` if external dependencies (like Redis or PostgreSQL) are added.
