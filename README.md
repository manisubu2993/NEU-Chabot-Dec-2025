# Flask Student Support Chatbot

A guided student-support assistant for a university campus, built with **Python / Flask** and deployed to **Azure App Service** through **GitHub Actions CI/CD**.

Students pick their college, their student type (new or continuing) and a topic, such as credentials, course registration, program information, transcripts, graduation, leave of absence, appeals, registration errors, or deferral and late arrival. The assistant then walks them through step-by-step answers with links to the right campus resources. When an answer doesn't help, the app collects structured feedback, decides whether the case needs a human advisor, and opens a ticket that staff can answer from a console.

The assistant is **rule-based by design** (no LLM). Its answers are predictable, auditable, and safe to run without sending student data to an external AI service.

---

## Features

**Guided conversation**
- Multi-step flows by college, student type and topic, with "back to start" and session resume
- Answers link straight to the relevant campus systems and policies

**Privacy guard**
- Free-text feedback is scanned for email addresses, phone numbers and student ID numbers
- Detected personal information is redacted before anything is stored, and the redaction is logged by type only

**Feedback and escalation**
- Thumbs up/down feedback, with multi-select reasons for negative answers (incorrect, not relevant, unclear, need more details) and an optional note
- Rule-based intent tagging: topic first, with a keyword fallback
- Escalation rules: repeated negative feedback on the same topic, urgency keywords ("deadline", "stuck", "today"…), or a request for more detail
- Template-based case summaries an advisor can read at a glance
- Knowledge-gap logging, which flags topics where students repeatedly need more information

**Ticketing**
- Students can open a support ticket from an escalated conversation and check its status
- Staff console (`/ticketing`) for listing, replying to and resolving tickets
- Optional SMTP email notification to advisors (simulated when SMTP isn't configured)

**Analytics dashboard** (`/admin`)
- Sessions, topic clicks, positive/negative feedback by topic, college and student type
- Date-range filtering, mid-session exit tracking, and CSV export of free-text feedback

---

## Architecture

```
Browser (HTML/CSS/JS)                Flask app (app.py)                     Storage
─────────────────────               ─────────────────────                  ─────────────
chat_v2.html      ──► /api/v2/start, /college, /status, /topic   ──►  analytics_store.json
                  ──► /api/v2/feedback  (privacy guard, intent,       (events + sessions)
                                         escalation, gap logging)
                  ──► /api/v2/ticket_create, /ticket_status    ──►  tickets_store.json
ticketing.html    ──► /api/v2/tickets_list, /ticket_reply
admin_v2.html     ──► /api/v2/analytics, CSV download          ──►  feedback CSV
```

- **Backend:** Flask 3, versioned REST API (`/api/v2/...`), Gunicorn in production
- **Frontend:** server-rendered templates with vanilla JavaScript and Chart.js for the dashboard
- **Storage:** lightweight JSON/CSV files, suitable for a pilot (see *Limitations*)
- **Deployment:** GitHub Actions builds on every push to `main` and deploys to Azure App Service

---

## API overview

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/v2/start` | Start a session |
| POST | `/api/v2/college` · `/status` · `/topic` | Step through the guided flow |
| POST | `/api/v2/feedback` | Record feedback (privacy guard, intent, escalation) |
| POST | `/api/v2/escalation_peek` | Check whether a session needs an advisor |
| POST | `/api/v2/ticket_create` | Open a support ticket |
| GET | `/api/v2/ticket_status` · `/tickets_list` | Ticket status / staff list |
| POST | `/api/v2/ticket_reply` | Staff reply and resolve |
| GET | `/api/v2/analytics` | Dashboard data |
| GET | `/health` | Health check |

---

## Run locally

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000. The dashboard is at `/admin` and the ticket console at `/ticketing`.

### Optional configuration (environment variables)

| Variable | Purpose |
|---|---|
| `ESCALATION_EMAIL_TO` | Advisor inbox for escalation emails |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM`, `SMTP_USE_TLS` | Enable real email sending (otherwise simulated) |

---

## Limitations and next steps

This is a pilot. Before a production rollout I would:
- Put `/admin` and `/ticketing` behind single sign-on
- Move from JSON files to a managed database (e.g. PostgreSQL)
- Add automated tests and linting to the CI pipeline
- Add an optional retrieval-augmented LLM layer over the campus knowledge base, keeping the rule-based flows as a guardrail

---

**Author:** Dr. Ganesh Subramanian · December 2025
