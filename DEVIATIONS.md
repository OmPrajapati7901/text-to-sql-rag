# Deviations from the design documents

`DESIGN/outputs/` targets an AWS deployment (Bedrock, managed OpenSearch, PostgreSQL). This
implementation targets the local stack described in `.env`. Everything below is a real gap.
None of it is claimed as done elsewhere.

## 1. No database-level row security — the most significant gap

§17 treats native row security as an **independent** enforcement boundary, separate from the
application. DuckDB has none.

What exists instead: the compiler emits a bound tenant predicate for every `tenant_scoped`
relation, the validator re-reads the emitted SQL and blocks if any scanned tenant-scoped table
lacks one, and the gateway attaches the warehouse `READ_ONLY`.

Why that is weaker: all three live inside the same application. A defect in the compiler *and*
a matching defect in the validator would not be caught by anything downstream. Real RLS fails
independently of application bugs.

**Fix:** a PostgreSQL adapter with RLS policies and a constrained non-owner execution role.
This is the highest-value follow-up in the whole project.

## 2. Certified surface is 2 tables, not 10–20

The catalog holds 12 tables across 4 domains, but only `orders` and `customer_history` are
`is_certified: true` and therefore executable. The other 10 exist as retrieval distractors and
drift targets.

The validator blocks execution against any non-certified table, so this is enforced rather
than merely documented.

## 3. Development cases are below the suggested count

Step 1 suggests 50–100 reviewed development cases. This build has 8 DuckDB fixture scenarios
and ~80 tests. The counterexample *fixtures* — the part that actually catches wrong semantics —
are complete: SCD overlap, SCD gap, fan-out, equal amounts, partial refund, empty result, DST,
tenant collision.

## 4. Second provider verified; Bedrock not implemented

**Resolved for OpenSearch.** The 22-test contract suite has been run against both
`InMemoryProvider` and `OpenSearchProvider` on OpenSearch 2.17.0 (11 tests per provider, all
passing), and the full §40A question returns the identical result through the real adapter
with 768-dimension kNN vectors. Same graph nodes, same business logic, different retrieval
backend, same answer.

Still outstanding: the Bedrock Knowledge Bases provider is not implemented, so portability
across a *managed* retrieval service with asynchronous ingestion semantics is untested.

The OpenSearch tests still skip when `localhost:9200` is unreachable, so CI without a
container silently covers only one provider.

## 5. Planner model is not pinned

`app/llm/client.py` documents this at the definition site. An `auto/*` alias lets the router
pick a different model per request, so the same question can produce different plan proposals.
Every concrete backend on the local router currently returns 402/401 (credits exhausted), so
the default is an alias.

`ttsql doctor` warns whenever the planner is unpinned. Set `TTSQL_PLANNER_MODEL` to a concrete
id once one has credit — that is the entire fix.

Note this contradiction already exists in `.env`: the comment above `RAG_GRADER_MODEL` says
"PIN a concrete id here, not an `auto/*` alias" and the value is `auto/best-fast`.

## 6. Confidence is evidence status, not a probability

§20 permits a calibrated selective-answering model only once independently adjudicated cases
exist. They do not. So answers carry `verified` / `unavailable` and never a numeric score.

## 7. Repair is deliberately narrow

Bounded repair is implemented and wired, but it only handles changes that **cannot alter a
number**: currently a sort key naming a column the plan never projected.

This is a design decision, not an omission. §18 forbids a repair from changing the requested
population, metric version, time interval, tenant scope, exactness or mandatory rules — and in
this system nearly every failure touches one of those. An unsupported grain, an unsupported
dimension or a missing metric all change what was asked. "Fixing" them silently is exactly the
substitution §45 prohibits, so they route to clarification or an explicit stop instead.

`app/planning/repair.py::guard` enforces this on every repair, so a future rule cannot widen
its own remit: it rejects any diff touching `metric_ids`, `time_range`, `filters`,
`attribution`, or widening `limit`.

## 8. Not implemented from milestones C/D

- **Semantic critic** (§16) — the optional LLM reviewer that compares original wording against
  the formal plan.
- **Conversation follow-ups** (§22) — each turn is independent; there is no plan-delta path
  for "now by region".
- **Caching** (§24), the production **FastAPI surface**, and the **observability/audit split**
  (§25). A local Chainlit developer UI and optional LangSmith traces now exist, but neither is
  the production API or the durable governed audit trail described by the design.
- **Evaluation harness** (§26) — blocked on pinning a planner model, per gap 5.
