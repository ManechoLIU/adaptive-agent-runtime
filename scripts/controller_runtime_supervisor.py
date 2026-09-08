#!/usr/bin/env python3
"""Host-neutral entrypoint for durable Adaptive Agent Runtime continuation.

The implementation remains in ``web_agent_health_supervisor`` for compatibility
with existing imports and receipts.  The service itself is not a Web identity
provider: without a trusted Web machine-event source it still performs only the
host-neutral Controller continuation and rule-wake work that Desktop Codex can
verify locally.
"""
from __future__ import annotations

try:
    from scripts.web_agent_health_supervisor import (
        health_supervisor_ready as controller_runtime_supervisor_ready,
        main,
        reconcile_all_web_agent_health_once as reconcile_all_controller_runtime_once,
        reconcile_registered_controller_rule_update_once,
        reconcile_web_agent_health_once as reconcile_controller_runtime_once,
        run_health_supervisor as run_controller_runtime_supervisor,
        write_health_heartbeat as write_controller_runtime_heartbeat,
    )
except ModuleNotFoundError:
    from web_agent_health_supervisor import (
        health_supervisor_ready as controller_runtime_supervisor_ready,
        main,
        reconcile_all_web_agent_health_once as reconcile_all_controller_runtime_once,
        reconcile_registered_controller_rule_update_once,
        reconcile_web_agent_health_once as reconcile_controller_runtime_once,
        run_health_supervisor as run_controller_runtime_supervisor,
        write_health_heartbeat as write_controller_runtime_heartbeat,
    )

__all__ = [
    "controller_runtime_supervisor_ready",
    "reconcile_all_controller_runtime_once",
    "reconcile_controller_runtime_once",
    "reconcile_registered_controller_rule_update_once",
    "run_controller_runtime_supervisor",
    "write_controller_runtime_heartbeat",
]


if __name__ == "__main__":
    raise SystemExit(main())
