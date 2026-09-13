from pathlib import Path
import json

root = Path('outputs/bedrock-text-to-sql-rag')
cards = root / 's3/text-to-sql/cards/catalog-demo-42'
def block(lang, text):
    return f'```{lang}\n{text.strip()}\n```'
def card(relative):
    return block('markdown', (cards / relative).read_text())
def config(name):
    return block('json', (root / 'examples/chunking' / (name + '.json')).read_text())

guide = '''# Implementing Text-to-SQL RAG with Amazon Bedrock Knowledge Bases and OpenSearch Serverless

**Implementation guide | 12 September 2026**

## 1. What you are building

Build a retrieval layer that finds the enterprise knowledge needed to create a correct SQL plan: glossary terms, metrics, tables, columns, rules and approved relationships. Bedrock handles ingestion and embedding; OpenSearch Serverless stores the vectors and searchable text; your LangGraph application calls Bedrock's `Retrieve` API.

This guide covers a **customer-managed Knowledge Base connected to OpenSearch Serverless**, not AWS's separate fully managed Knowledge Base offering or its structured-database query-generation feature.

**Recommended baseline:** S3 as the source; Markdown cards with one governed object per file; `NONE` chunking for cards; a separate S3 data source using `FIXED_SIZE` chunking for longer prose; hybrid retrieval with trusted metadata filters; optional evaluated reranking.

All enterprise names, definitions and permissions below are fictional. Numerical tuning settings are starting configurations, not measured accuracy claims. These files demonstrate retrieval preparation, not a complete SQL compiler or authorization system.

```mermaid
flowchart TD
    CAT[Approved catalog, metric and rule registries] --> EXPORT[Export versioned Markdown cards and metadata]
    DOCS[Reviewed business documentation] --> EXPORT
    EXPORT --> S3[(S3 source files)]
    S3 --> ING[Knowledge Base ingestion and embeddings]
    ING --> OS[(OpenSearch Serverless)]
    USER[User question and authenticated identity] --> LG[LangGraph retrieval node]
    LG --> POL[Build current authorization filter]
    POL --> RET[Bedrock Retrieve: hybrid search and optional reranking]
    RET --> OS
    OS --> HITS[Candidate text, metadata and source locations]
    HITS --> VERIFY[Reauthorize and resolve authoritative object versions]
    VERIFY --> DEP[Deterministic metric, rule and join expansion]
    DEP --> PLAN[Validated semantic plan]
    PLAN --> SQL[SQL compiler, authorization and execution]
```

The Knowledge Base discovers candidates. It does not guarantee that every mandatory rule or bridge table was retrieved. Exact metric definitions, rule applicability and approved join predicates must come from authoritative services after discovery.

## 2. Set up the AWS resources

1. Choose an AWS Region supporting your Knowledge Base, embedding model, OpenSearch Serverless configuration and optional reranker.
2. Create a general-purpose S3 bucket in the same Region as the Knowledge Base.
3. Create a Knowledge Base **with a vector store**, select an embedding model, and choose OpenSearch Serverless. In the console flow, configure its initial S3 source with inclusion prefix `text-to-sql/cards/` and chunking **NONE**. Save that source ID. Use the supported quick-create path for the vector store, retaining your organization's encryption and private-network requirements. Section 6 reuses this initial source and creates only the prose source. [Console creation flow](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-create.html)
4. If supplying an existing index, match its vector dimension to the chosen embedding model. Follow Bedrock's Serverless prerequisites: a compatible `faiss` vector engine, a text field that supports hybrid retrieval, and the required Bedrock metadata field. Do not reuse the Lucene mapping from a direct-OpenSearch example.
5. Configure the Knowledge Base service role for the source bucket, embedding model, vector collection and relevant KMS keys. Configure Serverless data-access/network policies so Bedrock can reach the collection.
6. Give only your trusted application service the required retrieval access. End users should go through that service.

Titan Text Embeddings V2 with 1,024 dimensions is a reasonable AWS-native baseline to evaluate, not a proven best model. In this workflow Bedrock generates document and query embeddings; your application does not need to calculate or upload vectors manually. [Vector-store setup](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-setup.html), [service-role permissions](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-permissions.html), [Titan embedding models](https://docs.aws.amazon.com/bedrock/latest/userguide/titan-embedding-models.html)

## 3. Which documents to ingest

### 3.1 Export business objects, not an entire schema dump

| Document type | One file should represent | Essential content |
|---|---|---|
| Domain | One business area | Scope, main concepts and object references. |
| Table | One physical table | Qualified name, purpose, grain, keys, relevant columns and relationship IDs. |
| Column | One business-relevant field | Qualified name, type, units, meaning, aliases and approved enum values where appropriate. |
| Metric | One metric version | Official meaning, measure, population, time semantics, dimensions and dependency references. |
| Rule | One rule version | Applicability, effect, required dependencies and authoritative reference. |
| Relationship | One approved relationship | Composite keys, direction, cardinality, temporal meaning and approved join type. |
| Glossary | One concept | Definition, synonyms, domain and canonical object mapping. |
| Business prose | One coherent document | Explanations, exceptions and methodology. |

Keep the complete executable registry outside the discovery corpus. Render searchable summaries into Markdown. Do not assume arbitrary JSON/JSONL registry exports will be ingested as supported text documents.

The supplied example bundle contains **15 source documents and 15 matching metadata sidecars**: 14 atomic cards and one methodology document. It is a small functional sample, not a ranking benchmark.

### 3.2 Supported formats

For this use case, prefer UTF-8 `.md` or `.txt`. They make identifiers and object boundaries explicit and avoid unnecessary document parsing.

AWS also lists `.html`, `.doc`, `.docx`, `.csv`, `.xls`, `.xlsx` and `.pdf` for standard document ingestion, with a documented 50 MB source-file limit. Parser and model constraints still apply; a large accepted file is not necessarily suitable as one embedding input. [Supported formats and limits](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-ds.html)

Do not use raw customer records, credentials or unrestricted sample values as metadata. Apply the intended discovery permissions before exporting text, including table summaries that mention restricted columns.

### 3.3 S3 layout

Upload the bundle's `s3/` contents while preserving their paths:

```text
s3://YOUR_BUCKET/text-to-sql/
  cards/catalog-demo-42/
    domains/commerce.md
    tables/orders.md
    tables/orders.md.metadata.json
    tables/customers.md
    columns/order_amount_usd.md
    metrics/revenue-v7.md
    metrics/revenue-v7.md.metadata.json
    rules/exclude-internal-customers-v2.md
    relationships/orders-customers-v2.md
    glossary/revenue.md
    ...each Markdown file has its own sidecar...
  prose/catalog-demo-42/
    revenue-methodology.md
    revenue-methodology.md.metadata.json
```

Create **two S3 data sources in the same Knowledge Base** with non-overlapping inclusion prefixes:

- `text-to-sql/cards/` uses `NONE`.
- `text-to-sql/prose/` uses `FIXED_SIZE`.

This separates chunking behavior without creating one data source per table. The snapshot subdirectory permits complete versioned releases; only publish a snapshot after both data sources are ready.

## 4. Concrete document and metadata examples

### 4.1 Table card: `tables/orders.md`

{{TABLE}}

For very wide tables, keep this card concise and publish separate column cards. Required keys must also be available through the exact catalog resolver; a missing column-card hit must not prevent dependency expansion.

### 4.2 Metric card: `metrics/revenue-v7.md`

{{METRIC}}

This definition intentionally specifies partial-refund behavior. Without that qualifier, a model might incorrectly infer a net-revenue calculation.

### 4.3 Column card: `columns/region_name.md`

{{COLUMN}}

### 4.4 Rule card: `rules/exclude-internal-customers-v2.md`

{{RULE}}

### 4.5 Relationship card: `relationships/orders-customers-v2.md`

{{JOIN}}

These references identify fictional registry records. Implement the canonical resolver against your real catalog/semantic services; the example URIs are not deployed endpoints.

### 4.6 Matching metadata sidecar

For `revenue-v7.md`, create **`revenue-v7.md.metadata.json` in the same S3 directory**:

{{METADATA}}

The sidecar uses Bedrock's `metadataAttributes` wrapper. It is not the same JSON shape as an OpenSearch document you index directly.

Use scalar strings for object IDs/versions and small, flat attributes for filters. The example `access_scope` is an application-defined entitlement segment, not an AWS-reserved field or a complete policy engine. Do not put lengthy metric expressions or all possible user IDs into the sidecar.

The simple sidecar form stores attributes for filtering without adding them to the embedding. Therefore, put searchable names, aliases and definitions in the Markdown body. AWS also supports typed attributes with explicit `includeForEmbedding`; use that only deliberately. Keep security identifiers out of semantic text. The documented sidecar limit is 10 KB. [S3 metadata format](https://docs.aws.amazon.com/bedrock/latest/userguide/s3-data-source-connector.html), [metadata guidance](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-metadata.html)

Metadata belongs to its source document and accompanies its derived chunks. If different sections require different permissions or object identities, split them into separate source files before ingestion.

## 5. Chunking choices and settings

### 5.1 Recommended baseline

| Content | Recommended method | Starting setting | Reason |
|---|---|---|---|
| Table, metric, rule and relationship cards | `NONE` | One object per file, usually 150–600 tokens | Preserve the object, qualifiers and dependency references together. |
| Short glossary/column cards | `NONE` | Often 80–250 tokens; shorter is fine | Avoid padding and duplicate fragments. |
| Longer prose | `FIXED_SIZE` | 512 maximum tokens, 12% overlap | Simple baseline for explanations and nearby exceptions. |
| Long manuals needing surrounding section context | Trial `HIERARCHICAL` | Parent 1,200; child 300; overlap 50 tokens | Retrieve a focused child and return broader parent context. |
| Prose with irregular topic changes | Trial `SEMANTIC` | Maximum 512; buffer 1; threshold 95 | Test topic-driven boundaries against the fixed-size baseline. |

These recommendations are not empirically validated on your corpus. Do not assume semantic or hierarchical chunking is more accurate because it is more elaborate.

`NONE` means each file is one chunk; it does not bypass model input limits. Keep large canonical definitions in the registry and create concise cards. Hierarchical parent/child chunking is a document feature, not enterprise domain routing or join-graph traversal. [Bedrock chunking behavior](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-chunking.html)

### 5.2 Exact configuration examples

The following objects are values for `vectorIngestionConfiguration` when creating the data source.

**Atomic cards:**

{{NONE}}

**Recommended prose baseline:**

{{FIXED}}

`overlapPercentage` is a percentage, not a token count: 12% of 512 is approximately 61 tokens. The current fixed-size API accepts integer overlap values from 1 to 99. For exact zero overlap, pre-split files and use `NONE`. [Fixed-size configuration](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_FixedSizeChunkingConfiguration.html)

**Optional semantic experiment:**

{{SEMANTIC}}

`bufferSize=1` incorporates neighboring sentences when identifying boundaries; it is not a token-overlap setting. [Semantic configuration](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_SemanticChunkingConfiguration.html)

**Optional hierarchical experiment:**

{{HIERARCHICAL}}

Specify two levels, with the larger parent first. Bedrock replaces retrieved children with parent chunks, so final result count can be lower than the requested candidate count. [Hierarchical configuration](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_HierarchicalChunkingConfiguration.html)

Chunking configuration cannot be changed after data-source creation. Run alternative strategies in isolated test data sources/Knowledge Bases, then deliberately replace the selected production configuration. Do not ingest the same source under multiple strategies into the active corpus and accidentally evaluate duplicates. [CreateDataSource](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_CreateDataSource.html)

## 6. Upload, create data sources and synchronize

The bundle includes `examples/ingest.py`. The following code is intended to run **from the extracted bundle directory in your configured AWS environment**. Install a current compatible `boto3` version and use your organization's normal AWS credential mechanism. No credentials are embedded in the examples.

### 6.1 Upload the documents

```python
import boto3
from examples.ingest import upload_sources

region = "YOUR_AWS_REGION"
bucket_name = "YOUR_BUCKET"
s3 = boto3.client("s3", region_name=region)

uploaded_files = upload_sources(s3, bucket_name, "s3")
# The sample has 30 files: 15 Markdown documents and 15 sidecars.
```

Upload all content and sidecars before starting ingestion. The local manifest stays outside `s3/` and records expected object identities and hashes.

**Published snapshot content and sidecars must never be overwritten.** The sample upload helper does not enforce immutability; use it only for a new snapshot prefix or identical-content retries. Production release tooling must reject changed content under an existing snapshot. S3 object versioning alone does not make this connector read an older release.

### 6.2 Reuse the cards source and create the prose source

The console route in Section 2 already created the cards source. Reuse its ID and create only the prose source below. Do not leave a broad or overlapping initial source with default chunking.

```python
import json
from pathlib import Path
from examples.ingest import create_source

control = boto3.client("bedrock-agent", region_name=region)
kb_id = "YOUR_KB_ID"
bucket_arn = f"arn:aws:s3:::{bucket_name}"

prose_config = json.loads(
    Path("examples/chunking/prose-fixed.json").read_text()
)["chunkingConfiguration"]

cards_source_id = "YOUR_EXISTING_CARDS_DATA_SOURCE_ID"
prose_source_id = create_source(
    control, kb_id=kb_id, bucket_arn=bucket_arn,
    name="business-prose", prefix="text-to-sql/prose/",
    chunking_configuration=prose_config,
    client_token="REPLACE_WITH_PERSISTED_CREATE_PROSE_UUID",
)
```

If your deployment instead created a Knowledge Base through the API **without any data sources**, create the cards source once using the same helper, prefix `text-to-sql/cards/`, name `metadata-cards`, and `chunking_configuration={"chunkingStrategy": "NONE"}`. Do this only when the cards source does not already exist.

Replace token placeholders with persisted UUID strings, such as values generated once with `str(uuid.uuid4())`. Reuse the token when retrying the same uncertain creation request. Save returned data-source IDs; do not recreate sources for every sync.

The helper wraps `create_data_source` with `dataSourceConfiguration.type="S3"`, the bucket ARN and inclusion prefix, and `vectorIngestionConfiguration`. The example chooses `dataDeletionPolicy="DELETE"`: deleting the data-source/Knowledge Base resource removes its ingested vectors; it does not delete the vector store itself. [CreateDataSource request and deletion policy](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_CreateDataSource.html)

### 6.3 Start and inspect ingestion

```python
from examples.ingest import start_sync, inspect_sync

job_id = start_sync(
    control, kb_id=kb_id, source_id=cards_source_id,
    client_token="REPLACE_WITH_PERSISTED_CARDS_SYNC_UUID",
)

job_status = inspect_sync(
    control, kb_id=kb_id, source_id=cards_source_id, job_id=job_id,
)
# Schedule another inspection with bounded backoff if still active.
# Start and inspect a separate job for prose_source_id too.
```

A start response means accepted, not searchable. Use a bounded polling deadline. Active statuses include `STARTING`, `IN_PROGRESS` and `STOPPING`; terminal statuses are `COMPLETE`, `FAILED` and `STOPPED`.

Before publishing the snapshot:

1. Require successful completion for both sources.
2. Inspect `failureReasons`, failed-document counts and skipped-document counts. Missing statistics are unknown, not automatically zero.
3. Reconcile indexed documents against the reviewed manifest, using ingestion diagnostics where needed.
4. Run readiness queries for representative object IDs, versions and restrictive filters.
5. Switch the application's active `snapshot_id` only after these checks pass.

Bedrock synchronization is incremental for changed source content. Updating S3 alone is not a completed Knowledge Base sync. AWS notes that retrieval visibility can lag completed synchronization for non-Aurora stores. [Sync behavior](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-data-source-sync-ingest.html), [job status](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_IngestionJob.html), [statistics](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_IngestionJobStatistics.html)

For later releases, upload a complete new snapshot under a new subdirectory and retain old snapshot files while active requests need them. S3 sync is not an atomic enterprise catalog transaction. Coordinate snapshot publication across sources and retain canonical metric/rule versions separately. Access revocation must use current policy rather than waiting for ingestion.

## 7. Query and tune retrieval

### 7.1 What happens on a request

1. LangGraph resolves the question and authenticated context.
2. Trusted code builds the allowed metadata scope and selects a published snapshot.
3. Bedrock embeds the search text and retrieves candidates from OpenSearch Serverless.
4. Hybrid search combines semantic similarity and text matching. Optional reranking reorders candidates.
5. `Retrieve` returns source content, metadata, locations and scores.
6. Your application reauthorizes records, fetches exact definitions and expands dependencies before planning SQL.

Use `Retrieve` for this workflow. It returns evidence you can validate inside LangGraph. The application's SQL execution path remains responsible for producing data answers. [Retrieve API](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_Retrieve.html)

### 7.2 Concrete request

This is the request shape constructed by the bundled helper. Replace the illustrative Knowledge Base ID with your real ID. The scope values must come from trusted configuration/current policy, not browser input or LLM output.

{{REQUEST}}

In boto3, the request is sent with:

```python
runtime = boto3.client("bedrock-agent-runtime", region_name=region)
response = runtime.retrieve(**request)
# response contains candidates; verify them before supplying model context.
```

The operational wrapper in `examples/retrieve.py` constructs mandatory filters, validates returned scope, requires a current-policy callback and preserves source references. Example application usage:

```python
from examples.retrieve import make_client, retrieve

runtime = make_client(region)
scope = {
    "knowledge_base_id": kb_id,
    "question": "Revenue by current customer region for January 2026",
    "tenant_id": "tenant-demo",
    "access_scope": "finance-analysts-demo",
    "snapshot_id": "catalog-demo-42",
}

# Your application implements this callback using its authenticated request
# context, policy service and authoritative version registry.
metric_page = retrieve(
    runtime,
    authorize_reference=current_policy_and_version_check,
    object_type="metric",
    **scope,
)
```

The callback is intentionally not replaced with an always-true placeholder. It must confirm permission and version validity or deny/raise. A pagination token means more response content remains; preserve the same scope when continuing.

### 7.3 Tuning options

| Setting | Starting recommendation | What to test |
|---|---|---|
| Search type | Explicit `HYBRID` | Compare with `SEMANTIC`; hybrid requires a compatible filterable text field. |
| Candidate count | `numberOfResults=40` | Compare 20, 40 and 80; API range is 1–100. |
| Reranking | Evaluate a supported Bedrock reranker, returning 20 candidates | Check improvement in complete evidence and metric/table ranking. |
| Object scope | Separate metric, glossary and table searches as needed | Prevent a large table corpus from crowding out metrics. |
| Domain scope | Use confirmed domains; preserve plausible alternatives | Avoid excluding cross-domain dependencies. |
| Context size | Start with roughly 10 discovery cards | Apply a token budget and always resolve mandatory dependencies. |

With hierarchical chunking, candidate count refers to retrieved children before parent substitution. It is not a guaranteed number of final documents. Reranking cannot recover required evidence absent from the candidate pool. [Retrieval settings](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_KnowledgeBaseVectorSearchConfiguration.html), [query customization](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-test-config.html)

To enable reranking in the helper, pass a **supported model ARN from your Region** as `reranker_model_arn`. It adds:

{{RERANK}}

The wrapper additionally limits which metadata fields are used for reranking. Do not assume every foundation-model ARN is a reranker. [Reranking configuration](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_VectorSearchBedrockRerankingConfiguration.html)

Start metric and table searches independently where useful. After selecting a metric, load exact required tables, columns, rules and relationships by ID from the registry. Use further retrieval for unresolved business meaning, not as the only way to discover mandatory dependencies.

### 7.4 Permission behavior

The example `tenant_id` plus `access_scope` equality filters demonstrate one simple precomputed entitlement segment. They do not implement arbitrary per-user column permissions, deny rules or all enterprise roles.

The policy adapter must establish that every document admitted by the filter is currently permitted. If it cannot, stop or use a separately secured path. Built-in reranking already sees retrieved content; a later application check cannot undo exposure. Never let model-generated implicit filters determine authorization.

For the conventional S3 configuration, AWS states that synchronized content is available to principals with `bedrock:Retrieve`. Restrict that permission to trusted services and prevent direct bypass. Database row/column security is enforced independently at execution. [S3 access considerations](https://docs.aws.amazon.com/bedrock/latest/userguide/s3-data-source-connector.html), [filter operators](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_RetrievalFilter.html)

## 8. Worked Text-to-SQL request

**Question:** “Revenue by current customer region for January 2026.”

The intended retrieval outcome is the approved Revenue metric and the meaning of current customer region. Actual ranking must be tested; no result ordering is claimed here.

The deterministic resolver then establishes:

| Plan element | Authoritative resolution in the demo |
|---|---|
| Metric | `finance.revenue@7` |
| Measure | Sum `orders.order_amount_usd` at order grain |
| Order population | Completed, non-test, not fully refunded |
| Customer population | `global.exclude_internal_customers@2` |
| Relationship | `commerce.orders_customers@2`, including tenant and customer keys |
| Dimension | `customers.region_name`, current attribution |
| Time | `ordered_at >= 2026-01-01T00:00:00Z` and `< 2026-02-01T00:00:00Z` |
| Minimal table set | Orders and customers |

The compiler validates the approved many-to-one join and applies population filters before aggregation. If the user instead asks for **region at the time of purchase**, the sample metadata says that historical attribution is unavailable. The application must not substitute current region.

If the question is “What does Revenue mean?”, return a definition with the approved metric citation; no SQL is needed. If it asks for a clearly specified calculation outside official metrics, bind verified fields/operators and clarify only unresolved meaning. Do not invent a new official KPI.

## 9. Validate before production

Run a small reviewed suite including exact metric names, synonyms, similar metric names, omitted rule dependencies, current versus historical region, wrong-tenant access, revoked access, stale snapshots and empty retrieval.

Measure object Recall@K and complete required-evidence coverage, then verify final semantic plans and results on controlled database fixtures. Compare chunking configurations on the same questions and source evidence, rather than comparing changing chunk IDs. Record latency, context tokens, failed/skipped ingestion and permission failures.

The supplied corpus is intentionally small. Use a representative enterprise catalog with distractors for realistic retrieval evaluation; use a much longer document to test prose chunking boundaries.

**Validation status:** The local files and JSON configurations were checked, and the Python retrieval wrapper was exercised with synthetic responses. No AWS resources were provisioned, no ingestion job was run, and no live retrieval or SQL accuracy benchmark was executed. Before deployment, validate service compatibility, quotas, policy behavior and ranking in your AWS environment.
'''

metric_meta = json.loads(Path(str(cards / 'metrics/revenue-v7.md') + '.metadata.json').read_text())
request = {
    'knowledgeBaseId': 'DEMO123456',
    'retrievalQuery': {'text': 'Revenue by current customer region for January 2026'},
    'retrievalConfiguration': {'vectorSearchConfiguration': {
        'numberOfResults': 40, 'overrideSearchType': 'HYBRID',
        'filter': {'andAll': [ {'equals': {'key': key, 'value': value}} for key, value in {
            'tenant_id': 'tenant-demo', 'access_scope': 'finance-analysts-demo',
            'snapshot_id': 'catalog-demo-42', 'publication_status': 'approved',
            'object_type': 'metric',
        }.items()]},
    }},
}
rerank = {'rerankingConfiguration': {
    'type': 'BEDROCK_RERANKING_MODEL', 'bedrockRerankingConfiguration': {
        'modelConfiguration': {'modelArn': 'YOUR_SUPPORTED_RERANKER_MODEL_ARN'},
        'numberOfRerankedResults': 20,
    },
}}
replacements = {
    'TABLE': card('tables/orders.md'), 'METRIC': card('metrics/revenue-v7.md'),
    'COLUMN': card('columns/region_name.md'), 'RULE': card('rules/exclude-internal-customers-v2.md'),
    'JOIN': card('relationships/orders-customers-v2.md'),
    'METADATA': block('json', json.dumps(metric_meta, indent=2)),
    'NONE': config('cards-none'), 'FIXED': config('prose-fixed'),
    'SEMANTIC': config('prose-semantic-experiment'),
    'HIERARCHICAL': config('prose-hierarchical-experiment'),
    'REQUEST': block('json', json.dumps(request, indent=2)),
    'RERANK': block('json', json.dumps(rerank, indent=2)),
}
for key, value in replacements.items():
    guide = guide.replace('{{' + key + '}}', value)
(root / 'IMPLEMENTATION_GUIDE.md').write_text(guide)
(root / 'examples/retrieve-request.json').write_text(json.dumps(request, indent=2) + '\n')
(root / 'README.md').write_text('''# Bedrock Text-to-SQL RAG starter

Read IMPLEMENTATION_GUIDE.md first. All business records are fictional.

- s3/: 15 source Markdown documents and 15 paired Bedrock metadata sidecars.
- examples/ingest.py: explicit upload, source creation, sync and inspection helpers.
- examples/retrieve.py: filtered hybrid retrieval, optional reranking and validation.
- examples/chunking/: four ingestion configurations; use NONE and FIXED_SIZE first.
- examples/retrieve-request.json: a concrete illustrative Bedrock request.
- manifest.json: expected source identities and hashes; do not upload as prose.

Use a current compatible boto3 in your configured AWS environment. The examples
do not run AWS operations automatically. Replace all deployment placeholders and
implement the authoritative authorization/version callback before live use.
No Knowledge Base, vector store, SQL database or enterprise registry is deployed
by this bundle. Markdown citations in the guide link to the verified AWS docs.
''')
print(f'Wrote implementation guide: {len(guide.split())} whitespace-separated words.')
