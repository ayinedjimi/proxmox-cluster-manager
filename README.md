# Proxmox Cluster Manager

**A comprehensive web-based monitoring and management dashboard for Proxmox VE clusters.**

Built by [Ayi NEDJIMI Consultants](https://github.com/ayinedjimi)

![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python)
![Flask](https://img.shields.io/badge/Flask-3.x-green?logo=flask)
![Proxmox](https://img.shields.io/badge/Proxmox-VE%208%2F9-orange?logo=proxmox)
![License](https://img.shields.io/badge/License-MIT-yellow)

---

## Features

### Dashboard (Vue rapide)
- **Cluster health score** (0-100) with real-time calculation
- **LED indicators** (green/yellow/red) per node with alert blink
- **Per-node metrics** with Grok-level thresholds: CPU, RAM, Disk, Swap, Load, I/O Wait
- **Cluster-wide VM/CT overview** with instant status
- **Intelligent correlation analysis** (oversubscription, I/O bottlenecks, memory pressure)

### Main View (Vue principale)
- **Circular gauges** for CPU, RAM, Disk, Swap per node
- **Live metrics**: Load Average, I/O Wait, Network In/Out, Uptime
- **Services monitoring**: critical Proxmox services status with alerts
- **Physical disks**: SMART health, wearout detection
- **Network interfaces**: IPs, bridges, gateways
- **VMs & Containers**: full table with CPU, RAM, Disk, Net I/O, Uptime
- **Storage**: usage per volume with type and content
- **QEMU Guest Agent**: 3-level detection (not configured / configured / connected)

### Performance Tab
- **PSI (Pressure Stall Information)**: CPU contention, memory pressure, I/O congestion
- **Sparkline charts**: 1-hour history for CPU, RAM, I/O, Load, Network
- **Performance summary**: cores available, load vs cores, I/O status

### Logs Tab (Journaux)
- **Syslog per node** with error/warning highlighting
- **Error tasks** highlighted across cluster
- **Cluster journal** with auth events
- **Filter & search**: filter by level (errors/warnings), full-text search

### Recommendations Tab
- **20+ automated checks**: security, backups, HA, firewall, DNS, VM config
- **VM-level analysis**: CPU type, SCSI controller, VirtIO, TRIM/discard, Guest Agent, BIOS, NUMA
- **Severity levels**: Critical / Warning / Info with actionable recommendations

### Audit Tab
- **Health score /100** with weighted checks
- **Grok-level thresholds**: CPU <60% ok / 60-85% warn / >90% crit
- **Per-category breakdown**: Cluster, CPU, Memory, I/O, PSI, Services, Hardware, Storage, VMs
- **VM & Container audit**: individual performance checks

### Guest Agent Integration
- **Automatic detection** of QEMU Guest Agent on each VM
- **One-click SSH install**: detects Linux distro and installs `qemu-guest-agent` remotely
- **Manual install guide**: step-by-step for all distros (Debian, RHEL, Alpine, Arch, SUSE, Windows)
- **Rich VM metrics when agent is active**:
  - Guest OS info, hostname, kernel, timezone
  - Filesystem usage per mount point
  - Network interfaces with IPs, RX/TX stats, errors
  - vCPU online status
  - VM-level PSI (Pressure Stall Information)
  - Disk read/write throughput

---

## Screenshots

The dashboard features a clean, light theme with color-coded metrics:
- **Green**: Normal / OK
- **Yellow**: Warning / Attention needed
- **Red**: Critical / Action required

---

## Requirements

- **Python** 3.10+
- **Proxmox VE** 7.x / 8.x / 9.x (single node or cluster)
- Network access from the monitoring machine to Proxmox API (port 8006)

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/ayinedjimi/proxmox-cluster-manager.git
cd proxmox-cluster-manager
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure

```bash
cp config.example.py config.py
```

Edit `config.py` with your Proxmox cluster details:

```python
PROXMOX_CLUSTER = {
    "hosts": ["192.168.1.10", "192.168.1.11"],  # Your Proxmox node IPs
    "port": 8006,
    "username": "root@pam",       # Or a dedicated monitoring user
    "password": "your_password",
}
```

> **Tip**: For production, create a dedicated Proxmox user with the `PVEAuditor` role instead of using root.

### 4. Run

```bash
python app.py
```

Open your browser at **http://localhost:5000**

---

## Configuration Reference

| Parameter | Default | Description |
|-----------|---------|-------------|
| `PROXMOX_CLUSTER.hosts` | - | List of Proxmox node IPs (tries each for redundancy) |
| `PROXMOX_CLUSTER.port` | `8006` | Proxmox API port |
| `PROXMOX_CLUSTER.username` | `root@pam` | Proxmox authentication user |
| `PROXMOX_CLUSTER.password` | - | Password for the user |
| `REFRESH_INTERVAL` | `10` | Auto-refresh interval in seconds |
| `SYSLOG_LINES` | `100` | Syslog lines per node in Logs tab |
| `TASK_LIMIT` | `30` | Recent tasks per node |
| `CLUSTER_LOG_MAX` | `50` | Cluster log entries |
| `WEB_HOST` | `0.0.0.0` | Listen address (`127.0.0.1` for local only) |
| `WEB_PORT` | `5000` | HTTP port |
| `WEB_DEBUG` | `False` | Debug mode (True for development) |

---

## Monitoring Thresholds

Based on production-grade monitoring best practices:

### Host Metrics
| Metric | Normal | Warning | Critical |
|--------|--------|---------|----------|
| CPU | < 60% | 60-85% | > 90% |
| RAM | < 80% | 80-90% | > 95% |
| Swap | 0 | > 0 | > 512 MB |
| Disk | < 70% | 70-85% | > 90% |
| Load | <= cores | 1.5x cores | 2x cores |
| I/O Wait | < 5% | 5-10% | > 15% |

### VM Metrics
| Metric | Normal | Warning | Critical |
|--------|--------|---------|----------|
| CPU | < 70% | 70-90% | 100% constant |
| RAM | < 80% | 80-90% | > 95% |
| Swap (guest) | 0 | > 0 | > 100 MB |
| Disk (guest) | < 80% | 80-90% | > 95% |

---

## Security Notes

- `config.py` is in `.gitignore` and **never committed** (contains credentials)
- Proxmox API is accessed over HTTPS (self-signed certs accepted)
- SSH install feature uses one-time connections (no keys stored)
- For production: use a dedicated Proxmox user with `PVEAuditor` role
- Consider running behind a reverse proxy (nginx) with HTTPS

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Dashboard UI |
| `/api/status` | GET | Full cluster status (nodes, VMs, CTs, storage) |
| `/api/recommendations` | GET | Configuration analysis and recommendations |
| `/api/logs` | GET | Syslog, tasks, and cluster journal |
| `/api/install-agent` | POST | Install QEMU guest agent via SSH |

---

## Production Deployment

For production use, consider using a WSGI server:

```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

Or with systemd:

```ini
# /etc/systemd/system/cluster-manager.service
[Unit]
Description=Proxmox Cluster Manager
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/proxmox-cluster-manager
ExecStart=/opt/proxmox-cluster-manager/venv/bin/gunicorn -w 4 -b 0.0.0.0:5000 app:app
Restart=always

[Install]
WantedBy=multi-user.target
```

---

## Contributing

Contributions are welcome! Please open an issue or pull request.

---

## License

MIT License - Copyright (c) 2026 Ayi NEDJIMI Consultants

See [LICENSE](LICENSE) for details.
