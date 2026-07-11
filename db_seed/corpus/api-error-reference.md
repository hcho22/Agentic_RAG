# API & Integration Error Reference

This document defines the error codes, configuration keys, and webhook behavior for Acme Co's developer API and integration platform. It is the reference customer-service agents and integrators consult when a partner reports a failed API call or a dropped webhook.

## Error Codes

Every API error response carries a stable machine-readable code in the `error.code` field alongside a human-readable `error.message`. Codes never change meaning across API versions.

`ERR-4102` is returned when the caller exceeds the per-minute request quota. The response includes a `Retry-After` header giving the number of seconds until the quota window resets. This is a rate-limit error, not an authentication failure.

`ERR-4103` is returned when the request body exceeds the 2 MB payload ceiling. The caller must split the batch into smaller requests; the platform does not truncate or partially accept an oversized payload.

`ERR-4210` is returned when an idempotency key is reused with a different request body than the original call. Replaying the exact same body returns the original cached response instead of this error.

`ERR-4290` is returned when a webhook endpoint is unreachable. After 20 consecutive delivery failures the endpoint is automatically disabled and the integrator is notified by email.

`ERR-5001` is returned when a downstream fulfillment service does not respond within the request deadline. This is a transient server-side error and the caller should retry with exponential backoff.

## Webhook Configuration Keys

Integration behavior is governed by named configuration keys set in the partner dashboard. Keys are UPPER_SNAKE_CASE and their values are validated at save time.

`WEBHOOK_RETRY_MAX` sets the number of delivery retries before a webhook event is moved to the dead-letter queue. The default is 6 and the maximum accepted value is 10.

`WEBHOOK_SIGNING_SECRET_ROTATION_DAYS` sets how often the signing secret is rotated. The default is 90 days; setting it to 0 disables automatic rotation.

`RATE_LIMIT_BURST` sets the number of requests allowed to exceed the steady-state per-minute quota in a short burst. The default burst allowance is 20 requests above the sustained rate.

`IDEMPOTENCY_KEY_TTL_HOURS` sets how long a cached idempotent response is retained. The default is 24 hours; after the TTL expires the same key is treated as a fresh request.

## Signing and Idempotency Headers

Every webhook delivery is signed and every mutating request may carry an idempotency key. The header names below are case-sensitive and must be sent verbatim.

The idempotency key is supplied by the caller in the `X-Acme-Idempotency-Key` request header. A missing header on a POST or PUT request is accepted but forfeits replay protection.

The webhook signature is delivered in the `X-Acme-Signature` header as a hex-encoded HMAC-SHA256 digest of the raw request body. When the digest does not match, the receiver should reject the delivery and the platform logs the error body `"signature verification failed"` against that endpoint.

A rejected signature does not count toward the `ERR-4290` disable threshold, because a signature mismatch indicates a secret-rotation lag rather than an unreachable endpoint.
