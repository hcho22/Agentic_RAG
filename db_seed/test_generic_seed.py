"""US-110 test: the generic corpus seeder seeds corpus + real grants only.

Three layers:

1. **Offline pure-function checks** that always run (no DB / no OpenAI):
   - deterministic document/chunk UUIDs, namespaced by seed label.
   - `load_docs` reads *.md / *.txt, sorts, and rejects slug collisions.
   - `parse_manifest` accepts a well-formed manifest and rejects every
     malformed shape (bad UUID, unknown principal_type, a grant to an
     undeclared principal, a group member that is not a declared user).
   - `default_manifest()` is owner-only (no users beyond the owner, no grants).
   - the module has **no reference** to the synthetic eval-viewer constants
     (`PARTIAL_VIEWER_ID` / `NO_ACCESS_VIEWER_ID`) or the `full/partial/
     no_access` ACL matrix — the structural guarantee behind US-110 AC2.

2. **DB roundtrip / grant checks** that run when
   `GENERIC_SEED_DATABASE_URL` (or `DATABASE_URL`) is set and `OPENAI_API_KEY`
   is available (the PRD US-110 validation test):
   - a manifest granting one real principal one document produces that grant's
     `chunk_acl` rows.
   - querying `chunk_acl` for the runner's synthetic eval-viewer UUIDs returns
     **zero** rows (the failure indicator).
   - a no-manifest seed is owner-only: **zero** `chunk_acl` rows.
   - re-seeding leaves `(stable_id, md5(content))` byte-identical.

Run:
    python -m db_seed.test_generic_seed
"""

from __future__ import annotations

import asyncio
import inspect
import io
import os
import tempfile
import tokenize
import uuid
from pathlib import Path

import asyncpg

import db_seed.generic_seed as gs
from db_seed.generic_seed import (
    ManifestError,
    chunk_uuid,
    default_manifest,
    document_uuid,
    load_docs,
    parse_manifest,
    seed,
    validate_grant_documents,
)

# The synthetic eval-viewer UUIDs the RUNNER (never the seeder) constructs. A
# generic seed must contain zero chunk_acl rows for these principals.
EVAL_VIEWER_NAMESPACE = uuid.UUID("00000000-0000-0000-0000-000000000042")
PARTIAL_VIEWER_ID = uuid.uuid5(EVAL_VIEWER_NAMESPACE, "eval-viewer-partial-access")
NO_ACCESS_VIEWER_ID = uuid.uuid5(EVAL_VIEWER_NAMESPACE, "eval-viewer-no-access")

# A small, self-contained manifest for the integration test. Real ids, one grant
# of one document to one user (the PRD validation-test setup).
OWNER_ID = uuid.UUID("00000000-0000-0000-0000-00000ce70001")
WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-00000ce700a0")
GRANTEE_ID = uuid.UUID("00000000-0000-0000-0000-00000ce70101")
GROUP_ID = uuid.UUID("00000000-0000-0000-0000-00000ce70201")

TEST_SEED_LABEL = "us110test"

DOC_A = "Refund Policy.md"
DOC_A_BODY = (
    "# Refund Policy\n\n"
    "Customers may request a refund within 30 days of the order's shipped_at "
    "date. Refunds are issued to the original payment method within 5 business "
    "days of approval.\n"
)
DOC_B = "shipping-faq.md"
DOC_B_BODY = (
    "# Shipping FAQ\n\n"
    "Standard shipping is 3 to 5 business days within the contiguous US. "
    "Expedited shipping delivers in 1 to 2 business days for a flat surcharge.\n"
)


def _write_corpus(tmp: Path) -> None:
    (tmp / DOC_A).write_text(DOC_A_BODY, encoding="utf-8")
    (tmp / DOC_B).write_text(DOC_B_BODY, encoding="utf-8")


def _manifest_dict() -> dict:
    """One real principal granted one document — the PRD validation setup."""
    return {
        "version": 1,
        "owner": {"id": str(OWNER_ID), "email": "owner@us110.test"},
        "workspace": {"id": str(WORKSPACE_ID), "name": "US110 Workspace"},
        "principals": {
            "users": [{"id": str(GRANTEE_ID), "email": "grantee@us110.test"}],
            "groups": [
                {"id": str(GROUP_ID), "name": "us110-team", "members": [str(GRANTEE_ID)]}
            ],
        },
        "grants": [
            {
                "document": "refund-policy",  # slug form of "Refund Policy.md"
                "principal_type": "user",
                "principal_id": str(GRANTEE_ID),
            }
        ],
    }


# ---------------------------------------------------------------------------
# 1. Offline pure-function checks
# ---------------------------------------------------------------------------


def _offline_checks() -> None:
    # Deterministic, namespaced UUIDs.
    assert document_uuid("a", "refund-policy") == document_uuid("a", "refund-policy")
    assert document_uuid("a", "refund-policy") != document_uuid("b", "refund-policy"), (
        "distinct seed labels must not collide on a shared slug"
    )
    assert chunk_uuid("a", "s", 0) == chunk_uuid("a", "s", 0)
    assert chunk_uuid("a", "s", 0) != chunk_uuid("a", "s", 1)
    assert chunk_uuid("a", "s", 0) != chunk_uuid("b", "s", 0)

    # load_docs: reads md+txt, sorted, rejects slug collisions.
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _write_corpus(tmp)
        (tmp / "notes.txt").write_text("A plain text note about returns.\n", "utf-8")
        docs = load_docs(tmp)
        names = [n for n, _ in docs]
        assert names == sorted(names), "load_docs must return files in sorted order"
        assert {"Refund Policy.md", "shipping-faq.md", "notes.txt"} == set(names)

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "Refund Policy.md").write_text("x", "utf-8")
        (tmp / "refund_policy.md").write_text("y", "utf-8")  # same slug
        try:
            load_docs(tmp)
            raise AssertionError("expected a slug-collision RuntimeError")
        except RuntimeError as exc:
            assert "collision" in str(exc)

    with tempfile.TemporaryDirectory() as d:
        try:
            load_docs(Path(d))  # empty dir
            raise AssertionError("expected RuntimeError on empty docs dir")
        except RuntimeError as exc:
            assert "no " in str(exc)

    # default_manifest is owner-only.
    dm = default_manifest()
    assert dm.users == () and dm.groups == () and dm.grants == (), (
        "the no-manifest default must carry no extra principals and no grants"
    )

    # parse_manifest: happy path.
    m = parse_manifest(_manifest_dict())
    assert m.owner_id == OWNER_ID and m.workspace_id == WORKSPACE_ID
    assert len(m.users) == 1 and m.users[0].id == GRANTEE_ID
    assert m.users[0].role == "member"  # default role
    assert len(m.groups) == 1 and m.groups[0].members == (GRANTEE_ID,)
    assert len(m.grants) == 1 and m.grants[0].principal_id == GRANTEE_ID

    # parse_manifest: fail-loud on every malformed shape.
    def _expect_error(mutate) -> None:
        data = _manifest_dict()
        mutate(data)
        try:
            parse_manifest(data)
            raise AssertionError(f"expected ManifestError for mutation {mutate}")
        except ManifestError:
            pass

    _expect_error(lambda d: d.pop("owner"))
    _expect_error(lambda d: d.pop("workspace"))
    _expect_error(lambda d: d["owner"].__setitem__("id", "not-a-uuid"))
    _expect_error(
        lambda d: d["principals"]["users"][0].__setitem__("role", "superadmin")
    )
    _expect_error(
        lambda d: d["grants"][0].__setitem__("principal_type", "role")
    )
    # Grant to an undeclared user.
    _expect_error(
        lambda d: d["grants"][0].__setitem__(
            "principal_id", "00000000-0000-0000-0000-00000ce70999"
        )
    )
    # Group member that is not a declared user.
    _expect_error(
        lambda d: d["principals"]["groups"][0].__setitem__(
            "members", ["00000000-0000-0000-0000-00000ce70999"]
        )
    )
    # A grant to the OWNER is legal even though the owner is not in the users list.
    owner_grant = _manifest_dict()
    owner_grant["grants"][0]["principal_id"] = str(OWNER_ID)
    parse_manifest(owner_grant)  # must not raise

    # validate_grant_documents: a grant naming a doc absent from the corpus is a
    # hard error raised BEFORE any embed / DB write. The real grant matches the
    # seeded corpus and must pass.
    corpus = [(DOC_A, DOC_A_BODY), (DOC_B, DOC_B_BODY)]
    validate_grant_documents(parse_manifest(_manifest_dict()), corpus)  # must not raise
    bad_grant = _manifest_dict()
    bad_grant["grants"][0]["document"] = "does-not-exist"
    try:
        validate_grant_documents(parse_manifest(bad_grant), corpus)
        raise AssertionError("expected RuntimeError for a grant to an absent document")
    except RuntimeError as exc:
        assert "not present" in str(exc)

    # STRUCTURAL US-110 AC2 guarantee: the seeder module uses no synthetic
    # eval-viewer scaffolding whatsoever. Check actual NAME tokens (identifiers),
    # not the raw source — the module docstring *names* the scaffolding it
    # deliberately omits, and that prose is the point, not a violation.
    src = inspect.getsource(gs)
    code_names = {
        tok.string
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type == tokenize.NAME
    }
    forbidden = {
        "PARTIAL_VIEWER_ID",
        "NO_ACCESS_VIEWER_ID",
        "no_access",
        "partial_access",
        "full_access",
        "ensure_viewer_users",
        "reset_viewer_acls",
    }
    leaked = forbidden & code_names
    assert not leaked, (
        f"generic_seed.py must not use eval scaffolding identifiers: {sorted(leaked)}"
    )

    print("offline checks passed")


# ---------------------------------------------------------------------------
# 2. DB roundtrip / grant checks (skips without DB + OpenAI)
# ---------------------------------------------------------------------------


async def _snapshot(conn: asyncpg.Connection) -> list[tuple[str, str]]:
    rows = await conn.fetch(
        """
        select c.stable_id, md5(c.content) as content_hash
          from public.chunks c
          join public.documents d on d.id = c.document_id
         where (d.metadata->>'generic_seed_label') = $1
         order by c.stable_id
        """,
        TEST_SEED_LABEL,
    )
    return [(r["stable_id"], r["content_hash"]) for r in rows]


async def _acl_count_for(conn: asyncpg.Connection, principal_id: uuid.UUID) -> int:
    return await conn.fetchval(
        """
        select count(*)
          from public.chunk_acl ca
          join public.chunks c on c.id = ca.chunk_id
          join public.documents d on d.id = c.document_id
         where (d.metadata->>'generic_seed_label') = $1
           and ca.principal_id = $2
        """,
        TEST_SEED_LABEL,
        principal_id,
    )


async def _cleanup(conn: asyncpg.Connection) -> None:
    """Drop this test's seed rows so a re-run starts clean (chunks + chunk_acl
    cascade via the documents FK)."""
    await conn.execute(
        """
        delete from public.documents
         where (metadata->>'generic_seed_label') = $1
        """,
        TEST_SEED_LABEL,
    )
    await conn.execute("delete from public.principals where id = $1", GROUP_ID)
    await conn.execute(
        "delete from auth.users where id = any($1::uuid[])",
        [OWNER_ID, GRANTEE_ID],
    )


async def _db_roundtrip() -> None:
    url = (
        os.environ.get("GENERIC_SEED_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
    )
    if not url:
        print("SKIP db roundtrip: GENERIC_SEED_DATABASE_URL/DATABASE_URL unset")
        return
    if not os.environ.get("OPENAI_API_KEY"):
        print("SKIP db roundtrip: OPENAI_API_KEY unset")
        return

    conn = await asyncpg.connect(url)
    try:
        await _cleanup(conn)  # start clean

        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            _write_corpus(tmp)
            manifest_path = tmp / "manifest.yaml"
            import yaml

            manifest_path.write_text(yaml.safe_dump(_manifest_dict()), "utf-8")

            # --- Seed WITH the manifest ---
            res = await seed(
                docs_dir=tmp,
                manifest_path=manifest_path,
                database_url=url,
                seed_label=TEST_SEED_LABEL,
            )
            assert res.documents == 2, f"expected 2 docs, got {res.documents}"
            assert res.chunks > 0

            # (Step 3) the manifest's real grant produced its chunk_acl rows.
            grant_rows = await _acl_count_for(conn, GRANTEE_ID)
            assert grant_rows > 0, "the manifest grant produced no chunk_acl rows"
            # The grant was for doc A only; its row count equals doc A's chunks.
            doc_a_chunks = await conn.fetchval(
                """
                select count(*) from public.chunks c
                  join public.documents d on d.id = c.document_id
                 where (d.metadata->>'generic_seed_label') = $1
                   and (d.metadata->>'filename_slug') = 'refund-policy'
                """,
                TEST_SEED_LABEL,
            )
            assert grant_rows == doc_a_chunks, (
                f"grant should cover exactly doc A's {doc_a_chunks} chunks, "
                f"got {grant_rows}"
            )
            assert res.chunk_acl_rows == grant_rows

            # (Step 2) zero chunk_acl rows for the synthetic eval viewers.
            for viewer in (PARTIAL_VIEWER_ID, NO_ACCESS_VIEWER_ID):
                n = await _acl_count_for(conn, viewer)
                assert n == 0, f"synthetic viewer {viewer} leaked into the seed: {n}"

            # The grantee is a real member; the eval viewers are absent from auth.
            assert await conn.fetchval(
                "select count(*) from auth.users where id = $1", GRANTEE_ID
            ) == 1
            for viewer in (PARTIAL_VIEWER_ID, NO_ACCESS_VIEWER_ID):
                assert await conn.fetchval(
                    "select count(*) from auth.users where id = $1", viewer
                ) == 0, f"seeder must not create synthetic viewer {viewer}"

            snap_a = await _snapshot(conn)

            # (Step 5) re-seed → byte-identical (stable_id, md5(content)).
            res2 = await seed(
                docs_dir=tmp,
                manifest_path=manifest_path,
                database_url=url,
                seed_label=TEST_SEED_LABEL,
            )
            snap_b = await _snapshot(conn)
            assert snap_a == snap_b, "re-seed changed (stable_id, content) pairs"
            assert res.chunks == res2.chunks

            print(
                f"manifest seed OK: {res.documents} docs, {res.chunks} chunks, "
                f"{grant_rows} grant rows, 0 synthetic-viewer rows, byte-stable"
            )

            # --- Re-seed WITHOUT a manifest → owner-only (empty chunk_acl) ---
            await _cleanup(conn)
            res_owner = await seed(
                docs_dir=tmp,
                manifest_path=None,
                database_url=url,
                seed_label=TEST_SEED_LABEL,
            )
            assert res_owner.chunk_acl_rows == 0
            total_acl = await conn.fetchval(
                """
                select count(*) from public.chunk_acl ca
                  join public.chunks c on c.id = ca.chunk_id
                  join public.documents d on d.id = c.document_id
                 where (d.metadata->>'generic_seed_label') = $1
                """,
                TEST_SEED_LABEL,
            )
            assert total_acl == 0, (
                f"a no-manifest seed must be owner-only (0 chunk_acl), got {total_acl}"
            )
            print(f"no-manifest seed OK: owner-only, {total_acl} chunk_acl rows")
    finally:
        try:
            await _cleanup(conn)
        finally:
            await conn.close()


async def main() -> None:
    _offline_checks()
    await _db_roundtrip()


if __name__ == "__main__":
    asyncio.run(main())
