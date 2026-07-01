"""US-038: backend ACL operations for document-level shares.

The data model (US-037) is denormalised: chunk_acl carries one row per
(chunk × principal) grant. Doc-level grants are a *materialisation* — this
module is what turns "share doc X with principal P" into N inserts and
"revoke" into N deletes, behind a small set of operations the API layer
(US-039) and the ingestion pipeline (re-chunk replay) can call.

Three public operations:

  * `grant_doc_to_principal` — idempotent bulk insert via
    `Prefer: resolution=ignore-duplicates` on the (chunk_id, principal_type,
    principal_id) primary key. Returns the count of *newly inserted* rows
    (zero on a re-grant). For a 500-chunk doc it's two HTTP roundtrips
    (one chunk-id lookup + one bulk insert), not a Python loop of 500.

  * `revoke_doc_from_principal` — single `DELETE chunk_acl?chunk_id=in.(...)
    &principal_type=eq.X&principal_id=eq.Y`. Returns the count of removed
    rows.

  * `list_doc_shares` — aggregates chunk_acl rows for a doc into one
    `ShareSummary` per principal, with `display_name` resolved against
    `profiles.email` (users) or `principals.name` (groups).

Two internal helpers used by the re-ingestion hook in main.py:

  * `snapshot_doc_acls` — reads current grants and dedupes to one entry
    per (principal_type, principal_id) so re-chunking can re-apply them
    against the *new* chunks.

  * `replay_doc_acls` — applies a snapshot via `grant_doc_to_principal`
    once. Loops over principals (typically 1–5 per doc, not 500).
"""

from __future__ import annotations

from typing import Literal

import httpx
from pydantic import BaseModel

PrincipalType = Literal["user", "group"]


class ShareSummary(BaseModel):
    """One row in the share dialog — one entry per principal granted access."""

    principal_type: PrincipalType
    principal_id: str
    display_name: str
    granted_at: str  # ISO-8601, the earliest created_at across the doc's chunks


class AclGrant(BaseModel):
    """Snapshot of a doc-level grant — what re-chunking replays.

    `granted_by` may be null on rows inserted before the column was wired up
    or by service-role tooling; the replay path passes it through verbatim.
    """

    principal_type: PrincipalType
    principal_id: str
    granted_by: str | None = None


async def _fetch_chunk_ids(
    http: httpx.AsyncClient,
    supabase_url: str,
    supabase_headers: dict[str, str],
    doc_id: str,
) -> list[str]:
    r = await http.get(
        f"{supabase_url}/rest/v1/chunks",
        params={"document_id": f"eq.{doc_id}", "select": "id"},
        headers=supabase_headers,
    )
    r.raise_for_status()
    return [row["id"] for row in r.json()]


async def grant_doc_to_principal(
    http: httpx.AsyncClient,
    supabase_url: str,
    supabase_headers: dict[str, str],
    doc_id: str,
    principal_type: PrincipalType,
    principal_id: str,
    granted_by: str | None,
) -> int:
    """Materialise a doc-level grant. Returns count of newly inserted rows.

    Idempotent: re-granting an already-granted (doc × principal) returns 0
    because PostgREST treats the conflicting rows as ignored under the
    `resolution=ignore-duplicates` preference and only echoes back the rows
    that were actually written.
    """
    chunk_ids = await _fetch_chunk_ids(http, supabase_url, supabase_headers, doc_id)
    if not chunk_ids:
        return 0
    rows = [
        {
            "chunk_id": cid,
            "principal_type": principal_type,
            "principal_id": principal_id,
            "granted_by": granted_by,
        }
        for cid in chunk_ids
    ]
    r = await http.post(
        f"{supabase_url}/rest/v1/chunk_acl",
        headers={
            **supabase_headers,
            "Prefer": "resolution=ignore-duplicates,return=representation",
        },
        json=rows,
    )
    r.raise_for_status()
    return len(r.json())


async def revoke_doc_from_principal(
    http: httpx.AsyncClient,
    supabase_url: str,
    supabase_headers: dict[str, str],
    doc_id: str,
    principal_type: PrincipalType,
    principal_id: str,
) -> int:
    """Drop every chunk_acl row for the doc × principal. Returns row count."""
    chunk_ids = await _fetch_chunk_ids(http, supabase_url, supabase_headers, doc_id)
    if not chunk_ids:
        return 0
    in_clause = ",".join(chunk_ids)
    r = await http.request(
        "DELETE",
        f"{supabase_url}/rest/v1/chunk_acl",
        params={
            "chunk_id": f"in.({in_clause})",
            "principal_type": f"eq.{principal_type}",
            "principal_id": f"eq.{principal_id}",
        },
        headers={**supabase_headers, "Prefer": "return=representation"},
    )
    r.raise_for_status()
    return len(r.json())


async def list_doc_shares(
    http: httpx.AsyncClient,
    supabase_url: str,
    supabase_headers: dict[str, str],
    doc_id: str,
) -> list[ShareSummary]:
    """One ShareSummary per principal granted access to the doc.

    Uses the doc-owner SELECT policy on chunk_acl (US-038 migration) so the
    caller — who must own the doc for this to return non-empty — can see
    grants made to other users/groups.
    """
    chunk_ids = await _fetch_chunk_ids(http, supabase_url, supabase_headers, doc_id)
    if not chunk_ids:
        return []
    in_clause = ",".join(chunk_ids)
    r = await http.get(
        f"{supabase_url}/rest/v1/chunk_acl",
        params={
            "chunk_id": f"in.({in_clause})",
            "select": "principal_type,principal_id,created_at",
        },
        headers=supabase_headers,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return []

    earliest_at: dict[tuple[str, str], str] = {}
    for row in rows:
        key = (row["principal_type"], row["principal_id"])
        existing = earliest_at.get(key)
        if existing is None or row["created_at"] < existing:
            earliest_at[key] = row["created_at"]

    user_ids = [pid for (ptype, pid) in earliest_at if ptype == "user"]
    group_ids = [pid for (ptype, pid) in earliest_at if ptype == "group"]

    user_emails = await _resolve_user_emails(
        http, supabase_url, supabase_headers, user_ids
    )
    group_names = await _resolve_group_names(
        http, supabase_url, supabase_headers, group_ids
    )

    summaries: list[ShareSummary] = []
    for (ptype, pid), granted_at in earliest_at.items():
        display = (user_emails if ptype == "user" else group_names).get(pid, pid)
        summaries.append(
            ShareSummary(
                principal_type=ptype,  # type: ignore[arg-type]
                principal_id=pid,
                display_name=display,
                granted_at=granted_at,
            )
        )
    summaries.sort(key=lambda s: (s.principal_type, s.granted_at, s.principal_id))
    return summaries


async def principal_has_doc_grant(
    http: httpx.AsyncClient,
    supabase_url: str,
    supabase_headers: dict[str, str],
    doc_id: str,
    principal_type: PrincipalType,
    principal_id: str,
) -> bool:
    """True iff the principal holds at least one chunk_acl grant on the doc.

    A targeted `limit=1` existence probe (one chunk-id lookup + one filtered
    read) for answering "is this doc published to principal P?" — far cheaper
    than `list_doc_shares`, which additionally resolves every grantee's
    profiles.email / principals.name. Uses the same doc-owner SELECT policy.
    """
    chunk_ids = await _fetch_chunk_ids(http, supabase_url, supabase_headers, doc_id)
    if not chunk_ids:
        return False
    in_clause = ",".join(chunk_ids)
    r = await http.get(
        f"{supabase_url}/rest/v1/chunk_acl",
        params={
            "chunk_id": f"in.({in_clause})",
            "principal_type": f"eq.{principal_type}",
            "principal_id": f"eq.{principal_id}",
            "select": "chunk_id",
            "limit": "1",
        },
        headers=supabase_headers,
    )
    r.raise_for_status()
    return len(r.json()) > 0


async def _resolve_user_emails(
    http: httpx.AsyncClient,
    supabase_url: str,
    supabase_headers: dict[str, str],
    user_ids: list[str],
) -> dict[str, str]:
    if not user_ids:
        return {}
    r = await http.get(
        f"{supabase_url}/rest/v1/profiles",
        params={"id": f"in.({','.join(user_ids)})", "select": "id,email"},
        headers=supabase_headers,
    )
    r.raise_for_status()
    return {row["id"]: row["email"] for row in r.json()}


async def _resolve_group_names(
    http: httpx.AsyncClient,
    supabase_url: str,
    supabase_headers: dict[str, str],
    group_ids: list[str],
) -> dict[str, str]:
    if not group_ids:
        return {}
    r = await http.get(
        f"{supabase_url}/rest/v1/principals",
        params={"id": f"in.({','.join(group_ids)})", "select": "id,name"},
        headers=supabase_headers,
    )
    r.raise_for_status()
    return {row["id"]: row["name"] for row in r.json()}


async def snapshot_doc_acls(
    http: httpx.AsyncClient,
    supabase_url: str,
    supabase_headers: dict[str, str],
    doc_id: str,
) -> list[AclGrant]:
    """Read current chunk_acl rows for a doc, deduped to one per principal.

    The snapshot is what the re-ingestion hook persists into
    documents.metadata.pending_acl_replay before destroying chunks, so the
    grants can be re-materialised against the new chunks.
    """
    chunk_ids = await _fetch_chunk_ids(http, supabase_url, supabase_headers, doc_id)
    if not chunk_ids:
        return []
    in_clause = ",".join(chunk_ids)
    r = await http.get(
        f"{supabase_url}/rest/v1/chunk_acl",
        params={
            "chunk_id": f"in.({in_clause})",
            "select": "principal_type,principal_id,granted_by",
        },
        headers=supabase_headers,
    )
    r.raise_for_status()
    seen: dict[tuple[str, str], AclGrant] = {}
    for row in r.json():
        key = (row["principal_type"], row["principal_id"])
        if key not in seen:
            seen[key] = AclGrant(
                principal_type=row["principal_type"],
                principal_id=row["principal_id"],
                granted_by=row.get("granted_by"),
            )
    return list(seen.values())


async def replay_doc_acls(
    http: httpx.AsyncClient,
    supabase_url: str,
    supabase_headers: dict[str, str],
    doc_id: str,
    grants: list[AclGrant],
) -> int:
    """Apply each grant once. Returns total rows inserted across grants."""
    total = 0
    for grant in grants:
        total += await grant_doc_to_principal(
            http,
            supabase_url,
            supabase_headers,
            doc_id,
            grant.principal_type,
            grant.principal_id,
            grant.granted_by,
        )
    return total
