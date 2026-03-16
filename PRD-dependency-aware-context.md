# PRD: Dependency-Aware LLM Context and Remediation Planning

**Author:** Jason Shearer / Claude
**Date:** 2026-03-15
**Status:** Draft
**Repo:** dadcoachengineer/oasisagent

---

## Problem Statement

OasisAgent's three-tier decision engine (T0 known fixes → T1 SLM classification → T2 cloud reasoning) processes events in isolation. When T2 receives a `ptr_container_exited` event for zigbee2mqtt, it gets the container's inspect data and logs — but has no structured knowledge that HAOS depends on zigbee2mqtt, that zigbee2mqtt runs on the RPi4 Docker host managed by Portainer, that HAOS is proxied through NPM, or that external access traverses a Cloudflare tunnel through a Proxmox LXC.

Without dependency context, T2 cannot distinguish between a standalone container crash and a cascading failure. It cannot recommend "check the upstream Docker host" because it doesn't know the upstream exists. It cannot produce a multi-step remediation plan because `RecommendedAction` is a single operation on a single handler.

The topology graph (`ServiceGraph`) and edge types (`depends_on`, `proxies_to`, `runs_on`, etc.) already exist or are being added. But the data never reaches T2. This PRD closes the gap between topology data and LLM inference.

**Impact of not solving:** T2 operates as a single-event reasoner with handler-scoped context. Root cause analysis for cross-domain failures requires operator intuition. Autonomous remediation is limited to single-system, single-action responses. The topology graph remains a visualization tool, not an inference tool.

---

## Goals

1. **T2 receives structured dependency context** for every event it processes — upstream dependencies, downstream dependents, and same-host services — derived from the ServiceGraph in real time.

2. **Multi-handler context assembly** — when T2 is invoked, the system gathers diagnostics from all handlers that own entities in the dependency subgraph, not just the handler for the event's system.

3. **T2 produces multi-step remediation plans** — ordered actions across multiple handlers with success criteria, replacing the current single-`RecommendedAction` output.

4. **Dependency context is auditable** — the subgraph injected into T2 is persisted in the decision audit trail, enabling post-incident review of what the model knew when it made its recommendation.

5. **Zero regression on existing single-system event processing** — events with no topology edges continue to flow through T0/T1/T2 exactly as they do today.

---

## Non-Goals

1. **Automated topology edge synthesis from adapter data** — cross-adapter edge creation (e.g., NPM upstream IP → Proxmox VM matching) is a separate feature. This PRD consumes whatever edges exist (manual or auto-discovered), it does not create them.

2. **Causal propagation modeling** — this PRD does not implement "if X goes down, predict that Y will fail in N minutes." It provides the dependency graph to T2 and lets the model reason about causality. Deterministic propagation rules are a Phase 3 concern.

3. **UI changes to the service map** — filter/focus/manual edge creation are covered in a separate PR (#224). This PRD is backend-only.

4. **T1 dependency awareness** — T1 (local SLM) is a classifier, not a reasoner. It should remain fast and lightweight. Dependency context goes to T2 only.

5. **Real-time topology updates** — topology discovery runs on a configurable interval (default 300s). This PRD does not add event-driven topology updates.

---

## User Stories

### Operator

- **As an operator**, I want T2 to tell me "the root cause is the cloudflared LXC tunnel disconnection, which is upstream of NPM and HAOS" when HA external access fails, so that I fix the root cause instead of restarting downstream services.

- **As an operator**, I want the remediation plan to say "1. Restart cloudflared LXC, 2. Verify NPM upstream, 3. Check HAOS integrations" in dependency order, so that I don't waste time fixing effects before causes.

- **As an operator**, I want to see what dependency context T2 had when it made a recommendation, so that I can audit whether a bad recommendation was caused by missing topology data or bad model reasoning.

### System (Autonomous)

- **As the decision engine**, I want to gather container inspect data from Portainer AND VM status from Proxmox AND proxy health from NPM when processing a cross-domain failure, so that T2 has complete context for root cause analysis.

- **As the decision engine**, I want to execute remediation steps in dependency order (upstream first, then downstream), so that cascading failures are resolved from the root cause outward.

---

## Requirements

### P0 — Must Have

#### P0.1: Dependency Subgraph Extraction

Add a `gather_dependency_context()` function that, given an event and the ServiceGraph, returns a structured dependency subgraph for T2 prompt injection.

**Interface:**

```python
async def gather_dependency_context(
    event: Event,
    graph: ServiceGraph,
    depth: int = 2,
) -> DependencyContext
```

**`DependencyContext` model (new, in `models.py`):**

```python
class DependencyContext(BaseModel):
    """Structured dependency subgraph for T2 prompt injection."""
    entity_id: str
    entity_type: str | None
    host_ip: str | None

    upstream: list[DependencyNode]     # entities this one depends on
    downstream: list[DependencyNode]   # entities that depend on this one
    same_host: list[DependencyNode]    # other entities on the same host/IP

    edges: list[DependencyEdgeInfo]    # all edges in the subgraph
```

```python
class DependencyNode(BaseModel):
    entity_id: str
    entity_type: str
    display_name: str
    host_ip: str | None
    edge_type: str          # relationship to the root entity
    depth: int              # hops from root (1 = direct dependency)
```

```python
class DependencyEdgeInfo(BaseModel):
    from_entity: str
    to_entity: str
    edge_type: str
    manually_edited: bool
```

**Traversal logic:** BFS over ServiceGraph edges, treating edge direction semantically:
- `depends_on`, `resolves_via` → follow `from_entity → to_entity` for upstream
- `runs_on` → follow to host for infrastructure context
- `proxies_to`, `forwards_to` → follow for traffic path
- Reverse direction for downstream (who depends on me)

**Depth cap:** Default 2 hops. Configurable per-event via `agent.dependency_context_depth` config field. Hard max of 5 to bound prompt size.

**Acceptance criteria:**
- Given an event for `portainer:rpi4/zigbee2mqtt` with edges `HAOS --depends_on--> zigbee2mqtt` and `zigbee2mqtt --runs_on--> portainer:rpi4`, returns upstream = [rpi4 host], downstream = [HAOS], with correct edge types.
- Returns empty lists (not errors) when entity has no topology edges.
- Traversal completes in <10ms for a 200-node graph.

---

#### P0.2: T2 Prompt Injection

Modify `ReasoningService.diagnose()` to accept and format `DependencyContext` into the T2 prompt.

**Current signature:**
```python
async def diagnose(
    self, event: Event, triage_result: TriageResult,
    entity_context: dict[str, Any] | None = None,
    known_fixes: list[dict[str, Any]] | None = None,
) -> DiagnosisResult
```

**New signature:**
```python
async def diagnose(
    self, event: Event, triage_result: TriageResult,
    entity_context: dict[str, Any] | None = None,
    known_fixes: list[dict[str, Any]] | None = None,
    dependency_context: DependencyContext | None = None,
) -> DiagnosisResult
```

**Prompt template addition** (in `diagnose_failure.py`):

```
## Service Dependencies

The affected entity has the following relationships in the infrastructure topology:

### Upstream (this entity depends on):
{upstream_formatted}

### Downstream (depends on this entity):
{downstream_formatted}

### Same Host:
{same_host_formatted}

### Dependency Edges:
{edges_formatted}

Use these relationships to:
1. Identify whether this failure could be caused by an upstream dependency issue
2. Assess the blast radius — which downstream services are affected
3. Recommend remediation in dependency order (fix upstream before downstream)
```

**Acceptance criteria:**
- T2 prompt includes dependency section when DependencyContext is provided.
- T2 prompt omits dependency section when DependencyContext is None or empty (backward compatible).
- Dependency context is clearly separated from entity_context (handler diagnostics) in the prompt.

---

#### P0.3: Decision Engine Integration

Wire `gather_dependency_context()` into `DecisionEngine.process_event()` at the T2 escalation point.

**Current T2 flow in `process_event()`:**
```python
# Step 3: T2
entity_context = await handler.get_context(event)  # single handler
diagnosis = await reasoning.diagnose(event, triage_result, entity_context)
```

**New T2 flow:**
```python
# Step 3: T2
entity_context = await handler.get_context(event)  # primary handler
dependency_ctx = await gather_dependency_context(event, self._service_graph)
diagnosis = await reasoning.diagnose(
    event, triage_result, entity_context,
    dependency_context=dependency_ctx,
)
```

**DecisionEngine constructor change:**
```python
def __init__(
    self, registry, guardrails,
    triage_service=None, reasoning_service=None,
    service_graph: ServiceGraph | None = None,  # NEW
)
```

**Acceptance criteria:**
- DecisionEngine accepts optional ServiceGraph.
- T2 receives dependency context when graph is available.
- T2 works without dependency context when graph is None (backward compatible).
- Dependency context is included in `DecisionResult.details` for audit.

---

#### P0.4: Audit Trail for Dependency Context

Add dependency context to the `DecisionDetails` TypedDict so it's persisted in InfluxDB audit records and visible in the timeline UI.

**New DecisionDetails fields:**
```python
dependency_upstream: list[str]    # entity_ids of upstream dependencies
dependency_downstream: list[str]  # entity_ids of downstream dependents
dependency_same_host: list[str]   # entity_ids on same host
dependency_depth: int             # traversal depth used
```

**Acceptance criteria:**
- Audit reader can retrieve dependency context for any T2 decision.
- Timeline UI event detail panel shows dependency context when present.

---

### P1 — Nice to Have

#### P1.1: Multi-Handler Context Assembly

When T2 is invoked, gather context from ALL handlers that own entities in the dependency subgraph, not just the handler for the event's system.

**Current:** `handler.get_context(event)` calls only the matching handler (e.g., Portainer handler for `system="portainer"`).

**Proposed:** After `gather_dependency_context()` identifies the subgraph, iterate over unique systems in the subgraph and call `get_context()` on each corresponding handler.

```python
async def gather_multi_handler_context(
    event: Event,
    dependency_ctx: DependencyContext,
    handlers: dict[str, Handler],
    graph: ServiceGraph,
) -> dict[str, Any]:
    """Gather context from all handlers in the dependency subgraph."""
    context: dict[str, Any] = {}

    # Primary handler context (existing behavior)
    primary = handlers.get(event.system)
    if primary:
        context["primary"] = await primary.get_context(event)

    # Dependency handler contexts (new)
    seen_systems: set[str] = {event.system}
    for node in dependency_ctx.upstream + dependency_ctx.downstream:
        system = _system_for_entity(node.entity_id, graph)
        if system and system not in seen_systems:
            seen_systems.add(system)
            handler = handlers.get(system)
            if handler:
                proxy_event = _make_context_event(node, event)
                try:
                    ctx = await asyncio.wait_for(
                        handler.get_context(proxy_event), timeout=5.0,
                    )
                    context[f"dependency:{system}:{node.entity_id}"] = ctx
                except Exception:
                    pass  # best-effort, don't block on dependency context

    return context
```

**Key design decisions:**
- Each handler call has a 5-second timeout (don't let a slow handler block the pipeline).
- Failures are swallowed (best-effort — missing dependency context is better than a failed decision).
- A synthetic event is created for each dependency entity to pass to `get_context()` (the handler uses `event.entity_id` to know what to inspect).

**Acceptance criteria:**
- When zigbee2mqtt (Portainer) fails and HAOS (HA handler) is a dependent, T2 receives both Portainer container context AND HA integration status.
- Handler timeout prevents pipeline stalls.
- Missing handlers or handler errors don't fail the decision.

---

#### P1.2: Multi-Step Remediation Plans

Extend `DiagnosisResult` to support ordered, multi-step remediation plans.

**New model (in `models.py`):**

```python
class RemediationStep(BaseModel):
    """A single step in a multi-step remediation plan."""
    order: int                              # execution order (1-based)
    action: RecommendedAction               # existing action model
    success_criteria: str                   # what "success" looks like
    depends_on: list[int] = Field(default_factory=list)  # step orders this depends on
    conditional: bool = False               # skip if dependency failed?
```

**DiagnosisResult change:**
```python
class DiagnosisResult(BaseModel):
    root_cause: str
    confidence: float
    recommended_actions: list[RecommendedAction]  # KEPT for backward compat
    remediation_plan: list[RemediationStep] | None = None  # NEW
    risk_assessment: str
    additional_context: str
    suggested_known_fix: dict[str, Any] | None = None
```

**T2 prompt addition:**
```
If the failure involves multiple systems, produce a remediation_plan with ordered steps.
Each step targets a specific handler and operation. Steps execute in order —
fix upstream causes before downstream effects. Include success_criteria for
each step so the system can verify before proceeding.
```

**Orchestrator dispatch change:**
```python
async def _dispatch_remediation_plan(
    self, event: Event, result: DecisionResult, plan: list[RemediationStep],
) -> None:
    """Execute remediation steps in dependency order."""
    completed: dict[int, bool] = {}

    for step in sorted(plan, key=lambda s: s.order):
        # Check dependencies
        for dep in step.depends_on:
            if not completed.get(dep, False):
                if step.conditional:
                    completed[step.order] = False
                    continue
                break

        # Guardrail check per step
        gr = self._guardrails.check(
            entity_id=step.action.target_entity_id or event.entity_id,
            risk_tier=step.action.risk_tier,
        )
        if not gr.allowed:
            completed[step.order] = False
            continue

        # Execute
        result = await self._execute_action(event, step.action)

        # Verify
        if result.status == ActionStatus.SUCCESS:
            verify = await handler.verify(event, step.action, result)
            completed[step.order] = verify.verified
        else:
            completed[step.order] = False
```

**Acceptance criteria:**
- T2 can return a 3-step plan: restart cloudflared (Proxmox) → verify NPM upstream → check HA integrations.
- Steps execute in order with dependency checking.
- Each step is independently guardrail-checked.
- Existing single-action T2 responses continue to work (remediation_plan is None).

---

### P2 — Future Considerations

#### P2.1: Blast Radius Estimation

Given a node failure, automatically compute the set of downstream entities that would be affected, weighted by edge type severity. Inject as "blast radius" context into T2 and surface in the timeline UI.

#### P2.2: Dependency Health Propagation

When a node transitions to an alert state, proactively check the health of its downstream dependents. If dependents are also unhealthy, create a correlation cluster before individual events arrive. This inverts the current reactive correlation model into a predictive one.

#### P2.3: Remediation Plan Templates

Allow operators to define reusable remediation plan templates (e.g., "cloudflare tunnel recovery playbook") that T2 can reference when dependency patterns match. Bridges the gap between fully autonomous T2 planning and fully manual operator procedures.

---

## Success Metrics

### Leading Indicators (change within weeks)

| Metric | Target | Measurement |
|--------|--------|-------------|
| T2 decisions with dependency context | >80% of T2 invocations include non-empty DependencyContext | Audit query: count decisions where `dependency_upstream` is non-empty |
| Dependency subgraph extraction latency | p99 < 10ms | Instrumentation in `gather_dependency_context()` |
| Multi-handler context assembly success rate | >90% of dependency handlers return context within timeout | Log count of timeout/error vs. success |

### Lagging Indicators (change over months)

| Metric | Target | Measurement |
|--------|--------|-------------|
| T2 root cause accuracy for cross-domain failures | >70% of T2 root_cause strings correctly identify the upstream cause (manual review sample) | Monthly audit of 20 random cross-domain T2 decisions |
| Operator override rate on T2 recommendations | <30% of T2 RECOMMEND actions are rejected by operator | Pending queue: approved vs. rejected ratio |
| Mean time to remediation for cascading failures | 50% reduction vs. current (baseline: measure first month) | Timestamp delta: first event in cluster → last action executed |

---

## Open Questions

1. **[Engineering] ServiceGraph thread safety:** `gather_dependency_context()` reads the in-memory graph from the decision engine's async context. The graph is updated by the discovery loop (also async). Is the current implementation safe under concurrent access, or do we need a read lock? The graph uses dict operations which are atomic in CPython, but complex traversals may see inconsistent state mid-update.

2. **[Engineering] Handler `get_context()` entity routing:** When calling `portainer_handler.get_context(proxy_event)` for a dependency entity, the handler uses `event.entity_id` to determine what to inspect. But the entity_id format for Portainer topology nodes is `portainer:primary/zigbee2mqtt` while the handler expects a container name or ID. Need to define the mapping from topology entity_id → handler-native identifier.

3. **[Engineering] T2 prompt token budget:** With dependency context + multi-handler context + entity context + known fixes, the T2 prompt could exceed practical limits for some models. What's the token budget for T2 context injection, and how do we prioritize when truncation is needed? Proposed: dependency context first (most unique value), then primary handler context, then dependency handler contexts (truncate deepest-first).

4. **[Product] Remediation plan approval UX:** Multi-step plans need a different approval flow than single actions. Does the operator approve the entire plan or individual steps? Proposed: approve the plan as a unit, with the ability to skip individual steps during execution.

5. **[Engineering] Circuit breaker interaction with multi-step plans:** If step 1 succeeds but step 2 fails, does the circuit breaker record a failure for step 2's entity only, or for the entire plan's root entity? Proposed: per-step entity tracking (each step has its own target_entity_id).

---

## Timeline Considerations

### Phase 1 (P0 items) — Target: 2 PRs

**PR A: Dependency context extraction + T2 injection + audit**
- `DependencyContext` / `DependencyNode` / `DependencyEdgeInfo` models
- `gather_dependency_context()` with BFS traversal
- `ReasoningService.diagnose()` signature change + prompt template update
- `DecisionEngine` wiring (ServiceGraph injection, context gathering)
- `DecisionDetails` audit fields
- Tests: subgraph extraction, prompt formatting, backward compatibility

**PR B: Config + documentation**
- `agent.dependency_context_depth` config field
- config.example.yaml update
- ARCHITECTURE.md update (§7 LLM context flow)

### Phase 2 (P1 items) — Target: 2 PRs

**PR C: Multi-handler context assembly**
- `gather_multi_handler_context()` function
- Synthetic event construction for dependency entities
- Per-handler timeout + error handling
- Tests: multi-handler gather, timeout behavior, missing handler fallback

**PR D: Multi-step remediation plans**
- `RemediationStep` model
- `DiagnosisResult.remediation_plan` field
- T2 prompt update for plan generation
- `_dispatch_remediation_plan()` in orchestrator
- Guardrail check per step
- Pending queue UX for plan approval
- Tests: plan ordering, dependency checking, conditional steps, guardrail per-step

### Dependencies

- Service map edge types and manual edge creation (PR #224) — must land first so operators can create the dependency edges that this feature consumes.
- Portainer adapter (PR #221, merged) — provides container topology nodes for the RPi4 use case.
- Cross-domain correlator (PR #219, merged) — provides the clustering foundation. This feature enriches what T2 knows about correlated events.

### Hard Constraints

- `RecommendedAction` is a public API consumed by the pending queue, audit system, notification channels, and UI. Changes must be additive (new optional fields only).
- T2 prompt changes must not break existing T2 output parsing. The `DiagnosisResult` schema evolution must be backward-compatible (new fields default to None/empty).
- `DecisionEngine.process_event()` latency budget: dependency context gathering + multi-handler context must add <100ms p99 to the existing T2 path.
