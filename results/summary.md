# Results summary

Generated 2026-11-18 16:23 UTC by `make report`. Results produced by code at commit `547498b`; paper tag `v1.0-paper`.

All numbers below come from runs of the code in this repository (see results/*.csv). Mocks run on one machine. Latencies are measured by a benchmark client inside the Docker compose network (running it from the Windows host adds ~44 ms of Docker Desktop port-forwarding to every request).

## Table 3: scenarios

| # | Scenario | Expected decision | Observed | Pass | Evidence completeness | Gateway latency p50 / p95 (ms) |
|---|---|---|---|---|---|---|
| S1 | New use case in sandbox makes its first call | allow; charged to sandbox node; span gov.use_case.id=UC-01; non-onboarded client refused at Identify; header claiming another use case ignored | HTTP 200 decision=allow uc=UC-01 model=model-b sandbox spend +26 uUSD; header UC-17 ignored=True; non-onboarded -> HTTP 401 identity_not_onboarded at identify | pass | 100% | 14.32 / 27.28 (n=200) |
| S2 | Request names a model outside the approved pairing | HTTP 403; gov.decision=deny; reason model_not_in_pairing; model C receives no call | HTTP 403 decision=deny reason=model_not_in_pairing stage=authorize; model-c calls 0->0 | pass | 100% | 8.01 / 14.99 (n=200) |
| S3 | Primary model unavailable | served by model B; gov.route.rank=2; gov.route.reason=primary_unhealthy | HTTP 200 model=model-b rank=2 reason=primary_unhealthy; model-a called 0x after circuit opened | pass | 100% | 13.00 / 24.76 (n=200) |
| S4a | No approved fallback, batch use case | HTTP 202 with hold_id; approver grants exception to C; completes on C; spans show hold then allow with gov.route.reason=exception, gov.exception.id, gov.approver; approval wait recorded separately | HTTP 202 hold_id=hold-bd51bf4d5c614c92 -> approved (wait 1967 ms) -> allow on model-c reason=exception exception=exc-40c604aef9134d87 approver=sim-approver-01 | pass | 100% | 41.73 / 55.45 (n=200) |
| S4b | No approved fallback, interactive use case | HTTP 200 with degraded=true; gov.decision=degrade; no unapproved model called | HTTP 200 degraded=True decision=degrade reason=no_eligible_model; models called during request: {} | pass | 100% | 13.42 / 24.57 (n=200) |
| S5 | Provider agreement for the primary has expired | served by model A; reason governance_record_expired; compiler warning logged | HTTP 200 model=model-a rank=2 reason=governance_record_expired versions=model-d=GR-D-v3,model-a=GR-A-v4; compile warning for model-d present=True | pass | 100% | 11.96 / 22.11 (n=200) |
| S6 | Agent calls an MCP tool not on its allowlist | update_case denied; tools/list for UC-30 returns only search_docs | tools/list=['search_docs']; tools/call update_case -> HTTP 403 deny/tool_not_allowed; server update_case calls 0->0; search_docs -> HTTP 200 | pass | 100% | 56.60 / 69.59 (n=200) |
| S7 | Agent fan-out exceeds the root budget | call that would exceed the root budget refused with 429 root_budget_exceeded; all calls share one gov.root_request.id; total charged <= root budget | orchestrator 200; agent-b model call 200; agent-c model call 429 root_budget_exceeded; 5 gateway requests share root fa729f63...; charged 3010 tokens (allowed usage 3010) of 5000 | pass | 100% | 7.31 / 13.23 (n=200) |
| S8 | KPI event joined to spend | cost_per_outcome.csv row for UC-17 = total gov.cost.amount / count of outcomes (computed independently) | UC-17 2026-11: spend 0.012192 USD / 4 outcomes = 0.003048; warehouse row = 0.0030479999999999995 | pass | 100% | n/a (offline join) |

## Added latency (allow path, UC-17 -> model-a, N=1,000 after 100 warm-up)

| path | concurrency | n | p50 ms | p95 ms | mean ms |
|---|---|---|---|---|---|
| direct | 1 | 1000 | 1.351 | 2.298 | 1.515 |
| gateway | 1 | 1000 | 11.752 | 22.756 | 13.006 |
| added_by_gateway (gateway - direct) | 1 | 1000 | 10.401 | 20.458 | 11.491 |
| direct | 10 | 1000 | 11.064 | 18.192 | 24.211 |
| gateway | 10 | 1000 | 105.736 | 193.778 | 115.257 |
| added_by_gateway (gateway - direct) | 10 | 1000 | 94.673 | 175.586 | 91.045 |
| gateway_s3_fallback | 1 | 200 | 12.798 | 24.355 | 13.986 |
| gateway_s5_skip_expired | 1 | 200 | 12.114 | 22.114 | 13.360 |

`added_by_gateway` = percentile of the gateway path minus the same percentile of the direct path (independent samples, not paired requests). At concurrency 10 the gateway, a single uvicorn worker, saturates one CPU core (measured ~100% CPU while OPA, Redis and the mock model stayed below ~25%), so the added latency there is mostly queueing inside the gateway process, visible as longer authorize/route spans that include event-loop wait. Scaling out workers would need shared circuit-breaker and quarantine state, which the prototype keeps in-process.

## Per-stage latency from spans (measured gateway requests)

| concurrency | stage | n | p50 ms | p95 ms |
|---|---|---|---|---|
| 1 | gateway.identify | 1000 | 0.609 | 1.023 |
| 1 | gateway.authorize | 1000 | 2.379 | 3.859 |
| 1 | gateway.budget | 1000 | 0.737 | 1.146 |
| 1 | gateway.route | 1000 | 2.497 | 4.078 |
| 1 | gateway.guard | 1000 | 0.087 | 0.152 |
| 1 | gateway.request | 1000 | 9.339 | 17.105 |
| 1 | gateway.upstream | 1000 | 2.156 | 3.346 |
| 1 | governance_total | 1000 | 6.371 | 11.751 |
| 10 | gateway.identify | 1000 | 0.626 | 1.246 |
| 10 | gateway.authorize | 1000 | 30.582 | 93.428 |
| 10 | gateway.budget | 1000 | 3.953 | 8.320 |
| 10 | gateway.route | 1000 | 32.373 | 95.698 |
| 10 | gateway.guard | 1000 | 0.091 | 0.163 |
| 10 | gateway.request | 1000 | 99.379 | 188.270 |
| 10 | gateway.upstream | 1000 | 19.142 | 44.317 |
| 10 | governance_total | 1000 | 76.536 | 158.454 |

`gateway.budget` and `gateway.guard` sum both phases (reserve + reconcile; request + response). `governance_total` = sum of the five stages; `gateway.upstream` is the model call.

## Human approval wait (S4a, excluded from gateway latency)

- S4a: 1966.5 ms (simulated approver with 0.5 s think time; excluded from gateway latency)

## Supplementary checks

| check | expected | observed | pass |
|---|---|---|---|
| tool_quarantine | changed tool definition -> deny tool_definition_changed; tool quarantined | call -> 403 tool_definition_changed; tools/list -> []; after restore still 403 | pass |
| hop_limit | delegation beyond hop limit 3 denied (hop_limit_exceeded) | delegation hops seen [1, 2, 3, 4]; denied at hop 4 | pass |
| snapshot_tamper | bad signature rejected; gateway keeps serving last known good | tampered -> 409 bad_signature; stale -> 409 stale_config_version; serving v10; UC-17 -> 200 | pass |
| pairing_signature_tamper | entry with bad pairing signature rejected at route time | served by model-b rank 2 reason pairing_signature_invalid; restored v12 | pass |
| redis_fail_modes | Redis down: fail_mode closed refuses (UC-17), fail_mode open allows and logs (UC-01) | UC-01 (open) -> 200 fail_open; UC-17 (closed) -> 429 budget_store_unavailable | pass |
| evidence_tamper | edit, deletion, or truncation of the evidence chain detected | intact=True; edit -> line 2: prev_hash mismatch (chain broken; an earlier line was edited or removed); delete -> line 1: prev_hash mismatch (chain broken; an earlier line was edited or removed); truncate -> head mismatch: head says 56 lines, file has 55 (tail edited or truncated) | pass |
| observation_mode | new deny rule in observation mode records would_deny and does not block | HTTP 200 decision=allow would_deny=max_tokens_cap | pass |

## Cost per outcome (results/cost_per_outcome.csv)

| level | key | period | spend USD | requests | outcomes | cost per outcome USD |
|---|---|---|---|---|---|---|
| business_unit | bu-finance | 2026-11 | 0.115062 | 431 | 0 | n/a |
| business_unit | bu-operations | 2026-11 | 0.732926 | 2671 | 4 | 0.18323150000000427 |
| business_unit | bu-sandbox | 2026-11 | 0.006063 | 213 | 0 | n/a |
| team | team-claims | 2026-11 | 0.687371 | 2660 | 4 | 0.17184275000000426 |
| team | team-reporting | 2026-11 | 0.115062 | 431 | 0 | n/a |
| team | team-research-agents | 2026-11 | 0.045399 | 5 | 0 | n/a |
| team | team-sandbox-lab | 2026-11 | 0.006063 | 213 | 0 | n/a |
| team | team-service-desk | 2026-11 | 0.000156 | 6 | 0 | n/a |
| use_case | UC-01 | 2026-11 | 0.006063 | 213 | 0 | n/a |
| use_case | UC-17 | 2026-11 | 0.687371 | 2660 | 4 | 0.17184275000000426 |
| use_case | UC-22 | 2026-11 | 0.000156 | 6 | 0 | n/a |
| use_case | UC-30 | 2026-11 | 0.045399 | 5 | 0 | n/a |
| use_case | UC-40 | 2026-11 | 0.115062 | 431 | 0 | n/a |

## Notes on failures

- none: every scenario and supplementary check passed.

See results/environment.md for hardware, software and convention versions.
