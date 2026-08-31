# Agent Delivery Contract

Use this contract for delegated implementation, review, research, recovery, and external-agent work. It complements runtime liveness: runtime evidence proves an Agent is executing; this contract proves it is executing the right bounded goal and returning auditable evidence.

## 1. Single Goal Assignment

Every delivered Assignment in `ACKED`, `ACTIVE`, or `CANDIDATE` has exactly one `primary_goal` plus:

- `success_criteria`: observable conditions that close the goal.
- `owned_scope`: the bounded files, interfaces, decisions, or evidence the Agent may change or produce.
- `forbidden_scope`: explicit nearby scope the Agent must not absorb. An empty list is allowed when ownership already makes the exclusion obvious.
- `parallelizable`: explicit boolean. `false` requires `dependency_reason`; `true` still remains subject to READY, ownership, and shared-contract gates.

RED, implementation, GREEN, review, and documentation are steps or evidence for one goal, not separate hidden goals. If the primary goal changes, freeze/terminate the current Assignment and issue a new one.

For Assignment-bound external execution, delivered ACK is a **launch gate**, not a retrospective quality check. The runner validates the exact Assignment/task/Agent identity plus repository root, branch, and current HEAD before spawning the external Agent. A useful result produced through a non-compliant launch may remain auxiliary evidence, but it cannot satisfy the required Assignment or Reviewer receipt.

## 2. Delivery Gate — Unified Result Envelope

All execution transports (native subagent, Grok/Kimi runner, future A2A transport) keep **transport completion** separate from the **delivery verdict** in the runtime terminal receipt. New terminal receipts use:

Host fallback is part of execution lineage, not a new Assignment identity. When host routing is involved, the dispatch/delivery evidence additionally records `controller_host`, `execution_host`, `host_fallback_level`, and the machine reason for crossing hosts. Moving Web→Desktop or Desktop→Web never resets attempt/recovery budget and never upgrades model tier merely because the host changed.

`task_id + assignment_id + attempt + lease_id + terminal_state + transport_outcome + delivery_outcome + summary + evidence[] + artifacts[] + next_action + retry_class`

`transport_outcome` answers only whether the provider/process completed, failed, was cancelled, or was transport-blocked. `delivery_outcome` is independently `pass | fail | blocked | unresolved`. Provider exit code `0` means transport completed only; without an explicit validated delivery receipt the delivery remains `unresolved`, never PASS. Legacy receipts with one `outcome` remain readable during migration but must not be emitted by new runner writes.

When code or a versioned artifact is produced, Delivery PASS requires explicit evidence and an exact artifact/revision. PASS evidence/artifact entries must be traceable locators rather than free-form prose; the runner and canonical runtime reject prose-only PASS claims. Transport-specific prose may be retained as non-PASS evidence, but prose or exit code alone is not an authoritative delivery result. If a provider process completed but its delivery receipt is unreadable or invalid, runtime still closes the process as `transport_outcome=completed` with `delivery_outcome=unresolved`, while the caller receives a non-zero error for the invalid receipt.

Runtime heartbeat is liveness only. A progress receipt extends the progress deadline only when at least one authoritative fingerprint changes (Git HEAD, tracked status hash, evidence receipt, artifact, or blocker evidence). Repeating the same fingerprint does not count as progress. Same-contract recovery is bounded to two recovery attempts; after exhaustion the same Assignment cannot start another attempt. Strategy-changing execution must use a new Assignment contract rather than silently resetting the counter. Checkpoint governance stays risk-tailored: checkpoint 只作为恢复锚点，ordinary short assignments do not gain a new mandatory approval step or controller-authored runtime field.

For external side effects (for example payment, publish, message send, resource creation, or third-party writes), contract v2 and later Assignment starts explicitly record `side_effect: true|false`; a side-effecting Assignment may also bind one stable `idempotency_key`. The canonical runtime freezes both facts for the Assignment. A terminal side-effect receipt must explicitly record whether the external result is unknown. If the previous result is unknown/ambiguous and no stable idempotency key or equivalent provider guarantee exists, the next attempt is rejected by the runtime start gate even when the transport failure class would normally be transient. Recovery cannot change `side_effect`, the key, or the contract version in place. Pre-v2 receipts already issued before this field existed remain readable as legacy v1 so an in-flight first attempt can close normally; their side-effect contract is recorded as unknown rather than silently treated as safe, and an ambiguous legacy result requires a new explicit Assignment before recovery. A stable idempotency key permits retry only inside the existing attempt/recovery budget; it never resets that budget or turns an unknown result into success.

## 3. Integration Gate — Evidence Chain

For implementation work, completion evidence should form the shortest applicable causal chain:

`RED/problem evidence -> exact revision/artifact -> GREEN/verification -> non-author review when required -> real-end acceptance when required -> integration decision`

Every downstream decision cites exact upstream evidence. A claim such as “reviewer approved” or “tests passed” without the relevant revision/receipt is insufficient for integration. Once a required Reviewer returns `PASS`, the control event must either complete integration with machine-verified candidate ancestry in the exact current main plus current-main regression evidence or record a real `ordered_integration` queue checkpoint; `FAIL` must return the same candidate to an acknowledged rework Assignment. These are transition evidence, not new lifecycle states.

For bug fixes and high-risk behavior changes (including migration, money/cost, authorization, state-machine, external-call, and release-critical logic), the non-author Reviewer is a **TDD causal-evidence reviewer**. Its review receipt sets `tdd_required=true` and must bind `red_evidence`, exact `candidate_revision`, `green_evidence`, `red_green_same_case=true`, `reviewer_counterexample`, and `verdict=PASS|FAIL`. The Reviewer verifies that the pre-fix RED genuinely exposed the same defect that becomes GREEN on the candidate, and performs a bounded counterexample or edge attack; a test added only after the fix that was never observed failing cannot establish the causal chain. Low-risk mechanical/text changes remain risk-tailored and do not require synthetic RED/GREEN ceremony.

Research-only or diagnostic work uses the analogous chain `question -> source/observation -> finding -> independent check when risk requires -> decision` rather than fabricating RED/GREEN steps.

## 4. Conflict Record

When independent Agents or reviewers reach materially conflicting conclusions about the same revision/decision, do not silently choose one. Record one conflict object containing:

- `conflict_id`, `task_id`, exact revision/decision under dispute;
- each position with its evidence references;
- `arbiter` (controller or explicitly assigned non-author adjudicator);
- `decision`, `decision_evidence`, and resulting `next_action`.

A conflict blocks the disputed integration decision until adjudicated. Code-review defects and design/requirements disputes may share this envelope but remain distinct conflict types.

## 5. Progressive Scale-up

A new provider, model route, or execution transport starts with bounded concurrency. Before raising concurrency, demonstrate one lifecycle covering dispatch/ACK, start, progress or checkpoint, terminal success, and one controlled failure/recovery path. Existing trusted routes do not repeat this ceremony unless the transport/auth/runtime contract materially changes.

Do not build a full A2A server or transactional event bus merely to satisfy this contract. The governance contract is transport-neutral so a future A2A execution layer can replace the current runner without replacing Assignment, evidence, review, or integration governance.
