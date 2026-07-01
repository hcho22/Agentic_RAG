"""US-081 (ADR-0008, amends ADR-0004): the multi-instance customer-SSE fan-out
bridge — a Postgres ``LISTEN``/``NOTIFY`` transport that feeds INTO the in-process
``ConversationFanout`` registry (US-082) so an agent reply written on ONE backend
instance reaches a customer SSE held open on ANOTHER, with no Redis/queue infra.

WHERE THIS SITS. ``conversation_fanout.ConversationFanout`` is the single-instance
delivery core: ``publish(conversation_id, event)`` fans a reply to every customer
SSE subscribed to that conversation ON THIS PROCESS. That is the whole delivery
path when the backend runs as one process (the common kit deployment). This module
is the OPTIONAL cross-instance layer for a horizontally-scaled deployment:

  * SEND side — after the agent-reply endpoint (US-082) durably writes the row and
    calls ``publish()`` locally, it also calls ``ConversationBridge.notify(...)``,
    which emits a Postgres ``NOTIFY`` on a single shared channel carrying the
    conversation id + the same event envelope + this process's instance id.
  * RECEIVE side — every instance runs ``ConversationBridge`` with a dedicated
    ``LISTEN`` connection. On each notification it calls ``publish()`` LOCALLY, so
    the registry stays the universal point of delivery and this bridge only feeds
    INTO it. A notification that ORIGINATED on this same instance is ignored (the
    local ``publish()`` at the call site already delivered it) — the instance-id
    tag is the dedup that prevents a double push on the originating process.

WHY ``NOTIFY`` AND NOT REDIS/A QUEUE. The reply is already durably persisted in
``conversation_messages`` (the US-071 transcript is the source of truth); the bridge
only needs a low-latency *nudge* so a remotely-held SSE refreshes promptly instead
of waiting for the customer's transcript poll. Postgres ``LISTEN``/``NOTIFY`` is an
exactly-fits, infra-free transport: the database every instance already shares is
the fan-out bus. A dropped notification is not a lost message — the customer
recovers it on their next transcript read (US-084's poll / reconnect), so the bridge
is best-effort by design and never sits on the agent's write path as a hard
dependency.

CONFIG-GATED, FAIL-SOFT. The bridge is constructed only when
``WIDGET_FANOUT_DATABASE_URL`` is set (a direct asyncpg DSN for the shared
Postgres); unset means single-instance fan-out only and this module is never
imported into the request path. The ``LISTEN`` side runs a supervised reconnect
loop (a dropped connection self-heals with backoff); ``notify`` is best-effort and
swallows transport errors at the call site. Neither side can crash a request or
startup: the worst case degrades to single-instance delivery + the transcript
backstop.

PAYLOAD BOUND. A Postgres ``NOTIFY`` payload is capped (~8000 bytes). Agent chat
replies are far smaller, but an abnormally long one is handled gracefully: if the
serialized envelope would exceed ``_MAX_NOTIFY_PAYLOAD_BYTES`` the cross-instance
nudge is skipped (logged), and the remote customer still recovers the reply via the
transcript. The local ``publish()`` always delivers in full regardless of size.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from typing import Any

import asyncpg

from conversation_fanout import ConversationFanout

log = logging.getLogger("agentic_rag.support.bridge")

# One shared channel for every conversation; the payload carries the
# `conversation_id` so the RECEIVE side routes via `publish()` (which delivers only
# to that conversation's local subscribers and to none other). A single channel
# keeps the LISTEN side to ONE persistent connection with ONE background task —
# every instance receives every nudge and `publish()` no-ops for conversations it
# holds no subscriber for, which is free. Per-conversation channels (dynamic
# LISTEN/UNLISTEN tied to the subscribe lifecycle) would cut idle cross-instance
# chatter but are unwarranted at support-reply volume; noted as a future refinement.
NOTIFY_CHANNEL = "widget_conversation_events"

# Postgres caps a NOTIFY payload at 8000 bytes; stay well under it. An oversized
# envelope skips the cross-instance nudge (the transcript recovers it) rather than
# raising on the agent's write path.
_MAX_NOTIFY_PAYLOAD_BYTES = 7000

# Listener supervisor cadences (seconds). The health probe forces prompt detection
# of a silently-dropped TCP connection (a NOTIFY connection is otherwise idle, so a
# half-open socket would not surface until the next write); reconnect backoff caps
# the retry storm when the DB is briefly unreachable.
_HEALTH_PROBE_SECONDS = 30.0
# A bound on every command run on the LISTEN connection (the one-time `add_listener`
# and the periodic `select 1` probe). Without it, the probe's read blocks until the
# OS TCP timeout (minutes) on a half-open socket, so the supervisor would go silently
# deaf instead of detecting the drop "promptly". Kept well under the probe interval
# so a dead socket surfaces within a few seconds and triggers the backoff-reconnect.
_HEALTH_PROBE_TIMEOUT = 5.0
# A bound on establishing a connection (the pool's eager connect + the LISTEN
# connect). `command_timeout` bounds only query execution AFTER a connection exists;
# it does NOT bound the initial connect, which otherwise falls back to asyncpg's
# ~60s default. Because `start()` awaits `_ensure_pool()` on the FastAPI startup path
# (and min_size=1 makes the pool connect eagerly), an unreachable DSN would stall
# boot for that ~60s before failing soft — contradicting the "never block boot" /
# "self-heals on first use" guarantee. A few seconds is enough to reach a healthy DB
# and short enough to degrade to single-instance fan-out promptly on a dead DSN.
_CONNECT_TIMEOUT = 10.0
_RECONNECT_BACKOFF_SECONDS = 2.0
_RECONNECT_BACKOFF_MAX_SECONDS = 30.0


def build_notify_payload(
    *, instance_id: str, conversation_id: str, event: dict
) -> str:
    """Serialize the cross-instance envelope: origin instance id + conversation + event.

    ``origin`` is the dedup tag: the RECEIVE side ignores a notification stamped with
    its OWN instance id (the local ``publish()`` already delivered it). Pure +
    side-effect-free so the SEND-side size guard and the encoding are unit-testable
    without a database.
    """
    return json.dumps(
        {
            "origin": instance_id,
            "conversation_id": conversation_id,
            "event": event,
        },
        separators=(",", ":"),
    )


def decode_notify_payload(payload: str) -> dict[str, Any] | None:
    """Parse a NOTIFY payload back into its envelope, or None when malformed.

    Defensive: a notification with a non-JSON or wrong-shaped payload (a foreign
    writer on the same channel, a truncated frame) is dropped, never raised, so the
    listener loop survives a bad message.
    """
    try:
        decoded = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


class ConversationBridge:
    """Cross-instance customer-SSE fan-out over Postgres LISTEN/NOTIFY.

    Owns a dedicated ``LISTEN`` connection (a supervised reconnect loop) and a small
    pool for emitting ``NOTIFY``. Every received notification that did NOT originate
    on this instance is replayed into the local ``ConversationFanout`` via
    ``publish()`` — so the registry remains the single delivery point and this bridge
    is purely the transport that carries an event between instances.
    """

    def __init__(
        self,
        *,
        dsn: str,
        fanout: ConversationFanout,
        channel: str = NOTIFY_CHANNEL,
        instance_id: str | None = None,
    ) -> None:
        self._dsn = dsn
        self._fanout = fanout
        self._channel = channel
        # A per-process random id; the dedup tag that stops a process from
        # re-delivering its OWN NOTIFY on top of its local publish().
        self._instance_id = instance_id or secrets.token_hex(8)
        self._pool: asyncpg.Pool | None = None
        self._pool_lock = asyncio.Lock()
        self._listen_task: asyncio.Task[None] | None = None
        self._listen_conn: asyncpg.Connection | None = None
        self._stopped = False

    @property
    def instance_id(self) -> str:
        return self._instance_id

    async def start(self) -> None:
        """Open the NOTIFY pool (best-effort) and launch the LISTEN supervisor.

        Neither step is allowed to crash startup: a momentarily-unreachable DB leaves
        the pool unset (``notify`` self-heals on first use) and the supervisor retries
        the ``LISTEN`` connection with backoff. The worst case is a degrade to
        single-instance delivery until the DB returns.
        """
        await self._ensure_pool()
        self._listen_task = asyncio.create_task(
            self._listen_loop(), name="widget-fanout-listen"
        )
        log.info(
            "widget_fanout_bridge.started channel=%s instance=%s",
            self._channel,
            self._instance_id,
        )

    async def stop(self) -> None:
        """Tear down the supervisor + connections; never raises (shutdown path)."""
        self._stopped = True
        if self._listen_task is not None:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._listen_task = None
        if self._listen_conn is not None:
            try:
                await self._listen_conn.close(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            self._listen_conn = None
        if self._pool is not None:
            try:
                await self._pool.close()
            except Exception:  # noqa: BLE001
                pass
            self._pool = None

    async def notify(self, conversation_id: str, event: dict) -> bool:
        """Emit a cross-instance nudge for a just-written reply; best-effort.

        Returns True when the NOTIFY was emitted, False when it was skipped (no pool,
        oversized payload, or a transport error). The caller does NOT depend on the
        return: the reply is already durable and was already delivered to LOCAL
        subscribers via ``publish()``; this only reaches subscribers on OTHER
        instances, and a miss is recovered by the customer's transcript poll/reconnect.
        """
        if self._stopped:
            return False
        pool = await self._ensure_pool()
        if pool is None:
            return False
        payload = build_notify_payload(
            instance_id=self._instance_id,
            conversation_id=conversation_id,
            event=event,
        )
        if len(payload.encode("utf-8")) > _MAX_NOTIFY_PAYLOAD_BYTES:
            # Too large for a single NOTIFY frame. Skip the cross-instance nudge —
            # the remote customer recovers this reply from the durable transcript.
            log.warning(
                "widget_fanout_bridge.notify_oversize conversation=%s — skipping "
                "cross-instance nudge (the transcript is durable)",
                conversation_id,
            )
            return False
        try:
            await pool.execute("select pg_notify($1, $2)", self._channel, payload)
            return True
        except Exception:  # noqa: BLE001 — transport is best-effort, never fatal
            log.warning(
                "widget_fanout_bridge.notify_failed conversation=%s — the local "
                "fan-out already delivered; remote SSEs recover via the transcript",
                conversation_id,
                exc_info=True,
            )
            return False

    # -- internals --------------------------------------------------------- #

    async def _ensure_pool(self) -> asyncpg.Pool | None:
        """Lazily create the small NOTIFY pool; None if the DB is unreachable.

        Guarded so a startup DB blip self-heals on the first ``notify`` that finds a
        reachable database. A small pool (the agent reply path is low-frequency) and
        a short command timeout keep the write path snappy.
        """
        if self._pool is not None or self._stopped:
            return self._pool
        async with self._pool_lock:
            if self._pool is not None or self._stopped:
                return self._pool
            try:
                self._pool = await asyncpg.create_pool(
                    dsn=self._dsn,
                    min_size=1,
                    max_size=2,
                    command_timeout=10,
                    timeout=_CONNECT_TIMEOUT,
                )
            except Exception:  # noqa: BLE001
                log.warning(
                    "widget_fanout_bridge.pool_unavailable — cross-instance NOTIFY "
                    "disabled until the DB is reachable (single-instance fan-out + "
                    "the transcript backstop still deliver)",
                    exc_info=True,
                )
                self._pool = None
            return self._pool

    def _on_notification(
        self, connection: object, pid: int, channel: str, payload: str
    ) -> None:
        """asyncpg LISTEN callback — replay a remote event into the local registry.

        Synchronous (asyncpg invokes it inline on the read loop) and matched to
        ``publish``'s synchronous, non-blocking contract. Ignores notifications this
        instance emitted (already delivered locally) and any malformed/foreign frame.
        """
        envelope = decode_notify_payload(payload)
        if envelope is None:
            return
        if envelope.get("origin") == self._instance_id:
            # Our own NOTIFY — the local publish() at the call site already delivered
            # this to our subscribers; replaying it would double-push.
            return
        conversation_id = envelope.get("conversation_id")
        event = envelope.get("event")
        if not isinstance(conversation_id, str) or not isinstance(event, dict):
            return
        # publish() delivers only to LOCAL subscribers of this conversation and
        # no-ops (returns 0) when this instance holds none — so every instance can
        # receive every nudge cheaply.
        self._fanout.publish(conversation_id, event)

    async def _listen_loop(self) -> None:
        """Supervised LISTEN connection: (re)connect, listen, probe, reconnect.

        A single persistent connection holds the ``LISTEN``; a periodic health probe
        forces prompt detection of a silently-dropped socket, and any failure drops
        back to a backoff-reconnect. Runs until ``stop()`` cancels it.
        """
        backoff = _RECONNECT_BACKOFF_SECONDS
        while not self._stopped:
            conn: asyncpg.Connection | None = None
            try:
                conn = await asyncpg.connect(
                    dsn=self._dsn,
                    timeout=_CONNECT_TIMEOUT,
                    command_timeout=_HEALTH_PROBE_TIMEOUT,
                )
                await conn.add_listener(self._channel, self._on_notification)
                self._listen_conn = conn
                log.info(
                    "widget_fanout_bridge.listening channel=%s instance=%s",
                    self._channel,
                    self._instance_id,
                )
                backoff = _RECONNECT_BACKOFF_SECONDS  # reset after a clean connect
                # Hold the connection open; the health probe surfaces a dropped
                # socket so we reconnect instead of going silently deaf.
                while not self._stopped:
                    await asyncio.sleep(_HEALTH_PROBE_SECONDS)
                    if conn.is_closed():
                        raise ConnectionError("LISTEN connection closed")
                    await conn.fetchval("select 1")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                if not self._stopped:
                    log.warning(
                        "widget_fanout_bridge.listen_reconnect in %.0fs — cross-"
                        "instance receive paused (single-instance fan-out + the "
                        "transcript backstop still deliver)",
                        backoff,
                        exc_info=True,
                    )
            finally:
                self._listen_conn = None
                if conn is not None:
                    try:
                        await conn.close(timeout=5)
                    except Exception:  # noqa: BLE001
                        pass
            if not self._stopped:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX_SECONDS)
