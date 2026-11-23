# Running the prototype as a Code Ocean capsule

A Code Ocean reproducible run executes inside one container, so `docker compose` is not available.
In this mode the same services run as local processes in that container (`STACK_MODE=native`,
[`scripts/native_stack.py`](../scripts/native_stack.py)), with the same ports, configuration and
pinned versions. `codeocean/run` reproduces every result and writes it to `/results`.

## 1. Environment

**Starter environment:** Python 3.12 (any Code Ocean starter with Python 3.12, e.g. the
"Python and R (JupyterLab/RStudio)" one with Python 3.12.8). R and JupyterLab are not used.

**apt-get** (add each):

```
redis-server
make
curl
ca-certificates
```

**pip** — add every line of [`pip-packages.txt`](pip-packages.txt) (exact pins from
`requirements.lock`, Windows-only packages removed). Regenerate it with `make codeocean-packages`
after changing `requirements.lock`.

**Environment variables:**

| name | value |
|---|---|
| `OPA_BIN` | `/opt/bin/opa` |
| `OTELCOL_BIN` | `/opt/bin/otelcol-contrib` |

**Post-install script:** paste the contents of [`postInstall`](postInstall). It downloads OPA v1.22.4
and the OpenTelemetry Collector (contrib) v0.165.0 into `/opt/bin` and checks both against pinned
SHA-256 values. All downloads happen at build time; the run needs no internet access.

## 2. Code and run

1. Import the repository into the capsule's `/code` folder (Git import or upload). No data assets
   are needed; `/data` stays empty.
2. Set `codeocean/run` as the **File to Run**.
3. Click **Reproducible Run**.

The run copies `/code` to `/scratch`, generates throw-away signing keys there (keys are never stored in
the capsule), compiles the control plane, starts the stack, runs all tests, the benchmark and the report,
and verifies the evidence chains. It never writes to `/code`.

Optional environment variables to shorten a trial run: `BENCH_N` (default 1000), `BENCH_WARMUP`
(default 100), `BENCH_SCENARIO_N` (default 200).

## 3. Outputs in `/results`

| file | content |
|---|---|
| `summary.md` | Table 3 (scenarios, decisions, evidence completeness, latency), latency tables, supplementary checks |
| `scenarios.csv`, `supplementary.csv` | per-check expected vs observed, pass/fail |
| `latency.csv`, `stage_latency.csv`, `scenario_latency.csv` | benchmark results |
| `approval_wait.csv`, `cost_per_outcome.csv` | S4a approval wait; S8 cost per outcome |
| `environment.md` | software versions (host hardware redacted) |
| `compile_warnings.json` | control-plane compiler warnings (e.g. the expired agreement in S5) |
| `evidence/` | hash-chained evidence files and their head files |
| `logs/` | one log per service |

The run exits non-zero if any test fails; `summary.md` lists the failed checks.

## Differences from the Docker Compose setup

- Services run as processes in one container rather than separate containers; Redis comes from the
  capsule's apt repository instead of the pinned `redis:8.8.4-alpine` image (its version is recorded
  in `environment.md`).
- The gateway reaches the mocks on `127.0.0.1` through `GOV_HOST_OVERRIDES`, set by the launcher.
- The Redis fail-mode check stops and restarts the `redis-server` process instead of the container.
- Latency numbers depend on the machine Code Ocean assigns and will differ from runs on other hardware.

## Check locally before uploading

With Docker running:

```bash
make codeocean-emulate
```

This builds [`emulate.Dockerfile`](emulate.Dockerfile) (same Python version, apt packages,
post-install script and pip pins), mounts the repository read-only at `/code`, runs `codeocean/run`,
and writes the outputs to `codeocean-results/`.
