# 👻 NightOps // GSoC 2026 Live Issue Radar

> *"Everything that can be done by plain code is done by plain code, and AI is only called when there's no other way."*

An autonomous, 24/7 live issue monitor and temporal radar matrix for **Google Summer of Code (GSoC) 2026** candidate organizations and repositories. 

Designed for prospective open-source contributors, mentors, and maintainers who need immediate awareness of uncontested issues the moment they are opened.

---

## ⚡ Core Philosophy

NightOps Radar follows an uncompromising **Code-First, Model-Last** architectural doctrine:

1. **Deterministic Execution (95%+ of operations)**: Standard Python code handles GitHub REST API polling, rate-limit budgeting, deduplication, pull-request filtering, comment-claim detection, and HTTP serving. Zero hallucinations, microsecond performance, and zero unnecessary token consumption.
2. **Bounded AI Escalation (Fallback only)**: LLMs (Gemini / OpenAI) are called strictly when a fresh, unassigned issue is detected to evaluate thread ambiguity, estimate difficulty (0–100), and produce a tactical 1-sentence contributor fit summary.

---

## 🎯 Key Features

* **Temporal Radar Matrix (Interactive Graph)**:
  * **Y-Axis**: Monitored GSoC Organizations (SymPy, Open Food Facts, Apache, Blender, Django, VideoLAN, CNCF / Kubernetes).
  * **X-Axis**: Real detection timestamps formatted strictly as `DD/MM/YYYY` (progressing chronologically from historical releases to `LIVE (NOW)`).
* **Pulsing / Popping Alerts**:
  * Newly detected issues animate with radiating radar ripples (`dot pops`) and trigger a Web Audio API synthesized chirp.
* **Glowing Vertical Laser Beam**:
  * Hovering over any issue dot projects a vertical neon guide line straight down to the X-axis, illuminating the exact detection timestamp (`⚡ Detected: DD/MM/YYYY HH:mm:ss`) and target bead.
* **Rich Hover Inspection HUD**:
  * Instant readout of issue title, number, maintainers/mentors, discussion activity, assignment status, and anchored GSoC project links.
* **One-Click Navigation**:
  * Clicking any issue dot opens the exact GitHub issue URL in a new tab.
* **Automated 10-Minute Polling Loop**:
  * Background daemon polls configured repositories every 600 seconds, with countdown ticker and manual *Poll Now* trigger.
* **Dual View (Radar & Table)**:
  * Toggle between the visual 2D temporal radar and a filterable tabular ledger.
* **Zero Mandatory Dependencies**:
  * The backend `server.py` runs natively using Python's standard library (`http.server`, `urllib`, `sqlite3`), requiring zero `pip install` commands to get started.

---

## 🏗️ Architecture

```
┌────────────────────────────────────────────────────────┐
│             1. DETERMINISTIC SCRAPER (CODE)            │
│  - Background daemon runs every 10 minutes             │
│  - Scrapes /repos/{owner}/{repo}/issues                │
│  - Filters PRs, checks assignees, drops claimed issues │
└──────────────────────────┬─────────────────────────────┘
                           │ (Only if unassigned & fresh)
                           ▼
┌────────────────────────────────────────────────────────┐
│            2. AI GATEWAY ESCALATION (LLM)              │
│  - Scored by Gemini 1.5 Flash / GPT-4o-mini            │
│  - Outputs difficulty (1-100) & 1-line fit summary     │
└──────────────────────────┬─────────────────────────────┘
                           │
                           ▼
┌────────────────────────────────────────────────────────┐
│            3. HTTP API & RADAR DASHBOARD               │
│  - Serves /api/gsoc/issues & live Web UI               │
│  - Renders glowing temporal matrix & detection beams   │
└────────────────────────────────────────────────────────┘
```

---

## 🚀 Quick Start

### 1. Run in GitHub Codespaces or Local Machine

Clone and launch directly with standard Python 3:

```bash
# Clone the repository
git clone https://github.com/Abhishekpandey123-git/NightOps-radar.git
cd NightOps-radar

# Start the independent backend
python3 server.py
```

The radar will automatically:
1. Start the background 10-minute poller.
2. Fetch live issues across configured GSoC repositories.
3. Serve the interactive dashboard at **`http://localhost:8080`**.

### 2. Standalone Frontend (GitHub Pages)

You can also host the frontend statically on GitHub Pages:
1. In your GitHub repository, go to **Settings** → **Pages**.
2. Select **Deploy from a branch** (`main` / `root`) and click **Save**.
3. Access the dashboard from any device at `https://abhishekpandey123-git.github.io/NightOps-radar/`.

---

## ⚙️ Configuration

### `config.yaml`

Modify monitored organizations, target repositories, mentors, and polling frequency:

```yaml
poll_interval_seconds: 600 # 10 minutes

organizations:
  - id: sympy
    name: SymPy
    repo: sympy/sympy
    gsocUrl: https://summerofcode.withgoogle.com/programs/2026/organizations/sympy
    mentors: "@oscarbenjamin, @asmeurer"
    active: true

  - id: openfoodfacts
    name: Open Food Facts
    repo: openfoodfacts/openfoodfacts-server
    gsocUrl: https://summerofcode.withgoogle.com/programs/2026/organizations/open-food-facts
    mentors: "@teolemon, @alexgarel"
    active: true
```

### Environment Variables (`.env`)

Optional API credentials to lift rate limits and enable LLM triage:

```bash
# Optional: Raises GitHub API rate limits from 60/hr to 5,000/hr
GITHUB_TOKEN=ghp_yourPersonalAccessTokenHere

# Optional: Enables AI difficulty scoring and tactical fit summaries
GEMINI_API_KEY=your_gemini_api_key_here
# or OPENAI_API_KEY=your_openai_api_key_here

PORT=8080
HOST=0.0.0.0
```

---

## 📡 API Reference

| Endpoint | Method | Description |
| :--- | :--- | :--- |
| `GET /` | `GET` | Serves the interactive radar dashboard (`index.html`) |
| `GET /api/gsoc/issues` | `GET` | Returns all detected issues, timestamps, maintainers, and AI summaries (CORS enabled) |
| `GET /api/gsoc/poll` | `GET / POST` | Triggers an immediate out-of-band poll cycle |
| `GET /api/health` | `GET` | Service health, tracked issue counts, and AI gateway status |

---

## 📂 Project Structure

```
NightOps-radar/
├── server.py              # Standalone HTTP server & daemon scheduler
├── backend/
│   ├── scout.py           # GitHub issue poller & claim detection
│   └── ai_gateway.py      # LLM escalation client & heuristic fallbacks
├── config.yaml            # Organization roster & polling schedule
├── index.html             # Standalone radar matrix frontend
├── run.sh                 # One-click startup runner
├── .env.example           # Environment template
├── .gitignore             # Git exclusion rules
├── LICENSE                # Apache-2.0 License
└── README.md              # Documentation
```

---

## 📜 License

Licensed under the [Apache License, Version 2.0](LICENSE).
