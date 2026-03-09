# OasisAgent Grafana Dashboards

Grafana dashboard templates that query the InfluxDB audit bucket.

## Prerequisites

- **Grafana** 10.0+ with the InfluxDB data source plugin
- **InfluxDB 2.x** with the `oasisagent` bucket populated by the audit writer
- Data source configured in Grafana using **Flux** query language (not InfluxQL)

## Setup

### 1. Configure the InfluxDB data source in Grafana

1. Go to **Connections > Data Sources > Add data source > InfluxDB**
2. Set **Query Language** to **Flux**
3. Set the **URL** to your InfluxDB instance (e.g., `http://localhost:8086`)
4. Under **InfluxDB Details**, set:
   - **Organization**: your InfluxDB org (default: `myorg`)
   - **Token**: your InfluxDB API token
   - **Default Bucket**: `oasisagent`
5. Click **Save & Test**

### 2. Import the dashboard

1. Go to **Dashboards > New > Import**
2. Click **Upload dashboard JSON file**
3. Select `oasisagent-overview.json`
4. Select your InfluxDB data source in the **DS_INFLUXDB** dropdown
5. Click **Import**

## Dashboard variables

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `DS_INFLUXDB` | Data source | (auto) | InfluxDB data source to query |
| `bucket` | Text | `oasisagent` | InfluxDB bucket name |

Both variables appear at the top of the dashboard and can be changed at runtime.

## Panels

### Row 1: Events

- **Event Volume by Source** — Stacked bar time series of ingested events grouped by source adapter (mqtt, ha_websocket, ha_log_poller). Shows event throughput over time.
- **Events by Severity** — Donut chart showing the distribution of events across severity levels (info, warning, error, critical). Color-coded: blue/yellow/orange/red.

### Row 2: Decisions

- **Decision Tier Distribution** — Horizontal bar chart showing how many decisions were resolved at each tier: T0 (known fix lookup), T1 (local SLM triage), T2 (cloud reasoning).
- **Disposition Breakdown** — Horizontal bar chart of decision outcomes: matched, dropped, escalated, blocked, correlated, dry_run, unmatched.

### Row 3: Actions

- **Action Results Over Time** — Stacked bar time series of handler action outcomes (success/failure/skipped/dry_run). Color-coded: green/red/blue/purple.
- **Action Duration by Handler** — Line chart of mean action execution time (ms) per handler (homeassistant, docker). Useful for spotting slow handler operations.

### Row 4: Infrastructure Health

- **Circuit Breaker Trips** — State timeline showing circuit breaker trip events by entity. Each entity gets a row; trip reasons are displayed as state values. Note: this panel requires `audit.write_circuit_breaker()` to be called from the orchestrator (not yet wired).
- **Verification Results** — Stat panel showing the verification pass rate as a percentage. Thresholds: red (<50%), yellow (50-80%), green (>80%).

## Panels not yet included

The following panels from the architecture spec (ARCHITECTURE.md section 16.8) depend on data not yet written to InfluxDB:

- **LLM token usage and cost** — requires `tokens_used` and `cost_estimate` fields in `oasis_decision`
- **Pending approval queue depth** — queue is in-memory + MQTT, not persisted to InfluxDB
- **Mean event processing latency** — requires a latency field in audit measurements

These panels will be added when the underlying data becomes available.

## Customization

### Grafana provisioning

To auto-provision the dashboard, add a provisioning config:

```yaml
# /etc/grafana/provisioning/dashboards/oasisagent.yaml
apiVersion: 1
providers:
  - name: OasisAgent
    folder: OasisAgent
    type: file
    options:
      path: /path/to/dashboards
      foldersFromFilesStructure: false
```

### Time range and refresh

The dashboard defaults to a 6-hour window with 30-second auto-refresh. Both are adjustable via Grafana's standard time picker controls.
