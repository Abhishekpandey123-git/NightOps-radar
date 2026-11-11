# NightOps: GSoC 2026 Live Issue Radar

A standalone, deterministic-first live monitoring radar and temporal matrix for Google Summer of Code (GSoC) 2026 organizations and repositories.

## Quick Start

### 1. Direct Browser Launch
Simply double-click `index.html` or open it directly in any modern browser (Chrome, Edge, Firefox, Brave, Safari). No build step, node_modules, or external web server is strictly required.

### 2. Local HTTP Server (Recommended)
To run with a clean local server:
```bash
# Python 3
python3 -m http.server 8080

# Or with npx (Node.js)
npx serve .
```
Then navigate to `http://localhost:8080`.

## Features
- **Temporal Radar Matrix**: Y-axis plots date and release time; X-axis segments monitored GSoC organizations.
- **Pulsing / Popping Alerts**: New and latest issues animate with radiating radar waves.
- **Hover Inspection HUD**: Hover over any dot to see maintainers, mentors, comments count, assignment status, and anchored GSoC project links.
- **Click to Open**: Clicking any issue dot opens the exact GitHub issue URL in a new tab.
- **Automated 10-Minute Polling Loop**: Built-in countdown timer and recurring poll mechanism with synthesized audio chimes.
- **Organization Manager**: Dynamic modal to add and toggle any custom GitHub repo and GSoC project page.
- **Table Feed View**: Alternative tabular ledger for sorting and filtering issues.

## Integration with NightOps Daemon
To connect this interface to your local `NightOps` Python service:
1. Have your `scout` module write detected GSoC issue JSON payloads to a local data file or endpoint (`/api/issues`).
2. Update the `APP_STATE.issues` fetch handler in `index.html` to pull directly from your local daemon API.
