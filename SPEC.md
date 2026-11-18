# Governed AI Gateway Prototype: Build Specification

**Purpose.** Build the prototype evaluated in the paper *From Onboarding to Business Value: A Reference Architecture for Governed Enterprise AI Gateways* (IEEE Intelligent Systems submission). The prototype must show that the four mechanisms can be built from open-source parts, that they decide correctly in the eight scenarios of Table 3, and how much latency the gateway adds.

**Audience.** This file is written for the builder of the prototype. Read it fully before writing code. Put a copy at the repository root as `SPEC.md`.

---

## 0. Rules for the builder (read first)

1. **Never invent results.** Every number in `results/` must come from a run of the code in this repository. If a run fails, report the failure; do not estimate.
2. **Synthetic data only.** No real organization names, credentials, model provider keys, or business data. All models, tools, and agents are local mocks.
3. **Pin everything.** Pin Python packages (`uv.lock` or `requirements.lock`), Docker images (tag plus digest), and the OpenTelemetry semantic-conventions version. Record all versions in `results/environment.md`.
4. **One command to reproduce.** `make all` brings up the stack, compiles the control plane, runs all tests, runs the benchmark, and writes the report.
5. **Commit in small steps** that follow the milestones in Section 9, with clear messages. Do not commit secrets; generate signing keys at setup into a git-ignored folder.
6. **Ask before** changing any requirement in this file. If something here is wrong or impossible, stop and explain.
7. **Check current docs** for the MCP Python SDK, the A2A specification (Agent Card path and JSON-RPC methods), and the OpenTelemetry generative AI conventions before using them. Note the versions you used.

---

## 1. Repository layout

```
governed-ai-gateway-prototype/
  SPEC.md                      # this file
  README.md                    # what it is, how to reproduce, how results map to the paper
  LICENSE                      # MIT
  Makefile                     # setup, up, compile, test, bench, report, all, down
  docker-compose.yml           # opa, redis, otel-collector, mocks, gateway, approval service
  control-plane/
    use_cases.yaml             # R1
    models.yaml                # R3: catalog with governance records and prices
    pairings.yaml              # R3: use case -> ranked models
    tools.yaml                 # R4: MCP servers, tools, hashes, read/write class
    agents.yaml                # R4: A2A agents from Agent Cards
    allowlists.yaml            # R4: use case -> tools/actions, agents
    budgets.yaml               # R5: budget tree, floors, ceilings, fail mode per limit
    policies/                  # R6: Rego policies + Rego unit tests
  cp_compile/                  # compiler: validate YAML, build snapshot, sign (R6, R8)
  gateway/                     # data plane: five stages (Identify, Authorize, Budget, Route, Guard)
  approval_service/            # human-in-the-loop queue (hold, approve, reject, timeout)
  mocks/
    model_server/              # OpenAI-compatible chat endpoint, configurable
    mcp_server/                # MCP server with tools (official MCP Python SDK)
    a2a_agents/                # A2A agents with Agent Cards
    kpi_emitter/               # posts business KPI events
  telemetry/
    otel-collector.yaml        # file exporter (JSON) for spans/metrics/logs
    evidence_store/            # append-only, hash-chained JSONL writer + verifier (R8)
    warehouse/                 # DuckDB SQL: cost per outcome (R7)
  tests/
    scenarios/                 # test_s1 ... test_s8 (Table 3)
    supplementary/             # tamper, fail-open/closed, quarantine, hop limit
  bench/                       # latency benchmark
  results/                     # generated: scenarios.csv, latency.csv, stage_latency.csv, environment.md, summary.md
```

---

## 2. Technology choices

| Part | Choice | Notes |
|---|---|---|
| Language | Python 3.12 | FastAPI + uvicorn + httpx + pydantic |
| Gateway | Reference implementation in this repo (FastAPI) | The five stages must be explicit, separately timed code paths. Do not hide them inside a third-party proxy. |
| Policy engine | Open Policy Agent server (Docker) | Rego policies loaded from the compiled snapshot; gateway calls the decision API |
| Counters and budgets | Redis (Docker) | Atomic reserve and reconcile (Lua script or MULTI) |
| Telemetry | OpenTelemetry Python SDK + OpenTelemetry Collector (contrib) | Collector file exporter writes JSON; no external backend needed |
| Evidence store | Append-only JSONL with SHA-256 hash chain | Verifier script detects any edit or deletion |
| Warehouse | DuckDB | Reads collector output and KPI events |
| Signing | Ed25519 (`cryptography`) | Snapshot signature and pairing-approval signatures |
| Tokens | JWT (`PyJWT`), Ed25519 or ES256 | Workload credentials, downstream scoped tokens, root-budget token |
| MCP mock | Official MCP Python SDK, streamable HTTP | |
| A2A mocks | Official A2A Python SDK if practical; otherwise a minimal JSON-RPC server that follows the spec | Serve the Agent Card at the path the current spec defines |
| Tests | pytest | Integration tests against the running stack |

---

## 3. Control plane (what to model)

### 3.1 Use cases (`use_cases.yaml`, R1)
Fields: `id`, `name`, `owner`, `business_unit`, `team`, `risk_tier` (low, medium, high), `data_classes`, `interaction` (interactive or batch), `state` (requested, sandbox, approved, production, suspended, retired), `approved_by`, `approved_at`, `expires_at`, `workloads` (client IDs of the apps and agents that serve it).

Create at least: `UC-01` (sandbox, low risk), `UC-17` (production, batch, confidential), `UC-22` (production, interactive, confidential), `UC-30` (production, agentic, uses MCP tools and A2A delegation), and `UC-40` (production, batch, for the expired-agreement scenario).

Promotion rule (enforced by the compiler): a use case cannot be `production` unless every paired model has evaluation evidence for it, the risk review for its tier is present, and an owner is set. Each state change writes a decision record (approver, evidence references, config version).

### 3.2 Model catalog (`models.yaml`, R3)
Per model deployment: `id`, `provider` (synthetic), `region`, `endpoint` (mock URL), `capabilities` (`context_length`, `tool_calling`, `structured_output`), `price_per_1k_input`, `price_per_1k_output` (synthetic USD), and a `governance_record`:
- `agreement`: `{ref, signed_at, expires_at}`
- `data_processing_terms`: `{ref, expires_at}`
- `third_party_risk_assessment`: `{ref, rating, valid_until}`
- `approved_data_classes`, `approved_regions`
- `evaluations`: list of `{use_case, suite, score, date}`

Create models A, B, C, D. Model B has a small `context_length`; models A and C have a large one. Model D's agreement is expired.

### 3.3 Pairings (`pairings.yaml`, R3)
Per use case: ordered list of `{model, rank, data_classes, regions, evaluation_ref, expires_at, approval: {approved_by, signature}}`. Signatures are Ed25519 over a canonical JSON of the entry, made by an "approving function" key generated at setup.

- `UC-17`: rank 1 = A, rank 2 = B. Model C is in the catalog and approved for `confidential`, but not paired with UC-17.
- `UC-22`: rank 1 = A, rank 2 = B (interactive, so it degrades instead of holding).
- `UC-40` (production, batch): rank 1 = D (expired agreement), rank 2 = A, for scenario 5.

### 3.4 Tools and agents (`tools.yaml`, `agents.yaml`, `allowlists.yaml`, R4)
- MCP server `mcp-docs` with tools `search_docs` (read), `get_customer_summary` (read), `update_case` (write). Store a SHA-256 hash of each tool's canonical definition (name, description, input schema) at registration.
- A2A agents `agent-b` and `agent-c`, registered from their Agent Cards.
- Allowlists: `UC-30` may call `search_docs` (read) and delegate to `agent-b` and `agent-c`; it may not call `update_case`. Hop limit = 3.

### 3.5 Budgets (`budgets.yaml`, R5)
Tree: `org > business_unit > team > use_case > user`. Each node: monthly money budget (USD), token rate limit (tokens per minute per model), guaranteed floor, burst ceiling, and `fail_mode` (open or closed) for when Redis is unreachable. Root-request budget default for agentic use cases: 5,000 tokens (configurable).

### 3.6 Policies (`policies/`, R6)
Rego policies for: authorization (default deny), pairing validity (pairing current, governance record current, region and data class allowed), fallback decision (degrade for interactive, hold for batch), and budget fail mode. Include Rego unit tests (`opa test`). Policies support an `observation` mode that records the decision without enforcing it.

### 3.7 Compiler (`cp_compile/`, R6, R8)
`make compile` validates all YAML against schemas, runs `opa test`, checks promotion rules, warns about governance records expiring within 30 days, resolves everything into one `snapshot.json` with a monotonically increasing `config_version`, and signs it. Output: `snapshot.json` + `snapshot.sig`.

---

## 4. Data plane (the five stages)

Each stage is its own function and its own OpenTelemetry span (`gateway.identify`, `gateway.authorize`, `gateway.budget`, `gateway.route`, `gateway.guard`) under one request span. A refusal ends the pipeline at the stage that refused.

1. **Identify (R2).** Validate the workload JWT; resolve `use_case_id` from the credential, never from a request header (a header that disagrees must be ignored and logged). Reject identities that are not onboarded. Create `root_request_id` if absent. For onward MCP/A2A calls, mint a downstream token scoped to that server (simulated RFC 8693 token exchange) carrying the originating use case and root request ID, and propagate W3C trace context.
2. **Authorize (R4).** Ask OPA. Default deny. For MCP `tools/list`, return only allowed tools. If a tool's current definition hash differs from the registered hash, deny with reason `tool_definition_changed` and mark the tool quarantined. Enforce the A2A hop limit.
3. **Budget (R5).** Estimate tokens (input token count + `max_tokens`), reserve atomically along the budget path and against the root-request budget (signed root-budget JWT issued by the gateway; callers cannot edit it). Reconcile with actual usage after the response. On Redis failure, apply each limit's `fail_mode` and log it. Refuse with HTTP 429 and a reason.
4. **Route (R3, R6).** Walk the pairing in rank order. Accept an entry only if: pairing current and signature valid; governance record current (agreement, data terms, risk assessment not expired; region and data class allowed); model healthy (circuit breaker: open after 3 consecutive failures, half-open after 30 s); capabilities sufficient (context length, tool calling, structured output). If none is left: interactive use case → return a degraded response (HTTP 200, `degraded: true`, reason); batch use case → store request and context in the approval service and return HTTP 202 with a `hold_id`. The router must never call a model that is not in the pairing, unless a recorded one-time exception from the approval service names it. Record `gov.route.rank`, `gov.route.reason`, and `gov.record.versions`.
5. **Guard.** Redact synthetic sensitive patterns (for example account numbers in a fixed synthetic format) from prompts before the call and from responses after; record counts of redactions, never the values.

**Configuration.** The gateway loads `snapshot.json`, verifies the signature, and keeps the last known good snapshot. A snapshot with a bad signature or a lower `config_version` is rejected and logged; serving continues on the last known good one.

---

## 5. Approval service

FastAPI service with SQLite. Endpoints: create hold (from gateway), list holds, approve with `{exception_model, approver, reason}`, reject, and automatic timeout (configurable, default 10 minutes). On approval, it notifies the gateway, which resumes the stored request, routes to the exception model only if that model's governance record is current and approved for the request's data class, and records the exception. Record approval wait time separately from gateway latency. Provide a small CLI for a human approver and a programmatic "simulated approver" for tests.

---

## 6. Telemetry, evidence, and warehouse

### 6.1 Span attributes (Table 2 of the paper)
Use OpenTelemetry generative AI attributes where defined (verify the exact names against the pinned semantic-conventions version and record it):
- `gen_ai.operation.name`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`

Governance attributes (enterprise namespace `gov.`):
- `gov.use_case.id`, `gov.business_unit`, `gov.team`, `gov.root_request.id`
- `gov.policy.version`, `gov.config.version`, `gov.decision` (allow, deny, degrade, hold), `gov.decision.reason`
- `gov.route.rank`, `gov.route.reason`, `gov.record.versions`
- `gov.cost.amount` (USD, from the catalog price table)
- `gov.exception.id`, `gov.approver` (when an exception applies)

If a name in the paper's Table 2 differs from the pinned convention, keep the convention name and list the difference in `results/environment.md` so the paper can be corrected.

### 6.2 Evidence store (R8)
Every governed decision and every control-plane change writes a redacted record to an append-only JSONL file where each line contains the SHA-256 of the previous line. `make verify-evidence` checks the chain.

### 6.3 Warehouse (R7)
`mocks/kpi_emitter` posts KPI events `{use_case_id, kpi_name, kpi_value, kpi_unit, timestamp}`. DuckDB SQL joins spend (from spans) and outcomes (from KPI events) by `use_case_id` and period and computes cost per outcome per use case, business unit, and team. Output `results/cost_per_outcome.csv`.

---

## 7. Mocks

- **Model server:** OpenAI-compatible `POST /v1/chat/completions`. Per model, configurable: fixed added delay (default 0 ms for benchmarks), failure mode (HTTP 503), and `context_length`. Deterministic token counting (document the rule, for example whitespace token count) and a `usage` block. Also an admin endpoint to switch failure on and off during tests.
- **MCP server:** the three tools above; an admin endpoint that changes `search_docs`'s description to trigger the hash mismatch.
- **A2A agents:** `agent-b` and `agent-c` that, when delegated a task, call the model through the gateway (so their spend counts against the root budget) and may delegate further (for the hop-limit test).

---

## 8. Tests and measurements

### 8.1 Scenarios (must match Table 3 of the paper)

| # | Scenario | Setup | Pass criteria (decision + evidence) |
|---|---|---|---|
| S1 | New use case in sandbox makes its first call | `UC-01` in sandbox | Allowed; budget charged to sandbox node; span has `gov.use_case.id=UC-01`. Negative check: a credential for a non-onboarded client is refused at Identify. A header claiming another use case is ignored. |
| S2 | Request names a model outside the approved pairing | `UC-17` requests model C directly | HTTP 403; `gov.decision=deny`; reason `model_not_in_pairing`; model C receives no call. |
| S3 | Primary model unavailable | Model A fails (503) until the circuit opens | Served by model B; `gov.route.rank=2`; `gov.route.reason=primary_unhealthy`. |
| S4a | No approved fallback, batch use case | `UC-17`; A circuit open; request larger than B's context length | HTTP 202 with `hold_id`; simulated approver grants exception to C; request completes on C; spans show `hold` then `allow`, `gov.route.reason=exception`, `gov.exception.id`, `gov.approver`; approval wait time recorded separately. |
| S4b | No approved fallback, interactive use case | Same conditions on `UC-22` | HTTP 200 with `degraded: true`; `gov.decision=degrade`; no unapproved model called. |
| S5 | Provider agreement for the primary has expired | `UC-40`: D (rank 1, expired), then A | Served by A; reason `governance_record_expired`; compiler warning logged. |
| S6 | Agent calls an MCP tool not on its allowlist | `UC-30` calls `update_case` | Denied; `tools/list` for `UC-30` returns only `search_docs`. |
| S7 | Agent fan-out exceeds the root budget | `UC-30` delegates to `agent-b` and `agent-c`, each calls the model; root budget 5,000 tokens | The call that would exceed the root budget is refused with 429, reason `root_budget_exceeded`; all calls share one `gov.root_request.id`; total charged ≤ root budget. |
| S8 | KPI event joined to spend | Run traffic for `UC-17`, emit KPI events | `cost_per_outcome.csv` row for `UC-17` equals total `gov.cost.amount` / count of outcomes, computed independently in the test. |

### 8.2 Supplementary checks (reported in the repo, may not appear in the paper)
- Tool definition change → quarantine (`tool_definition_changed`).
- A2A hop limit exceeded → denied.
- Tampered `snapshot.json` (bad signature) → rejected; gateway keeps serving on last known good.
- Tampered pairing signature → entry rejected at route time.
- Redis down: a `fail_mode: closed` limit refuses; a `fail_mode: open` limit allows and logs.
- Evidence chain edit → `verify-evidence` fails.
- Observation mode: a new deny policy in observation mode records `would_deny` and does not block.

### 8.3 Measures
1. **Decision correctness:** pass or fail per scenario (all pass criteria met). Write `results/scenarios.csv` with columns `scenario, expected, observed, pass`.
2. **Evidence completeness:** for each scenario, the share of required attributes present in the spans and evidence records needed to reconstruct the decision (required list per scenario defined in the test). Report as a percentage.
3. **Added latency:**
   - Baseline: the same request sent directly to the mock model (0 ms added delay).
   - Gateway path: identical request through the gateway, on the allow path (no fallback, no hold).
   - 100 warm-up requests, then N = 1,000 measured requests, concurrency 1; repeat at concurrency 10.
   - Report p50 and p95 of (gateway − direct) in ms, and per-stage p50/p95 from spans (`results/stage_latency.csv`).
   - Also measure the latency of S3 (fallback) and S5 (skip expired) decisions.
   - Exclude human approval wait time from latency; report it separately for S4a.
   - Record CPU, RAM, OS, Python, and Docker versions in `results/environment.md`.
4. **Report:** `make report` writes `results/summary.md` with a table shaped like Table 3 (scenario, expected decision, observed, latency p50/p95) and short notes on any failures.

---

## 9. Milestones (one or more commits each)

1. Skeleton: repo layout, Makefile, docker-compose with OPA, Redis, Collector, mock model server; health checks.
2. Control plane YAML, schemas, compiler, signing, `opa test`.
3. Gateway Identify and Authorize stages with spans; S1, S2, S6.
4. Budget stage with Redis and root-budget token; S7; fail-open/closed checks.
5. Route stage with pairings, governance checks, circuit breaker, capability checks; S3, S5.
6. Approval service, hold and degrade; S4a, S4b.
7. Guard stage, evidence store with hash chain, warehouse and KPI emitter; S8.
8. Supplementary checks, latency benchmark, report generation.
9. README: architecture overview mapped to the paper's Figure 1 and requirements R1–R8, reproduce steps, limitations (mocks, single machine, designed behavior rather than attack resistance).

---

## 10. GitHub

- Repository name: `governed-ai-gateway-prototype`. Keep it **private** until the author has reviewed the code and results; make it public before the paper is submitted, since the paper will cite it.
- The author handles GitHub authentication (`gh auth login`). Push after each milestone.
- Add a GitHub Actions workflow that runs `make compile` and the unit tests on each push (the full benchmark runs locally only).
- Tag the commit whose results go into the paper (for example `v1.0-paper`) and record the tag in `results/summary.md`.

---

## 11. What to send back to the author

`results/summary.md`, `results/scenarios.csv`, `results/latency.csv`, `results/stage_latency.csv`, `results/cost_per_outcome.csv`, `results/environment.md`, the exact component versions, the pinned OpenTelemetry semantic-conventions version (and any attribute-name differences from Table 2), and the repository URL and tag.
