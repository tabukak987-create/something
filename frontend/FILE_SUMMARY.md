# Frontend Folder Summary

## Purpose
The `frontend` directory provides the HTML/JS/CSS user interface for the Unified Bond Investment Gateway. It is a single-page application (SPA).

## Key Files
- **[ENTRY]** `index.html`: The root HTML template that loads all styles, scripts, and layout elements.
- **[ENTRY]** `test-login.html`: Utility page for authentication flows.
- **[CRITICAL]** `js/app.js`: Core client-side routing, state management, and UI rendering logic.
- **[CRITICAL]** `js/api.js`: Thin wrapper around the `fetch` API for communicating with `backend/main.py`.

## Structure
- `css/`: Styling definitions.
- `js/`: Client-side JavaScript.
- `dashboards/`: Specific sub-views or dashboard fragments.

## Token Efficiency Notes
- The UI relies heavily on Vanilla JS and local versions of libraries (like `Chart.js`). 
- **DO NOT** read the minified `chart.umd.min.js` file, as it is massive and completely irrelevant to business logic.
