# PgGatherApp

A local web application for collecting, storing, and analyzing [pg_gather](https://github.com/jobinau/pg_gather) PostgreSQL diagnostic reports. Connects directly to your PostgreSQL databases, runs the collection, generates reports, and presents the analysis as a modern native dashboard with AI-powered recommendations.

## What it does

- **Collect** diagnostics from any PostgreSQL 10-18 database via saved connection profiles
- **Generate** HTML reports using the pg_gather engine (vendored SQL, Docker-based)
- **Store** reports in a searchable, taggable library with notes
- **Analyze** with a native dashboard: 16 detail sections, 140+ diagnostic checks, per-cell hover tooltips with actionable suggestions
- **Compare** two snapshots of the same server to see configuration drift and metric changes
- **Monitor** continuously with lightweight collection (sessions, wait events, connections) and timeline charts
- **Recommend** parameter tuning based on your hardware specs (CPU, RAM, storage type, workload)
- **Integrate with AI** via built-in MCP server — connect Claude Desktop or any MCP client for natural language analysis

## Screenshots

The app presents a modern light-themed dashboard:

- **Library**: Card grid with summary stats, search, tag filtering
- **Report detail**: Health metric cards, findings with severity, sessions stacked bar, collapsible sections for tables/indexes/statements/wait events/HBA/replication/checkpoints
- **Compare**: Side-by-side parameter diff with metric changes
- **Timeline**: Interactive Chart.js graphs showing session/wait event/connection trends

## Requirements

- **Docker Desktop** running (provides the `postgres:17` engine container; pulled automatically on first use)
- **Python 3.10+** (3.12 recommended)

## Quick start

```bash
git clone <repo-url> PgGatherApp
cd PgGatherApp
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open http://127.0.0.1:8000

## Usage

### 1. Add a connection

Go to **Connections** and click **New connection**. Enter:

| Field | Description |
|-------|-------------|
| Name | A friendly label (e.g., "prod-primary") |
| Host | Database hostname or IP. `localhost` is auto-rewritten to `host.docker.internal` for Docker connectivity |
| Port | Default 5432 |
| Database | The database name to collect from |
| Username | A PostgreSQL user with `pg_monitor` role or superuser privileges |
| Password | Optional. Encrypted at rest using Fernet symmetric encryption (`storage/secret.key`) |
| SSL Mode | Default `prefer` |

Click **Test** (play button) to verify connectivity before collecting.

### 2. Collect a report

Click the **download button** on any connection card. This:

1. Runs `gather.sql` against your database via the Docker container's psql (~20-30 seconds)
2. Imports the TSV into the engine container's PostgreSQL
3. Generates the HTML report
4. Extracts structured data (sessions, tables, indexes, statements, wait events, parameters, HBA rules, replication, checkpoints, IO stats)
5. Stores everything in the library

The detail page polls automatically until done.

### 3. Upload a TSV

Already collected an `out.tsv` or `.tsv.gz` elsewhere? Go to **Upload TSV** and drag-and-drop the file. The app detects gzip by magic bytes and generates the report.

### 4. Browse reports

The **Library** page shows:
- Summary stats strip (total reports, servers tracked, latest collection)
- Floating search/filter toolbar (text search, status filter, tag chips)
- Card grid with PG version badges, status pills, relative timestamps

Click any card to open the native dashboard.

### 5. Report dashboard

Each report opens as a multi-section dashboard with:

| Section | What it shows |
|---------|---------------|
| **Health metric cards** | Connections, tables, indexes, total size, WAL rate — color-coded (green/amber/red) |
| **Findings** | 140+ diagnostic checks ported from pg_gather's JavaScript analysis. Each finding has severity, description, and links to pg_gather docs |
| **Server Info** | PG version, uptime, WAL position, timeline, binary directory |
| **Sessions** | Stacked bar (active/idle/idle-in-txn) + detailed session table with wait events, queries, blocker detection |
| **Database Overview** | Role, schemas, partitioned tables, unlogged tables, stats age |
| **Databases** | Per-database: size, cache hit, age, commits/day, rollbacks, temp files |
| **Connections by DB** | Active/idle/total/SSL/non-SSL per database |
| **Tables** | Bloat %, dead tuples, cache hit, age, vacuum status, indexes, PK — with per-row tooltips showing OID, DML rates/day, tablespace, FILLFACTOR recommendations |
| **Partitioned Tables** | Partition count, total size, prune effectiveness |
| **Indexes** | Scans, size, cache hit, unused/invalid flags |
| **Top Statements** | DB time %, calls, avg execution time, cache hit, block I/O — click to expand full query, double-click to copy |
| **Wait Events** | Category stacked bar + per-event breakdown with CPU core estimate |
| **Checkpoints & BGWriter** | Forced %, interval, sync time, buffer cleaning ratios — every cell has a tooltip |
| **Replication** | Lag bytes, slot status, hot_standby_feedback |
| **IO Statistics** | Reads/writes/hits/evictions/fsyncs per backend type |
| **HBA Rules** | Type, DB, user, address, method — with shadowed rule detection |
| **Extensions** | Risky extension flagging |
| **Roles** | Superuser count, auth method (MD5 flagged), connection breakdown |
| **Parameter Recommendations** | Interactive calculator: input CPU/RAM/storage/workload/filesystem, get tuned parameter suggestions with copy button |
| **Configuration** | All parameters grouped by category, searchable, overrides flagged amber |

**Hover tooltips**: Every warning cell explains what's wrong and what to do. Table names show full detail (OID, schema, DML/day, tablespace, recommendations) on hover.

**Double-click to copy**: Table names and query cells copy their detail text to clipboard.

**Column sorting**: Click any table header to sort asc/desc.

### 6. Compare snapshots

Go to **Compare** to see all servers with 2+ reports. Select two snapshots to see:
- **Server Metrics** diff: sessions, tables, indexes, connections, size, WAL rate
- **Parameter Changes**: full diff with changed/added/removed counts

Reports are matched by PostgreSQL `system_identifier` — the same cluster produces the same key regardless of host/port/failover.

### 7. Continuous monitoring

Enable lightweight monitoring on any connection:

1. Go to **Connections** and click the **timer button** (&#9201;) on a connection card
2. The scheduler collects dynamic metrics every 60 seconds via `template1` (partial mode)
3. Go to **Timeline** to see interactive charts:
   - **Sessions**: active/idle/idle-in-txn/total over time
   - **Wait Events**: top 10 events over time
   - **Connections**: total/SSL/non-SSL over time
4. Select time ranges: 1h, 6h, 24h, 7d, 30d

The timer button turns green when active. Click again to pause.

### 8. AI integration (MCP)

PgGatherApp includes a built-in [MCP](https://modelcontextprotocol.io/) server that exposes report data to AI assistants.

#### Setup with Claude Desktop

Add to `~/.claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "pggather": {
      "type": "streamable-http",
      "url": "http://localhost:8000/mcp/mcp"
    }
  }
}
```

#### What the AI can access

**Resources** (read-only data):

| URI | Description |
|-----|-------------|
| `pggather://servers` | All servers with report counts |
| `pggather://report/{id}/summary` | Key metrics and server info |
| `pggather://report/{id}/findings` | Diagnostic findings with severity |
| `pggather://report/{id}/params` | All PostgreSQL parameters |
| `pggather://report/{id}/tables` | Table health data |
| `pggather://report/{id}/indexes` | Index health data |
| `pggather://report/{id}/sessions` | Session details |
| `pggather://report/{id}/statements` | Top SQL statements |
| `pggather://report/{id}/bgwriter` | Checkpoint stats |
| `pggather://report/{id}/recommendations` | Tuning suggestions |
| `pggather://timeline/{server_key}` | Monitoring time-series |

**Tools** (actions):

| Tool | Description |
|------|-------------|
| `list_reports` | Find reports by server or status |
| `get_report_summary` | Complete diagnostic summary |
| `get_findings` | Findings grouped by severity |
| `get_parameter_recommendations` | Hardware-aware tuning with CPU/RAM/storage inputs |
| `compare_reports` | Diff two snapshots |

#### Pair with postgres-mcp for live analysis

For the most powerful setup, add [postgres-mcp](https://github.com/crystaldba/postgres-mcp) alongside:

```json
{
  "mcpServers": {
    "pggather": {
      "type": "streamable-http",
      "url": "http://localhost:8000/mcp/mcp"
    },
    "postgres": {
      "command": "uvx",
      "args": ["postgres-mcp", "--access-mode=restricted"],
      "env": {"DATABASE_URI": "postgresql://user:pass@host:5432/dbname"}
    }
  }
}
```

Now the AI has both the **pg_gather snapshot analysis** and **live database access** — it can cross-reference historical findings with current state and give expert recommendations.

Example prompts:
- "What are the top issues in my latest PostgreSQL report?"
- "Compare the last two snapshots and explain what got worse"
- "Recommend parameter changes for my 16-core, 64GB RAM server running OLTP"
- "Which tables need the most urgent attention?"

## Architecture

```
app/
  main.py              # FastAPI app, lifespan (worker + scheduler + MCP)
  config.py            # pydantic-settings: paths, Docker config, timeouts
  db.py                # SQLite schema + connection helper
  repository.py        # CRUD for reports, connections, tags, schedules, history
  jobs.py              # Background worker (collect -> generate -> extract)
  continuous.py        # Scheduled lightweight collection via template1
  crypto.py            # Fernet encrypt/decrypt for connection passwords
  storage.py           # Filesystem blob storage (TSV, HTML, logs per report)
  report_view.py       # Maps extracted JSON -> template-friendly view models
  param_recommend.py   # Parameter recommendation engine (~30 params)
  mcp_server.py        # MCP server (resources + tools for AI clients)
  templating.py        # Jinja2 env + custom filters
  pipeline/
    docker_runner.py   # Docker container lifecycle + collect + generate + extract
    extract.py         # TSV header + report HTML metadata parsing
    health.py          # Docker health check
  routers/             # FastAPI route handlers
    reports.py         # Library, detail, upload, download, tags, notes, recommend
    connections.py     # Connection CRUD, test, collect, schedule
    compare.py         # Compare picker + diff view
    timeline.py        # Timeline charts (continuous collection history)
    serve.py           # Serve stored HTML with sandbox CSP
    health.py          # /healthz, /health/docker
  templates/           # Jinja2 HTML templates
  static/css/app.css   # Design system (light theme, 350 lines)
  static/js/app.js     # Client JS (poll, sort, toggle, copy, recommend, dropzone)
vendor/pg_gather/      # Vendored gather.sql, gather_schema.sql, gather_report.sql
tests/                 # 47 tests (unit + route + docker integration)
```

### Data flow

```
Connection profile ──collect (gather.sql via container psql)──> raw.tsv
                                                                  |
                                                  generate (postgres:17 engine)
                                                                  |
                                                           report.html
                                                                  |
                                               extract (detail_json, report_json,
                                                        params_json, meta_json)
                                                                  |
                                                       SQLite (app.db) + storage/
```

### Security

- Passwords encrypted at rest (Fernet, `storage/secret.key`, chmod 600)
- Passwords decrypted only in-memory at collection time
- Passwords scrubbed from job logs and error tracebacks
- Passwords never returned in API responses
- Stored HTML served with `Content-Security-Policy: sandbox allow-scripts`
- Report iframe uses `sandbox="allow-scripts"` (no `allow-same-origin`)

## Tests

```bash
# Unit + route tests (no Docker needed)
.venv/bin/python -m pytest -q

# With Docker integration tests (needs pg_src container)
.venv/bin/python -m pytest -m docker -v
```

**47 tests** covering:
- TSV/HTML metadata extraction
- Encrypt/decrypt round-trips
- Repository CRUD, search, tag filtering, server_key matching
- Report view model mappings (metric cards, findings, detail sections)
- Formatter functions
- All HTTP routes (library, detail, upload, connections, compare, recommendations)
- CSP headers, Content-Disposition, password not exposed
- Docker integration (opt-in): real collect, >100KB HTML, detail extraction

## Updating the vendored engine

```bash
cp /path/to/pg_gather/{gather,gather_schema,gather_report}.sql vendor/pg_gather/
# Update vendor/pg_gather/VERSION with new engine_ver and commit hash
```

Reports are only safely comparable within the same engine version.

## Configuration

Environment variables (prefix `PGGATHER_`):

| Variable | Default | Description |
|----------|---------|-------------|
| `PGGATHER_DATA_DIR` | `storage/` | Blob storage directory |
| `PGGATHER_DB_PATH` | `app.db` | SQLite database path |
| `PGGATHER_DOCKER_IMAGE` | `postgres:17` | Engine container image |
| `PGGATHER_CONTAINER_NAME` | `pg_gather` | Engine container name |
| `PGGATHER_COLLECT_TIMEOUT` | `300` | Collection timeout (seconds) |
| `PGGATHER_GENERATE_TIMEOUT` | `600` | Generation timeout (seconds) |

Or use a `.env` file (auto-loaded by pydantic-settings).

## License

See LICENSE file.
