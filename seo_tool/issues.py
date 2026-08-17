"""
Modèle d'une issue SEO (snapshot `seo_issues`). Idempotent : l'id est un hash de
(type, object_id, field) -> re-crawler ne crée pas de doublon.
"""

from __future__ import annotations
import hashlib

# Pondération de sévérité pour le score global.
SEVERITY_WEIGHT = {"high": 3, "medium": 2, "low": 1}


def issue_id(type_: str, object_id: str, field: str) -> str:
    return hashlib.sha1(f"{type_}|{object_id}|{field}".encode()).hexdigest()[:16]


def make_issue(type_, severity, object_type, object_id, handle, field,
               current_value="", context=None, priority_score=0):
    return {
        "issue_id": issue_id(type_, object_id, field),
        "type": type_,
        "severity": severity,
        "object_type": object_type,
        "object_id": object_id,
        "handle": handle,
        "field": field,
        "current_value": current_value or "",
        "context": context or {},
        "suggested_value": "",
        "status": "open",
        "priority_score": int(priority_score),
    }


def empty(v) -> bool:
    return v is None or (isinstance(v, str) and v.strip() == "")
