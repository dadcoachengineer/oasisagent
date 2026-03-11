# Infrastructure Service API Research

Research date: 2026-03-10

This document catalogs the APIs, monitoring capabilities, actionable operations, and failure modes for six infrastructure services targeted for OasisAgent integration.

---

## 1. Proxmox VE (PVE)

### API Type and Authentication

- **Type:** REST API over HTTPS (port 8006)
- **Base URL:** `https://<host>:8006/api2/json/`
- **Auth methods:**
  - **API Tokens** (preferred for automation): Set `Authorization: PVEAPIToken=USER@REALM!TOKENID=UUID` header. Tokens can have separate permissions and expiration from the parent user.
  - **Ticket-based:** POST to `/api2/json/access/ticket` with username/password. Returns a ticket (cookie) + CSRF token. Tickets expire daily. Write operations (POST/PUT/DELETE) require the CSRF token in the `CSRFPreventionToken` header.
- **Python client:** `proxmoxer` library wraps the REST API with multiple auth backends.

### Key Monitoring Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/cluster/status` | GET | Cluster name, quorum status, per-node online/offline |
| `/cluster/resources` | GET | Unified view of all VMs, CTs, storage, nodes across cluster |
| `/cluster/ha/status/manager_status` | GET | HA manager state, service states, quorum info |
| `/cluster/ha/resources` | GET | HA-managed resources and their current state |
| `/nodes/{node}/status` | GET | Node CPU, memory, uptime, load, kernel version |
| `/nodes/{node}/qemu` | GET | List all VMs on a node |
| `/nodes/{node}/lxc` | GET | List all containers on a node |
| `/nodes/{node}/qemu/{vmid}/status/current` | GET | Current VM status (running, stopped, etc.) + resource usage |
| `/nodes/{node}/lxc/{vmid}/status/current` | GET | Current container status + resource usage |
| `/nodes/{node}/disks/list` | GET | Physical disks on a node |
| `/nodes/{node}/disks/zfs` | GET | ZFS pool status and health |
| `/nodes/{node}/storage/{storage}/status` | GET | Storage usage (total, used, available) |
| `/nodes/{node}/tasks` | GET | Task log (backups, migrations, etc.) with filtering |
| `/nodes/{node}/qemu/{vmid}/agent/exec` | POST | Execute command via QEMU guest agent |

### Actionable Operations

| Operation | Endpoint | Method |
|---|---|---|
| Start VM | `/nodes/{node}/qemu/{vmid}/status/start` | POST |
| Stop VM | `/nodes/{node}/qemu/{vmid}/status/stop` | POST |
| Shutdown VM (graceful) | `/nodes/{node}/qemu/{vmid}/status/shutdown` | POST |
| Reboot VM | `/nodes/{node}/qemu/{vmid}/status/reboot` | POST |
| Start container | `/nodes/{node}/lxc/{vmid}/status/start` | POST |
| Stop container | `/nodes/{node}/lxc/{vmid}/status/stop` | POST |
| Migrate VM | `/nodes/{node}/qemu/{vmid}/migrate` | POST |
| Migrate container | `/nodes/{node}/lxc/{vmid}/migrate` | POST |
| Create snapshot | `/nodes/{node}/qemu/{vmid}/snapshot` | POST |

### Notification/Event Hooks

Proxmox VE (8.3+) has a built-in notification system:

- **Notification events emitted:** storage replication failures, node fencing, backup completion/failure, package updates, and other system events.
- **Webhook targets:** Perform HTTP POST/PUT to a configurable URL. Supports templating in URL, headers, and body to inject message contents and metadata.
- **Matchers:** Route events to targets based on metadata. Examples:
  - `match-field exact:type=vzdump` (backup events)
  - `match-field exact:type=replication` (replication events)
  - `match-field exact:type=fencing` (node fencing events)
- **Config location:** `/etc/pve/notifications.cfg` and `/etc/pve/priv/notifications.cfg`
- **Backup hooks:** `vzdump` supports hook scripts invoked at phases of backup jobs (job-start, backup-start, pre-stop, pre-restart, backup-end, log-end, job-end, job-abort).

### Failure Modes Worth Detecting

1. **Node offline / loss of quorum** -- cluster partition, network failure
2. **VM/CT stopped unexpectedly** -- OOM kill, kernel panic, guest crash
3. **HA service state changes** -- service entering `fence`, `recovery`, `error` states
4. **ZFS pool degraded** -- disk failure, scrub errors, checksum errors
5. **Storage space exhaustion** -- datastore approaching capacity
6. **Backup job failure** -- vzdump errors, timeout, storage full
7. **Replication failure** -- network issues, target unavailable
8. **Task failures** -- any long-running task ending in error state
9. **High resource utilization** -- CPU/memory/IO sustained above thresholds
10. **Ceph health warnings** (if applicable) -- OSD down, PG degraded

---

## 2. Proxmox Backup Server (PBS)

### API Type and Authentication

- **Type:** REST API over HTTPS (port 8007)
- **Base URL:** `https://<host>:8007/api2/json/`
- **Auth methods:**
  - **API Tokens:** `Authorization: PBSAPIToken=user@realm!token-id:token-secret`
  - **Ticket-based:** Same pattern as PVE -- POST to `/api2/json/access/ticket`
- **Backup protocol:** Uses HTTP/2 upgrade with `proxmox-backup-protocol-v1` for actual backup data transfer (distinct from management API).

### Key Monitoring Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/status/datastore-usage` | GET | Usage stats for all datastores |
| `/config/datastore` | GET | Datastore configuration list |
| `/admin/datastore/{store}/status` | GET | Detailed status of a specific datastore |
| `/admin/datastore/{store}/snapshots` | GET | List backup snapshots in a datastore |
| `/admin/datastore/{store}/group-notes` | GET | Notes for backup groups |
| `/admin/datastore/{store}/verify` | POST | Trigger verification job |
| `/admin/datastore/{store}/gc` | POST | Trigger garbage collection |
| `/nodes/localhost/tasks` | GET | Task history with filtering (supports `since` parameter) |
| `/nodes/localhost/disks/list` | GET | Physical disk inventory |
| `/nodes/localhost/disks/smart/{disk}` | GET | SMART data for a specific disk |

### Actionable Operations

| Operation | Description |
|---|---|
| Trigger verification | POST to verify endpoint -- validates backup data integrity chunk by chunk |
| Trigger garbage collection | POST to gc endpoint -- reclaims space from removed snapshots |
| Prune old backups | Delete snapshots according to retention policy |
| Check datastore health | Query status + run verification |

### Notification System

PBS (4.x) shares the same notification framework as PVE:
- Webhook targets with templating
- Matchers for event type filtering
- Events for: backup job completion/failure, verification results, GC completion, tape operations

### Failure Modes Worth Detecting

1. **Backup job failure** -- network timeout, authentication failure, datastore full
2. **Verification failure** -- corrupt chunks, checksum mismatch
3. **Datastore space exhaustion** -- approaching capacity, GC not keeping up
4. **Missed backup window** -- expected backup didn't run on schedule
5. **SMART disk warnings** -- predictive disk failure on PBS host
6. **Stale backups** -- last successful backup older than threshold
7. **GC failures** -- unable to reclaim space
8. **Task errors** -- any task ending in error state

---

## 3. Portainer

### API Type and Authentication

- **Type:** REST API over HTTPS (default port 9443) or HTTP (9000)
- **Base URL:** `https://<host>:9443/api/`
- **Auth methods:**
  - **API Key** (preferred): Set `X-API-Key: <key>` header. Keys are per-user and inherit that user's permissions.
  - **JWT Token:** POST to `/api/auth` with `{"Username": "...", "Password": "..."}`. Returns JWT token. Pass as `Authorization: Bearer <token>` header.
- **Swagger docs:** Available at `https://<host>:9443/api/docs` or on SwaggerHub.

### Key Monitoring Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/endpoints` | GET | List all environments (Docker, Swarm, K8s) |
| `/api/endpoints/{id}/docker/containers/json` | GET | List containers in an environment (proxied Docker API) |
| `/api/endpoints/{id}/docker/containers/{containerId}/json` | GET | Inspect a specific container (health, state, config) |
| `/api/stacks` | GET | List all stacks and their status |
| `/api/endpoints/{id}/docker/events` | GET | Docker event stream for an environment |
| `/api/endpoints/{id}/docker/info` | GET | Docker host info (containers running/stopped/paused) |
| `/api/endpoints/{id}/docker/system/df` | GET | Disk usage (images, containers, volumes) |
| `/api/status` | GET | Portainer server status |

**Important:** Portainer acts as a reverse proxy to the Docker API. Container management endpoints follow Docker API patterns, accessed through `/api/endpoints/{id}/docker/...`.

### Actionable Operations

| Operation | Endpoint | Method |
|---|---|---|
| Start container | `/api/endpoints/{id}/docker/containers/{cid}/start` | POST |
| Stop container | `/api/endpoints/{id}/docker/containers/{cid}/stop` | POST |
| Restart container | `/api/endpoints/{id}/docker/containers/{cid}/restart` | POST |
| Deploy/update stack | `/api/stacks` | POST/PUT |
| Remove stack | `/api/stacks/{id}` | DELETE |
| Pull image | `/api/endpoints/{id}/docker/images/create` | POST |
| Container logs | `/api/endpoints/{id}/docker/containers/{cid}/logs` | GET |

### Webhook/Event Capabilities

- **Stack webhooks** (Business Edition only): Trigger stack redeployment via POST to a webhook URL. Useful for CD pipelines.
- **Docker events:** The `/events` endpoint (proxied) provides a real-time SSE stream of container lifecycle events (start, stop, die, health_status, etc.).
- **No native push notifications:** Portainer does not push events to external systems. Polling or Docker event stream consumption is required.

### Multi-Environment Management

- Each environment (endpoint) has a unique ID
- API access is scoped by user permissions per environment
- Supports Docker standalone, Docker Swarm, and Kubernetes environments
- Environment groups and tags for organization

### Failure Modes Worth Detecting

1. **Container unhealthy** -- Docker health check failing
2. **Container exited unexpectedly** -- exit code != 0, restart loop (OOMKilled, crash)
3. **Stack deployment failure** -- image pull failure, port conflict, resource exhaustion
4. **Container restart loop** -- repeated crash/restart cycles
5. **Environment unreachable** -- Docker daemon down, network partition
6. **Disk space exhaustion** -- Docker volumes, image layers filling disk
7. **Image pull failures** -- registry auth issues, network problems
8. **Orphaned containers** -- containers running outside of managed stacks

---

## 4. Synology NAS (DSM)

### API Type and Authentication

- **Type:** REST API over HTTPS (default port 5001)
- **Base URL:** `https://<host>:5001/webapi/`
- **Auth workflow:**
  1. **Query API info:** GET `/webapi/query.cgi?api=SYNO.API.Info&version=1&method=query` -- discovers available APIs and their paths
  2. **Login:** GET/POST `/webapi/entry.cgi?api=SYNO.API.Auth&version=6&method=login&account=<user>&passwd=<pass>&otp_code=<code>` -- returns session ID (sid)
  3. **Subsequent requests:** Include `_sid=<sid>` parameter
  4. **Logout:** Call `method=logout` on SYNO.API.Auth
  - Sessions expire after 7 days by default
- **Also supports SNMP** monitoring via Synology MIB files (updated March 2025).

### Key Monitoring APIs

| API Name | Key Methods | Description |
|---|---|---|
| `SYNO.Core.System` | `info` | System model, firmware version, uptime |
| `SYNO.Core.System.Utilization` | `get` | CPU, memory, network, disk I/O utilization |
| `SYNO.Core.Storage.Disk` | `list`, `get_smart_info`, `do_smart_test`, `get_smart_test_log` | Disk inventory and SMART health data |
| `SYNO.Core.Storage.Pool` | `list`, `get` | Storage pool (RAID group) status |
| `SYNO.Core.Storage.Volume` | `list`, `get` | Volume status, total/used/available space, temperature |
| `SYNO.Core.ExternalDevice.UPS` | `get` | UPS status, battery level, runtime remaining |
| `SYNO.Core.Service` | `get` | Running services and their status |
| `SYNO.Core.Package` | `list` | Installed packages and status |
| `SYNO.DSM.Info` | `getinfo` | DSM version, model, serial, temperature |

### SNMP OIDs (via Synology MIB)

| OID Prefix | Data |
|---|---|
| `.1.3.6.1.4.1.6574.1` | System (temp, fan status, power status) |
| `.1.3.6.1.4.1.6574.2.1.1` | Disk (model, type, status, temperature) |
| `.1.3.6.1.4.1.6574.3.1.1` | RAID (name, status) |
| `.1.3.6.1.4.1.6574.4` | UPS (status, load, battery charge, time remaining) |

### Actionable Operations

| Operation | API / Method |
|---|---|
| Restart a package/service | `SYNO.Core.Package` / `start`, `stop` |
| Trigger SMART test | `SYNO.Core.Storage.Disk` / `do_smart_test` |
| Initiate storage repair | Limited -- DSM prefers manual intervention for RAID repair |
| Reboot NAS | `SYNO.Core.System` / `reboot` |
| Shutdown NAS | `SYNO.Core.System` / `shutdown` |
| Send notification | Via DSM notification system or SNMP traps |

### Failure Modes Worth Detecting

1. **RAID degradation** -- disk failed in array, rebuilding state
2. **Disk SMART warnings** -- reallocated sectors, pending sectors, temperature
3. **Disk failure** -- complete disk offline
4. **Volume near capacity** -- configurable threshold (e.g., 90%)
5. **UPS on battery** -- power outage detected
6. **UPS low battery** -- imminent shutdown needed
7. **Service/package crash** -- service stopped unexpectedly
8. **High temperature** -- system or disk temperature above threshold
9. **Fan failure** -- fan speed zero or below minimum
10. **DSM update available** -- security patches pending

---

## 5. TuringPi (BMC)

### API Type and Authentication

- **Type:** REST API over HTTP
- **Base URL:** `http://<turingpi-ip>/api/bmc`
- **Request format:** Query parameters -- `/api/bmc?opt=<operation>&type=<type>&args=<args>`
- **Response format:** JSON array `[{ "response": ... }]`
- **All methods are GET** (even for state-changing operations)
- **Auth (firmware v2.0+):**
  - POST to `/api/bmc/authenticate` with `{"username": "...", "password": "..."}` -- returns token
  - Include token in subsequent requests
- **Firmware:** Currently at v2.0.3+

### Key Monitoring Endpoints

| Query Parameters | Description |
|---|---|
| `opt=get&type=power` | Get power status of all 4 node slots (on/off per slot) |
| `opt=get&type=usb` | Get current USB routing mode |
| `opt=get&type=sdcard` | Get microSD card info |
| `opt=get&type=info` | System and daemon info (firmware version, uptime, etc.) |
| `opt=get&type=info&opt2=system` | Detailed system information |
| `opt=get&type=info&opt2=daemon` | Detailed daemon information |
| `opt=get&type=uart` | Read buffered UART data from nodes (supports encoding param) |
| `opt=get&type=network` | Network switch status |

### Actionable Operations

| Query Parameters | Description |
|---|---|
| `opt=set&type=power&node=<1-4>&val=<0\|1>` | Power on/off a specific node slot |
| `opt=set&type=usb&node=<1-4>` | Route USB to a specific node |
| `opt=set&type=reboot&node=<1-4>` | Reboot a specific node |
| `opt=set&type=node_to_msd&node=<1-4>` | Reboot node into USB Mass Storage mode (for flashing) |
| `opt=set&type=clear_usb_boot&node=<1-4>` | Clear USB boot status |
| `opt=set&type=network_reset` | Reset network switch |
| `opt=set&type=reboot_bmc` | Reboot the BMC chip itself |
| `opt=set&type=restart_daemon` | Restart the system management daemon |
| `opt=set&type=uart&node=<1-4>&cmd=<data>` | Write data to a node's UART |

### Failure Modes Worth Detecting

1. **Node power loss** -- slot powered off unexpectedly
2. **Node unresponsive** -- powered on but not responding via UART or network
3. **BMC daemon crash** -- management daemon not responding
4. **USB routing stuck** -- USB not properly routed after operation
5. **Network switch issues** -- inter-node network connectivity lost
6. **SD card errors** -- microSD card not detected or read errors
7. **Firmware mismatch** -- nodes running incompatible firmware versions

---

## 6. PiKVM

### API Type and Authentication

- **Type:** REST API over HTTPS (default port 443)
- **Base URL:** `https://<pikvm-ip>/api/`
- **Auth methods:**
  - **Per-request basic auth:** Standard HTTP Basic Authentication with PiKVM credentials
  - **Session token:** Authenticate once, receive a token, pass as cookie for subsequent requests
  - **2FA support:** If enabled, append OTP code to password without spaces (e.g., password `foobar` + code `123456` = `foobar123456`)
- **Also supports:** IPMI and Redfish (DMTF standard) for integration with standard BMC tooling

### Key Monitoring Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/info` | GET | PiKVM system info (hardware, software versions, platform) |
| `/api/atx` | GET | ATX power state -- power LED, HDD LED, busy status |
| `/api/hid` | GET | HID device state (keyboard/mouse connected, parameters) |
| `/api/msd` | GET | Mass Storage Device state (connected, image info) |
| `/api/gpio` | GET | All GPIO channel states |
| `/api/streamer/state` | GET | Video streamer status |
| `/api/streamer/snapshot` | GET | Capture screenshot of connected display |
| `/api/streamer/ocr` | GET | OCR text from current display (useful for reading error screens) |

### Actionable Operations

| Endpoint | Method | Description |
|---|---|---|
| `/api/atx/power/on` | POST | Press power button (short press) |
| `/api/atx/power/off` | POST | Press power button (long press -- force off) |
| `/api/atx/power/off_hard` | POST | Hard power off |
| `/api/atx/power/reset` | POST | Press reset button |
| `/api/hid/type` | POST | Type text on the connected host |
| `/api/hid/key` | POST | Send keyboard shortcut |
| `/api/hid/mouse/move` | POST | Move mouse to coordinates |
| `/api/hid/mouse/click` | POST | Click mouse button |
| `/api/msd/connect` | POST | Connect virtual USB drive to host |
| `/api/msd/disconnect` | POST | Disconnect virtual USB drive |
| `/api/gpio/switch` | POST | Toggle a GPIO channel on/off |
| `/api/gpio/pulse` | POST | Send a pulse to a GPIO channel |
| `/api/wol` | POST | Send Wake-on-LAN packet |

### Redfish API (Industry Standard)

PiKVM implements DMTF Redfish at `/redfish/v1/`:
- `GET /redfish/v1/Systems/0` -- system state
- `POST /redfish/v1/Systems/0/Actions/ComputerSystem.Reset` -- power actions
- Compatible with standard IPMI/Redfish management tools

### Failure Modes Worth Detecting

1. **Host powered off unexpectedly** -- ATX power LED off when expected on
2. **Host hung / unresponsive** -- power on but no video output or frozen screen
3. **Boot failure** -- OCR can detect BIOS errors, kernel panics, GRUB failures on the display
4. **PiKVM device offline** -- the KVM unit itself not responding
5. **Video capture failure** -- streamer not producing frames
6. **USB HID disconnected** -- keyboard/mouse not recognized by host
7. **BSOD / kernel panic detection** -- via OCR on the video stream

---

## Integration Architecture Notes for OasisAgent

### Ingestion Strategy by Service

| Service | Primary Ingestion | Secondary/Fallback |
|---|---|---|
| Proxmox VE | Webhook notifications (push) | REST API polling |
| Proxmox Backup Server | Webhook notifications (push) | REST API polling for task status |
| Portainer | Docker event stream (SSE) | REST API polling for container health |
| Synology NAS | REST API polling | SNMP traps (if configured) |
| TuringPi | REST API polling | UART monitoring |
| PiKVM | REST API polling | Redfish/IPMI |

### Authentication Token Management

All six services use token or session-based auth. OasisAgent should:
- Store credentials in config via `${ENV_VAR}` references
- Implement token refresh/rotation (PVE tickets expire daily, Synology sessions in 7 days)
- Use API tokens over session tickets where available (PVE, PBS, Portainer)

### Proposed Adapter Priority for Phase 2

Based on failure detection value and API maturity:

1. **Proxmox VE** -- richest API, webhook push, highest value (VM/CT is the core workload layer)
2. **Portainer** -- Docker event stream provides near-real-time container health
3. **Proxmox Backup Server** -- backup integrity is critical, API similar to PVE
4. **Synology NAS** -- storage health is foundational, SNMP provides good coverage
5. **PiKVM** -- unique OCR capability for detecting boot failures invisible to other tools
6. **TuringPi** -- BMC-level control, simpler API, lower-frequency events

---

## Sources

- [Proxmox VE API Wiki](https://pve.proxmox.com/wiki/Proxmox_VE_API)
- [Proxmox VE API Viewer (Official)](https://pve.proxmox.com/pve-docs/api-viewer/)
- [Proxmox VE Notifications Documentation](https://pve.proxmox.com/pve-docs/chapter-notifications.html)
- [Proxmox VE HA Manager Documentation](https://pve.proxmox.com/pve-docs/chapter-ha-manager.html)
- [Proxmox VE Webhook Setup Guide](https://proxmobo.app/blog/set-up-webhook-notifications-in-proxmox)
- [Proxmox Backup Server Documentation](https://pbs.proxmox.com/docs/)
- [Proxmox Backup Server API Viewer](https://pbs.proxmox.com/docs/api-viewer/index.html)
- [Proxmox Backup Server Notifications](https://pbs.proxmox.com/docs/notifications.html)
- [Portainer API Documentation](https://docs.portainer.io/api/docs)
- [Portainer API Examples](https://docs.portainer.io/api/examples)
- [Portainer API Access](https://docs.portainer.io/api/access)
- [Portainer Webhooks](https://docs.portainer.io/user/docker/stacks/webhooks)
- [Portainer Docker Events](https://docs.portainer.io/user/docker/events)
- [Synology DSM Login Web API Guide](https://kb.synology.com/en-global/DG/DSM_Login_Web_API_Guide/2)
- [Synology SNMP MIB Guide (PDF, March 2025)](https://global.download.synology.com/download/Document/Software/DeveloperGuide/Firmware/DSM/All/enu/Synology_DiskStation_MIB_Guide.pdf)
- [Synology NAS Python API -- Supported APIs](https://n4s4.github.io/synology-api/docs/apis)
- [Synology DSM API List (Community)](https://gist.github.com/Rhilip/fed6b4f69e3cc19b79c4ab17b9a17e93)
- [Synology DSM Home Assistant Integration](https://www.home-assistant.io/integrations/synology_dsm/)
- [TuringPi BMC API Documentation](https://docs.turingpi.com/docs/turing-pi2-bmc-api)
- [TuringPi BMC Python Library](https://pypi.org/project/Turing-Pi-BMC/)
- [PiKVM HTTP API Reference](https://docs.pikvm.org/api/)
- [PiKVM API Source (GitHub)](https://github.com/pikvm/pikvm/blob/master/docs/api.md)
- [PiKVM IPMI & Redfish](https://docs.pikvm.org/ipmi/)
- [Proxmoxer Python Library](https://proxmoxer.github.io/docs/2.0/authentication/)
- [synologydsm-api Python Library](https://pypi.org/project/synologydsm-api/)
