# Governed Text-to-SQL

A semantic query compiler orchestrated by LangGraph. The LLM interprets language and proposes
a **typed semantic request**; authoritative services resolve names, metrics, rules,
relationships and permissions; a deterministic compiler produces SQL.

The model never writes SQL, never receives credentials, and never gets a generic query tool.

Implements milestones A–C of `DESIGN/outputs/langgraph-text-to-sql-implementation-plan.md`
against the architecture in `DESIGN/outputs/enterprise-text-to-sql-architecture.md`, retargeted
from AWS to the local stack in `.env`. Known gaps are in [DEVIATIONS.md](DEVIATIONS.md).

## Quick start

```bash
uv sync
uv run ttsql catalog publish          # validate + publish the immutable snapshot (already done)
uv run ttsql fixtures seed            # build the DuckDB warehouse scenarios
uv run ttsql doctor                   # which local services are reachable
uv run ttsql ask "Show monthly revenue by customer region in Q2 2026"
uv run pytest -q
```

### Chainlit UI

The local UI reuses the same `AppRuntime` and graph as the CLI. It adds progress steps,
clickable clarifications, Markdown results, provenance, a complete in-memory CSV download,
and placeholder-only SQL in a side panel.

```bash
uv sync
uv run ttsql fixtures seed
uv run chainlit run app/chainlit_ui.py -w
```

Open the URL printed by Chainlit (normally `http://localhost:8000`). Its health endpoint is
`http://localhost:8000/health`. Each question gets a new LangGraph thread; a clarification
resumes that same thread. The process shares one lazily initialized runtime, while each user
session has a lock that serializes its requests.

Chainlit also treats a generic `DEBUG` environment variable as its Boolean CLI debug flag. If
another local tool has set `DEBUG` to a non-Boolean value, unset it or start with
`DEBUG=false uv run chainlit run app/chainlit_ui.py -w`.

Chainlit configuration is server-controlled. Copy `.env.example` to `.env` and adjust these
values before starting the server:

| Variable | Default | Purpose |
|---|---:|---|
| `TTSQL_CHAINLIT_PRINCIPAL` | `analyst_full` | Trusted local fixture principal |
| `TTSQL_SCENARIO` | `golden` | DuckDB fixture scenario |
| `TTSQL_RETRIEVAL_PROVIDER` | `inmemory` | `inmemory` or `opensearch` |
| `TTSQL_USE_LLM` | `true` | Use OmniRoute when healthy; otherwise deterministic fallback |
| `TTSQL_USE_EMBEDDINGS` | `true` | Use Ollama embeddings when healthy |
| `TTSQL_CHAINLIT_SHOW_SQL` | `true` | Attach placeholder SQL in the side panel |
| `TTSQL_CHAINLIT_MAX_ROWS` | `100` | Rows displayed in chat; CSV still contains all rows |

There is deliberately no UI authentication, chat persistence, runtime settings, or principal
selector in this local-development version. Do not expose it as a production service.

### Optional LangSmith tracing

Set the canonical LangSmith variables in `.env`, then restart Chainlit:

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=your-key
LANGSMITH_PROJECT=texttosql-chainlit
# LANGSMITH_ENDPOINT=https://api.smith.langchain.com
```

Traces appear in the named project in LangSmith. Root runs are named `texttosql.ask` and
`texttosql.resume`; tags and metadata correlate the Chainlit session, request, scenario,
provider, model, policy epoch, catalog snapshot, and graph thread. LangGraph nodes appear as
children, and the wrapped OpenAI-compatible OmniRoute call appears below the node that made
it. The legacy `LANGCHAIN_TRACING_V2=true` flag remains accepted, but
`LANGSMITH_TRACING=true` is preferred.

For this fixture-data demo, traces intentionally contain question text, candidate graph
state, placeholder SQL, and result rows. Credentials, `TrustedScope`, live service objects,
and bound SQL parameter values stay outside graph state and trace metadata. Production data
needs a separate redaction policy before tracing is enabled.

Tracing is optional and observability-only. To disable it, unset the tracing variables or set
`LANGSMITH_TRACING=false`. A missing key or trace upload failure does not block query
execution.

Only DuckDB is required. Ollama, the OmniRoute router and OpenSearch are each optional and
degrade explicitly:

| Service | Missing behaviour |
|---|---|
| Ollama (`:11434`) | Keyword-only retrieval; no vector branch |
| OmniRoute (`:20128`) | Deterministic understander/proposer instead of the LLM |
| OpenSearch (`:9200`) | In-memory provider; `opensearch`-marked tests skip |

## The worked example

The acceptance target is §40A of the architecture document, so the expected output was
specified and reviewed before this code existed:

```
month       region_at_order  revenue_usd
----------  ---------------  -----------
2026-04-01  North            200.00
2026-06-01  South            50.00

Provenance
  metric        finance.revenue (v7)
  time          [2026-04-01T00:00:00+00:00, 2026-07-01T00:00:00+00:00) UTC, by month — end exclusive
  rules applied commerce.reporting_timezone@1, global.exclude_internal_customers@2, security.tenant_scope@5
  join path     commerce.orders_customers_asof@2
  evidence      verified
```

The golden fixture contains six orders that must **not** appear in that total — an internal
customer, a test customer, a test order, a fully refunded order, a pending order and another
tenant's order with a colliding `order_id`. Each is excluded for a different reason, so any one
leaking changes the answer.

## How a question becomes an answer

```
question
  → admit              reauthorize from the authenticated principal; derive TrustedScope
  → understand         LLM structured output → route + phrases (no identifiers)
  → retrieve           exact glossary match first; then BM25 + vector, fused by RRF
  → propose            LLM picks from OFFERED ids only; anything else is discarded
       ↳ clarify       suspend on material ambiguity; resume re-enters admit
  → bind_compile       resolve versions → rule closure → join plan → SQLGlot → 20 gates
       ↳ repair        bounded, meaning-preserving; rebinds and revalidates in full
  → execute            admission ticket → EXPLAIN → uniqueness attestation → query
  → answer             deterministic rendering with provenance
```

Every branch has an explicit terminal path. `unavailable`, `denied`, `unsupported` and
`clarify` are outcomes, not errors.

### Clarification

Raised only when alternatives change the answer and no authoritative default settles them
(§21). An exact glossary match is never ambiguous — "Revenue" binds to the official metric
rather than prompting a question.

```
$ ttsql ask "revenue"
Which period should this cover?
  1. Q2 2026 — 1 April to 30 June 2026 inclusive
  2. Calendar year 2026 — 1 January to 31 December 2026 inclusive
  3. Q1 2026 — 1 January to 31 March 2026 inclusive
```

Answering 1 gives 250.00; answering 3 gives 444.00. Different answers are the point — a
question that could not change the result should not be asked.

Three properties matter here:

- **The resume payload is untrusted.** It is validated against the options that were offered,
  so a resume cannot inject an object retrieval and authorization never saw.
- **Resume re-enters `admit`**, because a grant may have been revoked while the request was
  waiting. The clarify node is side-effect-free before its interrupt, since resuming restarts it.
- **A question that cannot change the outcome is never asked.** A principal who may not
  execute the metric gets the denial on turn one, not after choosing a period.

### What is deterministic

Everything except two steps. The LLM appears only in `understand` (what is this asking?) and
`propose` (which catalog IDs and operators?). Binding, rule closure, join planning,
compilation, validation, admission, execution and rendering contain no model call.

Milestone A's exit criterion — hand-authored semantic plans compiling and reproducing §40A
exactly — was met before any LLM existed in the codebase. `uv run ttsql plan` still runs that
path directly.

## Enforcement boundaries

**`TrustedScope` cannot be forged.** It requires a construction token only
`app/authorization/scope.py` can mint, so "the model fabricated a scope" is a type error. The
token is excluded from serialization, so it never reaches a checkpoint.

**Denies cascade down the action lattice.** Five distinct actions — `discover`, `read`,
`use_in_predicate`, `execute_metric`, `disclose`. A `read` deny cascades to
`use_in_predicate`, because masking a projection does not stop inference through filtering or
sorting (failure mode #25). Only an *exact-resource* grant overrides a cascade; a wildcard
cannot silently reopen a specific deny.

**Mandatory rules bypass retrieval entirely.** Asking for Revenue by region seeds only
`orders`. Rule closure pulls in `customer_history` and the temporal join — a table the user
never mentioned — because `global.exclude_internal_customers@2` requires it. Closure runs to a
fixed point, capped at 8 rounds, and a non-convergence error blocks rather than proceeding.

**Validation reads the emitted SQL, not the plan that produced it.** §15 is explicit that
comparing a plan to itself proves nothing. The compiled SQL is re-parsed with SQLGlot and
checked independently. Nine tamper tests mutate the SQL text while leaving the plan untouched;
each is caught by the gate that should catch it:

| Tamper | Caught by |
|---|---|
| Mandatory rule predicate removed | `MANDATORY_RULE_UNRESOLVED` |
| Tenant isolation removed | `MANDATORY_RULE_UNRESOLVED` |
| `SUM(DISTINCT)` as a fan-out repair | `VALIDATION_FAILED` |
| Partial join key (tenant dropped) | `VALIDATION_FAILED` |
| SCD open-interval clause dropped | `VALIDATION_FAILED` |
| Literal inlined instead of bound | `UNSAFE_QUERY` |
| Second statement appended | `UNSAFE_QUERY` |
| Forbidden function substituted | `UNSAFE_QUERY` |
| Swapped to a non-certified table | `UNSUPPORTED_CAPABILITY` |

A gate that raises is converted into a blocking finding: a validator that cannot complete has
proven nothing.

**The uniqueness attestation.** §40 makes this an execution prerequisite, and it is the one
check nothing downstream can replace. An overlapping SCD interval double counts a fact while
leaving the output grain *perfectly unique* — an inner join succeeds, `EXISTS` succeeds, and
the duplicate-key check sees nothing wrong. So before aggregating across an as-of join, one
diagnostic query confirms each fact matches exactly one dimension version. It counts a
non-nullable column from the **right** side, because `COUNT(*)` would count the left row even
when the LEFT JOIN matched nothing.

```
golden          facts=5 overlapping=0 unmatched=0  -> OK
scd_overlap     facts=2 overlapping=1 unmatched=0  -> BLOCK (would double count)
scd_gap         facts=1 overlapping=0 unmatched=1  -> BLOCK (would under-report)
```

**Admission tickets.** A ticket binds the canonical SQL hash, parameters, policy epoch,
snapshot and principal. Validated SQL cannot be swapped afterwards, a ticket cannot be reused
by another principal, and a changed policy epoch invalidates it.

## Counterexample fixtures

A single convenient dataset can make a wrong query look right, so each scenario is its own
database and each must *change* an answer or block if the semantics are wrong.

| Scenario | What it proves |
|---|---|
| `golden` | §40A exactly, with six differently-excluded rows |
| `partial_refund` | v7 **retains** a partially refunded amount; excluding it under-reports |
| `equal_amounts` | Two distinct 100.00 orders — `SUM(DISTINCT)` would report 100, not 200 |
| `scd_overlap` | Double counting that the grain check cannot see |
| `scd_gap` | Silent under-reporting from a coverage hole |
| `fanout` | One order, three lines — joining them triples the header amount |
| `empty` | A legitimate empty result, never repaired by loosening a filter |
| `dst` | Bucketing follows the governed reporting timezone, not the machine's |

## Layout

```
app/
  contracts/     Pydantic contracts: ids, scope, expressions, semantic_plan, catalog
  authorization/ policy engine (5 actions, cascading denies), scope establishment
  catalog/       immutable snapshot loader, exact authorized resolution by ID
  semantics/     metric closure, rule engine (classes + fixed-point closure)
  relationships/ typed multigraph, join planner with fan-out proofs
  retrieval/     service, embeddings, providers/{inmemory,opensearch}
  planning/      binder (proposal → bound plan), lowering (→ logical plan)
  sql/           compiler, dialects/duckdb, validators/suite, attestation, gateway
  results/       result contracts, deterministic rendering
  llm/           OmniRoute client, understand, propose
  graph/         state, nodes, build
  chainlit_ui.py local UI, clarification actions, CSV and SQL elements
ingestion/       source_bundle (reviewed records), publish, export (search cards)

Not yet present (milestone D scope, see DEVIATIONS.md): a production API, audit-grade
observability, evaluation/, the semantic critic, conversation follow-ups and caching.
catalog/         snapshots/local-001/ (published, immutable), policy.json
fixtures/        DuckDB scenario builder
tests/           unit / contract / opensearch / llm / e2e
```

Source records and search projections are separate artifacts. `ingestion/source_bundle.py` is
the reviewed source of truth; `publish.py` compiles it into a hashed immutable snapshot after
referential validation; `export.py` derives discovery cards. A card is a discovery aid — the
executable definition is always fetched from the catalog by ID.

## Tests

```bash
uv run pytest -m "unit or contract"   # no external services
uv run pytest -m e2e                  # DuckDB fixtures
uv run pytest -m llm                  # requires OmniRoute
uv run pytest -m opensearch           # requires localhost:9200
```

To run the OpenSearch half of the contract suite:

```bash
docker run -d --name opensearch -p 9200:9200 -e discovery.type=single-node \
  -e DISABLE_SECURITY_PLUGIN=true opensearchproject/opensearch:2.17.0
uv run pytest -m opensearch
```
