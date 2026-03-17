# ============================================================================
#  Cluster Manager
#  Copyright (c) 2026 Ayi NEDJIMI Consultants - Tous droits reserves
# ============================================================================

from flask import Flask, render_template, jsonify, request as flask_request
import requests
import urllib3
import time
import paramiko
from config import (
    PROXMOX_CLUSTER, REFRESH_INTERVAL,
    SYSLOG_LINES, TASK_LIMIT, CLUSTER_LOG_MAX,
    WEB_HOST, WEB_PORT, WEB_DEBUG,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

_auth_cache = {"ticket": None, "csrf": None, "host": None, "expires": 0}


def get_ticket():
    now = time.time()
    if _auth_cache["ticket"] and now < _auth_cache["expires"]:
        return _auth_cache["host"], _auth_cache["ticket"], _auth_cache["csrf"]
    for host in PROXMOX_CLUSTER["hosts"]:
        url = f"https://{host}:{PROXMOX_CLUSTER['port']}/api2/json/access/ticket"
        try:
            resp = requests.post(url, data={
                "username": PROXMOX_CLUSTER["username"],
                "password": PROXMOX_CLUSTER["password"],
            }, verify=False, timeout=5)
            if resp.status_code == 200:
                data = resp.json()["data"]
                _auth_cache["ticket"] = data["ticket"]
                _auth_cache["csrf"] = data["CSRFPreventionToken"]
                _auth_cache["host"] = host
                _auth_cache["expires"] = now + 7000
                return host, data["ticket"], data["CSRFPreventionToken"]
        except requests.RequestException:
            continue
    return None, None, None


def proxmox_api(host, endpoint):
    _, ticket, csrf = get_ticket()
    if not ticket:
        return {"error": "Impossible de s'authentifier"}
    url = f"https://{host}:{PROXMOX_CLUSTER['port']}/api2/json{endpoint}"
    cookies = {"PVEAuthCookie": ticket}
    headers = {"CSRFPreventionToken": csrf}
    try:
        resp = requests.get(url, headers=headers, cookies=cookies, verify=False, timeout=10)
        resp.raise_for_status()
        return resp.json().get("data", {})
    except requests.RequestException as e:
        return {"error": str(e)}


def fmt_bytes(b):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def fmt_uptime(seconds):
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    return f"{days}j {hours}h {minutes}m"


def fmt_speed(bps):
    bps *= 8
    for unit in ["bps", "Kbps", "Mbps", "Gbps"]:
        if bps < 1024:
            return f"{bps:.1f} {unit}"
        bps /= 1024
    return f"{bps:.1f} Tbps"


def fmt_timestamp(ts):
    return time.strftime("%d/%m %H:%M", time.localtime(ts))


# ── Routes ──────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", refresh_interval=REFRESH_INTERVAL)


@app.route("/api/status")
def api_status():
    host, ticket, _ = get_ticket()
    if not host:
        return jsonify({"error": "Impossible de se connecter au cluster Proxmox"}), 503

    cluster_status = proxmox_api(host, "/cluster/status")
    cluster_info = {"name": "Cluster", "quorate": False, "nodes_info": []}
    if isinstance(cluster_status, list):
        for item in cluster_status:
            if item.get("type") == "cluster":
                cluster_info["name"] = item.get("name", "Cluster")
                cluster_info["quorate"] = bool(item.get("quorate"))
                cluster_info["version"] = item.get("version")
                cluster_info["total_nodes"] = item.get("nodes")
            elif item.get("type") == "node":
                cluster_info["nodes_info"].append({
                    "name": item.get("name"), "ip": item.get("ip"),
                    "online": bool(item.get("online")), "nodeid": item.get("nodeid"),
                })

    ha_status = proxmox_api(host, "/cluster/ha/status/current")
    cluster_info["ha"] = {"status": "N/A"}
    if isinstance(ha_status, list):
        for item in ha_status:
            if item.get("type") == "quorum":
                cluster_info["ha"] = {"status": item.get("status", "N/A"), "quorate": bool(item.get("quorate"))}

    tasks = proxmox_api(host, "/cluster/tasks")
    cluster_info["recent_tasks"] = []
    if isinstance(tasks, list):
        for t in sorted(tasks, key=lambda x: x.get("starttime", 0), reverse=True)[:10]:
            cluster_info["recent_tasks"].append({
                "type": t.get("type", ""), "status": t.get("status", ""),
                "node": t.get("node", ""), "user": t.get("user", ""),
                "starttime": fmt_timestamp(t.get("starttime", 0)),
                "endtime": fmt_timestamp(t.get("endtime", 0)) if t.get("endtime") else "en cours",
            })

    nodes_list = proxmox_api(host, "/nodes")
    if isinstance(nodes_list, dict) and "error" in nodes_list:
        return jsonify({"error": nodes_list["error"]}), 503

    nodes = []
    for node in sorted(nodes_list, key=lambda n: n.get("node", "")):
        node_name = node.get("node", "unknown")
        node_online = node.get("status") == "online"
        node_ip = "N/A"
        for ni in cluster_info["nodes_info"]:
            if ni["name"] == node_name:
                node_ip = ni.get("ip", "N/A")

        node_data = {
            "name": node_name, "ip": node_ip, "online": node_online,
            "stats": None, "rrd": None, "rrd_history": [],
            "services": [], "disks": [], "network_interfaces": [],
            "vms": [], "containers": [], "storage": [],
        }

        if not node_online:
            nodes.append(node_data)
            continue

        ns = proxmox_api(host, f"/nodes/{node_name}/status")
        if isinstance(ns, dict) and "error" not in ns:
            cpu_info = ns.get("cpuinfo", {})
            memory = ns.get("memory", {})
            rootfs = ns.get("rootfs", {})
            swap = ns.get("swap", {})
            swap_used_raw = swap.get("used", 0)
            swap_total_raw = swap.get("total", 0)
            mem_total_raw = memory.get("total", 0)
            mem_avail_raw = memory.get("available", 0)
            node_data["stats"] = {
                "cpu_usage": round(ns.get("cpu", 0) * 100, 1),
                "cpu_model": cpu_info.get("model", "N/A"),
                "cpu_cores": cpu_info.get("cpus", 0),
                "cpu_sockets": cpu_info.get("sockets", 0),
                "cpu_mhz": cpu_info.get("mhz", ""),
                "cpu_hvm": bool(cpu_info.get("hvm", 0)),
                "memory_used": fmt_bytes(memory.get("used", 0)),
                "memory_total": fmt_bytes(mem_total_raw),
                "memory_free": fmt_bytes(memory.get("free", 0)),
                "memory_available": fmt_bytes(mem_avail_raw),
                "memory_pct": round(memory.get("used", 0) / max(mem_total_raw, 1) * 100, 1),
                "swap_used": fmt_bytes(swap_used_raw),
                "swap_total": fmt_bytes(swap_total_raw),
                "swap_used_bytes": swap_used_raw,
                "swap_pct": round(swap_used_raw / max(swap_total_raw, 1) * 100, 1),
                "disk_used": fmt_bytes(rootfs.get("used", 0)),
                "disk_total": fmt_bytes(rootfs.get("total", 0)),
                "disk_free": fmt_bytes(rootfs.get("free", 0)),
                "disk_pct": round(rootfs.get("used", 0) / max(rootfs.get("total", 1), 1) * 100, 1),
                "uptime": fmt_uptime(ns.get("uptime", 0)),
                "uptime_raw": ns.get("uptime", 0),
                "loadavg": ns.get("loadavg", ["0", "0", "0"]),
                "iowait_pct": round(ns.get("wait", 0) * 100, 2),
                "kernel": ns.get("kversion", "N/A"),
                "pveversion": ns.get("pveversion", "N/A"),
                "boot_mode": ns.get("boot-info", {}).get("mode", "N/A"),
                "secureboot": bool(ns.get("boot-info", {}).get("secureboot", 0)),
                "ksm_shared": ns.get("ksm", {}).get("shared", 0),
            }

        rrd = proxmox_api(host, f"/nodes/{node_name}/rrddata?timeframe=hour")
        if isinstance(rrd, list) and rrd:
            last = rrd[-1]
            netin_raw = last.get("netin", 0)
            netout_raw = last.get("netout", 0)
            node_data["rrd"] = {
                "loadavg": round(last.get("loadavg", 0), 2),
                "iowait": round(last.get("iowait", 0) * 100, 2),
                "netin": fmt_speed(netin_raw),
                "netout": fmt_speed(netout_raw),
                "netin_bytes": round(netin_raw),
                "netout_bytes": round(netout_raw),
                "mem_available": fmt_bytes(last.get("memavailable", 0)),
                "mem_available_bytes": last.get("memavailable", 0),
                "arcsize": fmt_bytes(last.get("arcsize", 0)),
                "psi_cpu_some": round(last.get("pressurecpusome", 0) * 100, 2),
                "psi_memory_some": round(last.get("pressurememorysome", 0) * 100, 2),
                "psi_memory_full": round(last.get("pressurememoryfull", 0) * 100, 2),
                "psi_io_some": round(last.get("pressureiosome", 0) * 100, 2),
                "psi_io_full": round(last.get("pressureiofull", 0) * 100, 2),
            }
            node_data["rrd_history"] = [{
                "time": e.get("time", 0),
                "cpu": round(e.get("cpu", 0) * 100, 1),
                "mem": round(e.get("memused", 0) / max(e.get("memtotal", 1), 1) * 100, 1),
                "io": round(e.get("iowait", 0) * 100, 2),
                "netin": round(e.get("netin", 0)),
                "netout": round(e.get("netout", 0)),
                "load": round(e.get("loadavg", 0), 2),
            } for e in rrd[-20:]]

        svcs = proxmox_api(host, f"/nodes/{node_name}/services")
        if isinstance(svcs, list):
            critical = ["pvedaemon", "pveproxy", "pve-cluster", "corosync",
                        "pve-ha-crm", "pve-ha-lrm", "pve-firewall",
                        "proxmox-firewall", "cron", "sshd", "chrony"]
            for s in svcs:
                svc_name = s.get("name", "")
                if svc_name in critical or s.get("state") != "running":
                    node_data["services"].append({
                        "name": svc_name, "state": s.get("state", "unknown"),
                        "desc": s.get("desc", ""), "critical": svc_name in critical,
                    })

        disks = proxmox_api(host, f"/nodes/{node_name}/disks/list")
        if isinstance(disks, list):
            for d in disks:
                node_data["disks"].append({
                    "devpath": d.get("devpath", ""), "model": d.get("model", "N/A"),
                    "size": fmt_bytes(d.get("size", 0)), "health": d.get("health", "N/A"),
                    "wearout": d.get("wearout", "N/A"), "serial": d.get("serial", "")[:16],
                })

        nets = proxmox_api(host, f"/nodes/{node_name}/network")
        if isinstance(nets, list):
            for n in nets:
                if n.get("active") and n.get("address"):
                    node_data["network_interfaces"].append({
                        "iface": n.get("iface", ""), "type": n.get("type", ""),
                        "address": n.get("address", ""), "netmask": n.get("netmask", ""),
                        "gateway": n.get("gateway", ""), "bridge_ports": n.get("bridge_ports", ""),
                    })

        for vm in (proxmox_api(host, f"/nodes/{node_name}/qemu") or []):
            if not isinstance(vm, dict):
                continue
            vmid = vm.get("vmid")
            vm_entry = {
                "vmid": vmid, "name": vm.get("name", "N/A"),
                "status": vm.get("status", "unknown"),
                "cpu": round(vm.get("cpu", 0) * 100, 1),
                "maxcpu": vm.get("cpus", vm.get("maxcpu", 0)),
                "mem": fmt_bytes(vm.get("mem", 0)), "maxmem": fmt_bytes(vm.get("maxmem", 0)),
                "mem_pct": round(vm.get("mem", 0) / max(vm.get("maxmem", 1), 1) * 100, 1),
                "mem_raw": vm.get("mem", 0), "maxmem_raw": vm.get("maxmem", 0),
                "uptime": fmt_uptime(vm.get("uptime", 0)),
                "uptime_raw": vm.get("uptime", 0),
                "disk": fmt_bytes(vm.get("maxdisk", 0)),
                "diskread": fmt_bytes(vm.get("diskread", 0)),
                "diskwrite": fmt_bytes(vm.get("diskwrite", 0)),
                "netin": fmt_bytes(vm.get("netin", 0)), "netout": fmt_bytes(vm.get("netout", 0)),
                "ha": vm.get("hastate", ""),
                "agent_enabled": False,
                "agent_running": False,
                "agent_status": "non configure",
                "guest_agent": {},
            }
            # Check agent configuration
            vm_cfg = proxmox_api(host, f"/nodes/{node_name}/qemu/{vmid}/config")
            if isinstance(vm_cfg, dict) and "error" not in vm_cfg:
                agent_val = str(vm_cfg.get("agent", ""))
                if agent_val and agent_val != "0":
                    vm_entry["agent_enabled"] = True
                    vm_entry["agent_status"] = "active (non joignable)" if vm.get("status") != "running" else "active (VM arretee)"
                else:
                    vm_entry["agent_status"] = "non configure"

            # Try guest agent for running VMs
            if vm.get("status") == "running" and vm_entry["agent_enabled"]:
                agent_info = proxmox_api(host, f"/nodes/{node_name}/qemu/{vmid}/agent/get-osinfo")
                if isinstance(agent_info, dict) and "error" not in agent_info:
                    result = agent_info.get("result", agent_info)
                    vm_entry["agent_running"] = True
                    vm_entry["agent_status"] = "connecte"
                    vm_entry["guest_agent"] = {
                        "os": result.get("pretty-name", result.get("name", "N/A")),
                        "kernel": result.get("kernel-release", ""),
                        "version": result.get("version-id", ""),
                        "machine": result.get("machine", ""),
                    }

                    # Hostname
                    hn = proxmox_api(host, f"/nodes/{node_name}/qemu/{vmid}/agent/get-host-name")
                    if isinstance(hn, dict) and "error" not in hn:
                        vm_entry["guest_agent"]["hostname"] = hn.get("result", {}).get("host-name", "")

                    # Timezone
                    tz = proxmox_api(host, f"/nodes/{node_name}/qemu/{vmid}/agent/get-timezone")
                    if isinstance(tz, dict) and "error" not in tz:
                        vm_entry["guest_agent"]["timezone"] = tz.get("result", {}).get("zone", "")

                    # Network interfaces
                    net_ifaces = proxmox_api(host, f"/nodes/{node_name}/qemu/{vmid}/agent/network-get-interfaces")
                    if isinstance(net_ifaces, dict) and "error" not in net_ifaces:
                        ifaces = []
                        for iface in net_ifaces.get("result", []):
                            if iface.get("name") == "lo":
                                continue
                            ips = []
                            for addr in iface.get("ip-addresses", []):
                                if addr.get("ip-address-type") == "ipv4":
                                    ips.append(addr.get("ip-address", ""))
                            stats = iface.get("statistics", {})
                            ifaces.append({
                                "name": iface.get("name", ""),
                                "mac": iface.get("hardware-address", ""),
                                "ips": ips,
                                "rx_bytes": fmt_bytes(stats.get("rx-bytes", 0)),
                                "tx_bytes": fmt_bytes(stats.get("tx-bytes", 0)),
                                "rx_errs": stats.get("rx-errs", 0),
                                "tx_errs": stats.get("tx-errs", 0),
                                "rx_dropped": stats.get("rx-dropped", 0),
                            })
                        vm_entry["guest_agent"]["interfaces"] = ifaces

                    # Filesystems
                    fs_info = proxmox_api(host, f"/nodes/{node_name}/qemu/{vmid}/agent/get-fsinfo")
                    if isinstance(fs_info, dict) and "error" not in fs_info:
                        filesystems = []
                        for fs in fs_info.get("result", []):
                            total = fs.get("total-bytes", 0)
                            used = fs.get("used-bytes", 0)
                            if total <= 0 or fs.get("type") in ("squashfs", "iso9660", "tmpfs", "devtmpfs"):
                                continue
                            filesystems.append({
                                "mount": fs.get("mountpoint", ""),
                                "name": fs.get("name", ""),
                                "type": fs.get("type", ""),
                                "total": fmt_bytes(total),
                                "used": fmt_bytes(used),
                                "free": fmt_bytes(max(total - used, 0)),
                                "pct": round(used / max(total, 1) * 100, 1),
                            })
                        vm_entry["guest_agent"]["filesystems"] = filesystems

                    # vCPUs
                    vcpus = proxmox_api(host, f"/nodes/{node_name}/qemu/{vmid}/agent/get-vcpus")
                    if isinstance(vcpus, dict) and "error" not in vcpus:
                        result_vcpus = vcpus.get("result", [])
                        vm_entry["guest_agent"]["vcpus_online"] = sum(1 for v in result_vcpus if v.get("online"))
                        vm_entry["guest_agent"]["vcpus_total"] = len(result_vcpus)

                    # VM RRD for PSI + disk I/O
                    vm_rrd = proxmox_api(host, f"/nodes/{node_name}/qemu/{vmid}/rrddata?timeframe=hour")
                    if isinstance(vm_rrd, list) and vm_rrd:
                        last = vm_rrd[-1]
                        vm_entry["guest_agent"]["rrd"] = {
                            "cpu": round(last.get("cpu", 0) * 100, 1),
                            "mem_used": fmt_bytes(last.get("mem", 0)),
                            "mem_total": fmt_bytes(last.get("maxmem", 0)),
                            "mem_host": fmt_bytes(last.get("memhost", 0)),
                            "mem_pct": round(last.get("mem", 0) / max(last.get("maxmem", 1), 1) * 100, 1),
                            "diskread": fmt_bytes(last.get("diskread", 0)),
                            "diskwrite": fmt_bytes(last.get("diskwrite", 0)),
                            "netin": fmt_speed(last.get("netin", 0)),
                            "netout": fmt_speed(last.get("netout", 0)),
                            "psi_cpu_some": round(last.get("pressurecpusome", 0) * 100, 2),
                            "psi_cpu_full": round(last.get("pressurecpufull", 0) * 100, 2),
                            "psi_mem_some": round(last.get("pressurememorysome", 0) * 100, 2),
                            "psi_mem_full": round(last.get("pressurememoryfull", 0) * 100, 2),
                            "psi_io_some": round(last.get("pressureiosome", 0) * 100, 2),
                            "psi_io_full": round(last.get("pressureiofull", 0) * 100, 2),
                        }

                else:
                    vm_entry["agent_status"] = "active mais non joignable (installer qemu-guest-agent dans la VM)"
            elif vm.get("status") == "running" and not vm_entry["agent_enabled"]:
                vm_entry["agent_status"] = "non configure"

            node_data["vms"].append(vm_entry)

        for ct in (proxmox_api(host, f"/nodes/{node_name}/lxc") or []):
            if not isinstance(ct, dict):
                continue
            node_data["containers"].append({
                "vmid": ct.get("vmid"), "name": ct.get("name", "N/A"),
                "status": ct.get("status", "unknown"),
                "cpu": round(ct.get("cpu", 0) * 100, 1),
                "maxcpu": ct.get("cpus", ct.get("maxcpu", 0)),
                "mem": fmt_bytes(ct.get("mem", 0)), "maxmem": fmt_bytes(ct.get("maxmem", 0)),
                "mem_pct": round(ct.get("mem", 0) / max(ct.get("maxmem", 1), 1) * 100, 1),
                "mem_raw": ct.get("mem", 0), "maxmem_raw": ct.get("maxmem", 0),
                "uptime": fmt_uptime(ct.get("uptime", 0)),
                "uptime_raw": ct.get("uptime", 0),
                "disk": fmt_bytes(ct.get("maxdisk", 0)),
                "diskread": fmt_bytes(ct.get("diskread", 0)),
                "diskwrite": fmt_bytes(ct.get("diskwrite", 0)),
                "netin": fmt_bytes(ct.get("netin", 0)), "netout": fmt_bytes(ct.get("netout", 0)),
            })

        storages = proxmox_api(host, f"/nodes/{node_name}/storage")
        if isinstance(storages, list):
            for st in storages:
                if st.get("active"):
                    node_data["storage"].append({
                        "storage": st.get("storage"), "type": st.get("type"),
                        "used": fmt_bytes(st.get("used", 0)),
                        "total": fmt_bytes(st.get("total", 0)),
                        "pct": round(st.get("used", 0) / max(st.get("total", 1), 1) * 100, 1),
                        "content": st.get("content", ""), "plugintype": st.get("plugintype", ""),
                    })

        nodes.append(node_data)

    return jsonify({"cluster": cluster_info, "api_host": host, "nodes": nodes})


# ── Journaux ────────────────────────────────────────────────────────────────

@app.route("/api/logs")
def api_logs():
    """Recupere les journaux de tous les noeuds + journal cluster."""
    host, ticket, _ = get_ticket()
    if not host:
        return jsonify({"error": "Non connecte"}), 503

    result = {"cluster_log": [], "nodes": []}

    # Cluster log
    clog = proxmox_api(host, f"/cluster/log?max={CLUSTER_LOG_MAX}")
    if isinstance(clog, list):
        for entry in clog:
            pri = entry.get("pri", 6)
            level = "error" if pri <= 3 else "warning" if pri <= 4 else "info"
            result["cluster_log"].append({
                "time": fmt_timestamp(entry.get("time", 0)),
                "time_raw": entry.get("time", 0),
                "node": entry.get("node", ""),
                "tag": entry.get("tag", ""),
                "msg": entry.get("msg", ""),
                "user": entry.get("user", ""),
                "level": level,
            })

    # Per-node logs
    nodes_list = proxmox_api(host, "/nodes")
    if not isinstance(nodes_list, list):
        return jsonify(result)

    for node in sorted(nodes_list, key=lambda n: n.get("node", "")):
        node_name = node.get("node", "")
        if node.get("status") != "online":
            continue

        node_log = {"name": node_name, "syslog": [], "tasks": [], "task_errors": []}

        # Syslog
        syslog = proxmox_api(host, f"/nodes/{node_name}/syslog?limit={SYSLOG_LINES}")
        if isinstance(syslog, list):
            for entry in syslog:
                line = entry.get("t", "") + " " + str(entry.get("n", ""))
                text = entry.get("t", "")
                level = "info"
                lower = text.lower()
                if any(w in lower for w in ["error", "fail", "fatal", "critical", "panic", "segfault", "oom"]):
                    level = "error"
                elif any(w in lower for w in ["warn", "timeout", "refused", "denied", "retry"]):
                    level = "warning"
                node_log["syslog"].append({"line": line, "level": level})

        # Recent tasks
        tasks = proxmox_api(host, f"/nodes/{node_name}/tasks?limit={TASK_LIMIT}")
        if isinstance(tasks, list):
            for t in tasks:
                status = t.get("status", "")
                level = "ok" if status == "OK" else "error" if status else "running"
                node_log["tasks"].append({
                    "type": t.get("type", ""),
                    "status": status,
                    "user": t.get("user", ""),
                    "starttime": fmt_timestamp(t.get("starttime", 0)),
                    "endtime": fmt_timestamp(t.get("endtime", 0)) if t.get("endtime") else "en cours",
                    "id": t.get("id", ""),
                    "level": level,
                })

        # Error tasks specifically
        err_tasks = proxmox_api(host, f"/nodes/{node_name}/tasks?limit=20&errors=1")
        if isinstance(err_tasks, list):
            for t in err_tasks:
                node_log["task_errors"].append({
                    "type": t.get("type", ""),
                    "status": t.get("status", ""),
                    "user": t.get("user", ""),
                    "starttime": fmt_timestamp(t.get("starttime", 0)),
                })

        result["nodes"].append(node_log)

    return jsonify(result)


# ── Recommandations ─────────────────────────────────────────────────────────

@app.route("/api/recommendations")
def api_recommendations():
    host, ticket, _ = get_ticket()
    if not host:
        return jsonify({"error": "Non connecte"}), 503

    recs = []

    def add(cat, severity, title, detail, current, recommended):
        recs.append({"category": cat, "severity": severity, "title": title,
                      "detail": detail, "current": str(current), "recommended": str(recommended)})

    # ── Cluster-level ──
    cluster_opts = proxmox_api(host, "/cluster/options")
    fw_opts = proxmox_api(host, "/cluster/firewall/options")
    ha_resources = proxmox_api(host, "/cluster/ha/resources")
    backup_jobs = proxmox_api(host, "/cluster/backup")
    replication = proxmox_api(host, "/cluster/replication")

    # HA
    if isinstance(ha_resources, list) and len(ha_resources) == 0:
        add("Haute Disponibilite", "warning", "Aucune ressource HA configuree",
            "Configurez la HA pour vos VMs/CTs critiques afin qu'elles migrent automatiquement en cas de panne d'un noeud. "
            "Allez dans Datacenter > HA > Add pour ajouter vos VMs.",
            "0 ressource HA", "Ajouter les VMs/CTs critiques dans HA")

    # Cluster firewall
    if isinstance(fw_opts, dict):
        if not fw_opts.get("enable"):
            add("Securite", "warning", "Firewall cluster desactive",
                "Le firewall au niveau du Datacenter n'est pas active. Il protege l'ensemble du cluster.",
                "Desactive", "Datacenter > Firewall > Options > Enable: Yes")

    # Node firewall
    nodes_list = proxmox_api(host, "/nodes")
    if isinstance(nodes_list, list):
        for node in nodes_list:
            nn = node.get("node", "")
            if node.get("status") != "online":
                continue
            nfw = proxmox_api(host, f"/nodes/{nn}/firewall/options")
            if isinstance(nfw, dict) and not nfw.get("enable"):
                add("Securite", "info", f"{nn}: Firewall noeud desactive",
                    "Le firewall au niveau du noeud n'est pas active.",
                    "Desactive", f"Node {nn} > Firewall > Options > Enable: Yes")

    # Backups
    if isinstance(backup_jobs, list):
        if len(backup_jobs) == 0:
            add("Sauvegarde", "critical", "Aucun job de backup configure",
                "Il n'y a aucune sauvegarde automatique programmee ! En cas de panne, vous perdrez toutes les donnees. "
                "Configurez via Datacenter > Backup > Add.",
                "0 backup programme", "Au minimum: backup hebdomadaire de toutes les VMs")
        else:
            # Check backup schedule details
            for job in backup_jobs:
                if not job.get("enabled", True):
                    add("Sauvegarde", "warning", f"Job backup desactive: {job.get('id', 'N/A')}",
                        "Un job de backup existe mais est desactive.",
                        "Desactive", "Reactiver le job de backup")
                compress = job.get("compress", "0")
                if compress == "0" or not compress:
                    add("Sauvegarde", "info", f"Backup sans compression: {job.get('id', 'N/A')}",
                        "Les backups ne sont pas compresses, ce qui consomme plus d'espace.",
                        f"compress={compress}", "Utiliser zstd (meilleur ratio/vitesse)")
    else:
        add("Sauvegarde", "critical", "Aucun job de backup configure",
            "Aucune sauvegarde automatique. Risque de perte de donnees totale.",
            "Non configure", "Datacenter > Backup > Add")

    # Replication
    if isinstance(replication, list) and len(replication) == 0:
        add("Replication", "info", "Aucune replication configuree",
            "La replication ZFS permet de copier les disques VM entre noeuds pour une migration rapide en cas de panne HA.",
            "Non configure", "Envisager la replication pour les VMs critiques si ZFS est utilise")

    # Cluster options
    if isinstance(cluster_opts, dict):
        if not cluster_opts.get("migration"):
            add("Cluster", "info", "Bande passante migration non limitee",
                "Sans limite, une migration live peut saturer le reseau. Definir une limite dans Datacenter > Options.",
                "Illimitee", "Limiter a 70-80% de la bande passante reseau")

    # ── Per-node checks ──
    if not isinstance(nodes_list, list):
        return jsonify(recs)

    for node in nodes_list:
        nn = node.get("node", "")
        if node.get("status") != "online":
            continue

        ns = proxmox_api(host, f"/nodes/{nn}/status")
        if not isinstance(ns, dict) or "error" in ns:
            continue

        cpu_info = ns.get("cpuinfo", {})
        memory = ns.get("memory", {})
        swap = ns.get("swap", {})
        rootfs = ns.get("rootfs", {})
        uptime = ns.get("uptime", 0)

        # IOMMU / Virtualization
        # (detected via CPU flags - basic check)

        # Uptime too long
        if uptime > 90 * 86400:
            days = int(uptime // 86400)
            add("Maintenance", "warning", f"{nn}: Uptime tres long ({days}j)",
                "Un uptime > 90 jours peut signifier des MAJ kernel non appliquees. Planifiez un reboot.",
                f"{days} jours", "Rebooter pour appliquer les mises a jour kernel")

        # Memory
        mem_pct = memory.get("used", 0) / max(memory.get("total", 1), 1) * 100
        mem_total_gb = memory.get("total", 0) / (1024**3)
        if mem_pct > 85:
            add("Performance", "critical" if mem_pct > 95 else "warning",
                f"{nn}: RAM elevee ({mem_pct:.0f}%)",
                "Risque de swap et degradation des performances.",
                f"{fmt_bytes(memory.get('used',0))} / {fmt_bytes(memory.get('total',0))}",
                "Ajouter de la RAM ou migrer des VMs/CTs")

        if mem_total_gb < 8:
            add("Performance", "warning", f"{nn}: RAM faible ({mem_total_gb:.1f} GB)",
                "Proxmox recommande minimum 8 GB de RAM par noeud pour un cluster en production.",
                f"{mem_total_gb:.1f} GB", "8 GB minimum, 16+ GB recommande")

        # Swap
        swap_pct = swap.get("used", 0) / max(swap.get("total", 1), 1) * 100
        if swap_pct > 10:
            add("Performance", "warning", f"{nn}: Swap utilise ({swap_pct:.0f}%)",
                "Le swap actif degrade fortement les performances.",
                f"{fmt_bytes(swap.get('used',0))}", "vm.swappiness=10, ajouter RAM")

        # Disk
        disk_pct = rootfs.get("used", 0) / max(rootfs.get("total", 1), 1) * 100
        if disk_pct > 80:
            add("Stockage", "critical" if disk_pct > 90 else "warning",
                f"{nn}: Disque root a {disk_pct:.0f}%",
                "Risque de saturation. Proxmox a besoin d'espace pour logs, ISO, backups.",
                f"{fmt_bytes(rootfs.get('used',0))} / {fmt_bytes(rootfs.get('total',0))}",
                "Nettoyer backups/snapshots anciens, etendre la partition")

        # APT updates
        updates = proxmox_api(host, f"/nodes/{nn}/apt/update")
        if isinstance(updates, list) and len(updates) > 0:
            security = [u for u in updates if "security" in u.get("Origin", "").lower()]
            if security:
                add("Securite", "critical", f"{nn}: {len(security)} MAJ securite en attente",
                    "Des mises a jour de securite critiques doivent etre appliquees.",
                    f"{len(security)} paquets", "apt update && apt full-upgrade")
            elif len(updates) > 10:
                add("Maintenance", "info", f"{nn}: {len(updates)} MAJ disponibles",
                    "Des mises a jour sont disponibles.",
                    f"{len(updates)} paquets", "apt update && apt full-upgrade")

        # DNS
        dns = proxmox_api(host, f"/nodes/{nn}/dns")
        if isinstance(dns, dict):
            if not dns.get("dns2") and not dns.get("dns3"):
                add("Reseau", "info", f"{nn}: Un seul serveur DNS configure",
                    "Un seul DNS = single point of failure. La resolution sera impossible si le DNS tombe.",
                    dns.get("dns1", "N/A"), "Ajouter dns2=8.8.8.8 ou dns2=9.9.9.9")
            if dns.get("search", "") == "local" or not dns.get("search"):
                add("Reseau", "info", f"{nn}: Domaine de recherche DNS generique",
                    "Le search domain est 'local' ou vide. Configurez votre vrai domaine.",
                    dns.get("search", "vide"), "Definir votre domaine interne (ex: lab.local)")

        # Services critiques
        svcs = proxmox_api(host, f"/nodes/{nn}/services")
        if isinstance(svcs, list):
            critical_down = [s for s in svcs if s.get("state") != "running"
                            and s.get("name") in ("pvedaemon", "pveproxy", "corosync", "pve-cluster")]
            if critical_down:
                add("Services", "critical", f"{nn}: Services critiques arretes",
                    f"Services down: {', '.join(s['name'] for s in critical_down)}. Le noeud ne fonctionne pas correctement.",
                    "Down", "systemctl restart <service>")

            # NTP
            ntp_svc = [s for s in svcs if s.get("name") == "chrony"]
            if ntp_svc and ntp_svc[0].get("state") != "running":
                add("Maintenance", "warning", f"{nn}: chrony (NTP) arrete",
                    "La synchronisation temps est essentielle pour le quorum cluster. chrony doit tourner.",
                    "Arrete", "systemctl enable --now chrony")

        # Storage
        storages = proxmox_api(host, f"/nodes/{nn}/storage")
        if isinstance(storages, list):
            for st in storages:
                if not st.get("active"):
                    continue
                pct = st.get("used", 0) / max(st.get("total", 1), 1) * 100
                if pct > 85:
                    add("Stockage", "warning" if pct < 95 else "critical",
                        f"{nn}: Stockage '{st.get('storage')}' a {pct:.0f}%",
                        f"Type: {st.get('plugintype','')} - Contenu: {st.get('content','')}",
                        f"{fmt_bytes(st.get('used',0))} / {fmt_bytes(st.get('total',0))}",
                        "Liberer de l'espace ou etendre le stockage")

        # Disk health
        disks = proxmox_api(host, f"/nodes/{nn}/disks/list")
        if isinstance(disks, list):
            for d in disks:
                health = d.get("health", "N/A")
                if health not in ("OK", "PASSED", "N/A"):
                    add("Materiel", "critical", f"{nn}: Disque {d.get('devpath','')} en mauvaise sante",
                        f"Modele: {d.get('model','')} - SMART: {health}",
                        health, "Remplacer le disque immediatement !")
                wearout = d.get("wearout", "N/A")
                if wearout != "N/A":
                    try:
                        w = int(str(wearout).replace("%", ""))
                        if w < 20:
                            add("Materiel", "warning",
                                f"{nn}: SSD {d.get('devpath','')} use (wear: {wearout})",
                                f"Le SSD est use a {100-w}%. Planifiez un remplacement.",
                                f"Wearout: {wearout}", "Remplacer le SSD prochainement")
                    except (ValueError, TypeError):
                        pass

        # ── VM-level checks ──
        vms = proxmox_api(host, f"/nodes/{nn}/qemu")
        if isinstance(vms, list):
            for vm in vms:
                vmid = vm.get("vmid")
                vm_name = vm.get("name", f"VM {vmid}")
                vm_cfg = proxmox_api(host, f"/nodes/{nn}/qemu/{vmid}/config")
                if not isinstance(vm_cfg, dict) or "error" in vm_cfg:
                    continue

                # CPU type
                cpu_type = vm_cfg.get("cpu", "kvm64")
                if cpu_type in ("kvm64", "qemu64"):
                    add("VM", "warning", f"{vm_name} ({vmid}): Type CPU non optimal",
                        "kvm64/qemu64 n'expose pas les instructions CPU modernes (AES-NI, AVX). "
                        "Utilisez 'host' pour les meilleures perfs ou 'x86-64-v2-AES' pour la compatibilite migration.",
                        cpu_type, "host (perf) ou x86-64-v2-AES (migration)")

                # SCSI controller
                scsihw = vm_cfg.get("scsihw", "")
                if scsihw and scsihw not in ("virtio-scsi-single", "virtio-scsi-pci"):
                    add("VM", "info", f"{vm_name} ({vmid}): Controleur disque non VirtIO",
                        "VirtIO SCSI Single avec iothread=1 offre les meilleures performances I/O.",
                        scsihw or "defaut", "virtio-scsi-single + iothread=1")

                # BIOS type
                bios = vm_cfg.get("bios", "seabios")
                if bios == "seabios":
                    add("VM", "info", f"{vm_name} ({vmid}): BIOS legacy (SeaBIOS)",
                        "OVMF (UEFI) est recommande pour les OS modernes. Necessaire pour Secure Boot.",
                        "SeaBIOS (legacy)", "OVMF (UEFI) pour Windows 11+, Linux moderne")

                # Machine type
                machine = vm_cfg.get("machine", "")
                if machine and "q35" not in machine:
                    add("VM", "info", f"{vm_name} ({vmid}): Chipset i440fx",
                        "Le chipset Q35 supporte PCIe natif, meilleure performance et compatibilite.",
                        machine or "i440fx", "q35")

                # QEMU Guest Agent
                if not vm_cfg.get("agent"):
                    add("VM", "warning", f"{vm_name} ({vmid}): Guest Agent desactive",
                        "Le QEMU Guest Agent permet: shutdown propre, freeze FS pour snapshots, "
                        "affichage IP dans l'interface. Installez qemu-guest-agent dans la VM.",
                        "Desactive", "agent: 1 + installer qemu-guest-agent dans la VM")

                # Ballooning
                balloon = vm_cfg.get("balloon")
                if balloon is not None and balloon == 0:
                    maxmem_gb = vm.get("maxmem", 0) / (1024**3)
                    if maxmem_gb > 2:
                        add("VM", "info", f"{vm_name} ({vmid}): Ballooning desactive",
                            "Le ballooning permet de recuperer la RAM inutilisee par la VM.",
                            "Desactive", "Activer avec minimum=512MB")

                # Disk discard/iothread
                for key, val in vm_cfg.items():
                    if not isinstance(val, str) or ":" not in val:
                        continue
                    if key.startswith(("scsi", "virtio", "ide", "sata")) and "media" not in val:
                        if "discard=on" not in val:
                            add("VM", "info",
                                f"{vm_name} ({vmid}): TRIM non active sur {key}",
                                "Discard/TRIM recupere l'espace libre dans la VM. Essentiel avec LVM-thin.",
                                "discard=off", "Ajouter discard=on")
                        if "iothread=1" not in val and key.startswith("scsi"):
                            add("VM", "info",
                                f"{vm_name} ({vmid}): iothread non active sur {key}",
                                "iothread dedie par disque ameliore les I/O (necessite virtio-scsi-single).",
                                "iothread=off", "Ajouter iothread=1")

                # Network VirtIO
                for key, val in vm_cfg.items():
                    if key.startswith("net") and isinstance(val, str):
                        if "virtio" not in val.lower() and ("e1000" in val.lower() or "rtl" in val.lower()):
                            add("VM", "warning",
                                f"{vm_name} ({vmid}): Interface reseau non VirtIO ({key})",
                                "VirtIO offre 10x les performances de e1000. Necesssite le driver VirtIO dans la VM.",
                                "e1000/rtl8139", "virtio (installer virtio-win pour Windows)")

                # Numa
                cores = vm_cfg.get("cores", 1)
                sockets = vm_cfg.get("sockets", 1)
                if sockets > 1 and not vm_cfg.get("numa"):
                    add("VM", "info", f"{vm_name} ({vmid}): NUMA non active avec multi-socket",
                        "Avec plusieurs sockets CPU, NUMA optimise l'acces memoire.",
                        "NUMA off", "Activer numa: 1")

        # ── Container checks ──
        cts = proxmox_api(host, f"/nodes/{nn}/lxc")
        if isinstance(cts, list):
            for ct in cts:
                ctid = ct.get("vmid")
                ct_name = ct.get("name", f"CT {ctid}")
                ct_cfg = proxmox_api(host, f"/nodes/{nn}/lxc/{ctid}/config")
                if not isinstance(ct_cfg, dict) or "error" in ct_cfg:
                    continue

                if not ct_cfg.get("unprivileged"):
                    add("CT", "warning", f"{ct_name} ({ctid}): Container privilegie",
                        "Un container privilegie partage le meme user namespace que l'hote. "
                        "Un container non-privilegie est beaucoup plus securise.",
                        "Privilegie", "Recreer en mode unprivileged si possible")

                if not ct_cfg.get("swap", 512):
                    pass  # swap 0 is fine for containers

    # ── Best practices generales ──
    add("Best Practice", "info", "Sauvegardes testees",
        "Verifiez regulierement que vos backups sont restaurables. Un backup non teste = pas de backup.",
        "A verifier", "Tester une restauration chaque mois")

    add("Best Practice", "info", "Supervision externe",
        "Mettez en place un monitoring externe (Zabbix, PRTG, Uptime Kuma) pour etre alerte si le cluster tombe.",
        "A verifier", "Configurer des alertes email/SMS")

    add("Best Practice", "info", "Documentation reseau",
        "Documentez les IPs, VLANs, et la topologie reseau du cluster pour faciliter le depannage.",
        "A verifier", "Tenir a jour un schema reseau")

    add("Best Practice", "info", "Compte monitoring dedie",
        "Utilisez un compte PVE dedie avec des droits limites (PVEAuditor) plutot que root pour le monitoring.",
        "root@pam", "Creer un user monitoring@pve avec role PVEAuditor")

    return jsonify(recs)


# ── Installation Agent QEMU via SSH ─────────────────────────────────────────

@app.route("/api/install-agent", methods=["POST"])
def api_install_agent():
    """Installe qemu-guest-agent sur une VM Linux via SSH."""
    data = flask_request.get_json()
    if not data:
        return jsonify({"success": False, "error": "Donnees manquantes"}), 400

    ip = data.get("ip", "").strip()
    username = data.get("username", "root").strip()
    password = data.get("password", "").strip()
    node = data.get("node", "")
    vmid = data.get("vmid", "")

    if not ip or not password:
        return jsonify({"success": False, "error": "IP et mot de passe requis"}), 400

    log_lines = []

    def log(msg):
        log_lines.append(msg)

    try:
        # Connect SSH
        log(f"Connexion SSH a {username}@{ip}...")
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(ip, port=22, username=username, password=password, timeout=10)
        log("Connexion SSH reussie.")

        # Detect OS
        log("Detection de la distribution...")
        _, stdout, _ = ssh.exec_command("cat /etc/os-release 2>/dev/null || cat /etc/redhat-release 2>/dev/null || uname -s", timeout=10)
        os_info = stdout.read().decode("utf-8", errors="replace").lower()

        distro = "unknown"
        install_cmd = ""
        enable_cmd = "systemctl enable --now qemu-guest-agent"

        if "debian" in os_info or "ubuntu" in os_info or "mint" in os_info:
            distro = "debian"
            install_cmd = "DEBIAN_FRONTEND=noninteractive apt-get update -qq && apt-get install -y -qq qemu-guest-agent"
        elif "centos" in os_info or "red hat" in os_info or "rhel" in os_info or "rocky" in os_info or "alma" in os_info or "oracle" in os_info:
            distro = "rhel"
            install_cmd = "yum install -y qemu-guest-agent || dnf install -y qemu-guest-agent"
        elif "fedora" in os_info:
            distro = "fedora"
            install_cmd = "dnf install -y qemu-guest-agent"
        elif "suse" in os_info or "sles" in os_info:
            distro = "suse"
            install_cmd = "zypper install -y qemu-guest-agent"
        elif "arch" in os_info:
            distro = "arch"
            install_cmd = "pacman -S --noconfirm qemu-guest-agent"
        elif "alpine" in os_info:
            distro = "alpine"
            install_cmd = "apk add qemu-guest-agent"
            enable_cmd = "rc-update add qemu-guest-agent default && rc-service qemu-guest-agent start"
        else:
            ssh.close()
            log(f"Distribution non reconnue: {os_info[:100]}")
            return jsonify({
                "success": False,
                "error": "Distribution Linux non reconnue",
                "os_info": os_info[:200],
                "log": log_lines,
            })

        log(f"Distribution detectee: {distro}")

        # Check if already installed
        log("Verification si deja installe...")
        _, stdout, _ = ssh.exec_command("which qemu-ga 2>/dev/null || which qemu-guest-agent 2>/dev/null || dpkg -l qemu-guest-agent 2>/dev/null | grep '^ii' || rpm -q qemu-guest-agent 2>/dev/null", timeout=10)
        already = stdout.read().decode("utf-8", errors="replace").strip()
        if already and "not installed" not in already:
            log("qemu-guest-agent est deja installe ! Activation...")
            _, stdout, stderr = ssh.exec_command(enable_cmd, timeout=30)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            log(f"Activation: {out.strip()} {err.strip()}")
            ssh.close()
            return jsonify({
                "success": True,
                "message": "qemu-guest-agent etait deja installe, il a ete active.",
                "distro": distro,
                "log": log_lines,
            })

        # Install
        log(f"Installation en cours ({install_cmd[:50]}...)...")
        _, stdout, stderr = ssh.exec_command(install_cmd, timeout=120)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()

        if exit_code != 0:
            log(f"Erreur installation (code {exit_code}): {err[:300]}")
            ssh.close()
            return jsonify({
                "success": False,
                "error": f"Installation echouee (code {exit_code})",
                "detail": err[:500],
                "distro": distro,
                "log": log_lines,
            })

        log("Installation reussie. Activation du service...")

        # Enable and start
        _, stdout, stderr = ssh.exec_command(enable_cmd, timeout=30)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        log(f"Service active: {out.strip()} {err.strip()}")

        # Verify
        log("Verification du service...")
        _, stdout, _ = ssh.exec_command("systemctl is-active qemu-guest-agent 2>/dev/null || rc-service qemu-guest-agent status 2>/dev/null", timeout=10)
        status = stdout.read().decode("utf-8", errors="replace").strip()
        log(f"Statut service: {status}")

        ssh.close()

        # Also enable agent in Proxmox VM config if we have node/vmid
        if node and vmid:
            host_api, ticket, csrf = get_ticket()
            if host_api and ticket:
                purl = f"https://{host_api}:{PROXMOX_CLUSTER['port']}/api2/json/nodes/{node}/qemu/{vmid}/config"
                try:
                    requests.put(purl,
                        headers={"CSRFPreventionToken": csrf},
                        cookies={"PVEAuthCookie": ticket},
                        data={"agent": "1"},
                        verify=False, timeout=5)
                    log("Configuration Proxmox mise a jour (agent=1)")
                except Exception:
                    log("Impossible de mettre a jour la config Proxmox")

        return jsonify({
            "success": True,
            "message": f"qemu-guest-agent installe et active avec succes sur {distro}",
            "distro": distro,
            "service_status": status,
            "log": log_lines,
        })

    except paramiko.AuthenticationException:
        log("Echec authentification SSH")
        return jsonify({"success": False, "error": "Authentification SSH echouee. Verifiez login/mot de passe.", "log": log_lines})
    except paramiko.SSHException as e:
        log(f"Erreur SSH: {e}")
        return jsonify({"success": False, "error": f"Erreur SSH: {e}", "log": log_lines})
    except TimeoutError:
        log("Timeout connexion SSH")
        return jsonify({"success": False, "error": "Timeout: impossible de se connecter en SSH. Verifiez que SSH est actif sur la VM.", "log": log_lines})
    except Exception as e:
        log(f"Erreur: {e}")
        return jsonify({"success": False, "error": str(e), "log": log_lines})


# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  Cluster Manager")
    print("  Copyright (c) 2026 Ayi NEDJIMI Consultants")
    print("=" * 60)
    print(f"  URL: http://localhost:{WEB_PORT}")
    print(f"  Cluster: {', '.join(PROXMOX_CLUSTER['hosts'])}")
    print("=" * 60)
    app.run(host=WEB_HOST, port=WEB_PORT, debug=WEB_DEBUG)
