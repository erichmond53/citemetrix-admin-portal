"""Segment query builder: turns a saved lead_segments.conditions JSON (flat AND array) into
a parameterized SQL WHERE clause against `leads l`. Kept in its own module rather than
leads_drip.py (already large, campaign-specific) since this is called from both the
automation-enroll route and any segment member-count preview.

Flat AND only (v1 scope, chosen over nested AND/OR to keep this shippable) -- every condition
in conditions['all'] must hold. Field names are looked up against a fixed whitelist before any
SQL is built; an unrecognized field or op raises ValueError before touching the database, so no
user-supplied string ever reaches raw SQL -- only %s-bound params carry values, same discipline
as every other query in this codebase.
"""
import json


def _tag_clause(op, value):
    if op == 'has':
        return 'EXISTS (SELECT 1 FROM lead_tags lt WHERE lt.lead_id=l.id AND lt.tag_id=%s)', [value]
    if op == 'not_has':
        return 'NOT EXISTS (SELECT 1 FROM lead_tags lt WHERE lt.lead_id=l.id AND lt.tag_id=%s)', [value]
    raise ValueError(f"unsupported op {op!r} for field 'tag'")


def _list_clause(op, value):
    if op == 'in':
        return 'EXISTS (SELECT 1 FROM list_memberships lm WHERE lm.lead_id=l.id AND lm.list_id=%s)', [value]
    raise ValueError(f"unsupported op {op!r} for field 'list'")


def _eq_clause(column):
    def handler(op, value):
        if op != 'eq':
            raise ValueError(f"unsupported op {op!r} for field targeting {column}")
        return f'{column} = %s', [value]
    return handler


def _contains_clause(column):
    def handler(op, value):
        if op != 'contains':
            raise ValueError(f"unsupported op {op!r} for field targeting {column}")
        return f'{column} LIKE %s', [f'%{value}%']
    return handler


def _created_at_clause(op, value):
    if op != 'between':
        raise ValueError(f"unsupported op {op!r} for field 'created_at'")
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("'created_at' between requires a [start, end] value")
    return 'l.created_at BETWEEN %s AND %s', [value[0], value[1]]


FIELD_HANDLERS = {
    'tag': _tag_clause,
    'list': _list_clause,
    'original_source': _eq_clause('l.original_source'),
    'stage': _eq_clause('l.stage'),
    'source_ref_id': _eq_clause('l.original_source_ref_id'),
    'company': _contains_clause('l.company'),
    'title': _contains_clause('l.title'),
    'created_at': _created_at_clause,
}


def build_segment_sql(conditions: dict):
    """conditions: {"all": [{"field": ..., "op": ..., "value": ...}, ...]}.
    Returns (where_sql, params) for `SELECT ... FROM leads l WHERE {where_sql}`.

    Raises ValueError on an empty condition list -- a segment with zero conditions would match
    every lead, which is never the intent for an enrollment source, so at least one condition
    is required -- or on any unrecognized field/op/malformed value."""
    rows = (conditions or {}).get('all') or []
    if not rows:
        raise ValueError("a segment needs at least one condition")

    clauses = []
    params = []
    for row in rows:
        field = row.get('field')
        op = row.get('op')
        value = row.get('value')
        handler = FIELD_HANDLERS.get(field)
        if handler is None:
            raise ValueError(f"unrecognized segment field {field!r}")
        clause_sql, clause_params = handler(op, value)
        clauses.append(clause_sql)
        params.extend(clause_params)

    return ' AND '.join(clauses), params


def describe_condition(row, tag_names=None, list_names=None):
    """Plain-English rendering of one condition row, for the segments list page -- resolves
    tag/list ids to their real names when available (falls back to '#id' for a deleted tag
    or list, rather than erroring)."""
    field = row.get('field')
    op = row.get('op')
    value = row.get('value')
    if field == 'tag':
        name = (tag_names or {}).get(value, f'#{value}')
        return f'{"has" if op == "has" else "does not have"} tag "{name}"'
    if field == 'list':
        name = (list_names or {}).get(value, f'#{value}')
        return f'is in list "{name}"'
    if field == 'created_at' and isinstance(value, (list, tuple)) and len(value) == 2:
        return f'created between {value[0]} and {value[1]}'
    if op == 'contains':
        return f'{field} contains "{value}"'
    return f'{field} = "{value}"'


def describe_segment(conditions, tag_names=None, list_names=None):
    """Plain-English summary of a full flat-AND condition set, e.g.
    'has tag "campaign:marblism" AND created between 2026-09-01 and 2026-09-22'."""
    rows = (conditions or {}).get('all') or []
    if not rows:
        return '(no conditions)'
    return ' AND '.join(describe_condition(r, tag_names, list_names) for r in rows)


def segment_member_ids(cursor, segment_id: int):
    """Evaluate a saved segment live -- no materialization, no sync job (a segment is
    evaluated fresh every time it's read; nothing keeps enrolling new matches afterward,
    per this build's own static-not-live scope). Returns a list of matching lead ids."""
    cursor.execute("SELECT conditions FROM lead_segments WHERE id=%s", (segment_id,))
    row = cursor.fetchone()
    if not row:
        raise ValueError(f"no such segment: {segment_id}")
    conditions = row['conditions']
    if isinstance(conditions, str):
        conditions = json.loads(conditions)
    where_sql, params = build_segment_sql(conditions)
    cursor.execute(f"SELECT l.id FROM leads l WHERE {where_sql}", params)
    return [r['id'] for r in cursor.fetchall()]
