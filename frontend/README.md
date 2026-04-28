# Folder Overview

## Purpose
The `frontend` folder contains the web-based user interface for the PF Trust Investment System. It provides a visual dashboard for institutional fixed-income operations.

## Key Files
- `index.html` → Main entry point and layout for the single-page application.
- `bond_agent_ui.html` → Separate UI specifically for the bond agent interactions.
- `test-login.html` → Authentication testing UI.

## Structure
- `css/` → Contains stylesheets (e.g., `app.css`).
- `js/` → Contains client-side JavaScript (`app.js`, `api.js`, `init.js`, etc.).
- `dashboards/` → Specialized dashboard views or templates.

## Rules
- Avoid heavy framework dependencies (Vanilla JS/HTML/CSS preferred).
- Local copies of libraries (like Chart.js) are used instead of CDNs to allow operation in restricted environments.
- Support both dark and light modes cleanly (`data-theme="light"`/`"dark"`).

## Dependencies
- Depends on the `backend` API (`/api/v1/*`) to fetch data and process operations.
