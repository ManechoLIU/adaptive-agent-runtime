# Controller Scoring Window V2 Design

## Goal
Make long-term controller scoring less sensitive to the last two events while keeping the controller's self-check lightweight and non-numeric.

## Formal scoring window
The default formal score uses the most recent 24 hours as the time boundary and selects at most five valid control events inside that boundary. If fewer than five exist, use all available valid events. A valid control event must have enough machine-verifiable evidence to evaluate controller behavior; noisy hook invocations or repeated reminders without a distinct controller decision are not separate events.

Current major unresolved anomalies and critical-path stalls are inspected separately from the event sample. They may affect dimensions and caps, but must not be double-counted as extra events.

If evidence inside the 24-hour boundary is too sparse to support a stable judgment, the evaluator must disclose the sparse sample rather than silently pulling convenient old events into the main window. Older evidence may only be a comparison baseline.

## Self-check
Controller Self-Check remains derived from the exact installed scoring model, non-numeric, and lightweight. It must not run the full 24-hour/five-event scoring procedure or expose the live score. It exists to steer current behavior using the formal model's standards; formal scoring remains the independent measurement loop.

## Compatibility
Keep the seven dimensions, attribution rules, caps, score-history storage, exact-model guard, and common-dir semantics unchanged. Existing historical score records remain readable and are not rewritten.
