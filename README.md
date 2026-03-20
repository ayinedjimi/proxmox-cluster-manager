![Platform](https://img.shields.io/badge/Platform-Proxmox%20VE%208%2F9-E57000?style=for-the-badge&logo=proxmox&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.x-000000?style=for-the-badge&logo=flask&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)

# Proxmox Cluster Manager

**Web-based monitoring, management and optimization dashboard for Proxmox VE clusters.**

[![Release](https://img.shields.io/github/v/release/ayinedjimi/proxmox-cluster-manager?style=flat-square)](https://github.com/ayinedjimi/proxmox-cluster-manager/releases)
[![Repo Size](https://img.shields.io/github/repo-size/ayinedjimi/proxmox-cluster-manager?style=flat-square&color=555)](https://github.com/ayinedjimi/proxmox-cluster-manager)

---

## Overview

| Feature | Description |
|:--------|:------------|
| **Quick View** | Real-time health score (0-100), LED indicators, correlation analysis |
| **Monitoring** | CPU, RAM, Disk, Swap, Load, I/O Wait, PSI metrics per node |
| **Performance** | Sparkline charts, 24h averages, vCPU allocation ratios |
| **Benchmarks** | CPU, Memory, Disk R/W, Network latency with SQLite history |
| **Storage** | Thin provisioning analysis, pie charts, overcommit detection |
| **Architecture** | Animated cluster topology, Corosync/Kronosnet, heartbeat, NTP |
| **Optimizations** | 80+ automated checks: IOMMU, ZFS/ARC, NUMA, VirtIO, Windows |
| **Diagnostics** | 21 detection rules, syslog analysis, SMART health, solutions |
| **Recommendations** | Security, backups, HA, firewall, VM config analysis |
| **Audit** | Weighted health score with production-grade thresholds |
| **Guest Agent** | One-click SSH install, full VM metrics when active |
| **God Mode** | VM actions, Ceph purge, replication pre-check |
| **Ceph Wizard** | Automated Ceph deployment with TUI and web interfaces |

---

## Monitoring Thresholds

### Host Metrics

| Metric | Normal | Warning | Critical |
|:-------|:------:|:-------:|:--------:|
| CPU | < 60% | 60-85% | > 90% |
| RAM | < 80% | 80-90% | > 95% |
| Swap | 0 | > 0 | > 512 MB |
| Disk | < 70% | 70-85% | > 90% |
| Load | ≤ cores | 1.5x cores | 2x cores |
| I/O Wait | < 5% | 5-10% | > 15% |

### VM Metrics

| Metric | Normal | Warning | Critical |
|:-------|:------:|:-------:|:--------:|
| CPU | < 70% | 70-90% | 100% |
| RAM | < 80% | 80-90% | > 95% |
| Swap (guest) | 0 | > 0 | > 100 MB |
| Disk (guest) | < 80% | 80-90% | > 95% |

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│                 Web Dashboard                    │
│            (Flask + HTML/JS/CSS)                 │
├─────────────────────────────────────────────────┤
│  Quick View │ Monitoring │ Storage │ Performance │
│  Benchmarks │ Architecture │ Optimizations       │
│  Diagnostics │ Recommendations │ Audit           │
│  God Mode │ Ceph Wizard │ Journaux               │
├─────────────────────────────────────────────────┤
│             Backend (Python/Flask)                │
│  Proxmox API ←→ SSH (Paramiko) ←→ SQLite         │
├─────────────────────────────────────────────────┤
│          Proxmox VE Cluster (3+ nodes)           │
│    Ceph │ ZFS │ LVM │ NFS │ iSCSI │ QEMU/LXC    │
└─────────────────────────────────────────────────┘
```

---

## Requirements

| Component | Minimum |
|:----------|:--------|
| Python | 3.10+ |
| Proxmox VE | 7.x / 8.x / 9.x |
| Nodes | 1+ (3 recommended) |
| Network | Access to Proxmox API (port 8006) |

---

## Installation

### Linux / macOS

```bash
git clone https://github.com/ayinedjimi/proxmox-cluster-manager.git
cd proxmox-cluster-manager
pip install -r requirements.txt
cp config.example.py config.py
# Edit config.py with your Proxmox credentials
python app.py
```

### Windows

```powershell
winget install Python.Python.3.12
git clone https://github.com/ayinedjimi/proxmox-cluster-manager.git
cd proxmox-cluster-manager
pip install -r requirements.txt
copy config.example.py config.py
notepad config.py
python app.py
```

### Windows (Virtual Environment)

```powershell
git clone https://github.com/ayinedjimi/proxmox-cluster-manager.git
cd proxmox-cluster-manager
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
copy config.example.py config.py
notepad config.py
python app.py
```

Open **http://localhost:5000**

---

## Configuration

```python
PROXMOX_CLUSTER = {
    "hosts": ["192.168.1.10", "192.168.1.11", "192.168.1.12"],
    "port": 8006,
    "username": "root@pam",
    "password": "your_password",
}
```

> For production, create a dedicated Proxmox user with `PVEAuditor` role.

| Parameter | Default | Description |
|:----------|:--------|:------------|
| `hosts` | — | Proxmox node IPs (tries each for redundancy) |
| `port` | `8006` | Proxmox API port |
| `username` | `root@pam` | Authentication user |
| `password` | — | Password |
| `REFRESH_INTERVAL` | `10` | Auto-refresh interval (seconds) |
| `WEB_HOST` | `0.0.0.0` | Listen address |
| `WEB_PORT` | `5000` | HTTP port |

---

## Included Scripts

### NFS Setup (`setup-nfs.sh`)

Interactive TUI script to configure NFS shared storage for Proxmox.

```bash
wget -O setup-nfs.sh https://raw.githubusercontent.com/ayinedjimi/proxmox-cluster-manager/master/setup-nfs.sh
bash setup-nfs.sh
```

### Ceph Setup (`setup-ceph.sh`)

Interactive TUI script to deploy Ceph on Proxmox VE clusters. Handles the PVE 9 permission bug, manual MON bootstrap, and OSD creation without rollback.

```bash
wget -O setup-ceph.sh https://raw.githubusercontent.com/ayinedjimi/proxmox-cluster-manager/master/setup-ceph.sh
bash setup-ceph.sh
```

---

## API Endpoints

| Endpoint | Method | Description |
|:---------|:------:|:------------|
| `/` | GET | Dashboard UI |
| `/ceph-wizard` | GET | Ceph deployment wizard |
| `/api/status` | GET | Full cluster status |
| `/api/storage` | GET | Storage analysis |
| `/api/performance` | GET | Performance metrics |
| `/api/optimizations` | GET | Optimization checks |
| `/api/recommendations` | GET | Configuration analysis |
| `/api/diagnostics` | GET | System diagnostics |
| `/api/logs` | GET | Syslog and tasks |
| `/api/architecture` | GET | Cluster topology |
| `/api/benchmark` | POST | Run benchmarks |
| `/api/benchmark/history` | GET | Benchmark history |
| `/api/install-agent` | POST | Install QEMU guest agent |
| `/api/vm/action` | POST | VM actions (God Mode) |
| `/api/ceph/scan` | GET | Ceph prerequisites scan |
| `/api/ceph/install` | POST | Ceph deployment steps |
| `/api/ceph/purge-all` | POST | Purge Ceph cluster |
| `/api/replication-check` | POST | Replication pre-check |

---

## Production Deployment

### Gunicorn

```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 app:app
```

### Systemd Service

```ini
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

### Windows Auto-Start

Create `start.bat`:
```batch
@echo off
cd /d "%~dp0"
python app.py
```
Place shortcut in `shell:startup`.

---

## Security

- `config.py` is in `.gitignore` (never committed)
- Proxmox API accessed over HTTPS (self-signed certs accepted)
- SSH connections use one-time sessions (no keys stored)
- Consider running behind a reverse proxy (nginx) with HTTPS

---

## Related Proxmox Resources

Comprehensive Proxmox VE guides by Ayi NEDJIMI Consultants:

- [Proxmox VE 9 : Guide Complet](https://ayinedjimi-consultants.fr/virtualisation/proxmox-ve-guide-complet.html)
- [Guide d'Optimisation Proxmox VE 9.0](https://www.ayinedjimi-consultants.fr/virtualisation/optimisation-proxmox.html)
- [Guide de Dimensionnement Proxmox VE 9.0](https://www.ayinedjimi-consultants.fr/virtualisation/dimensionnement-proxmox.html)
- [Memento Securite Proxmox VE 9](https://www.ayinedjimi-consultants.fr/virtualisation/securite-proxmox.html)
- [Audit Securite Proxmox 9](https://www.ayinedjimi-consultants.fr/virtualisation.html)
- [Evolutions Proxmox VE (V7 a V9)](https://www.ayinedjimi-consultants.fr/virtualisation/evolutions-proxmox.html)
- [Migration VMware vers Proxmox 9](https://www.ayinedjimi-consultants.fr/virtualisation/migration-vmware-proxmox.html)
- [Synchronisation NTP pour Proxmox VE](https://www.ayinedjimi-consultants.fr/virtualisation/ntp-proxmox.html)

Visit [ayinedjimi-consultants.fr](https://www.ayinedjimi-consultants.fr) for more.

---

## License

MIT License — Copyright (c) 2026 [Ayi NEDJIMI Consultants](https://www.ayinedjimi-consultants.fr)

See [LICENSE](LICENSE) for details.

---

<p align="center">
  <b>Ayi NEDJIMI Consultants</b> — Infrastructure & Virtualization<br>
  <a href="https://www.ayinedjimi-consultants.fr">www.ayinedjimi-consultants.fr</a>
</p>
