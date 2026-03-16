## Review: DB-First Component Building (#210)

**Verdict: Approve.** Clean, well-structured migration. 2541 tests passing.

### What's solid

- **Two-phase startup** (`_build_infrastructure()` → `_build_db_components()`) is the right split. Deterministic singletons from config, user components from DB rows with per-row fault isolation.
- **Registry-driven dynamic import** eliminates the 16-entry `adapter_map` dict. `TypeMeta.module_path` + `class_name` is the single source of truth — adding a new adapter type is now a one-line registry entry.
- **`restart_service()` / `restart_notification()` refactor** is the biggest operational win. Building directly from row config instead of round-tripping through `store.load_config()` is much tighter.
- **Backward-compat `_build_components()`** wrapper chains infra + config so existing tests don't break.
- **HTTP poller aggregation** (many rows → one adapter) correctly handled as a special case in Pass 1.
- **Scanner cross-refs** resolved by ordering handlers before scanners (Pass 2 → Pass 3). Good dependency awareness.
- **Test coverage** is thorough: happy path, fault isolation, empty DB, disabled rows, unknown types, multiple MQTT warning, registry completeness assertions. `TestRegistryModulePaths` acts as a compile-time guardrail against forgetting `module_path` on new types.

### Minor notes (none are blockers)

1. **`import importlib` + `get_type_meta` inside method bodies** — The three `_build_*_from_row` methods all do this. `importlib` is fine (cheap stdlib), but `get_type_meta` could be hoisted to module-level. Minor redundancy.

2. **`_find_handler_config` return type** — Annotated as `object | None` but actually returns a specific Pydantic model. `BaseModel | None` would be more precise.

3. **Docstring clarity on `_build_handler_from_row`** — Says "Returns None for non-handler service types." More accurate: "Returns None for infrastructure types (those with empty `module_path`)."

4. **Parallel scanner builders** — `_build_scanners_from_config()` and `_build_scanners_from_db()` are two paths that both need updating when new scanner types are added. Worth a comment noting this, or consider unifying in a future PR.

5. **No test for malformed scanner sub-config** — The `try/except` around `ScannerConfig(**...)` handles it, but an explicit test case would strengthen the edge-case coverage.

🤖 Reviewed with [Claude Code](https://claude.com/claude-code)
