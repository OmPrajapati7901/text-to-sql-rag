"""Small Bedrock Knowledge Bases retrieval example; importing it makes no AWS calls.

Use with a customer-managed Knowledge Base backed by OpenSearch Serverless and
a filterable text field. Install a current boto3 release in your application.

Only trusted application code may choose the KB, tenant, access_scope, snapshot,
object_type and reranker ARN. Do not expose these arguments as an LLM tool or
accept them from unvalidated HTTP input. access_scope equality is an illustrative
precomputed entitlement segment, not a general authorization/ABAC implementation.
The policy service must establish that every matching document is currently
authorized before retrieval, especially before built-in reranking sees content.
If indexed access labels cannot provide that guarantee, disable built-in reranking
and use an independently secured retrieval path before reranking authorized hits.

Returned text, metadata and locations remain untrusted data. Re-fetch exact
definitions from canonical_ref, check versions, and expand dependencies through
authoritative services before SQL planning. This module does not log responses.

API references:
https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_Retrieve.html
https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_KnowledgeBaseVectorSearchConfiguration.html
https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent-runtime_VectorSearchBedrockRerankingConfiguration.html
"""

from collections.abc import Callable


def _required_string(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def make_client(region_name):
    """Explicitly create a boto3 client using your application's AWS credentials."""
    import boto3

    return boto3.client(
        "bedrock-agent-runtime",
        region_name=_required_string(region_name, "region_name"),
    )


def build_retrieve_request(
    *, knowledge_base_id, question, tenant_id, access_scope, snapshot_id,
    object_type=None, reranker_model_arn=None, next_token=None,
):
    """Construct an authorized HYBRID request; do not infer policy from question."""
    knowledge_base_id = _required_string(knowledge_base_id, "knowledge_base_id")
    question = _required_string(question, "question")
    values = {
        "tenant_id": _required_string(tenant_id, "tenant_id"),
        "access_scope": _required_string(access_scope, "access_scope"),
        "snapshot_id": _required_string(snapshot_id, "snapshot_id"),
        "publication_status": "approved",
    }
    if object_type is not None:
        values["object_type"] = _required_string(object_type, "object_type")

    vector_search = {
        "numberOfResults": 40,
        "overrideSearchType": "HYBRID",
        "filter": {"andAll": [
            {"equals": {"key": key, "value": value}}
            for key, value in values.items()
        ]},
    }
    if reranker_model_arn is not None:
        vector_search["rerankingConfiguration"] = {
            "type": "BEDROCK_RERANKING_MODEL",
            "bedrockRerankingConfiguration": {
                "modelConfiguration": {"modelArn": _required_string(
                    reranker_model_arn, "reranker_model_arn"
                )},
                "numberOfRerankedResults": 20,
                "metadataConfiguration": {
                    "selectionMode": "SELECTIVE",
                    "selectiveModeConfiguration": {
                        "fieldsToInclude": [
                            {"fieldName": "object_id"},
                            {"fieldName": "object_type"},
                        ]
                    },
                },
            },
        }
    request = {
        "knowledgeBaseId": knowledge_base_id,
        "retrievalQuery": {"text": question},
        "retrievalConfiguration": {"vectorSearchConfiguration": vector_search},
    }
    if next_token is not None:
        request["nextToken"] = _required_string(next_token, "next_token")
    return request


def retrieve(client, *, authorize_reference: Callable[[dict], bool], **parameters):
    """Retrieve one page and reauthorize identifiers before exposing result text.

    authorize_reference must consult current authoritative policy/version data,
    using object_id, canonical_ref and source_version, and return exactly True to
    permit disclosure. An unavailable policy service must raise or return False.
    A post-retrieval check does not undo prior exposure to a built-in reranker.
    Callers must handle service exceptions without broadening filters on retry.
    A nonempty next_token signals a partial page, not absent evidence.
    """
    if not callable(authorize_reference):
        raise ValueError("A current-policy authorization callback is required")
    request = build_retrieve_request(**parameters)
    response = client.retrieve(**request)
    if response.get("guardrailAction") == "INTERVENED":
        raise RuntimeError("Retrieval was interrupted by a guardrail")

    search_config = request["retrievalConfiguration"]["vectorSearchConfiguration"]
    expected = {
        item["equals"]["key"]: item["equals"]["value"]
        for item in search_config["filter"]["andAll"]
    }
    results = []
    for hit in response.get("retrievalResults", []):
        metadata = hit.get("metadata", {})
        if any(metadata.get(key) != value for key, value in expected.items()):
            raise PermissionError("Retrieved metadata did not match required scope")

        reference = {key: _required_string(metadata.get(key), key) for key in (
            "object_id", "object_type", "source_version", "snapshot_id",
            "canonical_ref",
        )}
        if authorize_reference(dict(reference)) is not True:
            raise PermissionError("Current policy or version check denied a result")

        content = hit.get("content", {})
        if content.get("type") not in (None, "TEXT") or not isinstance(
            content.get("text"), str
        ):
            raise ValueError("This example expects text-only source cards")
        location = hit.get("location", {})
        if location.get("type") != "S3":
            raise ValueError("This example expects S3 document locations")
        source_uri = _required_string(location.get("s3Location", {}).get("uri"),
                                      "s3_uri")
        results.append({
            "metadata": reference,
            "text": content["text"],
            "location": {"type": "S3", "s3_uri": source_uri},
            "score": hit.get("score"),
        })
    return {"results": results, "next_token": response.get("nextToken")}
