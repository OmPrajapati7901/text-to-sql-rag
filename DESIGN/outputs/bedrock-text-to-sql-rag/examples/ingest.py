"""Explicit-call ingestion helpers. Importing this module makes no AWS calls.

Install boto3 in your application environment and pass authenticated clients.
These helpers do not provision a Knowledge Base, IAM policies or a vector store.
"""
from pathlib import Path


def upload_sources(s3_client, bucket_name, local_s3_root):
    """Upload the sample s3/ tree without flattening content/sidecar paths."""
    root = Path(local_s3_root)
    files = sorted(root.rglob('*'))
    files = [p for p in files if p.is_file()]
    if not bucket_name or not files:
        raise ValueError('A bucket and nonempty source directory are required')
    for path in files:
        if path.suffix != '.md' and not path.name.endswith('.md.metadata.json'):
            raise ValueError(f'Unexpected source file: {path.name}')
    for path in files:
        s3_client.upload_file(str(path), bucket_name, path.relative_to(root).as_posix())
    return len(files)


def create_source(client, *, kb_id, bucket_arn, name, prefix,
                  chunking_configuration, client_token):
    """Call once per source. Persist the token and returned source ID."""
    return client.create_data_source(
        knowledgeBaseId=kb_id,
        name=name,
        clientToken=client_token,
        dataDeletionPolicy='DELETE',
        dataSourceConfiguration={
            'type': 'S3',
            's3Configuration': {
                'bucketArn': bucket_arn,
                'inclusionPrefixes': [prefix],
            },
        },
        vectorIngestionConfiguration={
            'chunkingConfiguration': chunking_configuration,
        },
    )['dataSource']['dataSourceId']


def start_sync(client, *, kb_id, source_id, client_token):
    """A new sync needs a new token; an uncertain retry reuses its token."""
    return client.start_ingestion_job(
        knowledgeBaseId=kb_id,
        dataSourceId=source_id,
        clientToken=client_token,
        description='Ingest reviewed catalog snapshot',
    )['ingestionJob']['ingestionJobId']


def inspect_sync(client, *, kb_id, source_id, job_id):
    """Return diagnostic status; COMPLETE is not a publication decision."""
    job = client.get_ingestion_job(
        knowledgeBaseId=kb_id, dataSourceId=source_id, ingestionJobId=job_id,
    )['ingestionJob']
    status = job['status']
    known = {'STARTING', 'IN_PROGRESS', 'STOPPING', 'COMPLETE', 'FAILED', 'STOPPED'}
    if status not in known:
        raise RuntimeError('Unknown ingestion status; inspect before continuing')
    statistics = job.get('statistics', {})
    return {
        'status': status,
        'terminal': status in {'COMPLETE', 'FAILED', 'STOPPED'},
        'failed_documents': statistics.get('numberOfDocumentsFailed'),
        'skipped_documents': statistics.get('numberOfDocumentsSkipped'),
        'statistics': statistics,
        'failure_reasons': job.get('failureReasons', []),
    }
