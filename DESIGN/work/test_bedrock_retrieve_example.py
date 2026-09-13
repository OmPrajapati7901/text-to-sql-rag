"""Offline checks only: no AWS credentials, SDK client or network calls."""
import copy
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "retrieve_example", ROOT / "outputs/bedrock-text-to-sql-rag/examples/retrieve.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

PARAMETERS = dict(
    knowledge_base_id="ABCDEF1234", question="Revenue by customer region",
    tenant_id="tenant-demo", access_scope="finance-readers-v3",
    snapshot_id="catalog-demo-42",
)
RESPONSE = {"retrievalResults": [{
    "metadata": {
        "tenant_id": "tenant-demo", "access_scope": "finance-readers-v3",
        "snapshot_id": "catalog-demo-42", "publication_status": "approved",
        "object_id": "finance.revenue", "object_type": "metric",
        "source_version": "7", "canonical_ref": "registry:finance.revenue@7",
        "irrelevant_private_metadata": "must-not-be-released",
    },
    "content": {"type": "TEXT", "text": "Approved Revenue discovery card"},
    "location": {"type": "S3", "s3Location": {"uri": "s3://example/revenue.txt"}},
    "score": 0.5,
}], "nextToken": "test-next-page"}


class FakeClient:
    def __init__(self, response=None):
        self.response = copy.deepcopy(RESPONSE if response is None else response)
        self.requests = []

    def retrieve(self, **request):
        self.requests.append(request)
        return copy.deepcopy(self.response)


class ExampleChecks(unittest.TestCase):
    def test_empty_required_values_are_rejected(self):
        for key in PARAMETERS:
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    MODULE.build_retrieve_request(**(PARAMETERS | {key: " "}))

    def test_object_searches_preserve_mandatory_scope(self):
        for kind in ("metric", "table", "column", "relationship"):
            request = MODULE.build_retrieve_request(**PARAMETERS, object_type=kind)
            config = request["retrievalConfiguration"]["vectorSearchConfiguration"]
            self.assertEqual(config["numberOfResults"], 40)
            self.assertEqual(config["overrideSearchType"], "HYBRID")
            conditions = {f["equals"]["key"]: f["equals"]["value"]
                          for f in config["filter"]["andAll"]}
            self.assertEqual(conditions, {
                "tenant_id": "tenant-demo", "access_scope": "finance-readers-v3",
                "snapshot_id": "catalog-demo-42", "publication_status": "approved",
                "object_type": kind,
            })

    def test_optional_reranking_and_pagination(self):
        request = MODULE.build_retrieve_request(
            **PARAMETERS,
            reranker_model_arn="arn:aws:bedrock:us-east-1::foundation-model/example",
            next_token="next-page",
        )
        config = request["retrievalConfiguration"]["vectorSearchConfiguration"]
        rerank = config["rerankingConfiguration"]
        self.assertEqual(rerank["type"], "BEDROCK_RERANKING_MODEL")
        self.assertEqual(rerank["bedrockRerankingConfiguration"]
                         ["numberOfRerankedResults"], 20)
        self.assertEqual(request["nextToken"], "next-page")

    def test_source_scope_mismatch_stops_release(self):
        response = copy.deepcopy(RESPONSE)
        response["retrievalResults"][0]["metadata"]["tenant_id"] = "other-tenant"
        with self.assertRaises(PermissionError):
            MODULE.retrieve(FakeClient(response), authorize_reference=lambda _: True,
                            **PARAMETERS)

    def test_current_policy_denial_stops_release(self):
        with self.assertRaises(PermissionError):
            MODULE.retrieve(FakeClient(), authorize_reference=lambda _: False,
                            **PARAMETERS)

    def test_missing_canonical_reference_stops_release(self):
        response = copy.deepcopy(RESPONSE)
        del response["retrievalResults"][0]["metadata"]["canonical_ref"]
        with self.assertRaises(ValueError):
            MODULE.retrieve(FakeClient(response), authorize_reference=lambda _: True,
                            **PARAMETERS)

    def test_authorized_results_preserve_provenance_and_page_state(self):
        references = []

        def authorize(reference):
            references.append(reference)
            return True

        response = MODULE.retrieve(FakeClient(), authorize_reference=authorize,
                                   **PARAMETERS)
        hit = response["results"][0]
        self.assertEqual(references[0]["object_id"], "finance.revenue")
        self.assertEqual(hit["location"]["s3_uri"], "s3://example/revenue.txt")
        self.assertEqual(response["next_token"], "test-next-page")
        self.assertNotIn("irrelevant_private_metadata", hit["metadata"])


if __name__ == "__main__":
    unittest.main()
