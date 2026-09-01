# Web Controller Origin Identity Design

## Problem

Adaptive Agent Runtime can bind a ChatGPT Web session to an existing logical Controller only when it receives a trustworthy Web session identity. The current AI-Bridge 0.2.37 installation identifies its host profile as `chatgpt_web`, but its exposed status/audit contract does not provide a ChatGPT conversation/thread/session identifier tied to the tool call that invoked AI-Bridge.

A browser tab URL contains a real ChatGPT conversation UUID, but reading the currently active tab is not a trustworthy origin proof: the user may invoke the tool from a different device or from a different ChatGPT conversation than the active Mac tab. Repository path, desktop Codex thread ID, Controller registry membership, or a user-supplied string are likewise insufficient proof.

Therefore the current Runtime behavior must remain fail-closed until AI-Bridge can attest the originating ChatGPT conversation for the current tool call.

## Goal

Allow a ChatGPT Web conversation to resume an existing unique logical Controller without creating a second Controller, by carrying a host-attested origin conversation identity from AI-Bridge into Adaptive Agent Runtime.

## Non-goals

- Do not infer Controller identity from repository path.
- Do not scrape or trust the active Chrome tab URL as the caller identity.
- Do not reuse a desktop Codex thread ID as a Web conversation ID.
- Do not add a second Controller registry or a parallel lifecycle state machine.
- Do not patch or modify the signed `/Applications/AI-Bridge.app` binary in place.
- Do not weaken current fail-closed behavior when origin identity is unavailable.

## Trust boundary

The trusted statement must originate at the ChatGPT host / AI-Bridge request boundary, not inside Adaptive Agent Runtime.

AI-Bridge must expose an origin attestation for each tool call containing at minimum:

- `origin_host`: exact host class, initially `chatgpt_web`.
- `origin_conversation_id`: the stable ChatGPT conversation identifier associated with the request that invoked the tool.
- `call_receipt`: the existing per-call receipt identifier, or an equivalent unforgeable/host-issued request correlation identifier.
- `origin_attested`: boolean or equivalent schema signal that distinguishes host-supplied identity from user/tool arguments.

The origin identity must be supplied by the host integration itself. A caller-provided tool argument named `conversation_id` is not trusted unless AI-Bridge independently attests it.

## AI-Bridge contract

Preferred contract: extend the normal AI-Bridge tool result envelope (or a dedicated read-only origin-context tool) with a host-attested origin block, for example:

```json
{
  "call_receipt": "<host-issued receipt>",
  "origin": {
    "host": "chatgpt_web",
    "conversation_id": "<stable ChatGPT conversation UUID>",
    "attested": true
  }
}
```

The exact field names may follow AI-Bridge conventions, but the semantics are mandatory:

1. The conversation ID is taken from the request's host context, not the browser's active tab.
2. The origin statement is correlated to the same tool call as the receipt.
3. Missing, malformed, unsupported-host, or unattested origin data is treated as unavailable.
4. Redacted audit should record that an origin identity was present and its host class, but should avoid unnecessarily logging the raw conversation ID if product privacy policy does not require it.

## Runtime integration

Adaptive Agent Runtime adds a narrow adapter that accepts only the AI-Bridge host-attested origin context.

For `chatgpt_web`:

1. Validate that origin attestation is present.
2. Validate that `origin_conversation_id` has the expected stable identifier shape.
3. Resolve the repository's existing unique logical Controller from the current registry.
4. Bind `origin_conversation_id` into the existing `__controller_sessions__` registry entry for that logical Controller using the existing atomic/locked binding path.
5. Reuse the existing `require_web_controller_session(...)` and lifecycle identity checks afterward.
6. Resume the existing Controller; never create a second logical Controller as a side effect of Web binding.

If any identity or uniqueness check fails, the adapter returns a blocking result and does not mutate the binding registry.

## Resume versus Replace

Origin identity provisioning does not decide whether a Controller should be replaced. The handover forensic check remains authoritative:

- Prefer `Resume` while the existing logical Controller remains recoverable.
- `Replace` requires independent machine evidence that the prior Controller is no longer recoverable.
- A newly created ChatGPT Web conversation does not itself justify replacement.

## Failure behavior

The following must fail closed:

- AI-Bridge provides no origin conversation identity.
- Origin identity is user/tool supplied but not host-attested.
- Origin host is not `chatgpt_web` for the Web binding path.
- Conversation ID is malformed.
- Repository resolves to zero or more than one logical Controller.
- Conversation ID is already bound to a different logical Controller.
- Binding registry contains conflicting duplicate state.

No fallback may use active browser tab, repository path, old Controller ID, or current process environment as a substitute identity.

## Rollout

### Phase A — AI-Bridge prerequisite

Implement host-attested origin identity in AI-Bridge. This requires source access or a supported AI-Bridge extension/configuration interface. The installed signed App alone is not a safe modification target.

### Phase B — Runtime consumer

After the AI-Bridge contract exists, add the Runtime consumer and TDD coverage around successful binding and all fail-closed cases.

### Phase C — SelfAlone handover verification

With both layers installed:

1. Run the Controller Handover Forensic Check.
2. Confirm SelfAlone has exactly one logical Controller.
3. Invoke AI-Bridge from the intended Web Controller conversation.
4. Verify the attested origin conversation ID binds to that existing Controller.
5. Verify installed/loaded Runtime revision and rule handshake agree.
6. Resume from the previously proven machine-state interruption point.

## Testing

AI-Bridge tests must prove that origin identity comes from host request context, including a negative test showing that an unrelated active Chrome tab cannot influence it.

Runtime tests must cover:

- attested Web conversation binds to the one existing Controller;
- missing origin fails closed;
- unattested/user-supplied origin fails closed;
- malformed conversation ID fails closed;
- zero Controllers fails closed;
- two Controllers for the same repository fail closed;
- conversation already bound to another Controller fails closed;
- repeated binding of the same conversation to the same Controller is idempotent;
- lifecycle events after binding use the verified Web session identity;
- no new logical Controller is created by the binding flow.

## Current machine evidence

As of 2026-09-01 on this Mac:

- Adaptive Agent Runtime installed revision is `b900583c652a91e79bd1785d8db58c2822c2db86`.
- SelfAlone has one registered logical Controller.
- SelfAlone rule handshake is loaded/acknowledged at `b900583c652a91e79bd1785d8db58c2822c2db86`.
- `ADAPTIVE_DELIVERY_WEB_SESSION_ID` is not populated in the AI-Bridge shell environment.
- AI-Bridge 0.2.37 status identifies the active host profile as `chatgpt_web`.
- AI-Bridge redacted audit exposes receipt-level metadata but no conversation/thread/session/origin identifier.
- The installed AI-Bridge application bundle contains signed executables/resources, not editable source code.

These facts justify retaining fail-closed Runtime behavior until Phase A exists.
