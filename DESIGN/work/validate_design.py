from pathlib import Path
import re
import json
import ast
import sqlite3

p = Path('outputs/enterprise-text-to-sql-architecture.md')
s = p.read_text()
heads = re.findall(r'^## (\d+)\. (.+)$', s, re.M)
assert [int(x[0]) for x in heads] == list(range(1, 44))
blocks = re.findall(r'^```([^\n]*)\n(.*?)^```\s*$', s, re.M | re.S)
for lang, body in blocks:
    if lang == 'json':
        json.loads(body)
    elif lang == 'python':
        ast.parse(body)

example = s[s.index('## 40.'):s.index('## 41.')]
queries = re.findall(r'^```sql\n(.*?)^```', example, re.M | re.S)
assert len(queries) == 3
conn = sqlite3.connect(':memory:')
for schema in ['sales', 'crm', 'support']:
    conn.execute(f"ATTACH DATABASE ':memory:' AS {schema}")
conn.execute('CREATE TABLE sales.orders(tenant_id TEXT,order_id TEXT,customer_id TEXT,ordered_at TEXT,order_amount_usd NUMERIC,order_status TEXT,is_test BOOLEAN,refund_status TEXT)')
conn.execute('CREATE TABLE crm.customer_history(tenant_id TEXT,customer_id TEXT,valid_from TEXT,valid_to TEXT,region TEXT,is_internal BOOLEAN,is_test_customer BOOLEAN)')
conn.execute('CREATE TABLE support.tickets(tenant_id TEXT,ticket_id TEXT,customer_id TEXT,opened_at TEXT,is_test BOOLEAN)')
history = [
    ('T1','C1','2020-01-01T00:00:00Z','2026-05-01T00:00:00Z','North',0,0),
    ('T1','C1','2026-05-01T00:00:00Z',None,'South',0,0),
    ('T1','C2','2020-01-01T00:00:00Z',None,'North',1,0),
    ('T1','C3','2020-01-01T00:00:00Z',None,'North',0,0),
    ('T1','C4','2020-01-01T00:00:00Z',None,'North',0,0),
    ('T2','C1','2020-01-01T00:00:00Z',None,'OtherTenant',0,0),
]
conn.executemany('INSERT INTO crm.customer_history VALUES(?,?,?,?,?,?,?)', history)
orders = [
    ('T1','O1','C1','2026-04-10T00:00:00Z',100,'completed',0,'none'),
    ('T1','O2','C1','2026-06-10T00:00:00Z',50,'completed',0,'none'),
    ('T1','O3','C2','2026-04-12T00:00:00Z',200,'completed',0,'none'),
    ('T1','O4','C3','2026-04-12T00:00:00Z',300,'completed',1,'none'),
    ('T1','O5','C3','2026-04-12T00:00:00Z',400,'completed',0,'fully_refunded'),
    ('T1','O6','C4','2026-04-12T00:00:00Z',100,'completed',0,'none'),
    ('T2','O1','C1','2026-04-10T00:00:00Z',900,'completed',0,'none'),
]
conn.executemany('INSERT INTO sales.orders VALUES(?,?,?,?,?,?,?,?)', orders)
conn.executemany('INSERT INTO support.tickets VALUES(?,?,?,?,?)', [
    ('T1','S1','C1','2026-04-10T00:00:00Z',0),
    ('T1','S2','C1','2026-06-10T00:00:00Z',0),
    ('T1','S3','C4','2026-04-10T00:00:00Z',0),
    ('T2','S1','C1','2026-04-10T00:00:00Z',0),
])
params = {'1':'T1','2':'2026-04-01T00:00:00Z','3':'2026-07-01T00:00:00Z',
          '4':'completed','5':'fully_refunded'}
q_a = queries[0].replace("date_trunc('month', o.ordered_at AT TIME ZONE 'UTC')::date",
                         "substr(o.ordered_at, 1, 7) || '-01'")
result_a = conn.execute(q_a, params).fetchall()
assert result_a == [('2026-04-01','North',200), ('2026-06-01','South',50)], result_a
result_b = conn.execute(queries[1], params).fetchall()
assert result_b == [('C1',150,2), ('C4',100,1)], result_b

# The data-quality prerequisite must detect an overlapping customer version.
coverage_check = '''
SELECT o.tenant_id, o.order_id, COUNT(c.customer_id) AS match_count
FROM sales.orders o LEFT JOIN crm.customer_history c
 ON o.tenant_id=c.tenant_id AND o.customer_id=c.customer_id
 AND o.ordered_at>=c.valid_from
 AND (o.ordered_at<c.valid_to OR c.valid_to IS NULL)
WHERE o.tenant_id='T1'
GROUP BY o.tenant_id,o.order_id HAVING COUNT(c.customer_id)<>1
'''
assert conn.execute(coverage_check).fetchall() == []
conn.execute("INSERT INTO crm.customer_history VALUES ('T1','C1','2026-04-01T00:00:00Z','2026-04-30T00:00:00Z','Wrong',0,0)")
assert conn.execute(coverage_check).fetchall() == [('T1','O1',2)]
conn.execute("DELETE FROM crm.customer_history WHERE region='Wrong'")

conn.execute('DELETE FROM sales.orders')
cohort_orders = [
    ('T1','J1','C1','2026-01-10T00:00:00Z',100,'completed',0,'none'),
    ('T1','J2','C1','2026-03-15T00:00:00Z',100,'completed',0,'none'),
    ('T1','J3','C4','2026-01-20T00:00:00Z',100,'completed',0,'none'),
    ('T1','J4','C3','2025-12-20T00:00:00Z',100,'completed',0,'none'),
    ('T1','J5','C3','2026-01-20T00:00:00Z',100,'completed',0,'none'),
    ('T1','J6','C2','2026-01-10T00:00:00Z',100,'completed',0,'none'),
]
conn.executemany('INSERT INTO sales.orders VALUES(?,?,?,?,?,?,?,?)', cohort_orders)
cohort_params = {'1':'T1','2':'completed','3':'fully_refunded',
                 '4':'2026-01-01T00:00:00Z','5':'2026-02-01T00:00:00Z',
                 '6':'2026-03-01T00:00:00Z','7':'2026-04-01T00:00:00Z'}
result_c = conn.execute(queries[2], cohort_params).fetchall()
assert result_c == [(2,1,50.0)], result_c
zero_cohort = dict(cohort_params, **{'4':'2027-01-01T00:00:00Z','5':'2027-02-01T00:00:00Z'})
assert conn.execute(queries[2], zero_cohort).fetchall() == [(0,0,None)]

report = {
    'sections': len(heads),
    'json_blocks_valid': sum(l=='json' for l,b in blocks),
    'python_syntax_valid': sum(l=='python' for l,b in blocks),
    'mermaid_blocks': sum(l=='mermaid' for l,b in blocks),
    'word_count': len(s.split()),
    'fixture_A': result_a, 'fixture_B': result_b, 'fixture_C': result_c,
    'adversarial_checks': ['SCD overlap detected', 'empty cohort returns null percentage'],
    'limits': 'Synthetic fixtures evaluated with SQLite. A uses equivalent month extraction for UTC ISO timestamps. This does not certify PostgreSQL dialect, RLS, execution plans, or production adapters.'
}
Path('work/validation-report.json').write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
