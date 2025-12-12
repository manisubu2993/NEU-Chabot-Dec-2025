
# app.py — robust routing + synced analytics (multi-reason negatives, mid-use exits, context propagation)
from flask import Flask, request, jsonify, render_template, send_file
from datetime import datetime, timezone
from collections import defaultdict, Counter
import uuid, json, os
import csv  # for free-form CSV capture
import re   # for privacy guard + intent rules

app = Flask(__name__, static_folder='static', template_folder='templates')

STORE_PATH = 'analytics_store.json'
CSV_PATH   = 'feedback_freeform_feedback.csv'

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
    add_event({"type": "session_start", "session_id": session_id, "ts": _now_iso()})
    return jsonify({
        "title": "Hi! I’m your personal NU Assistant",
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
    session_upsert_context(sid, college=college)
    add_event({"type": "set_college", "session_id": sid, "college": college, "ts": _now_iso()})
    return jsonify({
        "prompt": f"Great — {college}. Are you a new or continuing student?",
        "statuses": ["Incoming Student", "Continuing Student"]
    })

@app.route('/api/v2/status', methods=['POST'])
def api_status():
    data = request.get_json(force=True) or {}
    sid = data.get('session_id')
    status = data.get('status')
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


    return jsonify({"ok": True})

@app.route('/api/v2/start_over', methods=['POST'])
def api_start_over():
    data = request.get_json(force=True) or {}
    sid = data.get('session_id')
    add_event({"type": "start_over", "session_id": sid, "ts": _now_iso()})
    return jsonify({
        "title": "Hi! I’m your personal NU Assistant",
        "intro": "How can I help you today?<br>Select your college below to get started.",
        "colleges": [
            "College of Professional Studies",
            "College of Engineering",
            "College of Science"
        ]
    })

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
