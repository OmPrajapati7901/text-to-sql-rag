# Bedrock Text-to-SQL RAG starter

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
