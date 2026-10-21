"""Read-only dashboard API. Importing this module never connects to a database."""

from decimal import Decimal
from http.server import BaseHTTPRequestHandler
import json
import os
from urllib.parse import parse_qs, urlparse


def query(cursor, statement, parameters):
    cursor.execute(statement, parameters)
    return cursor.fetchall()


def payload(cursor, endpoint, run_id, issue_id=None):
    if endpoint == 'overview':
        info = query(cursor, 'SELECT run_id,summary,declared_scope_count,loaded_at FROM runs WHERE run_id=%s', (run_id,))
        if not info:
            return None
        totals = query(cursor, """SELECT COUNT(*) AS all_ids,
            COUNT(*) FILTER (WHERE status='completed') AS completed,
            COUNT(*) FILTER (WHERE status='quarantined') AS quarantined,
            COUNT(*) FILTER (WHERE status='pending') AS pending,
            COUNT(*) FILTER (WHERE status='completed' AND needs_review) AS needs_review
            FROM records WHERE run_id=%s""", (run_id,))[0]
        output = dict(run=info[0], totals=totals)
        output['quarantined_by_reason'] = query(cursor,
            "SELECT reason,COUNT(*) AS count FROM records WHERE run_id=%s AND status='quarantined' GROUP BY reason ORDER BY reason", (run_id,))
        output['topics'] = query(cursor, "SELECT topic AS label,COUNT(*) AS count FROM records WHERE run_id=%s AND status='completed' GROUP BY topic ORDER BY count DESC,topic", (run_id,))
        output['intents'] = query(cursor, "SELECT intent AS label,COUNT(*) AS count FROM records WHERE run_id=%s AND status='completed' GROUP BY intent ORDER BY count DESC,intent", (run_id,))
        output['severities'] = query(cursor, "SELECT severity AS label,COUNT(*) AS count FROM records WHERE run_id=%s AND status='completed' GROUP BY severity ORDER BY severity", (run_id,))
        verified = query(cursor, 'SELECT summary FROM verify_summary WHERE run_id=%s', (run_id,))
        output['verifier'] = verified[0]['summary'] if verified else None
        return output
    if endpoint == 'ranking':
        return query(cursor, '''SELECT k.*,i.name,i.description FROM ranking k JOIN issues i
            ON i.run_id=k.run_id AND i.issue_id=k.issue_id WHERE k.run_id=%s ORDER BY k.rank''', (run_id,))
    if endpoint == 'issues':
        issues = query(cursor, '''SELECT i.*,k.rank,k.complaint_count,k.severity_sum,k.mean_severity,k.priority_score
            FROM issues i LEFT JOIN ranking k ON i.run_id=k.run_id AND i.issue_id=k.issue_id
            WHERE i.run_id=%s AND i.issue_id=%s''', (run_id, issue_id))
        if not issues:
            return None
        issue = issues[0]
        issue['reviews'] = query(cursor, '''SELECT r.review_id,r.topic,r.intent,r.severity,r.sentiment,
            r.needs_review,r.evidence_quote,r.review_rating,r.review_timestamp,r.app_version
            FROM membership m JOIN records r ON r.review_id=m.review_id AND r.run_id=m.run_id
            WHERE m.run_id=%s AND m.issue_id=%s AND r.status='completed'
            ORDER BY r.severity DESC,r.review_id LIMIT 20''', (run_id, issue_id))
        return issue
    if endpoint == 'memo':
        saved = query(cursor, 'SELECT markdown,check_status FROM memo WHERE run_id=%s', (run_id,))
        if not saved:
            return None
        return dict(saved[0], claims=payload(cursor, 'claims', run_id))
    if endpoint == 'claims':
        return query(cursor, '''SELECT c.claim_id,c.issue_id,c.metric,c.value FROM claims c
            JOIN ranking k ON k.run_id=c.run_id AND k.issue_id=c.issue_id
            WHERE c.run_id=%s ORDER BY k.rank,c.claim_id''', (run_id,))
    raise ValueError('Unknown endpoint')


def dispatch(endpoint, run_id, issue_id=None):
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row, connect_timeout=10) as db:
        with db.cursor() as cursor:
            cursor.execute('SET TRANSACTION READ ONLY')
            return payload(cursor, endpoint, run_id, issue_id)


def json_default(value):
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    raise TypeError('Unsupported JSON value')


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        params = parse_qs(url.query)
        path = url.path.rstrip('/').split('/')
        endpoint = params.get('endpoint', [path[2] if len(path)>2 else 'overview'])[0]
        issue_id = params.get('issue_id', [path[3] if len(path)>3 else None])[0]
        if endpoint not in ('overview','ranking','issues','memo','claims'):
            self.respond(404, {'error':'Unknown endpoint'})
            return
        try:
            result = dispatch(endpoint, params.get('run_id',['full_v1'])[0], issue_id)
            self.respond(404 if result is None else 200, {'error':'Not found'} if result is None else result)
        except Exception:
            self.respond(503, {'error':'Database unavailable; check server configuration and load the run.'})

    def respond(self, status, value):
        body = json.dumps(value, default=json_default, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
