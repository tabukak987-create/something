# Navigation Guide

When exploring the repository, adhere to the following rules:

1. **Domain Logic:** Always look inside `backend/app/domain/<domain_name>` for business logic. Do not look for flat services in `app/services/`.
2. **API Endpoints:** Expected to be in `backend/app/api/` (if present) or within specific domain boundary routes.
3. **Tests:** All tests are contained within the root `tests/` directory.
4. **AI Rules:** Refer to the files in `.aicontext/` for rules regarding AI assistance, file maps, and system constraints.
