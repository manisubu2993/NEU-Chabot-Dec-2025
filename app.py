
# app.py — robust routing + synced analytics (multi-reason negatives, mid-use exits, context propagation)
from flask import Flask, request, jsonify, render_template, send_file
from datetime import datetime, timezone
from collections import defaultdict, Counter
import uuid, json, os
import csv  # for free-form CSV capture
import re   # for privacy guard + intent rules

import smtplib
from email.message import EmailMessage


import time  # for cognitive session timestamps
app = Flask(__name__, static_folder='static', template_folder='templates')

STORE_PATH = 'analytics_store.json'
CSV_PATH   = 'feedback_freeform_feedback.csv'

# ---------- Step 6 Enhancement: advisor notification (optional SMTP) ----------
ESCALATION_EMAIL_TO = os.environ.get('ESCALATION_EMAIL_TO', 'g.subramanian@northeastern.edu')
SMTP_HOST = os.environ.get('SMTP_HOST','')
SMTP_PORT = int(os.environ.get('SMTP_PORT','587'))
SMTP_USER = os.environ.get('SMTP_USER','')
SMTP_PASS = os.environ.get('SMTP_PASS','')
SMTP_FROM = os.environ.get('SMTP_FROM', SMTP_USER or 'no-reply@example.com')
SMTP_USE_TLS = os.environ.get('SMTP_USE_TLS','1') not in ('0','false','False')



# ---------- cognitive session state (NO LLM; demo-safe; additive only) ----------
# In-memory session cognition for staging/demo. Does NOT change UI/flow; only tracks progress/context.
COGNITIVE_SESSIONS = {}  # session_id -> dict
COGNITIVE_TTL_SECONDS = 60 * 60 * 4  # 4 hours

def _cog_now():
    return time.time()

def cog_init(session_id: str):
    """Initialize cognitive session state if missing/expired."""
    if not session_id:
        return
    s = COGNITIVE_SESSIONS.get(session_id)
    now = _cog_now()
    if (not s) or (now - s.get("last_active", 0) > COGNITIVE_TTL_SECONDS):
        COGNITIVE_SESSIONS[session_id] = {
            "task": None,              # current task/topic
            "steps_done": [],          # ordered list of completed milestones
            "failed_attempts": 0,      # simple counter (used later)
            "escalation_ready": False, # used later
            "escalation_reason": "",
            "escalation_summary": None,
            "last_topic": None,
            "last_intent": None,
            "created_at": now,
            "last_active": now
        }
    else:
        s["last_active"] = now

def cog_mark(session_id: str, step: str = None, task: str = None):
    """Update cognitive session state in a non-breaking way."""
    if not session_id:
        return
    cog_init(session_id)
    s = COGNITIVE_SESSIONS.get(session_id)
    if not s:
        return
    if task is not None:
        s["task"] = task
    if step:
        # avoid duplicates; keep order
        if step not in s["steps_done"]:
            s["steps_done"].append(step)
    s["last_active"] = _cog_now()

def cog_reset(session_id: str):
    """Reset cognitive state for a session (used by start_over)."""
    if session_id and session_id in COGNITIVE_SESSIONS:
        del COGNITIVE_SESSIONS[session_id]

def cog_note_negative_feedback(session_id: str, *, college=None, status=None, topic=None,
                              intent=None, reason_keys=None, reason_labels=None, note=""):
    """Update cognitive escalation signals without changing UI/flow.

    This is deliberately conservative: it only marks escalation readiness when the
    existing non-LLM escalation rules fire.
    """
    if not session_id:
        return
    cog_init(session_id)
    s = COGNITIVE_SESSIONS.get(session_id)
    if not s:
        return

    # Track last-known context for later (Step 5) UI rendering.
    if topic is not None:
        s["last_topic"] = topic
        s["task"] = topic
    if intent is not None:
        s["last_intent"] = intent

    # Increment a simple failure counter.
    s["failed_attempts"] = int(s.get("failed_attempts") or 0) + 1

    # Evaluate escalation using existing rules.
    rk = set(reason_keys or [])
    esc, why = _should_escalate(session_id, topic, rk, note)
    if esc:
        s["escalation_ready"] = True
        s["escalation_reason"] = why or ""
        s["escalation_summary"] = _build_case_summary(
            session_id, college, status, topic, intent,
            reason_labels or [], note
        )

    s["last_active"] = _cog_now()

REASON_LABELS = {
    'incorrect': 'Information is Incorrect',
    'not_relevant': 'Information is Not Relevant',
    'unclear': 'Information is Unclear',
    'need_more_details': 'I Need More Details'
}
REASON_KEYS = set(REASON_LABELS.keys())
REASON_VALUES = set(REASON_LABELS.values())

# ---------- helpers ----------
def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def load_store():
    if not os.path.exists(STORE_PATH):
        return {"events": [], "sessions": {}}
    try:
        with open(STORE_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {"events": [], "sessions": {}}

def save_store(store):
    with open(STORE_PATH, 'w', encoding='utf-8') as f:
        json.dump(store, f, ensure_ascii=False, indent=2)

def add_event(ev):
    store = load_store()
    store["events"].append(ev)
    if len(store["events"]) > 50000:
        store["events"] = store["events"][-25000:]
    save_store(store)

def parse_date_yyyy_mm_dd(s):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except:
        return None

def within_range(ts_iso, start_d, end_d):
    try:
        ts = datetime.strptime(ts_iso, "%Y-%m-%dT%H:%M:%SZ").date()
    except:
        return False
    if start_d and ts < start_d: return False
    if end_d and ts > end_d: return False
    return True

def canonicalize_reason(label):
    if not label:
        return None
    s = str(label).strip()
    if s in REASON_VALUES:
        for k, v in REASON_LABELS.items():
            if v == s:
                return k
    s2 = s.lower()
    if s in REASON_KEYS: return s
    if 'incorrect' in s2: return 'incorrect'
    if 'irrelevant' in s2 or ('not' in s2 and 'relevant' in s2): return 'not_relevant'
    if 'unclear' in s2: return 'unclear'
    if ('need' in s2 or 'more' in s2) and 'detail' in s2: return 'need_more_details'
    return None

def session_upsert_context(session_id, **kwargs):
    store = load_store()
    sess = store["sessions"].get(session_id, {})
    for k, v in kwargs.items():
        if v is not None:
            sess[k] = v
    store["sessions"][session_id] = sess
    save_store(store)

def get_session_ctx(session_id):
    return load_store()["sessions"].get(session_id, {})

def get_last_topic_for_session(session_id):
    store = load_store()
    for e in reversed(store["events"]):
        if e.get("session_id") == session_id and e.get("type") == "topic_click" and e.get("topic"):
            return e.get("topic")
    return None

def parse_boolish(val):
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ('true','1','yes','y','on'): return True
        if s in ('false','0','no','n','off'): return False
    return None

def serve_template(candidates):
    base = app.template_folder or 'templates'
    for name in candidates:
        path = os.path.join(base, name)
        if os.path.exists(path):
            return render_template(name)
    existing = sorted(os.listdir(base)) if os.path.isdir(base) else []
    return (f"Template not found. Tried: {candidates}. "
            f"cwd={os.getcwd()} templates_folder={base} existing={existing}", 404)

# --- normalize “Incoming Student” to “New Student” in analytics only (for old events)
def _norm_status(s):
    s = (s or '').strip()
    if s.lower().startswith('incoming'):
        return 'New Student'
    return s

# --- append a row to CSV for free-form negative feedback
def _append_freeform_csv(row):
    try:
        new = not os.path.exists(CSV_PATH)
        with open(CSV_PATH, 'a', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            if new:
                w.writerow(['ts','session_id','college','status','topic','reason_key','reason_label','note'])
            w.writerow(row)
    except Exception:
        pass

# ---------- Step 6 Enhancement: send escalation email (safe, optional) ----------
def send_escalation_email(*, to_email: str, subject: str, body: str) -> dict:
    """Send an email if SMTP is configured; otherwise simulate.

    To enable real sending, set env vars: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM.
    """
    to_email = (to_email or '').strip()
    if not to_email:
        return {'sent': False, 'mode': 'simulated', 'error': 'missing_to'}
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
        # demo-safe: do not fail ticketing if mail is not configured
        return {'sent': False, 'mode': 'simulated', 'error': 'smtp_not_configured'}

    try:
        msg = EmailMessage()
        msg['From'] = SMTP_FROM
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.set_content(body)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            if SMTP_USE_TLS:
                s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        return {'sent': True, 'mode': 'smtp'}
    except Exception as e:
        return {'sent': False, 'mode': 'smtp_error', 'error': str(e)}



# ---------- Best-5 enhancements (NO LLM; non-breaking, append-only) ----------
# 1) Privacy guard (block/redact emails, NUIDs, phone numbers) for any free-form text we receive
# 2) Rule-based intent tagging (stored alongside existing events; ignored by old dashboards unless you choose to surface it)
# 3) Smart escalation trigger (logs an escalation event + structured summary; does not change UI)
# 4) Structured case summary (template-based, no AI)
# 5) Knowledge-gap logging (flags unanswered/unclear areas for continuous improvement)

# --- Privacy patterns (conservative) ---
_RE_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
# North American-style phone numbers (very loose; catches common forms)
_RE_PHONE = re.compile(r"(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}")
# NUID: often 8–9 digits; we also catch 'NUID: 001234567' style
_RE_NUID  = re.compile(r"\b(?:nuid\s*[:#-]?\s*)?\d{8,9}\b", re.IGNORECASE)

def _detect_pii_types(text):
    """Return a set of detected PII types in text. No storage of raw PII."""
    found = set()
    if not text:
        return found
    s = str(text)
    if _RE_EMAIL.search(s): found.add("email")
    if _RE_PHONE.search(s): found.add("phone")
    # NUID regex may match other long numbers; we only treat it as NUID if it includes 'nuid' OR is 8-9 digits
    # (still conservative for safety; we redact either way)
    if _RE_NUID.search(s): found.add("nuid")
    return found

def _redact_pii(text):
    """Redact PII from text. Returns (redacted_text, detected_types_set)."""
    if text is None:
        return "", set()
    s = str(text)
    pii = _detect_pii_types(s)
    if not pii:
        return s, set()
    # Redact in a stable order
    s = _RE_EMAIL.sub("[REDACTED_EMAIL]", s)
    s = _RE_PHONE.sub("[REDACTED_PHONE]", s)
    s = _RE_NUID.sub("[REDACTED_NUID]", s)
    return s, pii

def _log_privacy_if_needed(session_id, pii_types, where=""):
    if not pii_types:
        return
    add_event({
        "type": "privacy_block",
        "session_id": session_id,
        "where": where,
        "pii_types": sorted(list(pii_types)),
        "ts": _now_iso()
    })

# --- Rule-based intent tagging ---
_INTENT_BY_TOPIC = {
    "Northeastern Credentials": "Credentials / Login",
    "Course Registration": "Registration",
    "Program Information": "Program Info",
    "Transcripts": "Transcripts",
    "Graduation": "Graduation",
    "Leave of Absence": "Leave / LOA",
    "Appeals & Grade Dispute": "Appeals / Grade Dispute",
    "Registration Error (New Students)": "Registration Error",
    "Deferral and Late Arrivals (New Students)": "Deferral / Late Arrival"
}

_INTENT_KEYWORDS = [
    ("Registration Error", ["error", "unable", "can't register", "registration error", "holds", "prereq", "prerequisite"]),
    ("Credentials / Login", ["password", "login", "signin", "sign in", "mfa", "duo", "credentials"]),
    ("Deferral / Late Arrival", ["deferral", "late", "arrival", "visa delay", "delay"]),
    ("Transcripts", ["transcript"]),
    ("Graduation", ["graduation", "convocation"]),
    ("Appeals / Grade Dispute", ["appeal", "grade dispute", "regrade"]),
    ("Leave / LOA", ["leave of absence", "loa", "withdraw"]),
    ("Program Info", ["program", "course plan", "curriculum", "requirements"]),
]

def infer_intent(topic=None, note=None):
    """Infer intent without LLM. Topic-first, then keyword fallback."""
    if topic and topic in _INTENT_BY_TOPIC:
        return _INTENT_BY_TOPIC[topic]
    blob = " ".join([str(topic or ""), str(note or "")]).lower()
    for label, kws in _INTENT_KEYWORDS:
        if any(k in blob for k in kws):
            return label
    return "General"

# --- Escalation + knowledge gap helpers (non-breaking: logs only) ---
_URGENT_KWS = ("urgent", "asap", "deadline", "today", "tomorrow", "immediately", "stuck", "can't", "cannot", "unable")

def _count_neg_for_session_topic(session_id, topic):
    if not session_id or not topic:
        return 0
    store = load_store()
    c = 0
    for e in store.get("events", []):
        if e.get("session_id") == session_id and e.get("type") == "feedback_negative" and e.get("topic") == topic:
            c += 1
    return c

def _should_escalate(session_id, topic, reason_keys, note):
    """Simple, safe escalation rules. Logs escalation but does not change UI."""
    note_l = (note or "").lower()
    # 1) Repeat failures on same topic in same session
    if _count_neg_for_session_topic(session_id, topic) >= 2:
        return True, "Repeated negative feedback on same topic"
    # 2) Urgency keywords
    if any(k in note_l for k in _URGENT_KWS):
        return True, "Urgency detected in note"
    # 3) Missing content signal
    if (reason_keys and ("need_more_details" in reason_keys)) or (reason_keys and (None in reason_keys)):
        return True, "Needs more details / unknown reason"
    return False, ""

def _build_case_summary(session_id, college, status, topic, intent, reason_labels, note):
    """Template-based advisor-ready summary (no AI)."""
    return {
        "session_id": session_id,
        "college": college or "",
        "student_type": _norm_status(status) or "",
        "topic": topic or "",
        "intent": intent or "General",
        "reasons": [str(r) for r in (reason_labels or []) if str(r).strip()],
        "note": (note or "").strip(),
        "ts": _now_iso()
    }


# ---------- routes: UI ----------
@app.route('/health')
def health():
    return jsonify({"ok": True, "cwd": os.getcwd(), "templates": app.template_folder, "static": app.static_folder})

@app.route('/')
def root():
    return serve_template(['chat_v2.html', 'chat v2.html'])

@app.route('/admin')
def admin():
    return serve_template(['admin_v2.html', 'admin-v2.html', 'admin v2.html', 'admin.html'])

# ---------- minimal bot API ----------
@app.route('/api/v2/start', methods=['POST'])
def api_start():
    data = request.get_json(force=True) or {}
    session_id = data.get('session_id') or str(uuid.uuid4())
    cog_init(session_id)
    cog_mark(session_id, step='session_started')
    add_event({"type": "session_start", "session_id": session_id, "ts": _now_iso()})
    return jsonify({
        "title": "Hi! I’m your Student Support Assistant",
        "intro": "How can I help you today?<br>Select your college below to get started.",
        "colleges": [
            "College of Professional Studies",
            "College of Engineering",
            "College of Science"
        ]
    })

@app.route('/api/v2/college', methods=['POST'])
def api_college():
    data = request.get_json(force=True) or {}
    sid = data.get('session_id')
    college = data.get('college')
    # Guard against accidental “college” pollution (e.g., if a UI bug shows student-type buttons).
    VALID_COLLEGES = {
        "College of Professional Studies",
        "College of Engineering",
        "College of Science"
    }
    INVALID_COLLEGE = {"Incoming Student", "Continuing Student", "New Student"}
    cog_init(sid)
    cog_mark(sid, step='college_selected')
    # If an invalid value comes in, keep the last valid college (if any) instead of overwriting.
    ctx = get_session_ctx(sid)
    if (college in INVALID_COLLEGE) or (college not in VALID_COLLEGES):
        college = ctx.get('college')
    else:
        session_upsert_context(sid, college=college)
        add_event({"type": "set_college", "session_id": sid, "college": college, "ts": _now_iso()})
    college_display = college or "your college"
    return jsonify({
        "prompt": f"Great — {college_display}. Are you a new or continuing student?",
        "statuses": ["Incoming Student", "Continuing Student"]
    })

@app.route('/api/v2/status', methods=['POST'])
def api_status():
    data = request.get_json(force=True) or {}
    sid = data.get('session_id')
    status = data.get('status')
    cog_init(sid)
    cog_mark(sid, step='student_type_selected')
    session_upsert_context(sid, status=status)
    add_event({"type": "set_status", "session_id": sid, "status": status, "ts": _now_iso()})
    topics = ["Northeastern Credentials", "Course Registration", "Program Information",
              "Transcripts", "Graduation", "Leave of Absence", "Appeals & Grade Dispute"]
    return jsonify({
        "confirm": f"Got it — {status}. Choose a topic:",
        "topics": topics, "backLabel": "Back to the Start"
    })

@app.route('/api/v2/topic', methods=['POST'])
def api_topic():
    data = request.get_json(force=True) or {}
    sid = data.get('session_id')
    topic = data.get('topic')
    cog_init(sid)
    cog_mark(sid, step='topic_selected', task=topic)
    ctx = get_session_ctx(sid)
    intent = infer_intent(topic=topic)
    add_event({
        "type": "topic_click",
        "session_id": sid,
        "college": ctx.get('college') or data.get('college'),
        "status": ctx.get('status') or data.get('status'),
        "topic": topic,
        "intent": intent,
        "ts": _now_iso()
    })
    return jsonify({"question": f"You selected {topic}. Did this answer your question?",
                    "yesLabel": "Yes", "noLabel": "No"})

@app.route('/api/v2/answer', methods=['POST'])
def api_answer():
    return jsonify({
        "reply": "Here’s the information you need.",
        "feedbackYes": "This was helpful",
        "feedbackNo": "This wasn’t helpful",
        "startOver": "Back to the Start"
    })

@app.route('/api/v2/feedback', methods=['POST'])
def api_feedback():
    """
    Accepts either:
      A) old style: { choice: 'positive'|'negative', reason?/reasons? ... }
      B) new style from chat_v2 negative-checkbox UI: { helpful: false, reasons: [...], note?, topic? }
      C) new style positive: { helpful: true }
      D) session end: { choice: 'end' }
    """
    data = request.get_json(force=True) or {}
    sid = data.get('session_id')
    ctx = get_session_ctx(sid) if sid else {}

    # Normalize "helpful" and "choice" robustly (fixes positives being counted as negatives)
    helpful_norm = parse_boolish(data.get('helpful'))
    choice_raw = (data.get('choice') or '').strip().lower()

    is_positive = None
    if helpful_norm is not None:
        is_positive = helpful_norm
    elif choice_raw in ('positive','pos','yes','y','helpful','thumbs_up'):
        is_positive = True
    elif choice_raw in ('negative','neg','no','n','not helpful','thumbs_down'):
        is_positive = False
    else:
        # Infer from presence of reasons
        reasons = data.get('reasons')
        if isinstance(reasons, list) and any(str(r).strip() for r in reasons):
            is_positive = False
        elif any(data.get(k) for k in ('reason','reason_label','reason_key')):
            is_positive = False
        else:
            is_positive = True

    college = data.get('college') or ctx.get('college')
    status  = data.get('status')  or ctx.get('status')
    topic   = data.get('topic')   or get_last_topic_for_session(sid)
    intent  = infer_intent(topic=topic, note=data.get('note'))

    if is_positive:
        add_event({
            "type": "feedback_positive",
            "session_id": sid, "college": college, "status": status,
            "topic": topic, "intent": intent, "ts": _now_iso()
        })
        return jsonify({"ok": True})

    # negative path
    raw = data.get('reasons')
    if raw is None:
        raw = data.get('reason') or data.get('reason_label') or data.get('reason_key')
    if not isinstance(raw, list):
        raw = [raw]

    recorded = False
    freeform_labels = []
    reason_keys_seen = []
    reason_labels_seen = []
    for r in raw:
        if r is None or str(r).strip() == '':
            continue
        key = canonicalize_reason(r)
        pretty = REASON_LABELS.get(key) if key else (str(r) or 'Unknown')
        reason_keys_seen.append(key)
        reason_labels_seen.append(pretty)
        add_event({
            "type": "feedback_negative",
            "session_id": sid, "college": college, "status": status,
            "topic": topic, "reason_key": key, "reason_label": pretty, "intent": intent, "ts": _now_iso()
        })
        recorded = True
        if key is None:
            freeform_labels.append(str(r).strip())

    if not recorded:
        reason_keys_seen.append(None)
        reason_labels_seen.append("Unknown")
        add_event({
            "type": "feedback_negative",
            "session_id": sid, "college": college, "status": status,
            "topic": topic, "reason_key": None, "reason_label": "Unknown", "intent": intent, "ts": _now_iso()
        })

    note_raw = (data.get('note') or '')
    note, pii_types = _redact_pii(note_raw)
    _log_privacy_if_needed(sid, pii_types, where='feedback.note')
    note = note.strip()
    if freeform_labels or note:
        ts = _now_iso()
        for label in freeform_labels:
            label_red, label_pii = _redact_pii(label)
            _log_privacy_if_needed(sid, label_pii, where='feedback.reason')
            _append_freeform_csv([ts, sid, college, status, topic, None, label_red, ""])
        if note:
            _append_freeform_csv([ts, sid, college, status, topic, 'freeform_note', 'Freeform Note', note])

    # --- Step 6 support: remember last selected reasons for demo ticket summaries ---
    try:
        cog_init(sid)
        s_cog = COGNITIVE_SESSIONS.get(sid) if sid else None
        if s_cog is not None:
            s_cog["last_reason_labels"] = list(reason_labels_seen)
            s_cog["last_reason_keys"] = list(reason_keys_seen)
            s_cog["last_note"] = note
            COGNITIVE_SESSIONS[sid] = s_cog
    except Exception:
        pass

    # --- Best-5: knowledge gap logging (no UI change) ---
    # Flag gaps when students need more detail or reasons are unknown/freeform.
    if ('need_more_details' in reason_keys_seen) or (None in reason_keys_seen):
        add_event({
            "type": "knowledge_gap",
            "session_id": sid, "college": college, "status": status,
            "topic": topic, "intent": intent,
            "reason_keys": [rk if rk is not None else None for rk in reason_keys_seen],
            "ts": _now_iso()
        })

    # --- Best-5: smart escalation trigger (logs only; UI can ignore) ---
    escalate, esc_reason = _should_escalate(sid, topic, set(reason_keys_seen), note)
    if escalate:
        summary = _build_case_summary(sid, college, status, topic, intent, reason_labels_seen, note)
        add_event({
            "type": "escalation",
            "session_id": sid, "college": college, "status": status,
            "topic": topic, "intent": intent,
            "why": esc_reason,
            "summary": summary,
            "ts": _now_iso()
        })

    # --- Step 4: smart escalation state (silent; UI can ignore) ---
    # This does NOT change the response or flow; it only marks escalation readiness in memory.
    cog_note_negative_feedback(
        sid,
        college=college,
        status=status,
        topic=topic,
        intent=intent,
        reason_keys=reason_keys_seen,
        reason_labels=reason_labels_seen,
        note=note
    )


    return jsonify({"ok": True})


@app.route('/api/v2/escalation_peek', methods=['POST'])
def api_escalation_peek():
    """Return escalation readiness for the current session (demo-safe).

    Step 4 adds this endpoint so Step 5 can optionally show an advisor handoff card
    without changing any existing UI/flow. If the front-end never calls this,
    nothing changes.
    """
    data = request.get_json(force=True) or {}
    sid = data.get('session_id')
    cog_init(sid)
    s = COGNITIVE_SESSIONS.get(sid) if sid else None
    if not s:
        return jsonify({"ok": True, "escalation_ready": False})
    return jsonify({
        "ok": True,
        "escalation_ready": bool(s.get("escalation_ready")),
        "reason": s.get("escalation_reason") or "",
        "summary": s.get("escalation_summary") if s.get("escalation_ready") else None
    })


# ---------- Step 6: demo ticketing (student-visible status) ----------
# Demo-safe in-memory ticket store. Does NOT integrate with any advisor system.
TICKETS = {}  # ticket_id -> dict

def _ticket_now():
    return time.time()

def _ticket_status_for_age(age_s: float) -> str:
    # Kept for backward compatibility; Step 6 enhanced uses explicit resolution.
    return "In Progress"

def _ticket_public_view(t: dict, *, include_admin: bool = False) -> dict:
    """Return ticket view for the student; optionally include admin-only fields."""
    if not t:
        return {"exists": False}

    view = {
        "exists": True,
        "ticket_id": t.get("ticket_id") or "",
        "status": t.get("status") or "In Progress",
        "created_at": t.get("created_at") or "",
        "summary": t.get("summary") or None,
        "email_to": t.get("email_to") or "",
        "email_mode": t.get("email_mode") or "",
        "email_sent": bool(t.get("email_sent")),
    }

    if include_admin:
        view["_admin"] = {
            "resolve_url": t.get("resolve_url") or "",
            "resolve_key": t.get("resolve_key") or ""
        }

    return view

@app.route('/api/v2/start_over', methods=['POST'])
def api_start_over():
    data = request.get_json(force=True) or {}
    sid = data.get('session_id')
    cog_reset(sid)
    # New session cognition begins after start_over
    cog_init(sid)
    cog_mark(sid, step='session_started')
    add_event({"type": "start_over", "session_id": sid, "ts": _now_iso()})
    return jsonify({
        "title": "Hi! I’m your Student Support Assistant",
        "intro": "How can I help you today?<br>Select your college below to get started.",
        "colleges": [
            "College of Professional Studies",
            "College of Engineering",
            "College of Science"
        ]
    })


# ---------- Step 6: ticketing store (persistent + advisor console) ----------
TICKETS_PATH = 'tickets_store.json'

def _load_tickets():
    if not os.path.exists(TICKETS_PATH):
        return {}
    try:
        with open(TICKETS_PATH, 'r', encoding='utf-8') as f:
            return json.load(f) or {}
    except Exception:
        return {}

def _save_tickets(tickets: dict):
    try:
        with open(TICKETS_PATH, 'w', encoding='utf-8') as f:
            json.dump(tickets, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _ticket_make_id():
    return "T-" + uuid.uuid4().hex[:8].upper()

def _ticket_status_label(t: dict) -> str:
    return "Resolved" if (t or {}).get("resolved") else "In Progress"

def _ticket_public_view(t: dict) -> dict:
    if not t:
        return {"exists": False}
    return {
        "exists": True,
        "ticket_id": t.get("ticket_id",""),
        "status": _ticket_status_label(t),
        "created_at": t.get("created_at",""),
        "updated_at": t.get("updated_at",""),
        "college": t.get("college",""),
        "student_type": t.get("student_type",""),
        "topic": t.get("topic",""),
        "reasons": t.get("reasons",[]) or [],
        "note": t.get("note","") or "",
        "advisor_reply": t.get("advisor_reply","") or ""
    }

def _ticket_create_from_payload(session_id: str, payload: dict) -> dict:
    tickets = _load_tickets()
    topic = (payload.get("topic") or "").strip()

    # reuse open ticket for same session+topic
    for t in tickets.values():
        if t.get("session_id") == session_id and (t.get("topic") or "") == topic and not t.get("resolved"):
            return t

    tid = _ticket_make_id()
    now = _now_iso()
    t = {
        "ticket_id": tid,
        "session_id": session_id,
        "created_at": now,
        "updated_at": now,
        "resolved": False,
        "college": payload.get("college","") or "",
        "student_type": _norm_status(payload.get("status") or payload.get("student_type") or "") or "",
        "topic": payload.get("topic","") or "",
        "intent": payload.get("intent","") or "",
        # store ALL selected reasons as LABELS exactly from UI payload
        "reasons": [str(x) for x in (payload.get("reasons") or []) if str(x).strip()],
        "note": (payload.get("note") or "").strip(),
        "advisor_reply": ""
    }
    tickets[tid] = t
    _save_tickets(tickets)
    return t

def _ticket_resolve(ticket_id: str, reply: str = "") -> dict:
    tickets = _load_tickets()
    t = tickets.get(ticket_id)
    if not t:
        return {}
    t["advisor_reply"] = (reply or "").strip()
    t["resolved"] = True
    t["updated_at"] = _now_iso()
    tickets[ticket_id] = t
    _save_tickets(tickets)
    return t


@app.route('/ticketing')
def ticketing_page():
    return serve_template(['ticketing.html'])

@app.route('/api/v2/ticket_create', methods=['POST'])
def api_ticket_create():
    data = request.get_json(force=True) or {}
    sid = (data.get('session_id') or '').strip()
    if not sid:
        return jsonify({"ok": False, "error": "missing_session_id"}), 400

    # PII-safe note
    note_raw = data.get("note") or ""
    note, pii_types = _redact_pii(note_raw)
    _log_privacy_if_needed(sid, pii_types, where='ticket.note')
    data["note"] = note.strip()

    # Convert reason keys to labels if needed, but keep labels as-is
    reasons = data.get("reasons")
    if isinstance(reasons, list):
        fixed=[]
        for r in reasons:
            r = str(r).strip()
            if r in REASON_LABELS:
                fixed.append(REASON_LABELS.get(r))
            else:
                fixed.append(r)
        data["reasons"] = [x for x in fixed if x]

    ctx = get_session_ctx(sid)
    # Step 6: if UI did not send reasons/note/topic, use last captured negative feedback
    if not (data.get('reasons') and isinstance(data.get('reasons'), list) and len(data.get('reasons'))>0):
        try:
            data['reasons'] = ctx.get('last_negative_reasons') or []
        except Exception:
            data['reasons'] = []
    if not (data.get('note') and str(data.get('note')).strip()):
        try:
            data['note'] = (ctx.get('last_negative_note') or '').strip() or (data.get('note') or '')
        except Exception:
            pass
    if not (data.get('topic') and str(data.get('topic')).strip()):
        try:
            data['topic'] = (ctx.get('last_negative_topic') or '').strip() or (data.get('topic') or '')
        except Exception:
            pass
    data.setdefault("college", ctx.get("college"))
    data.setdefault("status", ctx.get("status"))
    data.setdefault("topic", data.get("topic") or get_last_topic_for_session(sid))
    data.setdefault("intent", infer_intent(topic=data.get("topic"), note=data.get("note")))

    t = _ticket_create_from_payload(sid, data)

    try:
        add_event({"type":"ticket_created","session_id":sid,"college":t.get("college"),"status":t.get("student_type"),
                   "topic":t.get("topic"),"ticket_id":t.get("ticket_id"),"ts":_now_iso()})
    except Exception:
        pass

    return jsonify({"ok": True, "ticket": _ticket_public_view(t)})

@app.route('/api/v2/ticket_status', methods=['GET'])
def api_ticket_status():
    ticket_id = (request.args.get('ticket_id') or '').strip()
    sid = (request.args.get('session_id') or '').strip()

    tickets = _load_tickets()
    t = tickets.get(ticket_id) if ticket_id else None

    # fallback by session
    if not t and sid:
        candidates = [x for x in tickets.values() if x.get("session_id")==sid]
        candidates.sort(key=lambda x: x.get("created_at",""), reverse=True)
        if candidates:
            t = candidates[0]

    return jsonify({"ok": True, "ticket": _ticket_public_view(t)})

@app.route('/api/v2/tickets_list', methods=['GET'])
def api_tickets_list():
    start = request.args.get('start','').strip()
    end   = request.args.get('end','').strip()
    college = request.args.get('college','').strip()
    topic = request.args.get('topic','').strip()

    sd = parse_date_yyyy_mm_dd(start) if start else None
    ed = parse_date_yyyy_mm_dd(end) if end else None

    tickets = _load_tickets()
    items=[]
    for t in tickets.values():
        ts = (t.get("created_at") or "")
        if sd or ed:
            if not within_range(ts, sd, ed):
                continue
        if college and t.get("college") != college:
            continue
        if topic and t.get("topic") != topic:
            continue
        items.append(_ticket_public_view(t))

    items.sort(key=lambda x: x.get("created_at",""), reverse=True)
    return jsonify({"ok": True, "tickets": items})

@app.route('/api/v2/ticket_reply', methods=['POST'])
def api_ticket_reply():
    data = request.get_json(force=True) or {}
    ticket_id = (data.get("ticket_id") or "").strip()
    reply = (data.get("reply") or "").strip()
    if not ticket_id:
        return jsonify({"ok": False, "error": "missing_ticket_id"}), 400

    reply2, pii_types = _redact_pii(reply)
    _log_privacy_if_needed("", pii_types, where='ticket.reply')
    t = _ticket_resolve(ticket_id, reply=reply2)
    if not t:
        return jsonify({"ok": False, "error": "ticket_not_found"}), 404

    try:
        add_event({"type":"ticket_resolved","session_id":t.get("session_id"),"ticket_id":ticket_id,"ts":_now_iso()})
    except Exception:
        pass

    return jsonify({"ok": True, "ticket": _ticket_public_view(t)})

# ---------- analytics ----------
def aggregate(events, college=None, status=None):
    if college:
        events = [e for e in events if e.get('college') == college]
    if status:
        status_norm = _norm_status(status)
        events = [e for e in events if _norm_status(e.get('status')) == status_norm]

    sess_first_college = {}
    for e in events:
        sid = e.get('session_id')
        if sid and sid not in sess_first_college and e.get('college'):
            sess_first_college[sid] = e.get('college')
    by_college_sessions = Counter(sess_first_college.values())

    topic_clicks = [e for e in events if e.get('type') == 'topic_click']
    by_topic = Counter([e.get('topic') for e in topic_clicks if e.get('topic')])

    status_topic = Counter()
    for e in topic_clicks:
        st, tp = e.get('status'), e.get('topic')
        st = _norm_status(st)
        if st and tp:
            status_topic[f"{st} | {tp}"] += 1

    pos = [e for e in events if e.get('type') == 'feedback_positive']
    neg = [e for e in events if e.get('type') == 'feedback_negative']

    pos_by_topic = Counter([e.get('topic') for e in pos if e.get('topic')])
    neg_by_topic = Counter([e.get('topic') for e in neg if e.get('topic')])

    pos_by_topic_by_status = defaultdict(Counter)
    neg_by_topic_by_status = defaultdict(Counter)
    for e in pos:
        st, tp = _norm_status(e.get('status')), e.get('topic')
        if st and tp:
            pos_by_topic_by_status[st][tp] += 1
    for e in neg:
        st, tp = _norm_status(e.get('status')), e.get('topic')
        if st and tp:
            neg_by_topic_by_status[st][tp] += 1

    neg_reasons = Counter()
    for e in neg:
        key = e.get('reason_key') or canonicalize_reason(e.get('reason_label'))
        if key in REASON_KEYS:
            neg_reasons[key] += 1

    neg_top_issues = [
        {"topic": t, "question": "Associated negative feedback", "count": int(c)}
        for t, c in neg_by_topic.most_common(8) if t
    ]

    mid = [e for e in events if e.get('type') == 'mid_use']

    # --- Best-5 (non-LLM) ops signals (non-breaking additions) ---
    privacy_blocks = [e for e in events if e.get('type') == 'privacy_block']
    escalations    = [e for e in events if e.get('type') == 'escalation']
    knowledge_gaps = [e for e in events if e.get('type') == 'knowledge_gap']

    intent_counts = Counter()
    for e in events:
        it = (e.get('intent') or '').strip()
        if it:
            intent_counts[it] += 1

    meta = {
        "total_unique_sessions": len(set([e.get('session_id') for e in events if e.get('session_id')])),
        "total_topic_clicks": sum(by_topic.values())
    }
    feedback = {
        "positive": len(pos),
        "negative": len(neg),
        "drop_off": max(0, meta["total_topic_clicks"] - (len(pos) + len(neg))),
        "mid_use": len(mid),
        "neg_reasons": {REASON_LABELS[k]: int(v) for k, v in neg_reasons.items()}
    }

    def kv(counter):
        out = []
        for k, v in counter.items():
            if not k:
                continue
            out.append({"label": str(k).strip(), "value": int(v)})
        return out

    canonical_topics = ["Northeastern Credentials", "Course Registration", "Program Information",
        "Transcripts", "Graduation", "Leave of Absence", "Appeals & Grade Dispute",
        "Registration Error (New Students)", "Deferral and Late Arrivals (New Students)"]
    for t in canonical_topics:
        by_topic.setdefault(t, 0)
        pos_by_topic.setdefault(t, 0)
        neg_by_topic.setdefault(t, 0)

    return {
        "by_college": kv(by_college_sessions),
        "by_topic": kv(by_topic),
        "status_topic": [{"label": k, "value": int(v)} for k, v in status_topic.items()],
        "pos_by_topic": kv(pos_by_topic),
        "neg_by_topic": kv(neg_by_topic),
        "pos_by_topic_by_status": {st: kv(c) for st, c in pos_by_topic_by_status.items()},
        "neg_by_topic_by_status": {st: kv(c) for st, c in neg_by_topic_by_status.items()},
        "neg_top_issues": neg_top_issues,
        "feedback": feedback,
        "neg_reasons": feedback["neg_reasons"],
        "ops": {
            "escalations": len(escalations),
            "knowledge_gaps": len(knowledge_gaps),
            "privacy_blocks": len(privacy_blocks),
            "intents": {k: int(v) for k, v in intent_counts.items()}
        },
        "meta": meta
    }

@app.route('/api/v2/analytics')
def api_analytics():
    q = request.args
    start = parse_date_yyyy_mm_dd(q.get('start') or '')
    end   = parse_date_yyyy_mm_dd(q.get('end') or '')
    college = q.get('college') or None
    status  = q.get('status') or None

    store = load_store()
    events = [e for e in store["events"] if within_range(e.get('ts', ''), start, end)]
    return jsonify(aggregate(events, college=college, status=status))

# ---------- CSV download helpers (non-breaking; used by Admin download button) ----------
@app.route('/api/v2/csv-exists', methods=['GET'])
def api_csv_exists():
    try:
        exists = os.path.exists(CSV_PATH) and os.path.getsize(CSV_PATH) > 0
        size = os.path.getsize(CSV_PATH) if exists else 0
        return jsonify({"exists": bool(exists), "size": int(size), "filename": os.path.basename(CSV_PATH)})
    except Exception:
        return jsonify({"exists": False, "size": 0, "filename": os.path.basename(CSV_PATH)})

@app.route('/download/feedback_freeform_feedback.csv', methods=['GET', 'HEAD'])
def download_negative_csv():
    if not os.path.exists(CSV_PATH):
        return jsonify({"error": "CSV not found"}), 404
    if request.method == 'HEAD':
        # presence check for UI; no body
        return ('', 200)
    # stream as attachment
    return send_file(CSV_PATH, mimetype='text/csv', as_attachment=True, download_name='feedback_freeform_feedback.csv')

if __name__ == '__main__':
    if not os.path.exists(STORE_PATH):
        save_store({"events": [], "sessions": {}})
    app.run(host='0.0.0.0', port=5000, debug=True)
