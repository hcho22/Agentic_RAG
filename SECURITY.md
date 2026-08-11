# Security Policy

Purvia is a permissions-aware RAG platform: per-document ACLs are part of the
retrieval predicate, every retrieval call runs under the viewer's JWT, and a hard
workspace tenant boundary sits above per-document sharing.
Because the core promise is *a viewer never sees a chunk they were not granted*,
we take reports about that boundary - and everything that protects it - seriously.

## Reporting a vulnerability

**Please report vulnerabilities privately through GitHub, not in a public issue or pull request.**

Use the repository's **Security** tab -> **Report a vulnerability** to open a
private [GitHub Security Advisory](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability).
Private vulnerability reporting is enabled on this repository, so this is the
preferred and only guaranteed-confidential channel.
There is no published security email; the GitHub advisory flow is sufficient and
keeps the report, discussion, and eventual fix in one private place until disclosure.

A useful report includes:

- what boundary or property you believe is broken (e.g. a viewer seeing a chunk
  outside their ACL or workspace, a token accepted where it should be rejected);
- the smallest reproduction you can manage - request/response, seed data, or a
  failing test against the affected function is ideal;
- the commit or deployment you observed it on;
- the impact you think it has.

Please do not run automated scanners or volumetric tests against any shared or
hosted deployment, and do not access, modify, or exfiltrate data that is not
yours while investigating.

## Supported versions

Purvia is an actively developed application that tracks `main`, not a
released-and-versioned library.
There are no long-lived release branches and no backport commitment.

**In scope for fixes: the current `main` and the currently deployed version.**
If you are running an older checkout, please confirm the issue reproduces on
`main` before reporting, or say clearly which commit you tested.

## Scope

The following are the areas where a bug matters most, roughly in priority order.

**In scope:**

- **Permissions / ACL enforcement in the retrieval path** - the core security
  property. Any way a viewer retrieves a chunk they were not granted: a broken or
  bypassable ACL predicate, a post-filter that leaks via timing or payload size,
  or the `role` / `is_bot` administrative flags influencing visibility.
- **Tenant isolation** - crossing the workspace boundary: a member of one
  workspace reading, claiming, or influencing another workspace's chunks,
  conversations, or support queue.
- **Authentication and JWT handling** - forged, replayed, confused, or
  over-scoped tokens; the self-signed short-lived support-bot token; the opaque
  per-conversation customer token; anything that lets a request act as a
  principal it is not.
- **Secret handling** - exposure of server-side secrets (the Supabase service
  role key, the Supabase JWT secret, provider API keys, a minted bot JWT, or a
  raw customer token) in a response body, SSE event, log line, or client bundle.
- **Injection** - SQL injection (including via the text-to-SQL `query_database`
  tool over the allowlisted read-only schema) and prompt injection that causes
  the model to leak content across an ACL or tenant boundary or to exfiltrate
  secrets.
- Any other bypass of the trust boundaries described in the project docs
  (`README.md`, `CONTEXT.md`, and `docs/adr/0002-workspace-tenant-isolation.md`).

**Out of scope:**

- Vulnerabilities in third-party services themselves (Supabase, OpenAI, and other
  providers) - report those to the respective vendor. Misuse of them *in this
  codebase* is in scope.
- Findings that require already-compromised credentials, a malicious workspace
  administrator acting within their own workspace, or physical/host access.
- Volumetric denial of service and raw traffic floods. (Logic-level cost or
  abuse amplification against the public widget surface *is* in scope.)
- Missing hardening with no demonstrated impact (e.g. a header preference) absent
  a concrete exploit.

## What to expect

This is a small, actively developed project, so these are honest, non-binding
expectations rather than a contractual SLA:

- We aim to acknowledge a valid report within about a week.
- We practise coordinated disclosure: we will work with you on a fix and a
  disclosure timeline, and we ask that you give us a reasonable window to ship a
  fix before disclosing publicly.
- We are grateful for reports and will credit reporters who want it when an
  advisory is published. There is no paid bug-bounty program.

Thank you for helping keep Purvia's permission boundaries honest.
