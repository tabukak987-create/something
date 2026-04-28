# Deployment Checklist — PF Trust Bond Intelligence Platform
=============================================================

**Version:** Read from `VERSION` file (single source of truth)
**Last Updated:** 2026-03-03

---

## Pre-Deployment

- [ ] **Version bump**: Update `VERSION` file and commit
- [ ] **Changelog**: Update `CHANGELOG.md` with all changes since last release
- [ ] **All tests passing**: Run `python -m pytest` — zero failures
- [ ] **Security scan**: Run `python security_pentest.py` — all checks pass
- [ ] **Benchmark**: Run `python performance_benchmark.py` — meets SLA thresholds
- [ ] **No hardcoded secrets**: `grep -rn "password\|secret\|api_key" --include="*.py" | grep -v "os.getenv\|os.environ\|\.get("` returns nothing
- [ ] **Database migration reviewed**: If schema changes, migration tested on staging first
- [ ] **Dependency audit**: `pip audit` — no known vulnerabilities in dependencies
- [ ] **Backup taken**: Run `Backup_Encrypted.ps1` and verify SHA-256 checksum

## Staging Validation

- [ ] **Staging stack up**: `docker compose -f docker-compose.staging.yml up -d`
- [ ] **All healthchecks green**: All 7 services report healthy
- [ ] **Smoke tests pass**: `docker compose -f docker-compose.staging.yml run --rm smoke-test`
- [ ] **Auth flow verified**: Login, MFA setup, token refresh, logout all work
- [ ] **API regression test**: Hit all critical endpoints, verify response schemas
- [ ] **WebSocket connectivity**: Connect to `/ws/prices` and verify data flow
- [ ] **Redis connectivity**: Token blacklist using Redis (not in-memory fallback)
- [ ] **Staging teardown**: `docker compose -f docker-compose.staging.yml down -v`

## Production Deployment

- [ ] **Maintenance window announced**: Notify all stakeholders 24h in advance
- [ ] **Current production backup**: Fresh backup taken immediately before deploy
- [ ] **Blue-green or rolling update**: Never direct-replace running containers
- [ ] **Deploy command**:
  ```bash
  docker compose -f docker-compose.yml pull
  docker compose -f docker-compose.yml up -d --remove-orphans
  ```
- [ ] **Health check verification**: `curl http://localhost:9000/api/v1/health` returns `"status": "healthy"`
- [ ] **Log monitoring**: Watch logs for 15 minutes post-deploy for errors:
  ```bash
  docker compose logs -f --tail=100 gateway
  ```
- [ ] **Functional smoke test**: Verify login, view portfolio, run compliance check

## Post-Deployment

- [ ] **Version confirmed**: Health endpoint returns correct version from `VERSION` file
- [ ] **Monitoring dashboards checked**: Grafana shows normal metrics
- [ ] **Alert channels verified**: Test alert delivery (email, webhook)
- [ ] **Rollback plan confirmed**: Team knows the rollback procedure (see `ROLLBACK_PROCEDURE.md`)
- [ ] **Deploy recorded**: Log deployment in `deployment_log.md` with:
  - Version deployed
  - Deployer name
  - Timestamp
  - Any issues encountered
  - Rollback status (not needed / executed)

## Emergency Contacts

| Role              | Contact           | Escalation Time |
|-------------------|-------------------|-----------------|
| Platform Lead     | [Configure]       | Immediate       |
| DBA               | [Configure]       | 15 minutes      |
| Infrastructure    | [Configure]       | 30 minutes      |
| Compliance Officer| [Configure]       | 1 hour          |

---

**RULE: No deployment proceeds without ALL pre-deployment and staging items checked.**
