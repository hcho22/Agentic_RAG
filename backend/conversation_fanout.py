"""US-081/082 (ADR-0008, amends ADR-0004): the in-process customer-SSE fan-out
registry — the single-instance delivery core through which async agent replies
reach the anonymous customer WITHOUT Supabase Realtime.

The anonymous customer is structurally OFF the Supabase JWT/Realtime surface
(US-071): their reconnect credential is an opaque token, never a Supabase
session, so they cannot hold a supabase-js Realtime channel. Instead the customer
holds a long-lived BACKEND SSE (US-081), authorized by that opaque token, and the
backend pushes server-originated conversation events (a human agent's reply,
US-082) down that SSE. This module is the seam those two stories meet at:

  * the PUBLISHER side (US-082): after a workspace agent's reply is durably
    written to `conversation_messages`, `POST /widget/conversations/{id}/agent-reply`
    calls `publish(conversation_id, event)` to fan it to every customer SSE
    currently subscribed to that conversation on THIS instance.
  * the SUBSCRIBER side (US-081): the customer GET SSE endpoint opens a
    `subscribe(conversation_id)` and drains the yielded queue for the life of the
    connection, formatting each event as an SSE frame.

WHY A DEDICATED IN-PROCESS REGISTRY (not Supabase Realtime): keeping the customer
leg backend-mediated is the ADR-0008 max-isolation stance — the customer never
touches the Supabase trust surface, so no RLS/Realtime policy ever has to reason
about an anonymous principal. The registry is a per-`conversation_id` set of
`asyncio.Queue` subscribers; `publish` fans one event to every subscriber of one
conversation and to NO other conversation, which is the US-081 security property
(a customer SSE never carries another conversation's data).

SINGLE-INSTANCE vs MULTI-INSTANCE. This module is the single-instance core, which
is the whole delivery path when the backend runs as one process (the common kit
deployment) and is what US-082's fan-out triggers today. US-081's AC layers a
Postgres `LISTEN/NOTIFY` transport ON TOP for the multi-instance case: an
agent-reply on instance A emits a `NOTIFY` keyed by `conversation_id`, and each
instance's `LISTEN` handler calls `publish()` LOCALLY — so this registry stays the
universal point of delivery and the cross-instance bridge only feeds INTO it (no
Redis/queue infra added). That transport is deliberately NOT in this module: it is
US-081's, and bolting it on here is a clean addition (a `notify()` alongside the
local `publish()` at the call site, plus a background `LISTEN` task that publishes
what it receives) that does not change this core.

Thread/loop model: this registry is confined to the asyncio event loop. `publish`
is synchronous and non-blocking (`Queue.put_nowait`) so an agent's write path is
never blocked by a slow customer SSE; a full queue drops the live push with a
warning rather than blocking, because the transcript (`conversation_messages`, read
via the US-071 GET) is the durable source of truth and a dropped push is recovered
on the customer's next reconnect/transcript fetch.
"""

from __future__ import annotations

import contextlib
import logging
from typing import AsyncIterator

import asyncio

log = logging.getLogger("agentic_rag.support.fanout")


class ConversationFanout:
    """A per-`conversation_id` in-process pub/sub for customer-SSE delivery.

    Holds, for each conversation that currently has at least one open customer SSE,
    the set of that SSE's subscriber queues. `publish` drops an event onto every
    subscriber of one conversation; `subscribe` is an async context manager that
    registers a fresh queue for the life of a connection and unregisters it on exit
    (so a closed SSE leaves no leaked subscriber). A conversation with no open SSE
    holds no state — the dict entry is created on first subscribe and removed when
    its last subscriber leaves, so an idle backend keeps no per-conversation memory.
    """

    def __init__(self, *, max_queue: int = 256) -> None:
        # conversation_id -> set of subscriber queues. Created lazily on the first
        # subscribe for a conversation, removed when its last subscriber departs.
        self._subscribers: dict[str, set["asyncio.Queue[dict]"]] = {}
        # Per-subscriber bound: a runaway publisher (or a stuck SSE that stops
        # draining) cannot grow one queue without limit. On overflow the live push
        # is dropped (see `publish`), never buffered unboundedly.
        self._max_queue = max_queue

    @contextlib.asynccontextmanager
    async def subscribe(
        self, conversation_id: str
    ) -> AsyncIterator["asyncio.Queue[dict]"]:
        """Register a subscriber queue for `conversation_id` for the body's lifetime.

        Yields an `asyncio.Queue` the caller (US-081's customer SSE generator) drains
        with `await queue.get()` in a loop. On exit — normal close, client
        disconnect, or cancellation — the queue is unregistered, so a dropped SSE
        never leaves a phantom subscriber that would make `publish` think a customer
        is still listening.
        """
        queue: "asyncio.Queue[dict]" = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.setdefault(conversation_id, set()).add(queue)
        try:
            yield queue
        finally:
            subs = self._subscribers.get(conversation_id)
            if subs is not None:
                subs.discard(queue)
                if not subs:
                    # Last subscriber gone — drop the conversation entry so an idle
                    # backend holds no per-conversation state.
                    self._subscribers.pop(conversation_id, None)

    def publish(self, conversation_id: str, event: dict) -> int:
        """Fan `event` to every subscriber of `conversation_id`; return the count.

        Synchronous and non-blocking by design: the agent's write path calls this
        after the durable `conversation_messages` write and must not await a slow
        customer SSE. Delivers ONLY to subscribers of this exact conversation (the
        US-081 cross-conversation-isolation property), and to none when no SSE is
        open (returns 0 — the reply is still durable in the transcript). A full
        subscriber queue drops the push with a warning rather than blocking; the
        customer recovers it on reconnect via the US-071 transcript.
        """
        subs = self._subscribers.get(conversation_id)
        if not subs:
            return 0
        delivered = 0
        # Iterate a snapshot: a subscriber may unregister (SSE close) concurrently
        # with this fan-out within the loop's cooperative scheduling.
        for queue in tuple(subs):
            try:
                queue.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                log.warning(
                    "conversation_fanout.queue_full conversation=%s — dropping a "
                    "live push (the transcript is durable; the customer recovers it "
                    "on reconnect)",
                    conversation_id,
                )
        return delivered

    def subscriber_count(self, conversation_id: str) -> int:
        """Number of open customer SSEs for `conversation_id` (0 when none)."""
        return len(self._subscribers.get(conversation_id, ()))

    @property
    def conversation_count(self) -> int:
        """Number of conversations with at least one open customer SSE."""
        return len(self._subscribers)


def message_event(message: dict) -> dict:
    """The published-event envelope for a new `conversation_messages` row (US-082).

    A small, stable shape the US-081 customer-SSE consumer maps to an SSE frame.
    Carries only customer-safe message fields (id/role/content/created_at) — no
    workspace id, no `bot_user_id`, no internal topology. A human agent's reply is
    persisted as `role='assistant'` (the schema's only support-side role; see
    US-082), so the customer sees it exactly as it appears in the transcript.
    """
    return {
        "type": "message",
        "message": {
            "id": message.get("id"),
            "role": message.get("role"),
            "content": message.get("content"),
            "created_at": message.get("created_at"),
        },
    }
