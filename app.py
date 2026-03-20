# ============================================================================
#  Cluster Manager
#  Copyright (c) 2026 Ayi NEDJIMI Consultants - Tous droits reserves
# ============================================================================

from flask import Flask, render_template, jsonify, request as flask_request
import requests
import urllib3
import time
import paramiko
import sqlite3
import json as json_lib
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
from config import (
    PROXMOX_CLUSTER, REFRESH_INTERVAL,
    SYSLOG_LINES, TASK_LIMIT, CLUSTER_LOG_MAX,
    WEB_HOST, WEB_PORT, WEB_DEBUG,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# SQLite for benchmark history
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "benchmarks.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS benchmarks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT DEFAULT (datetime('now','localtime')),
        node TEXT, node_ip TEXT, bench_type TEXT,
        results TEXT, raw TEXT
    )""")
    conn.commit()
    conn.close()


init_db()

_auth_cache = {"ticket": None, "csrf": None, "host": None, "expires": 0}

# Status cache - avoid hitting Proxmox API on every browser refresh
_status_cache = {"data": None, "time": 0, "lock": threading.Lock()}
STATUS_CACHE_TTL = 5  # seconds


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
    # Return cached data if fresh (skip if nocache param)
    now = time.time()
    force = flask_request.args.get("nocache")
    if not force:
        with _status_cache["lock"]:
            if _status_cache["data"] and now - _status_cache["time"] < STATUS_CACHE_TTL:
                return jsonify(_status_cache["data"])

    result = _build_status()
    if isinstance(result, tuple):
        return result

    with _status_cache["lock"]:
        _status_cache["data"] = result
        _status_cache["time"] = time.time()

    return jsonify(result)


def _build_status():
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

    def _fetch_node(node):
        """Fetch all data for one node (runs in thread)."""
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
            return node_data

        # Parallel fetch all node-level data
        node_calls = {
            "status": f"/nodes/{node_name}/status",
            "rrd": f"/nodes/{node_name}/rrddata?timeframe=hour",
            "services": f"/nodes/{node_name}/services",
            "disks": f"/nodes/{node_name}/disks/list",
            "network": f"/nodes/{node_name}/network",
            "qemu": f"/nodes/{node_name}/qemu",
            "lxc": f"/nodes/{node_name}/lxc",
            "storage": f"/nodes/{node_name}/storage",
        }
        node_results = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(proxmox_api, host, ep): key for key, ep in node_calls.items()}
            for f in as_completed(futs):
                try:
                    node_results[futs[f]] = f.result()
                except Exception:
                    pass

        ns = node_results.get("status", {})
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

        rrd = node_results.get("rrd")
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

        svcs = node_results.get("services")
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

        disks = node_results.get("disks")
        if isinstance(disks, list):
            for d in disks:
                node_data["disks"].append({
                    "devpath": d.get("devpath", ""), "model": d.get("model", "N/A"),
                    "size": fmt_bytes(d.get("size", 0)), "health": d.get("health", "N/A"),
                    "wearout": d.get("wearout", "N/A"), "serial": d.get("serial", "")[:16],
                })

        nets = node_results.get("network")
        if isinstance(nets, list):
            for n in nets:
                if n.get("active") and n.get("address"):
                    node_data["network_interfaces"].append({
                        "iface": n.get("iface", ""), "type": n.get("type", ""),
                        "address": n.get("address", ""), "netmask": n.get("netmask", ""),
                        "gateway": n.get("gateway", ""), "bridge_ports": n.get("bridge_ports", ""),
                    })

        for vm in (node_results.get("qemu") or []):
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

            # Try guest agent for running VMs (all calls in parallel)
            if vm.get("status") == "running" and vm_entry["agent_enabled"]:
                base = f"/nodes/{node_name}/qemu/{vmid}"
                agent_calls = {
                    "osinfo": f"{base}/agent/get-osinfo",
                    "hostname": f"{base}/agent/get-host-name",
                    "timezone": f"{base}/agent/get-timezone",
                    "network": f"{base}/agent/network-get-interfaces",
                    "fsinfo": f"{base}/agent/get-fsinfo",
                    "vcpus": f"{base}/agent/get-vcpus",
                    "rrd": f"{base}/rrddata?timeframe=hour",
                }
                agent_results = {}
                with ThreadPoolExecutor(max_workers=7) as pool:
                    futs = {pool.submit(proxmox_api, host, ep): key for key, ep in agent_calls.items()}
                    for f in as_completed(futs):
                        try:
                            agent_results[futs[f]] = f.result()
                        except Exception:
                            pass

                agent_info = agent_results.get("osinfo", {})
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

                    hn = agent_results.get("hostname", {})
                    if isinstance(hn, dict) and "error" not in hn:
                        vm_entry["guest_agent"]["hostname"] = hn.get("result", {}).get("host-name", "")

                    tz = agent_results.get("timezone", {})
                    if isinstance(tz, dict) and "error" not in tz:
                        vm_entry["guest_agent"]["timezone"] = tz.get("result", {}).get("zone", "")

                    net_ifaces = agent_results.get("network", {})
                    if isinstance(net_ifaces, dict) and "error" not in net_ifaces:
                        ifaces = []
                        for iface in net_ifaces.get("result", []):
                            if iface.get("name") == "lo":
                                continue
                            ips = [addr.get("ip-address", "") for addr in iface.get("ip-addresses", []) if addr.get("ip-address-type") == "ipv4"]
                            stats = iface.get("statistics", {})
                            ifaces.append({
                                "name": iface.get("name", ""), "mac": iface.get("hardware-address", ""), "ips": ips,
                                "rx_bytes": fmt_bytes(stats.get("rx-bytes", 0)), "tx_bytes": fmt_bytes(stats.get("tx-bytes", 0)),
                                "rx_errs": stats.get("rx-errs", 0), "tx_errs": stats.get("tx-errs", 0), "rx_dropped": stats.get("rx-dropped", 0),
                            })
                        vm_entry["guest_agent"]["interfaces"] = ifaces

                    fs_info = agent_results.get("fsinfo", {})
                    if isinstance(fs_info, dict) and "error" not in fs_info:
                        filesystems = []
                        for fs in fs_info.get("result", []):
                            total = fs.get("total-bytes", 0)
                            used = fs.get("used-bytes", 0)
                            if total <= 0 or fs.get("type") in ("squashfs", "iso9660", "tmpfs", "devtmpfs"):
                                continue
                            filesystems.append({"mount": fs.get("mountpoint", ""), "name": fs.get("name", ""), "type": fs.get("type", ""),
                                "total": fmt_bytes(total), "used": fmt_bytes(used), "free": fmt_bytes(max(total - used, 0)),
                                "pct": round(used / max(total, 1) * 100, 1)})
                        vm_entry["guest_agent"]["filesystems"] = filesystems

                    vcpus = agent_results.get("vcpus", {})
                    if isinstance(vcpus, dict) and "error" not in vcpus:
                        result_vcpus = vcpus.get("result", [])
                        vm_entry["guest_agent"]["vcpus_online"] = sum(1 for v in result_vcpus if v.get("online"))
                        vm_entry["guest_agent"]["vcpus_total"] = len(result_vcpus)

                    vm_rrd = agent_results.get("rrd")
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

        for ct in (node_results.get("lxc") or []):
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

        storages = node_results.get("storage")
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

        return node_data

    # Fetch all nodes in parallel
    nodes = []
    with ThreadPoolExecutor(max_workers=len(nodes_list)) as executor:
        futures = {executor.submit(_fetch_node, n): n for n in sorted(nodes_list, key=lambda n: n.get("node", ""))}
        for future in as_completed(futures):
            try:
                nodes.append(future.result())
            except Exception:
                pass
    nodes.sort(key=lambda n: n.get("name", ""))

    return {"cluster": cluster_info, "api_host": host, "nodes": nodes}


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


# ── Stockage detaille ───────────────────────────────────────────────────────

def parse_disk_size(size_str):
    """Parse une taille de disque Proxmox (ex: '32G', '2098201K', '500M') en bytes."""
    if not size_str:
        return 0
    size_str = str(size_str).strip()
    try:
        if size_str.endswith("K"):
            return int(size_str[:-1]) * 1024
        elif size_str.endswith("M"):
            return int(size_str[:-1]) * 1024 * 1024
        elif size_str.endswith("G"):
            return int(size_str[:-1]) * 1024 * 1024 * 1024
        elif size_str.endswith("T"):
            return int(size_str[:-1]) * 1024 * 1024 * 1024 * 1024
        else:
            return int(size_str)
    except (ValueError, TypeError):
        return 0


@app.route("/api/storage")
def api_storage():
    """Analyse detaillee du stockage avec detection thin provisioning."""
    host, ticket, _ = get_ticket()
    if not host:
        return jsonify({"error": "Non connecte"}), 503

    nodes_list = proxmox_api(host, "/nodes")
    if not isinstance(nodes_list, list):
        return jsonify({"error": "Impossible de lister les noeuds"}), 503

    result = {"storages": [], "alerts": []}

    for node in sorted(nodes_list, key=lambda n: n.get("node", "")):
        nn = node.get("node", "")
        if node.get("status") != "online":
            continue

        # Get storages
        storages = proxmox_api(host, f"/nodes/{nn}/storage")
        if not isinstance(storages, list):
            continue

        for st in storages:
            if not st.get("active"):
                continue

            storage_name = st.get("storage", "")
            storage_type = st.get("type", "")
            plugintype = st.get("plugintype", storage_type)
            total = st.get("total", 0)
            used = st.get("used", 0)
            avail = st.get("avail", 0)
            is_thin = plugintype in ("lvmthin", "zfspool", "rbd", "cephfs")

            used_fraction = st.get("used_fraction", 0)

            storage_data = {
                "node": nn,
                "storage": storage_name,
                "type": storage_type,
                "plugintype": plugintype,
                "total": total,
                "total_fmt": fmt_bytes(total),
                "used": used,
                "used_fmt": fmt_bytes(used),
                "used_fraction": round(used_fraction * 100, 1),
                "avail": avail,
                "avail_fmt": fmt_bytes(avail),
                "pct": round(used / max(total, 1) * 100, 1),
                "content": st.get("content", ""),
                "shared": bool(st.get("shared", 0)),
                "is_thin": is_thin,
                "volumes": [],
                "vol_count": 0,
                "provisioned_total": 0,
                "provisioned_fmt": "0 B",
                "provisioned_pct": 0,
                "overcommit": False,
                "free": max(total - used, 0),
                "free_fmt": fmt_bytes(max(total - used, 0)),
            }

            # List volumes in this storage
            content = proxmox_api(host, f"/nodes/{nn}/storage/{storage_name}/content")
            if isinstance(content, list):
                total_provisioned = 0
                for vol in content:
                    vol_size = vol.get("size", 0)
                    vol_used = vol.get("used", vol_size)  # used may differ from size for thin
                    total_provisioned += vol_size
                    vol_entry = {
                        "volid": vol.get("volid", ""),
                        "vmid": vol.get("vmid", ""),
                        "format": vol.get("format", ""),
                        "content": vol.get("content", ""),
                        "size": vol_size,
                        "size_fmt": fmt_bytes(vol_size),
                    }
                    storage_data["volumes"].append(vol_entry)

                storage_data["vol_count"] = len(content)
                storage_data["provisioned_total"] = total_provisioned
                storage_data["provisioned_fmt"] = fmt_bytes(total_provisioned)

                if total > 0:
                    storage_data["provisioned_pct"] = round(total_provisioned / total * 100, 1)
                    storage_data["overcommit"] = total_provisioned > total

                # For thin: if used=0 but volumes exist, use provisioned as "allocated"
                if is_thin and used == 0 and total_provisioned > 0:
                    storage_data["used"] = total_provisioned
                    storage_data["used_fmt"] = fmt_bytes(total_provisioned)
                    storage_data["pct"] = round(total_provisioned / max(total, 1) * 100, 1)
                    storage_data["note"] = "Usage estime via volumes provisionnes"

            # For thin storages, also check VM configs for disk max sizes
            if is_thin and "images" in st.get("content", ""):
                # Get all VMs and CTs on this node and sum their disk sizes on this storage
                vm_disk_max_total = 0
                vm_disk_details = []

                for vm in (proxmox_api(host, f"/nodes/{nn}/qemu") or []):
                    if not isinstance(vm, dict):
                        continue
                    vmid = vm.get("vmid")
                    vm_name = vm.get("name", f"VM {vmid}")
                    vm_cfg = proxmox_api(host, f"/nodes/{nn}/qemu/{vmid}/config")
                    if not isinstance(vm_cfg, dict) or "error" in vm_cfg:
                        continue

                    for key, val in vm_cfg.items():
                        if not isinstance(val, str) or ":" not in val:
                            continue
                        if not any(key.startswith(p) for p in ("scsi", "virtio", "ide", "sata", "efidisk")):
                            continue
                        if "media=cdrom" in val:
                            continue
                        # Check if this disk is on this storage
                        if val.startswith(f"{storage_name}:"):
                            # Parse size from config
                            size_val = 0
                            for part in val.split(","):
                                if part.startswith("size="):
                                    size_val = parse_disk_size(part.split("=")[1])
                                    break
                            if size_val > 0:
                                vm_disk_max_total += size_val
                                vm_disk_details.append({
                                    "vmid": vmid,
                                    "name": vm_name,
                                    "disk": key,
                                    "max_size": size_val,
                                    "max_size_fmt": fmt_bytes(size_val),
                                    "type": "qemu",
                                })

                for ct in (proxmox_api(host, f"/nodes/{nn}/lxc") or []):
                    if not isinstance(ct, dict):
                        continue
                    ctid = ct.get("vmid")
                    ct_name = ct.get("name", f"CT {ctid}")
                    ct_cfg = proxmox_api(host, f"/nodes/{nn}/lxc/{ctid}/config")
                    if not isinstance(ct_cfg, dict) or "error" in ct_cfg:
                        continue

                    for key, val in ct_cfg.items():
                        if not isinstance(val, str) or ":" not in val:
                            continue
                        if not (key == "rootfs" or key.startswith("mp")):
                            continue
                        if val.startswith(f"{storage_name}:"):
                            size_val = 0
                            for part in val.split(","):
                                if part.startswith("size="):
                                    size_val = parse_disk_size(part.split("=")[1])
                                    break
                            if size_val > 0:
                                vm_disk_max_total += size_val
                                vm_disk_details.append({
                                    "vmid": ctid,
                                    "name": ct_name,
                                    "disk": key,
                                    "max_size": size_val,
                                    "max_size_fmt": fmt_bytes(size_val),
                                    "type": "lxc",
                                })

                storage_data["vm_provisioned_total"] = vm_disk_max_total
                storage_data["vm_provisioned_fmt"] = fmt_bytes(vm_disk_max_total)
                storage_data["vm_disk_details"] = vm_disk_details

                if total > 0 and vm_disk_max_total > 0:
                    storage_data["vm_overcommit_pct"] = round(vm_disk_max_total / total * 100, 1)
                    storage_data["vm_overcommit"] = vm_disk_max_total > total
                    real_avail = total - vm_disk_max_total
                    storage_data["real_avail"] = max(real_avail, 0)
                    storage_data["real_avail_fmt"] = fmt_bytes(max(real_avail, 0))
                    storage_data["real_avail_negative"] = real_avail < 0

                    if vm_disk_max_total > total:
                        result["alerts"].append({
                            "level": "critical",
                            "msg": f"{nn}/{storage_name}: Overcommit thin provisioning ! "
                                   f"VMs provisionees: {fmt_bytes(vm_disk_max_total)} > "
                                   f"Capacite: {fmt_bytes(total)} "
                                   f"({storage_data['vm_overcommit_pct']}%)",
                        })
                    elif vm_disk_max_total > total * 0.8:
                        result["alerts"].append({
                            "level": "warning",
                            "msg": f"{nn}/{storage_name}: Thin provisioning a {storage_data['vm_overcommit_pct']}% "
                                   f"de la capacite ({fmt_bytes(vm_disk_max_total)} / {fmt_bytes(total)})",
                        })

            result["storages"].append(storage_data)

    return jsonify(result)


# ── Optimisations ───────────────────────────────────────────────────────────

@app.route("/api/optimizations")
def api_optimizations():
    """Checklist d'optimisations pour tous les noeuds et VMs."""
    host, ticket, _ = get_ticket()
    if not host:
        return jsonify({"error": "Non connecte"}), 503

    checks = []

    def chk(cat, target, name, ok, current, recommended, howto):
        checks.append({"cat": cat, "target": target, "name": name,
                       "ok": ok, "current": str(current),
                       "recommended": str(recommended), "howto": howto})

    nodes_list = proxmox_api(host, "/nodes")
    if not isinstance(nodes_list, list):
        return jsonify(checks)

    # Get node IPs from cluster status
    cluster_info = {"nodes_info": []}
    cluster_status = proxmox_api(host, "/cluster/status")
    if isinstance(cluster_status, list):
        for item in cluster_status:
            if item.get("type") == "node":
                cluster_info["nodes_info"].append({
                    "name": item.get("name"), "ip": item.get("ip"),
                })

    for node in sorted(nodes_list, key=lambda n: n.get("node", "")):
        nn = node.get("node", "")
        if node.get("status") != "online":
            continue

        ns = proxmox_api(host, f"/nodes/{nn}/status")
        if not isinstance(ns, dict) or "error" in ns:
            continue

        cpu_info = ns.get("cpuinfo", {})
        cpu_flags = cpu_info.get("flags", "")
        memory = ns.get("memory", {})
        mem_total_gb = memory.get("total", 0) / (1024**3)

        # ── NODE-LEVEL CHECKS ──

        # HVM support
        chk("CPU", nn, "Virtualisation materielle (HVM)",
            bool(cpu_info.get("hvm")), "HVM actif" if cpu_info.get("hvm") else "HVM absent",
            "Activer VT-x/AMD-V dans le BIOS",
            "BIOS > Advanced > CPU > Intel VT-x ou AMD SVM: Enable")

        # Nested virtualization
        has_nested = "vmx" in cpu_flags or "svm" in cpu_flags
        chk("CPU", nn, "Nested virtualization disponible",
            has_nested, "Disponible" if has_nested else "Non disponible",
            "Activer nested virt pour VMs qui hebergent des hyperviseurs",
            "echo 1 > /sys/module/kvm_intel/parameters/nested (Intel) ou kvm_amd (AMD)")

        # AES-NI
        has_aes = "aes" in cpu_flags
        chk("CPU", nn, "AES-NI (acceleration chiffrement)",
            has_aes, "Present" if has_aes else "Absent",
            "Necessaire pour chiffrement performant (LUKS, TLS)",
            "Fonction CPU materielle, non activable par logiciel")

        # AVX2
        has_avx2 = "avx2" in cpu_flags
        chk("CPU", nn, "Instructions AVX2",
            has_avx2, "Present" if has_avx2 else "Absent",
            "Acceleration calcul pour Ceph, ZFS, compression",
            "Fonction CPU materielle")

        # RAM > 8GB
        chk("Memoire", nn, "RAM >= 8 GB",
            mem_total_gb >= 8, f"{mem_total_gb:.1f} GB",
            "8 GB minimum, 16+ GB recommande pour production",
            "Ajouter des barrettes RAM")

        # KSM
        ksm_shared = ns.get("ksm", {}).get("shared", 0)
        chk("Memoire", nn, "KSM (Kernel Samepage Merging) actif",
            ksm_shared > 0 or True,  # KSM service running is enough
            f"Partage: {fmt_bytes(ksm_shared)}" if ksm_shared > 0 else "Actif (0 pages partagees)",
            "KSM deduplique la RAM entre VMs identiques",
            "Actif par defaut sur Proxmox. ksmtuned gere automatiquement.")

        # Network MTU (jumbo frames)
        nets = proxmox_api(host, f"/nodes/{nn}/network")
        if isinstance(nets, list):
            for n in nets:
                if n.get("type") == "bridge" and n.get("active"):
                    mtu_raw = n.get("mtu", "") or ""
                    mtu = int(mtu_raw) if str(mtu_raw).isdigit() else 1500
                    iface = n.get("iface", "")
                    chk("Reseau", nn, f"MTU {iface}",
                        mtu >= 9000,
                        f"MTU={mtu}",
                        "MTU 9000 (jumbo frames) pour meilleures performances reseau/Ceph",
                        f"Node > Network > {iface} > MTU: 9000. ATTENTION: tous les equipements doivent supporter jumbo frames.")

        # Boot mode
        boot_mode = ns.get("boot-info", {}).get("mode", "")
        chk("Systeme", nn, "Boot mode UEFI",
            boot_mode == "efi", boot_mode or "inconnu",
            "UEFI recommande pour Secure Boot et fonctionnalites modernes",
            "Reinstaller en mode UEFI si necessaire")

        # ── NTP + ZFS (via SSH) ──
        try:
            node_ip = None
            for ni in cluster_info.get("nodes_info", []):
                if ni.get("name") == nn:
                    node_ip = ni.get("ip")
            if not node_ip:
                node_ip = PROXMOX_CLUSTER["hosts"][0]

            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(node_ip,
                        username=PROXMOX_CLUSTER["username"].split("@")[0],
                        password=PROXMOX_CLUSTER["password"], timeout=5)

            # ── NTP CHECK ──
            _, stdout, _ = ssh.exec_command("chronyc tracking 2>/dev/null", timeout=5)
            chrony_out = stdout.read().decode("utf-8", errors="replace")
            _, stdout, _ = ssh.exec_command("chronyc sources 2>/dev/null | grep '\\^' | wc -l", timeout=5)
            ntp_count = stdout.read().decode().strip()
            ntp_count = int(ntp_count) if ntp_count.isdigit() else 0

            ntp_active = bool(chrony_out.strip())
            ntp_synced = False
            ntp_drift = 0
            ntp_stratum = 0
            for line in chrony_out.split("\n"):
                if "System time" in line:
                    import re as _re
                    m = _re.search(r'([\d.]+) seconds', line)
                    if m:
                        ntp_drift = float(m.group(1))
                        ntp_synced = ntp_drift < 1
                if "Stratum" in line:
                    try:
                        ntp_stratum = int(line.split(":")[1].strip())
                    except (ValueError, IndexError):
                        pass

            chk("NTP", nn, "Chrony (NTP) actif",
                ntp_active, "Actif" if ntp_active else "Inactif !",
                "Chrony doit etre actif pour la synchronisation du cluster",
                "systemctl enable --now chrony")

            chk("NTP", nn, f"Sources NTP configurees ({ntp_count})",
                ntp_count >= 2,
                f"{ntp_count} source(s)",
                "Au moins 2 sources NTP pour la redondance",
                "Editer /etc/chrony/chrony.conf, ajouter: server ntp.ubuntu.com iburst")

            chk("NTP", nn, "Synchronisation NTP",
                ntp_synced,
                f"Drift: {ntp_drift*1000:.1f} ms" if ntp_active else "Non synchronise",
                "Le drift doit etre < 1 seconde pour le quorum Corosync",
                "chronyc makestep pour forcer une synchro immediate")

            if ntp_active and ntp_drift > 0.1:
                chk("NTP", nn, "Drift excessif",
                    False,
                    f"{ntp_drift*1000:.1f} ms",
                    "Un drift > 100ms peut causer des problemes de cluster",
                    "Verifier la source NTP: chronyc sources -v")

            # Check if ZFS module is loaded
            _, stdout, _ = ssh.exec_command("cat /sys/module/zfs/parameters/zfs_arc_max 2>/dev/null", timeout=5)
            arc_max_str = stdout.read().decode().strip()

            _, stdout, _ = ssh.exec_command("zpool list -H 2>/dev/null", timeout=5)
            zpool_out = stdout.read().decode().strip()

            _, stdout, _ = ssh.exec_command("cat /proc/spl/kstat/zfs/arcstats 2>/dev/null | grep -E '^(size|c_max|c_min|hits|misses)' | awk '{print $1,$3}'", timeout=5)
            arc_stats_raw = stdout.read().decode().strip()

            _, stdout, _ = ssh.exec_command("cat /etc/modprobe.d/zfs.conf 2>/dev/null", timeout=5)
            zfs_conf = stdout.read().decode().strip()

            ssh.close()

            has_zfs = bool(arc_max_str)
            has_pools = bool(zpool_out)
            arc_max = int(arc_max_str) if arc_max_str.isdigit() else 0
            mem_total_bytes = memory.get("total", 0)

            if has_zfs:
                # ZFS is loaded
                chk("ZFS", nn, "Module ZFS charge",
                    True, "Charge",
                    "ZFS disponible pour stockage haute performance",
                    "Module ZFS charge automatiquement sur Proxmox")

                if has_pools:
                    chk("ZFS", nn, "Pools ZFS actifs",
                        True, zpool_out.split("\n")[0] if zpool_out else "Actifs",
                        "Au moins un pool ZFS est configure",
                        "zpool list pour voir les pools")
                else:
                    chk("ZFS", nn, "Pools ZFS actifs",
                        True,  # informational, not a failure
                        "Aucun pool (LVM utilise)",
                        "ZFS charge mais pas de pool. Normal si vous utilisez LVM.",
                        "zpool create <pool> <device> pour creer un pool")

                # ARC max check
                if arc_max > 0:
                    arc_max_gb = arc_max / (1024**3)
                    arc_pct_of_ram = round(arc_max / max(mem_total_bytes, 1) * 100, 1)

                    # Best practice: ARC should be 50% of RAM if ZFS is used for VMs
                    # If no pools, ARC should be minimal to save RAM for VMs
                    if has_pools:
                        is_good = 25 <= arc_pct_of_ram <= 75
                        chk("ZFS", nn, f"ARC cache max ({arc_max_gb:.1f} GB = {arc_pct_of_ram}% RAM)",
                            is_good,
                            f"{arc_max_gb:.1f} GB ({arc_pct_of_ram}% de {mem_total_gb:.0f} GB RAM)",
                            "Avec pools ZFS actifs: ARC entre 25-50% de la RAM pour equilibrer cache et VMs",
                            "echo 'options zfs zfs_arc_max=BYTES' > /etc/modprobe.d/zfs.conf && update-initramfs -u. "
                            f"Recommande: {int(mem_total_bytes * 0.5)} bytes ({mem_total_gb * 0.5:.0f} GB)")
                    else:
                        # No pools: ARC should be minimal
                        is_good = arc_pct_of_ram <= 15
                        chk("ZFS", nn, f"ARC cache max ({arc_max_gb:.1f} GB = {arc_pct_of_ram}% RAM)",
                            is_good,
                            f"{arc_max_gb:.1f} GB ({arc_pct_of_ram}% de {mem_total_gb:.0f} GB RAM)",
                            "Sans pool ZFS actif: limiter ARC au minimum pour liberer la RAM aux VMs",
                            "echo 'options zfs zfs_arc_max=134217728' > /etc/modprobe.d/zfs.conf && update-initramfs -u "
                            "(128 MB minimum)")

                    # Persistent config check
                    has_persistent = "zfs_arc_max" in zfs_conf
                    chk("ZFS", nn, "ARC max configure de facon persistante",
                        has_persistent,
                        "/etc/modprobe.d/zfs.conf present" if has_persistent else "Non persistant",
                        "La config ARC doit etre dans /etc/modprobe.d/zfs.conf pour survivre aux reboots",
                        f"echo 'options zfs zfs_arc_max={arc_max}' > /etc/modprobe.d/zfs.conf && update-initramfs -u")
                else:
                    chk("ZFS", nn, "ARC cache max defini",
                        False, "Non defini (0 = illimite !)",
                        "ATTENTION: sans limite, ARC peut consommer toute la RAM et affamer les VMs !",
                        f"echo 'options zfs zfs_arc_max={int(mem_total_bytes * 0.5)}' > /etc/modprobe.d/zfs.conf && update-initramfs -u")

                # ARC hit ratio (if available)
                arc_stats = {}
                for line in arc_stats_raw.split("\n"):
                    parts = line.split()
                    if len(parts) == 2:
                        arc_stats[parts[0]] = int(parts[1]) if parts[1].isdigit() else 0

                hits = arc_stats.get("hits", 0)
                misses = arc_stats.get("misses", 0)
                if hits + misses > 100:
                    hit_ratio = round(hits / (hits + misses) * 100, 1)
                    chk("ZFS", nn, f"ARC hit ratio ({hit_ratio}%)",
                        hit_ratio >= 80,
                        f"{hit_ratio}% (hits={hits}, misses={misses})",
                        "Un ratio > 80% est bon. Si trop bas, augmenter zfs_arc_max",
                        "Augmenter la taille ARC ou ajouter un L2ARC (SSD cache)")

            # ── IOMMU / PASSTHROUGH ──
            _, stdout, _ = ssh.exec_command("dmesg 2>/dev/null | grep -i 'IOMMU\\|DMAR\\|AMD-Vi' | head -3", timeout=5)
            iommu_dmesg = stdout.read().decode().strip()
            _, stdout, _ = ssh.exec_command("cat /proc/cmdline 2>/dev/null", timeout=5)
            cmdline = stdout.read().decode().strip()

            iommu_enabled = "iommu=pt" in cmdline or "intel_iommu=on" in cmdline or "amd_iommu=on" in cmdline
            chk("IOMMU/Passthrough", nn, "IOMMU active (iommu=pt)",
                iommu_enabled, "Actif" if iommu_enabled else "Non active",
                "Necessaire pour PCI passthrough (GPU, NIC SR-IOV). Ajouter iommu=pt au kernel.",
                "Editer /etc/default/grub: GRUB_CMDLINE_LINUX_DEFAULT='quiet intel_iommu=on iommu=pt' puis update-grub && reboot")

            has_pt = "intel_iommu=on" in cmdline or "amd_iommu=on" in cmdline
            chk("IOMMU/Passthrough", nn, "Intel VT-d / AMD-Vi active dans kernel",
                has_pt, cmdline.split("quiet")[1].strip()[:60] if "quiet" in cmdline else cmdline[:60],
                "Ajouter intel_iommu=on (Intel) ou amd_iommu=on (AMD) dans GRUB",
                "/etc/default/grub > GRUB_CMDLINE_LINUX_DEFAULT puis update-grub && reboot")

            # IOMMU groups check
            _, stdout, _ = ssh.exec_command("find /sys/kernel/iommu_groups/ -type l 2>/dev/null | wc -l", timeout=5)
            iommu_groups = stdout.read().decode().strip()
            iommu_count = int(iommu_groups) if iommu_groups.isdigit() else 0
            chk("IOMMU/Passthrough", nn, f"Groupes IOMMU ({iommu_count})",
                iommu_count > 0 if iommu_enabled else True,
                f"{iommu_count} groupes" if iommu_count > 0 else "Aucun (IOMMU desactive)",
                "Des groupes IOMMU propres sont necessaires pour le passthrough",
                "find /sys/kernel/iommu_groups/ -type l pour verifier. Si groups sales: pcie_acs_override=downstream,multifunction (dernier recours)")

            # ── KERNEL TWEAKS ──
            _, stdout, _ = ssh.exec_command("cat /proc/sys/vm/swappiness 2>/dev/null", timeout=5)
            swappiness = stdout.read().decode().strip()
            swap_val = int(swappiness) if swappiness.isdigit() else 60
            chk("Systeme Kernel", nn, f"vm.swappiness ({swap_val})",
                swap_val <= 10,
                f"swappiness={swap_val}",
                "Reduire a 10 ou 1 pour eviter le swap inutile quand il y a de la RAM",
                "echo 'vm.swappiness=10' >> /etc/sysctl.conf && sysctl -p")

            # Hugepages
            _, stdout, _ = ssh.exec_command("cat /proc/meminfo 2>/dev/null | grep HugePages_Total", timeout=5)
            hp = stdout.read().decode().strip()
            hp_total = 0
            if hp:
                try:
                    hp_total = int(hp.split(":")[1].strip())
                except (ValueError, IndexError):
                    pass
            chk("Systeme Kernel", nn, "Hugepages",
                True,  # informational
                f"{hp_total} pages" if hp_total > 0 else "Non configure",
                "Activer hugepages (2M/1G) pour VMs memoire-intensive ameliore les performances TLB",
                "echo 'vm.nr_hugepages=1024' >> /etc/sysctl.conf (pour 2GB de hugepages 2M)")

            # VFIO modules
            _, stdout, _ = ssh.exec_command("lsmod 2>/dev/null | grep vfio | head -5", timeout=5)
            vfio = stdout.read().decode().strip()
            chk("IOMMU/Passthrough", nn, "Modules VFIO charges",
                True,
                "Charges" if vfio else "Non charges (normal si pas de passthrough)",
                "VFIO necessaire pour PCI passthrough. Charger vfio-pci, vfio_iommu_type1",
                "echo 'vfio\nvfio_iommu_type1\nvfio_pci\nvfio_virqfd' >> /etc/modules && update-initramfs -u")

            # CSM check (UEFI boot)
            _, stdout, _ = ssh.exec_command("[ -d /sys/firmware/efi ] && echo 'UEFI' || echo 'BIOS'", timeout=5)
            boot_type = stdout.read().decode().strip()
            chk("BIOS/UEFI", nn, "Boot UEFI (CSM desactive)",
                boot_type == "UEFI",
                boot_type,
                "Desactiver CSM dans le BIOS pour boot UEFI pur. Necessaire pour Secure Boot",
                "BIOS > Boot > CSM: Disabled. Reinstaller Proxmox en mode UEFI si necessaire.")

            # Services inutiles (standalone check)
            _, stdout, _ = ssh.exec_command("systemctl is-active pve-ha-crm pve-ha-lrm corosync 2>/dev/null", timeout=5)
            ha_services = stdout.read().decode().strip().split("\n")
            # Only flag if single node without HA
            if cluster_info.get("total_nodes", 3) == 1:
                if all(s == "active" for s in ha_services):
                    chk("Systeme", nn, "Services HA sur noeud standalone",
                        False, "HA actif sur noeud unique",
                        "Desactiver HA/Corosync sur un noeud standalone pour economiser des ressources",
                        "systemctl disable --now pve-ha-crm pve-ha-lrm corosync (noeud standalone uniquement !)")

            # ── NUMA HOST ──
            _, stdout, _ = ssh.exec_command("lscpu 2>/dev/null | grep 'NUMA node(s)'", timeout=5)
            numa_out = stdout.read().decode().strip()
            numa_nodes = 1
            try:
                numa_nodes = int(numa_out.split(":")[1].strip())
            except (ValueError, IndexError):
                pass
            if numa_nodes > 1:
                chk("CPU NUMA", nn, f"Hote multi-NUMA ({numa_nodes} noeuds)",
                    True, f"{numa_nodes} noeuds NUMA",
                    "Hote multi-socket/NUMA: activer NUMA sur les VMs pour optimiser l'acces memoire",
                    "VM > Hardware > Processors > Enable NUMA. Aligner les vCPU aux noeuds NUMA physiques.")

            ssh.close()

        except Exception:
            pass  # SSH failed, skip checks silently

        # ── VM-LEVEL CHECKS ──
        vms = proxmox_api(host, f"/nodes/{nn}/qemu")
        if not isinstance(vms, list):
            continue

        for vm in vms:
            vmid = vm.get("vmid")
            vname = vm.get("name", f"VM {vmid}")
            target = f"{vname} ({vmid})"
            cfg = proxmox_api(host, f"/nodes/{nn}/qemu/{vmid}/config")
            if not isinstance(cfg, dict) or "error" in cfg:
                continue

            # Safe int conversion helper
            def cfgi(key, default=0):
                v = cfg.get(key, default)
                try:
                    return int(v)
                except (ValueError, TypeError):
                    return default

            # CPU type
            cpu_type = cfg.get("cpu", "kvm64")
            is_good_cpu = cpu_type not in ("kvm64", "qemu64")
            chk("VM CPU", target, "Type CPU optimise",
                is_good_cpu, cpu_type,
                "host (max perf) ou x86-64-v2-AES (compatible migration live)",
                "VM > Hardware > Processors > Type: host")

            # Nested virt on VM
            cpu_str = str(cfg.get("cpu", ""))
            vm_nested = "+vmx" in cpu_str or "host" in cpu_type
            chk("VM CPU", target, "Nested virtualization",
                True,  # informational
                "Actif" if vm_nested else "Desactive",
                "Necessaire uniquement si la VM doit heberger des VMs (Docker/KVM inside)",
                "VM > Hardware > Processors > Type: host (expose toutes les instructions)")

            # vCPU allocation
            cores = cfgi("cores", 1)
            sockets = cfgi("sockets", 1)
            total_vcpu = cores * sockets
            ratio = total_vcpu / max(cpu_info.get("cpus", 1), 1)
            chk("VM CPU", target, f"Allocation vCPU ({total_vcpu} vCPU)",
                total_vcpu <= cpu_info.get("cpus", 1),
                f"{total_vcpu} vCPU ({cores}c x {sockets}s) / {cpu_info.get('cpus', '?')} cores hote",
                "Ne pas depasser le nombre de cores physiques sauf si charge legere",
                "VM > Hardware > Processors: ajuster cores/sockets")

            # CPU limit/affinity
            cpulimit = cfgi("cpulimit", 0)
            chk("VM CPU", target, "CPU limit defini",
                cpulimit > 0 if total_vcpu > 2 else True,
                f"cpulimit={cpulimit}" if cpulimit else "Pas de limite",
                "Definir cpulimit evite qu'une VM monopolise le CPU",
                "VM > Hardware > Processors > CPU limit (ex: 2.0 pour 200%)")

            # NUMA
            numa = cfgi("numa", 0)
            chk("VM CPU", target, "NUMA",
                bool(numa) if sockets > 1 or total_vcpu >= 4 else True,
                "Actif" if numa else "Desactive",
                "Activer NUMA pour VMs multi-socket ou >= 4 vCPU",
                "VM > Hardware > Processors > Enable NUMA")

            # Memory ballooning
            balloon = cfg.get("balloon", None)
            if balloon is not None:
                try:
                    balloon = int(balloon)
                except (ValueError, TypeError):
                    balloon = None
            mem_mb = cfgi("memory", 0)
            chk("VM Memoire", target, f"RAM ({mem_mb} MB)",
                mem_mb >= 512, f"{mem_mb} MB",
                "Adapter selon le role de la VM",
                "VM > Hardware > Memory")

            if balloon is not None and balloon == 0 and mem_mb > 2048:
                chk("VM Memoire", target, "Ballooning",
                    False, "Desactive",
                    "Activer pour recuperer la RAM inutilisee",
                    "VM > Hardware > Memory > Ballooning: cocher, Minimum: 512 MB")
            elif balloon is None or balloon != 0:
                chk("VM Memoire", target, "Ballooning",
                    True, "Actif",
                    "OK - permet la recuperation dynamique de RAM",
                    "")

            # SCSI controller
            scsihw = cfg.get("scsihw", "")
            chk("VM Disque", target, "Controleur SCSI",
                scsihw == "virtio-scsi-single",
                scsihw or "defaut (lsi)",
                "virtio-scsi-single (meilleure performance I/O)",
                "VM > Hardware > ajouter disque > SCSI Controller: VirtIO SCSI Single")

            # Disk optimizations
            for key, val in cfg.items():
                if not isinstance(val, str) or ":" not in val:
                    continue
                if not any(key.startswith(p) for p in ("scsi", "virtio", "ide", "sata")):
                    continue
                if "media=cdrom" in val:
                    continue

                # iothread
                has_iothread = "iothread=1" in val
                chk("VM Disque", target, f"{key}: iothread",
                    has_iothread, "Actif" if has_iothread else "Desactive",
                    "iothread=1 dedie un thread I/O par disque (necessite virtio-scsi-single)",
                    f"VM > Hardware > {key} > Advanced > IO Thread: cocher")

                # discard/TRIM
                has_discard = "discard=on" in val
                chk("VM Disque", target, f"{key}: TRIM/Discard",
                    has_discard, "Actif" if has_discard else "Desactive",
                    "Recupere l'espace libere (essentiel avec LVM-thin/ZFS)",
                    f"VM > Hardware > {key} > Advanced > Discard: cocher")

                # cache
                has_cache = "cache=" in val
                cache_type = ""
                if has_cache:
                    for p in val.split(","):
                        if p.startswith("cache="):
                            cache_type = p.split("=")[1]
                chk("VM Disque", target, f"{key}: cache",
                    not has_cache or cache_type in ("none", "writethrough", ""),
                    f"cache={cache_type}" if has_cache else "none (defaut)",
                    "none ou writethrough recommande (writeback risque de perte)",
                    f"VM > Hardware > {key} > Advanced > Cache: None")

                # interface type
                is_ide = key.startswith("ide") or key.startswith("sata")
                if is_ide:
                    chk("VM Disque", target, f"{key}: interface",
                        False, key.split(":")[0],
                        "Utiliser scsi (VirtIO) au lieu de ide/sata pour les performances",
                        "Migrer le disque vers une interface scsi")

            # Network
            for key, val in cfg.items():
                if key.startswith("net") and isinstance(val, str):
                    is_virtio = "virtio" in val.lower()
                    chk("VM Reseau", target, f"{key}: modele VirtIO",
                        is_virtio, "VirtIO" if is_virtio else "e1000/rtl8139",
                        "VirtIO offre les meilleures performances reseau",
                        f"VM > Hardware > {key} > Model: VirtIO")

                    has_queues = "queues=" in val
                    if is_virtio and total_vcpu > 1:
                        chk("VM Reseau", target, f"{key}: multi-queue",
                            has_queues, "Actif" if has_queues else "Desactive",
                            f"Multi-queue permet de repartir le trafic sur {total_vcpu} vCPU",
                            f"VM > Hardware > {key} > Multiqueue: {min(total_vcpu, 8)}")

            # BIOS
            bios = cfg.get("bios", "seabios")
            chk("VM Systeme", target, "BIOS UEFI (OVMF)",
                bios == "ovmf", bios,
                "OVMF (UEFI) recommande pour OS modernes et Secure Boot",
                "VM > Hardware > BIOS: OVMF. Necessite un disque EFI.")

            # Machine type
            machine = cfg.get("machine", "")
            is_q35 = "q35" in str(machine)
            chk("VM Systeme", target, "Chipset Q35",
                is_q35, machine or "i440fx (defaut)",
                "Q35 supporte PCIe natif, IOMMU, meilleures performances",
                "VM > Hardware > Machine: q35")

            # Guest agent
            agent = cfg.get("agent", "")
            chk("VM Systeme", target, "QEMU Guest Agent",
                bool(agent) and str(agent) != "0",
                "Actif" if agent and str(agent) != "0" else "Desactive",
                "Shutdown propre, freeze FS, affichage IP",
                "VM > Options > QEMU Guest Agent: Enable. Installer qemu-guest-agent dans la VM.")

            # ── WINDOWS-SPECIFIC ──
            ostype = cfg.get("ostype", "")
            is_windows = ostype in ("win11", "win10", "win8", "win7", "wvista", "wxp", "w2k22", "w2k19", "w2k16", "w2k12", "w2k8")

            if is_windows:
                # Nested virt for Credential Guard / HVCI
                cpu_str = str(cfg.get("cpu", ""))
                has_nested = "host" in cpu_type or "+vmx" in cpu_str
                chk("VM Windows", target, "Nested Virtualization (Credential Guard / HVCI)",
                    has_nested,
                    "Actif (type=host ou +vmx)" if has_nested else "Desactive",
                    "Necessaire pour Credential Guard, Device Guard, HVCI, WSL2, Hyper-V inside",
                    "VM > Hardware > Processors > Type: host (ou ajouter flag +vmx)")

                # Windows CPU flags
                has_pcid = "+pcid" in cpu_str
                has_spec = "+spec-ctrl" in cpu_str or "+ssbd" in cpu_str
                chk("VM Windows", target, "CPU flags securite (pcid, spec-ctrl, ssbd)",
                    has_pcid and has_spec,
                    cpu_str[:60] if cpu_str else cpu_type,
                    "Ajouter +pcid,+spec-ctrl,+ssbd pour Spectre/Meltdown mitigation",
                    "VM > Hardware > Processors > Extra CPU Flags: +pcid,+spec-ctrl,+ssbd")

                # VirtIO drivers check (Windows needs special drivers)
                has_virtio_disk = any(k.startswith("scsi") or k.startswith("virtio") for k in cfg if isinstance(cfg.get(k), str) and ":" in cfg[k] and "media" not in cfg.get(k, ""))
                chk("VM Windows", target, "Disque VirtIO (drivers requis)",
                    has_virtio_disk,
                    "VirtIO" if has_virtio_disk else "IDE/SATA",
                    "VirtIO SCSI avec drivers virtio-win pour meilleures performances",
                    "Installer virtio-win ISO, puis migrer disques vers SCSI VirtIO")

                # TPM for Windows 11
                has_tpm = any(k.startswith("tpmstate") for k in cfg)
                if ostype == "win11":
                    chk("VM Windows", target, "TPM 2.0 (requis Win11)",
                        has_tpm,
                        "Present" if has_tpm else "Absent",
                        "Windows 11 requiert TPM 2.0. Ajouter un TPM virtuel.",
                        "VM > Hardware > Add > TPM State")

                # Hyper-V enlightenments (auto with ostype=win*)
                chk("VM Windows", target, "Hyper-V Enlightenments",
                    ostype.startswith("win"),
                    f"ostype={ostype} (active auto)",
                    "Les enlightenments Hyper-V ameliorent les performances Windows de 10-30%",
                    "VM > Options > OS Type: selecteur Windows correct")

            # ── PCI PASSTHROUGH ──
            pci_devices = [k for k in cfg if k.startswith("hostpci")]
            if pci_devices:
                for pci_key in pci_devices:
                    pci_val = str(cfg.get(pci_key, ""))
                    has_allf = "all-functions" in pci_val or "x-vga" in pci_val
                    chk("VM Passthrough", target, f"{pci_key}: PCI passthrough",
                        True, pci_val[:50],
                        "Verifier que le device est dans un groupe IOMMU propre",
                        "VM > Hardware > PCI Device. Utiliser 'All Functions' pour multi-function.")

        # ── CT CHECKS ──
        cts = proxmox_api(host, f"/nodes/{nn}/lxc")
        if isinstance(cts, list):
            for ct in cts:
                ctid = ct.get("vmid")
                ctname = ct.get("name", f"CT {ctid}")
                target = f"{ctname} ({ctid})"
                ct_cfg = proxmox_api(host, f"/nodes/{nn}/lxc/{ctid}/config")
                if not isinstance(ct_cfg, dict) or "error" in ct_cfg:
                    continue

                # Unprivileged
                chk("CT Securite", target, "Container non-privilegie",
                    bool(ct_cfg.get("unprivileged")),
                    "Non-privilegie" if ct_cfg.get("unprivileged") else "Privilegie",
                    "Les containers non-privilegies sont plus securises",
                    "Recreer le container en cochant 'Unprivileged'")

                # Nesting
                features = str(ct_cfg.get("features", ""))
                chk("CT", target, "Nesting",
                    True,
                    "Actif" if "nesting=1" in features else "Desactive",
                    "Necessaire pour Docker dans un container LXC",
                    "CT > Options > Features: nesting=1")

    return jsonify(checks)


# ── Performance detaillee ───────────────────────────────────────────────────

@app.route("/api/performance")
def api_performance():
    """Metrics de performance detaillees pour tous les noeuds et VMs."""
    host, ticket, _ = get_ticket()
    if not host:
        return jsonify({"error": "Non connecte"}), 503

    result = {"nodes": [], "cluster_totals": {}}
    total_cpu_cap = 0
    total_cpu_used = 0
    total_mem = 0
    total_mem_used = 0
    total_vcpu_alloc = 0

    nodes_list = proxmox_api(host, "/nodes")
    if not isinstance(nodes_list, list):
        return jsonify(result)

    for node in sorted(nodes_list, key=lambda n: n.get("node", "")):
        nn = node.get("node", "")
        if node.get("status") != "online":
            continue

        ns = proxmox_api(host, f"/nodes/{nn}/status")
        if not isinstance(ns, dict) or "error" in ns:
            continue

        cpu_info = ns.get("cpuinfo", {})
        memory = ns.get("memory", {})
        swap = ns.get("swap", {})
        cores = cpu_info.get("cpus", 0)
        cpu_usage = round(ns.get("cpu", 0) * 100, 1)
        mem_total = memory.get("total", 0)
        mem_used = memory.get("used", 0)
        mem_avail = memory.get("available", 0)

        total_cpu_cap += cores
        total_cpu_used += (cpu_usage / 100) * cores
        total_mem += mem_total
        total_mem_used += mem_used

        # RRD hour data for history
        rrd = proxmox_api(host, f"/nodes/{nn}/rrddata?timeframe=hour")
        rrd_day = proxmox_api(host, f"/nodes/{nn}/rrddata?timeframe=day")
        history = []
        if isinstance(rrd, list):
            for e in rrd[-30:]:
                history.append({
                    "time": e.get("time", 0),
                    "cpu": round(e.get("cpu", 0) * 100, 1),
                    "mem": round(e.get("memused", 0) / max(e.get("memtotal", 1), 1) * 100, 1),
                    "io": round(e.get("iowait", 0) * 100, 2),
                    "load": round(e.get("loadavg", 0), 2),
                    "netin": round(e.get("netin", 0)),
                    "netout": round(e.get("netout", 0)),
                })

        # Day averages
        day_avg = {"cpu": 0, "mem": 0, "io": 0, "load": 0}
        if isinstance(rrd_day, list) and rrd_day:
            cpu_vals = [e.get("cpu", 0) for e in rrd_day if e.get("cpu") is not None]
            mem_vals = [e.get("memused", 0) / max(e.get("memtotal", 1), 1) for e in rrd_day if e.get("memtotal")]
            io_vals = [e.get("iowait", 0) for e in rrd_day if e.get("iowait") is not None]
            load_vals = [e.get("loadavg", 0) for e in rrd_day if e.get("loadavg") is not None]
            if cpu_vals:
                day_avg["cpu"] = round(sum(cpu_vals) / len(cpu_vals) * 100, 1)
            if mem_vals:
                day_avg["mem"] = round(sum(mem_vals) / len(mem_vals) * 100, 1)
            if io_vals:
                day_avg["io"] = round(sum(io_vals) / len(io_vals) * 100, 2)
            if load_vals:
                day_avg["load"] = round(sum(load_vals) / len(load_vals), 2)

        # PSI from latest RRD
        psi = {}
        if isinstance(rrd, list) and rrd:
            last = rrd[-1]
            psi = {
                "cpu_some": round(last.get("pressurecpusome", 0) * 100, 2),
                "mem_some": round(last.get("pressurememorysome", 0) * 100, 2),
                "mem_full": round(last.get("pressurememoryfull", 0) * 100, 2),
                "io_some": round(last.get("pressureiosome", 0) * 100, 2),
                "io_full": round(last.get("pressureiofull", 0) * 100, 2),
            }

        # VM resource allocation
        vcpu_total = 0
        mem_alloc = 0
        vm_perfs = []
        vms = proxmox_api(host, f"/nodes/{nn}/qemu")
        if isinstance(vms, list):
            for vm in vms:
                if vm.get("status") != "running":
                    continue
                vm_vcpu = vm.get("cpus", vm.get("maxcpu", 0))
                vm_mem = vm.get("maxmem", 0)
                vcpu_total += vm_vcpu
                mem_alloc += vm_mem
                vm_perfs.append({
                    "vmid": vm.get("vmid"),
                    "name": vm.get("name", ""),
                    "cpu": round(vm.get("cpu", 0) * 100, 1),
                    "vcpu": vm_vcpu,
                    "mem_used": fmt_bytes(vm.get("mem", 0)),
                    "mem_max": fmt_bytes(vm_mem),
                    "mem_pct": round(vm.get("mem", 0) / max(vm_mem, 1) * 100, 1),
                    "diskread": fmt_bytes(vm.get("diskread", 0)),
                    "diskwrite": fmt_bytes(vm.get("diskwrite", 0)),
                    "netin": fmt_bytes(vm.get("netin", 0)),
                    "netout": fmt_bytes(vm.get("netout", 0)),
                })

        total_vcpu_alloc += vcpu_total

        cts = proxmox_api(host, f"/nodes/{nn}/lxc")
        if isinstance(cts, list):
            for ct in cts:
                if ct.get("status") != "running":
                    continue
                vcpu_total += ct.get("cpus", ct.get("maxcpu", 0))
                mem_alloc += ct.get("maxmem", 0)

        node_data = {
            "name": nn,
            "cpu_cores": cores,
            "cpu_model": cpu_info.get("model", ""),
            "cpu_mhz": cpu_info.get("mhz", ""),
            "cpu_usage": cpu_usage,
            "cpu_free": round(100 - cpu_usage, 1),
            "mem_total": fmt_bytes(mem_total),
            "mem_used": fmt_bytes(mem_used),
            "mem_avail": fmt_bytes(mem_avail),
            "mem_pct": round(mem_used / max(mem_total, 1) * 100, 1),
            "swap_used": fmt_bytes(swap.get("used", 0)),
            "swap_pct": round(swap.get("used", 0) / max(swap.get("total", 1), 1) * 100, 1),
            "loadavg": ns.get("loadavg", ["0", "0", "0"]),
            "iowait": round(ns.get("wait", 0) * 100, 2),
            "vcpu_allocated": vcpu_total,
            "vcpu_ratio": round(vcpu_total / max(cores, 1), 1),
            "mem_allocated": fmt_bytes(mem_alloc),
            "mem_alloc_pct": round(mem_alloc / max(mem_total, 1) * 100, 1),
            "psi": psi,
            "day_avg": day_avg,
            "history": history,
            "vm_perfs": vm_perfs,
        }
        result["nodes"].append(node_data)

    result["cluster_totals"] = {
        "cpu_cores": total_cpu_cap,
        "cpu_used_cores": round(total_cpu_used, 1),
        "cpu_pct": round(total_cpu_used / max(total_cpu_cap, 1) * 100, 1),
        "vcpu_allocated": total_vcpu_alloc,
        "vcpu_ratio": round(total_vcpu_alloc / max(total_cpu_cap, 1), 1),
        "mem_total": fmt_bytes(total_mem),
        "mem_used": fmt_bytes(total_mem_used),
        "mem_pct": round(total_mem_used / max(total_mem, 1) * 100, 1),
    }

    return jsonify(result)


# ── Benchmarks ──────────────────────────────────────────────────────────────

def ssh_exec(host, cmd, timeout=30):
    """Execute une commande SSH et retourne stdout."""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=PROXMOX_CLUSTER["username"].split("@")[0],
                password=PROXMOX_CLUSTER["password"], timeout=10)
    _, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    ssh.close()
    return out, err


@app.route("/api/benchmark", methods=["POST"])
def api_benchmark():
    """Lance un benchmark sur un noeud Proxmox via SSH."""
    data = flask_request.get_json()
    if not data:
        return jsonify({"error": "Donnees manquantes"}), 400

    node_ip = data.get("node_ip", "").strip()
    bench_type = data.get("type", "").strip()
    node_name = data.get("node_name", node_ip)

    if not node_ip or not bench_type:
        return jsonify({"error": "IP et type de benchmark requis"}), 400

    result = {"node": node_name, "ip": node_ip, "type": bench_type, "results": [], "raw": ""}

    try:
        if bench_type == "pveperf":
            out, err = ssh_exec(node_ip, "pveperf 2>&1", timeout=60)
            result["raw"] = out
            for line in out.strip().split("\n"):
                line = line.strip()
                if ":" in line:
                    parts = line.split(":", 1)
                    key = parts[0].strip()
                    val = parts[1].strip()
                    rating = "good"
                    if "BOGOMIPS" in key:
                        try:
                            v = float(val.split()[0])
                            rating = "good" if v > 10000 else "warn" if v > 5000 else "bad"
                        except ValueError:
                            pass
                    elif "REGEX" in key:
                        try:
                            v = float(val.split()[0])
                            rating = "good" if v > 1000000 else "warn" if v > 500000 else "bad"
                        except ValueError:
                            pass
                    elif "DNS" in key:
                        try:
                            v = float(val.split()[0])
                            rating = "good" if v < 20 else "warn" if v < 50 else "bad"
                        except ValueError:
                            pass
                    elif "READ" in key or "WRITE" in key:
                        try:
                            v = float(val.split()[0])
                            rating = "good" if v > 200 else "warn" if v > 50 else "bad"
                        except ValueError:
                            pass
                    result["results"].append({"name": key, "value": val, "rating": rating})

        elif bench_type == "cpu":
            out, err = ssh_exec(node_ip,
                "openssl speed -seconds 3 -evp aes-256-cbc 2>&1", timeout=30)
            result["raw"] = out
            for line in out.strip().split("\n"):
                if "aes-256-cbc" in line.lower() and "bytes" not in line.lower():
                    parts = line.split()
                    if len(parts) >= 7:
                        result["results"].append({"name": "AES-256-CBC 16B", "value": parts[1], "rating": "info"})
                        result["results"].append({"name": "AES-256-CBC 1KB", "value": parts[4], "rating": "info"})
                        result["results"].append({"name": "AES-256-CBC 16KB", "value": parts[6], "rating": "info"})

            # Also get CPU single-thread perf
            out2, _ = ssh_exec(node_ip,
                "openssl speed -seconds 3 -evp sha256 2>&1", timeout=30)
            result["raw"] += "\n" + out2
            for line in out2.strip().split("\n"):
                if "sha256" in line.lower() and "bytes" not in line.lower():
                    parts = line.split()
                    if len(parts) >= 7:
                        result["results"].append({"name": "SHA-256 16KB", "value": parts[6], "rating": "info"})

        elif bench_type == "disk_write":
            # Sequential write 256MB
            out, err = ssh_exec(node_ip,
                "dd if=/dev/zero of=/tmp/_bench_write bs=1M count=256 oflag=direct 2>&1 && rm -f /tmp/_bench_write",
                timeout=60)
            result["raw"] = out + err
            # Parse dd output: "256+0 records out ... 268 MB/s"
            combined = out + err
            for line in combined.split("\n"):
                if "copied" in line or "MB/s" in line or "GB/s" in line:
                    result["results"].append({"name": "Ecriture sequentielle 256MB",
                                             "value": line.strip(), "rating": "info"})
                    # Extract speed
                    import re
                    m = re.search(r'([\d.]+)\s*(MB|GB)/s', line)
                    if m:
                        speed = float(m.group(1))
                        if m.group(2) == "GB":
                            speed *= 1024
                        rating = "good" if speed > 200 else "warn" if speed > 50 else "bad"
                        result["results"].append({"name": "Debit ecriture", "value": f"{speed:.0f} MB/s",
                                                 "rating": rating})

        elif bench_type == "disk_read":
            # Sequential read (from cache, gives max throughput)
            ssh_exec(node_ip, "dd if=/dev/zero of=/tmp/_bench_read bs=1M count=256 oflag=direct 2>&1", timeout=60)
            out, err = ssh_exec(node_ip,
                "dd if=/tmp/_bench_read of=/dev/null bs=1M count=256 iflag=direct 2>&1 && rm -f /tmp/_bench_read",
                timeout=60)
            result["raw"] = out + err
            combined = out + err
            for line in combined.split("\n"):
                if "copied" in line or "MB/s" in line or "GB/s" in line:
                    result["results"].append({"name": "Lecture sequentielle 256MB",
                                             "value": line.strip(), "rating": "info"})
                    import re
                    m = re.search(r'([\d.]+)\s*(MB|GB)/s', line)
                    if m:
                        speed = float(m.group(1))
                        if m.group(2) == "GB":
                            speed *= 1024
                        rating = "good" if speed > 200 else "warn" if speed > 50 else "bad"
                        result["results"].append({"name": "Debit lecture", "value": f"{speed:.0f} MB/s",
                                                 "rating": rating})

        elif bench_type == "network":
            # Ping latency between all nodes
            for target_host in PROXMOX_CLUSTER["hosts"]:
                if target_host == node_ip:
                    continue
                out, err = ssh_exec(node_ip, f"ping -c 5 -q {target_host} 2>&1", timeout=15)
                result["raw"] += out + "\n"
                for line in out.split("\n"):
                    if "avg" in line:
                        # rtt min/avg/max/mdev
                        import re
                        m = re.search(r'([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)', line)
                        if m:
                            avg = float(m.group(2))
                            rating = "good" if avg < 1 else "warn" if avg < 5 else "bad"
                            result["results"].append({
                                "name": f"Latence vers {target_host}",
                                "value": f"min={m.group(1)}ms avg={m.group(2)}ms max={m.group(3)}ms",
                                "rating": rating,
                            })
                    elif "packet loss" in line:
                        import re
                        m = re.search(r'(\d+)% packet loss', line)
                        if m:
                            loss = int(m.group(1))
                            if loss > 0:
                                result["results"].append({
                                    "name": f"Perte paquets vers {target_host}",
                                    "value": f"{loss}%",
                                    "rating": "bad" if loss > 1 else "warn",
                                })

        elif bench_type == "memory":
            # Memory bandwidth via dd
            out, err = ssh_exec(node_ip,
                "dd if=/dev/zero of=/dev/null bs=1M count=4096 2>&1", timeout=30)
            result["raw"] = out + err
            combined = out + err
            for line in combined.split("\n"):
                if "copied" in line or "GB/s" in line or "MB/s" in line:
                    result["results"].append({"name": "Bande passante memoire",
                                             "value": line.strip(), "rating": "info"})
                    import re
                    m = re.search(r'([\d.]+)\s*(MB|GB)/s', line)
                    if m:
                        speed = float(m.group(1))
                        if m.group(2) == "GB":
                            speed *= 1024
                        rating = "good" if speed > 5000 else "warn" if speed > 2000 else "bad"
                        result["results"].append({"name": "Debit memoire", "value": f"{speed:.0f} MB/s",
                                                 "rating": rating})

        else:
            return jsonify({"error": f"Type de benchmark inconnu: {bench_type}"}), 400

        # Save to SQLite
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("INSERT INTO benchmarks (node, node_ip, bench_type, results, raw) VALUES (?,?,?,?,?)",
                         (node_name, node_ip, bench_type,
                          json_lib.dumps(result.get("results", [])),
                          result.get("raw", "")))
            conn.commit()
            conn.close()
        except Exception:
            pass

        return jsonify(result)

    except paramiko.AuthenticationException:
        return jsonify({"error": "Authentification SSH echouee"}), 500
    except Exception as e:
        return jsonify({"error": str(e), "results": result.get("results", [])}), 500


@app.route("/api/benchmark/history")
def api_benchmark_history():
    """Retourne l'historique des benchmarks."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM benchmarks ORDER BY timestamp DESC LIMIT 200"
        ).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Architecture enrichie ───────────────────────────────────────────────────

@app.route("/api/architecture")
def api_architecture():
    """Données d'architecture enrichies: Corosync, knet, réplication, ZFS."""
    host_api, ticket, _ = get_ticket()
    if not host_api:
        return jsonify({"error": "Non connecte"}), 503

    result = {
        "corosync": {}, "nodes_detail": [], "replication": [],
        "zfs_tuning": {}, "network_links": [],
    }

    # Corosync totem config
    totem = proxmox_api(host_api, "/cluster/config/totem")
    if isinstance(totem, dict) and "error" not in totem:
        result["corosync"]["cluster_name"] = totem.get("cluster_name", "")
        result["corosync"]["secauth"] = totem.get("secauth", "off")
        result["corosync"]["link_mode"] = totem.get("link_mode", "")
        result["corosync"]["ip_version"] = totem.get("ip_version", "")
        result["corosync"]["config_version"] = totem.get("config_version", "")
        ifaces = totem.get("interface", {})
        result["corosync"]["links"] = []
        if isinstance(ifaces, dict):
            for num, iface in ifaces.items():
                result["corosync"]["links"].append({"linknumber": num, **iface})

    # Corosync nodes config
    cnodes = proxmox_api(host_api, "/cluster/config/nodes")
    if isinstance(cnodes, list):
        result["corosync"]["nodes"] = cnodes

    # Qdevice
    qdevice = proxmox_api(host_api, "/cluster/config/qdevice")
    result["corosync"]["qdevice"] = bool(qdevice) if isinstance(qdevice, dict) and qdevice else False

    # Cluster status
    cluster_status = proxmox_api(host_api, "/cluster/status")
    if isinstance(cluster_status, list):
        for item in cluster_status:
            if item.get("type") == "cluster":
                result["corosync"]["quorate"] = bool(item.get("quorate"))
                result["corosync"]["total_nodes"] = item.get("nodes")

    # Replication
    repl = proxmox_api(host_api, "/cluster/replication")
    if isinstance(repl, list):
        result["replication"] = repl

    # Per-node details via SSH
    nodes_list = proxmox_api(host_api, "/nodes")
    if not isinstance(nodes_list, list):
        return jsonify(result)

    # Get node IPs
    node_ips = {}
    if isinstance(cluster_status, list):
        for item in cluster_status:
            if item.get("type") == "node":
                node_ips[item.get("name")] = item.get("ip")

    for node in sorted(nodes_list, key=lambda n: n.get("node", "")):
        nn = node.get("node", "")
        if node.get("status") != "online":
            result["nodes_detail"].append({"name": nn, "online": False})
            continue

        nd = {"name": nn, "online": True, "ip": node_ips.get(nn, ""),
              "transport": "", "link_status": [], "jumbo_frames": False,
              "mtu": 1500, "zfs": {}, "corosync_ok": False, "heartbeat_ok": False}

        node_ip = node_ips.get(nn)
        if not node_ip:
            result["nodes_detail"].append(nd)
            continue

        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(node_ip,
                        username=PROXMOX_CLUSTER["username"].split("@")[0],
                        password=PROXMOX_CLUSTER["password"], timeout=5)

            # Corosync status (knet transport, link status)
            _, stdout, _ = ssh.exec_command("corosync-cfgtool -s 2>/dev/null", timeout=5)
            coro_status = stdout.read().decode()
            if "transport knet" in coro_status:
                nd["transport"] = "knet (Kronosnet)"
            elif "transport udp" in coro_status:
                nd["transport"] = "udp"
            else:
                nd["transport"] = "knet"

            # Parse link status
            for line in coro_status.split("\n"):
                if "nodeid:" in line and "localhost" not in line:
                    parts = line.strip().split()
                    nid = ""
                    status = ""
                    for i, p in enumerate(parts):
                        if p == "nodeid:":
                            nid = parts[i + 1].rstrip(":")
                        if p in ("connected", "disconnected"):
                            status = p
                    if nid:
                        nd["link_status"].append({"nodeid": nid, "status": status})

            nd["corosync_ok"] = all(l["status"] == "connected" for l in nd["link_status"])
            nd["heartbeat_ok"] = nd["corosync_ok"]

            # MTU / Jumbo frames
            _, stdout, _ = ssh.exec_command("ip link show vmbr0 2>/dev/null | head -1", timeout=5)
            link_out = stdout.read().decode()
            import re
            mtu_match = re.search(r'mtu (\d+)', link_out)
            if mtu_match:
                nd["mtu"] = int(mtu_match.group(1))
                nd["jumbo_frames"] = nd["mtu"] >= 9000

            # NTP / Chrony
            _, stdout, _ = ssh.exec_command("chronyc tracking 2>/dev/null", timeout=5)
            chrony_out = stdout.read().decode("utf-8", errors="replace")
            nd["ntp"] = {"active": False, "synced": False, "drift": "N/A", "stratum": 0,
                         "source": "", "sources_count": 0}
            if chrony_out:
                nd["ntp"]["active"] = True
                for line in chrony_out.split("\n"):
                    if "System time" in line:
                        import re as _re
                        m = _re.search(r'([\d.]+) seconds', line)
                        if m:
                            drift_s = float(m.group(1))
                            if drift_s < 0.001:
                                nd["ntp"]["drift"] = f"{drift_s*1000000:.0f} us"
                            elif drift_s < 1:
                                nd["ntp"]["drift"] = f"{drift_s*1000:.1f} ms"
                            else:
                                nd["ntp"]["drift"] = f"{drift_s:.2f} s"
                            nd["ntp"]["drift_seconds"] = drift_s
                            nd["ntp"]["synced"] = drift_s < 1
                    if "Stratum" in line:
                        try:
                            nd["ntp"]["stratum"] = int(line.split(":")[1].strip())
                        except (ValueError, IndexError):
                            pass
                    if "Reference ID" in line:
                        nd["ntp"]["source"] = line.split("(")[1].rstrip(")") if "(" in line else line.split(":")[1].strip()

            _, stdout, _ = ssh.exec_command("chronyc sources 2>/dev/null | grep '\\^' | wc -l", timeout=5)
            count = stdout.read().decode().strip()
            nd["ntp"]["sources_count"] = int(count) if count.isdigit() else 0

            # Corosync token (from config)
            _, stdout, _ = ssh.exec_command("grep -A2 'totem' /etc/corosync/corosync.conf 2>/dev/null | grep token", timeout=5)
            token_line = stdout.read().decode().strip()
            if token_line and ":" in token_line:
                nd["corosync_token"] = token_line.split(":")[1].strip()
            else:
                nd["corosync_token"] = "1000 (defaut)"

            # ZFS tuning
            _, stdout, _ = ssh.exec_command("cat /sys/module/zfs/parameters/zfs_arc_max 2>/dev/null", timeout=5)
            arc_max = stdout.read().decode().strip()
            _, stdout, _ = ssh.exec_command("cat /proc/spl/kstat/zfs/arcstats 2>/dev/null | awk '/^size|^c_max|^hits|^misses/{print $1,$3}'", timeout=5)
            arc_raw = stdout.read().decode().strip()
            _, stdout, _ = ssh.exec_command("zpool list -H -o name,size,alloc,free,health 2>/dev/null", timeout=5)
            zpool_out = stdout.read().decode().strip()
            _, stdout, _ = ssh.exec_command("cat /etc/modprobe.d/zfs.conf 2>/dev/null", timeout=5)
            zfs_conf = stdout.read().decode().strip()

            nd["zfs"]["loaded"] = bool(arc_max)
            nd["zfs"]["arc_max"] = int(arc_max) if arc_max.isdigit() else 0
            nd["zfs"]["arc_max_fmt"] = fmt_bytes(int(arc_max)) if arc_max.isdigit() else "N/A"
            nd["zfs"]["persistent_config"] = "zfs_arc_max" in zfs_conf
            nd["zfs"]["config_file"] = zfs_conf

            # ARC stats
            arc_stats = {}
            for line in arc_raw.split("\n"):
                parts = line.split()
                if len(parts) == 2 and parts[1].isdigit():
                    arc_stats[parts[0]] = int(parts[1])
            nd["zfs"]["arc_size"] = fmt_bytes(arc_stats.get("size", 0))
            hits = arc_stats.get("hits", 0)
            misses = arc_stats.get("misses", 0)
            nd["zfs"]["hit_ratio"] = round(hits / max(hits + misses, 1) * 100, 1)

            # Pools
            nd["zfs"]["pools"] = []
            if zpool_out:
                for line in zpool_out.split("\n"):
                    parts = line.split()
                    if len(parts) >= 5:
                        nd["zfs"]["pools"].append({
                            "name": parts[0], "size": parts[1],
                            "alloc": parts[2], "free": parts[3],
                            "health": parts[4],
                        })

            ssh.close()
        except Exception:
            pass

        result["nodes_detail"].append(nd)

    return jsonify(result)


# ── Diagnostics ─────────────────────────────────────────────────────────────

DIAGNOSTIC_RULES = [
    # (pattern_in_log, category, severity, title, solution)
    ("oom-killer", "Memoire", "critical", "OOM Killer active - Un processus a ete tue par manque de RAM",
     "Ajouter de la RAM, reduire la memoire des VMs, ou activer le ballooning. Verifier: dmesg | grep -i oom"),
    ("oom_reaper", "Memoire", "critical", "OOM Reaper - Recuperation memoire d'urgence",
     "Le systeme manque critiquement de RAM. Migrer des VMs ou ajouter de la memoire."),
    ("out of memory", "Memoire", "critical", "Out of Memory detecte",
     "Ajouter de la RAM ou reduire la charge. Verifier swap et ballooning."),
    ("no active links", "Reseau Cluster", "warning", "Corosync: Lien entre noeuds inactif",
     "Verifier la connectivite reseau entre les noeuds. Verifier: corosync-cfgtool -s. Causes: switch, cable, firewall."),
    ("inotify poll request in wrong process", "PVE Proxy", "info", "PVE Proxy: inotify dans mauvais processus",
     "Benin - se produit lors du reload du proxy. Pas d'action necessaire. Si frequent: systemctl restart pveproxy"),
    ("unable to write lrm status", "HA Manager", "warning", "HA LRM ne peut pas ecrire son statut",
     "Le filesystem cluster /etc/pve n'est pas accessible. Verifier: pvecm status. Peut indiquer un probleme de quorum."),
    ("Permission denied", "Systeme", "warning", "Acces refuse a un fichier",
     "Verifier les permissions du fichier concerne. Possible probleme pmxcfs si /etc/pve."),
    ("connection reset by peer", "Reseau", "info", "Connexion reinitialise par le client",
     "Le client a ferme la connexion. Normal si le navigateur a ete ferme. Pas d'action."),
    ("cgroup: fork rejected", "Systeme", "critical", "Fork rejete par cgroup - Limite de processus atteinte",
     "Augmenter la limite PIDs du cgroup ou reduire le nombre de processus."),
    ("i/o error", "Stockage", "critical", "Erreur I/O detectee",
     "URGENT: Verifier le disque avec smartctl -a /dev/sdX. Possible defaillance materielle."),
    ("ext4.*error", "Stockage", "critical", "Erreur filesystem ext4",
     "Verifier avec fsck (hors ligne). Possible corruption ou disque defaillant."),
    ("zfs.*error", "Stockage", "warning", "Erreur ZFS detectee",
     "Verifier: zpool status. Possible disque defaillant dans le pool."),
    ("DEGRADED", "Stockage", "critical", "Pool ZFS/RAID en mode degrade",
     "URGENT: Un disque du pool est defaillant. Remplacer le disque au plus vite. zpool status pour details."),
    ("task.*failed", "Taches", "warning", "Tache Proxmox echouee",
     "Verifier les details dans Datacenter > Taches. Causes courantes: espace disque, permissions, reseau."),
    ("apt-get update.*failed", "Mises a jour", "warning", "Echec de la mise a jour APT",
     "Verifier /etc/apt/sources.list. Possible probleme DNS ou proxy. Tester: apt-get update manuellement."),
    ("CRIT.*corosync", "Cluster", "critical", "Erreur critique Corosync",
     "Le cluster est instable. Verifier: pvecm status, corosync-cfgtool -s. Possible perte de quorum."),
    ("split.brain", "Cluster", "critical", "Split-brain detecte",
     "URGENT: Les noeuds ne communiquent plus. Risque de corruption. Verifier le reseau immediatement."),
    ("bond.*link.*down", "Reseau", "warning", "Lien bonding down",
     "Un lien du bond est tombe. Verifier le cable/switch. Le bonding assure la redondance."),
    ("temperature", "Materiel", "warning", "Alerte temperature",
     "Verifier la ventilation du serveur. Nettoyer les filtres. lm-sensors pour details."),
    ("mce.*hardware error", "Materiel", "critical", "Erreur materielle MCE",
     "Machine Check Exception detecte. Possible probleme CPU/RAM. Verifier mcelog."),
    ("nf_conntrack.*table full", "Reseau", "warning", "Table conntrack pleine",
     "Augmenter: sysctl -w net.netfilter.nf_conntrack_max=262144. Ajouter dans /etc/sysctl.conf"),
    ("blocked for more than", "Systeme", "warning", "Processus bloque (hung task)",
     "Un processus est bloque sur une operation I/O. Verifier le stockage et les disques."),
]


@app.route("/api/diagnostics")
def api_diagnostics():
    """Analyse les journaux systeme et propose des diagnostics avec solutions."""
    host_api, ticket, _ = get_ticket()
    if not host_api:
        return jsonify({"error": "Non connecte"}), 503

    result = {"nodes": [], "summary": {"critical": 0, "warning": 0, "info": 0}}

    nodes_list = proxmox_api(host_api, "/nodes")
    if not isinstance(nodes_list, list):
        return jsonify(result)

    # Get node IPs
    cluster_status = proxmox_api(host_api, "/cluster/status")
    node_ips = {}
    if isinstance(cluster_status, list):
        for item in cluster_status:
            if item.get("type") == "node":
                node_ips[item.get("name")] = item.get("ip")

    for node in sorted(nodes_list, key=lambda n: n.get("node", "")):
        nn = node.get("node", "")
        if node.get("status") != "online":
            continue

        node_ip = node_ips.get(nn)
        if not node_ip:
            continue

        nd = {"name": nn, "ip": node_ip, "issues": [], "services_failed": [],
              "disk_health": [], "network_errors": [], "raw_errors": []}

        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(node_ip,
                        username=PROXMOX_CLUSTER["username"].split("@")[0],
                        password=PROXMOX_CLUSTER["password"], timeout=5)

            # System errors (last 24h)
            _, stdout, _ = ssh.exec_command(
                "journalctl -p err --since '24 hours ago' -n 50 --no-pager -o short 2>/dev/null", timeout=10)
            sys_errors = stdout.read().decode("utf-8", errors="replace")

            # Warnings too
            _, stdout, _ = ssh.exec_command(
                "journalctl -p warning --since '24 hours ago' -n 30 --no-pager -o short 2>/dev/null", timeout=10)
            sys_warnings = stdout.read().decode("utf-8", errors="replace")

            # Kernel errors
            _, stdout, _ = ssh.exec_command(
                "dmesg -l err,crit,alert,emerg -T 2>/dev/null | tail -20", timeout=10)
            kernel_errors = stdout.read().decode("utf-8", errors="replace")

            # Failed services
            _, stdout, _ = ssh.exec_command(
                "systemctl --failed --no-pager --no-legend 2>/dev/null", timeout=5)
            failed_svcs = stdout.read().decode("utf-8", errors="replace").strip()

            # Disk SMART
            _, stdout, _ = ssh.exec_command(
                "smartctl -H /dev/sda 2>/dev/null | grep -i 'health\\|result'", timeout=5)
            smart_out = stdout.read().decode("utf-8", errors="replace").strip()

            # Network errors
            _, stdout, _ = ssh.exec_command(
                "ip -s link show 2>/dev/null | grep -A1 'errors\\|dropped' | grep -v '0$' | head -10", timeout=5)
            net_errors = stdout.read().decode("utf-8", errors="replace").strip()

            # APT check
            _, stdout, _ = ssh.exec_command(
                "apt-get check 2>&1 | grep -i 'error\\|broken' | head -5", timeout=5)
            apt_errors = stdout.read().decode("utf-8", errors="replace").strip()

            ssh.close()

            # Analyze all logs with rules
            all_logs = sys_errors + "\n" + sys_warnings + "\n" + kernel_errors
            seen_rules = set()

            for line in all_logs.split("\n"):
                line_lower = line.lower().strip()
                if not line_lower:
                    continue

                for pattern, cat, severity, title, solution in DIAGNOSTIC_RULES:
                    if pattern.lower() in line_lower and pattern not in seen_rules:
                        seen_rules.add(pattern)
                        nd["issues"].append({
                            "category": cat,
                            "severity": severity,
                            "title": title,
                            "solution": solution,
                            "sample": line.strip()[:200],
                        })
                        result["summary"][severity] = result["summary"].get(severity, 0) + 1

            # Failed services
            if failed_svcs:
                for line in failed_svcs.split("\n"):
                    parts = line.split()
                    if parts:
                        svc_name = parts[0]
                        nd["services_failed"].append(svc_name)
                        nd["issues"].append({
                            "category": "Services",
                            "severity": "critical",
                            "title": f"Service en echec: {svc_name}",
                            "solution": f"Verifier: systemctl status {svc_name}. Relancer: systemctl restart {svc_name}. Logs: journalctl -u {svc_name} -n 20",
                            "sample": line.strip(),
                        })
                        result["summary"]["critical"] += 1

            # SMART
            if smart_out:
                nd["disk_health"].append(smart_out)
                if "FAILED" in smart_out.upper():
                    nd["issues"].append({
                        "category": "Materiel",
                        "severity": "critical",
                        "title": "Disque SMART: ECHEC - Remplacement urgent !",
                        "solution": "Le disque montre des signes de defaillance. Planifier un remplacement IMMEDIAT. Sauvegarder les donnees.",
                        "sample": smart_out,
                    })
                    result["summary"]["critical"] += 1

            # APT
            if apt_errors:
                nd["issues"].append({
                    "category": "Mises a jour",
                    "severity": "warning",
                    "title": "Probleme APT detecte",
                    "solution": "Verifier /etc/apt/sources.list. Essayer: apt-get update && apt-get -f install",
                    "sample": apt_errors[:200],
                })
                result["summary"]["warning"] += 1

            # Store raw errors for display
            for line in sys_errors.split("\n")[:20]:
                if line.strip():
                    nd["raw_errors"].append(line.strip())

        except Exception as e:
            nd["issues"].append({
                "category": "Connexion",
                "severity": "critical",
                "title": f"Impossible de se connecter en SSH a {nn}",
                "solution": f"Verifier que SSH est actif et que le mot de passe est correct. Erreur: {e}",
                "sample": str(e),
            })
            result["summary"]["critical"] += 1

        result["nodes"].append(nd)

    return jsonify(result)


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


# ── God Mode (VM actions) ───────────────────────────────────────────────────

@app.route("/api/vm/action", methods=["POST"])
def api_vm_action():
    """Execute une action sur une VM/CT (start, stop, kill, delete)."""
    data = flask_request.get_json()
    if not data:
        return jsonify({"success": False, "error": "Donnees manquantes"}), 400

    node = data.get("node", "").strip()
    vmid = data.get("vmid", "")
    action = data.get("action", "").strip()
    vm_type = data.get("type", "qemu").strip()  # qemu or lxc

    if not node or not vmid or not action:
        return jsonify({"success": False, "error": "node, vmid et action requis"}), 400

    host_api, ticket, csrf = get_ticket()
    if not host_api:
        return jsonify({"success": False, "error": "Non connecte"}), 503

    # Find actual host for this node
    node_ip = None
    cluster_status = proxmox_api(host_api, "/cluster/status")
    if isinstance(cluster_status, list):
        for item in cluster_status:
            if item.get("type") == "node" and item.get("name") == node:
                node_ip = item.get("ip")

    target_host = node_ip or host_api
    base = f"https://{target_host}:{PROXMOX_CLUSTER['port']}/api2/json/nodes/{node}/{vm_type}/{vmid}"

    cookies = {"PVEAuthCookie": ticket}
    headers = {"CSRFPreventionToken": csrf}

    try:
        if action == "start":
            r = requests.post(f"{base}/status/start", headers=headers, cookies=cookies, verify=False, timeout=10)
        elif action == "shutdown":
            r = requests.post(f"{base}/status/shutdown", headers=headers, cookies=cookies, verify=False, timeout=10)
        elif action == "stop":
            r = requests.post(f"{base}/status/stop", headers=headers, cookies=cookies, data={"skiplock": 1}, verify=False, timeout=10)
        elif action == "reboot":
            r = requests.post(f"{base}/status/reboot", headers=headers, cookies=cookies, verify=False, timeout=10)
        elif action == "reset":
            r = requests.post(f"{base}/status/reset", headers=headers, cookies=cookies, data={"skiplock": 1}, verify=False, timeout=10)
        elif action == "delete":
            # Must be stopped first
            status_r = requests.get(f"{base}/status/current", headers=headers, cookies=cookies, verify=False, timeout=5)
            if status_r.status_code == 200:
                vm_status = status_r.json().get("data", {}).get("status", "")
                if vm_status == "running":
                    # Force stop first
                    requests.post(f"{base}/status/stop", headers=headers, cookies=cookies, data={"skiplock": 1}, verify=False, timeout=10)
                    time.sleep(5)
            r = requests.delete(f"{base}", headers=headers, cookies=cookies, params={"purge": 1, "destroy-unreferenced-disks": 1, "skiplock": 1}, verify=False, timeout=10)
        else:
            return jsonify({"success": False, "error": f"Action inconnue: {action}"}), 400

        if r.status_code == 200:
            # Invalidate status cache
            with _status_cache["lock"]:
                _status_cache["time"] = 0
            return jsonify({"success": True, "message": f"Action '{action}' executee sur VM {vmid}", "upid": r.json().get("data", "")})
        else:
            error_msg = r.json().get("message", r.text[:200]) if r.headers.get("content-type", "").startswith("application/json") else r.text[:200]
            return jsonify({"success": False, "error": error_msg})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)})


# ── Pre-replication check ───────────────────────────────────────────────────

@app.route("/api/replication-check", methods=["POST"])
def api_replication_check():
    """Analyse les prerequis de replication entre deux noeuds."""
    data = flask_request.get_json()
    source = data.get("source", "").strip()
    target = data.get("target", "").strip()

    if not source or not target:
        return jsonify({"error": "Source et destination requis"}), 400
    if source == target:
        return jsonify({"error": "Source et destination doivent etre differents"}), 400

    host_api, ticket, _ = get_ticket()
    if not host_api:
        return jsonify({"error": "Non connecte"}), 503

    checks = []
    blockers = 0
    warnings = 0

    def chk(name, ok, detail, severity="blocker"):
        nonlocal blockers, warnings
        if not ok:
            if severity == "blocker":
                blockers += 1
            else:
                warnings += 1
        checks.append({"name": name, "ok": ok, "detail": detail, "severity": severity})

    # Get node IPs
    node_ips = {}
    cluster_status = proxmox_api(host_api, "/cluster/status")
    if isinstance(cluster_status, list):
        for item in cluster_status:
            if item.get("type") == "node":
                node_ips[item.get("name")] = item.get("ip")

    # Check nodes are online
    nodes = proxmox_api(host_api, "/nodes")
    if not isinstance(nodes, list):
        return jsonify({"error": "Impossible de lister les noeuds"}), 503

    src_online = any(n.get("node") == source and n.get("status") == "online" for n in nodes)
    tgt_online = any(n.get("node") == target and n.get("status") == "online" for n in nodes)
    chk(f"Noeud source ({source}) en ligne", src_online, "En ligne" if src_online else "HORS LIGNE !")
    chk(f"Noeud destination ({target}) en ligne", tgt_online, "En ligne" if tgt_online else "HORS LIGNE !")

    if not src_online or not tgt_online:
        return jsonify({"checks": checks, "blockers": blockers, "warnings": warnings,
                        "ready": False, "summary": "Noeuds non accessibles"})

    # Check via SSH on both nodes
    results = {}
    for node_name in [source, target]:
        node_ip = node_ips.get(node_name)
        if not node_ip:
            continue
        nd = {"zfs_available": False, "zfs_datasets": [], "storage_zfs": [],
              "storage_types": {}, "free_space": {}, "connectivity": False,
              "ram_free": 0, "cpu_cores": 0}
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(node_ip, username=PROXMOX_CLUSTER["username"].split("@")[0],
                        password=PROXMOX_CLUSTER["password"], timeout=5)

            # ZFS available?
            _, stdout, _ = ssh.exec_command("which zfs 2>/dev/null && zfs list -t filesystem -H -o name 2>/dev/null", timeout=5)
            zfs_out = stdout.read().decode().strip()
            nd["zfs_available"] = bool(zfs_out) and "/zfs" not in zfs_out  # not just the binary path
            _, stdout, _ = ssh.exec_command("zfs list -t filesystem -H -o name,used,avail 2>/dev/null", timeout=5)
            datasets = stdout.read().decode().strip()
            if datasets:
                nd["zfs_available"] = True
                for line in datasets.split("\n"):
                    parts = line.split("\t")
                    if len(parts) >= 3:
                        nd["zfs_datasets"].append({"name": parts[0], "used": parts[1], "avail": parts[2]})

            # Storage types
            _, stdout, _ = ssh.exec_command("pvesm status 2>/dev/null", timeout=5)
            pvesm = stdout.read().decode().strip()
            for line in pvesm.split("\n")[1:]:
                parts = line.split()
                if len(parts) >= 6:
                    sname = parts[0]
                    stype = parts[1]
                    nd["storage_types"][sname] = stype
                    try:
                        nd["free_space"][sname] = int(parts[5]) * 1024  # KiB to bytes
                    except (ValueError, IndexError):
                        pass

            # Network connectivity to other node
            other_ip = node_ips.get(target if node_name == source else source)
            if other_ip:
                _, stdout, _ = ssh.exec_command(f"ping -c 2 -W 2 {other_ip} 2>/dev/null | tail -1", timeout=10)
                ping_out = stdout.read().decode().strip()
                nd["connectivity"] = "0% packet loss" in ping_out or "avg" in ping_out

            # RAM and CPU
            ns = proxmox_api(host_api, f"/nodes/{node_name}/status")
            if isinstance(ns, dict) and "error" not in ns:
                nd["ram_free"] = ns.get("memory", {}).get("free", 0)
                nd["cpu_cores"] = ns.get("cpuinfo", {}).get("cpus", 0)

            ssh.close()
        except Exception as e:
            chk(f"Connexion SSH a {node_name}", False, str(e))

        results[node_name] = nd

    src = results.get(source, {})
    tgt = results.get(target, {})

    # ── ZFS CHECKS (required for replication) ──
    chk(f"ZFS disponible sur {source}",
        src.get("zfs_available", False),
        f"{len(src.get('zfs_datasets',[]))} datasets" if src.get("zfs_available") else "PAS DE ZFS ! La replication Proxmox necessite ZFS.",
        "blocker")

    chk(f"ZFS disponible sur {target}",
        tgt.get("zfs_available", False),
        f"{len(tgt.get('zfs_datasets',[]))} datasets" if tgt.get("zfs_available") else "PAS DE ZFS ! La replication Proxmox necessite ZFS.",
        "blocker")

    # Storage compatibility
    src_storages = set(src.get("storage_types", {}).keys())
    tgt_storages = set(tgt.get("storage_types", {}).keys())
    common = src_storages & tgt_storages
    chk("Stockages communs entre source et destination",
        len(common) > 0,
        f"Communs: {', '.join(common)}" if common else f"Source: {', '.join(src_storages)} | Dest: {', '.join(tgt_storages)}",
        "blocker")

    # ZFS storage specifically
    src_zfs_sto = [s for s, t in src.get("storage_types", {}).items() if t in ("zfspool", "zfs")]
    tgt_zfs_sto = [s for s, t in tgt.get("storage_types", {}).items() if t in ("zfspool", "zfs")]
    chk(f"Stockage ZFS sur {source}",
        len(src_zfs_sto) > 0 or src.get("zfs_available"),
        f"ZFS storages: {', '.join(src_zfs_sto)}" if src_zfs_sto else "Aucun storage ZFS (LVM-thin n'est PAS compatible replication)",
        "blocker" if not src.get("zfs_available") else "warning")

    chk(f"Stockage ZFS sur {target}",
        len(tgt_zfs_sto) > 0 or tgt.get("zfs_available"),
        f"ZFS storages: {', '.join(tgt_zfs_sto)}" if tgt_zfs_sto else "Aucun storage ZFS sur la destination",
        "blocker" if not tgt.get("zfs_available") else "warning")

    # Network connectivity
    chk(f"Connectivite reseau {source} → {target}",
        src.get("connectivity", False),
        "Ping OK" if src.get("connectivity") else "ECHEC PING ! Verifier le reseau.",
        "blocker")

    chk(f"Connectivite reseau {target} → {source}",
        tgt.get("connectivity", False),
        "Ping OK" if tgt.get("connectivity") else "ECHEC PING !",
        "blocker")

    # Free space on target
    for sname, free in tgt.get("free_space", {}).items():
        chk(f"Espace libre sur {target}/{sname}",
            free > 10 * 1024**3,
            fmt_bytes(free),
            "warning" if free > 5 * 1024**3 else "blocker")

    # RAM available
    chk(f"RAM libre sur {target}",
        tgt.get("ram_free", 0) > 1024**3,
        fmt_bytes(tgt.get("ram_free", 0)),
        "warning")

    # VMs on source that could be replicated
    vms = proxmox_api(host_api, f"/nodes/{source}/qemu")
    vm_list = []
    if isinstance(vms, list):
        for vm in vms:
            vmid = vm.get("vmid")
            vm_name = vm.get("name", f"VM {vmid}")
            vm_cfg = proxmox_api(host_api, f"/nodes/{source}/qemu/{vmid}/config")
            disks_on_zfs = False
            disk_info = []
            if isinstance(vm_cfg, dict):
                for k, v in vm_cfg.items():
                    if isinstance(v, str) and ":" in v and "media" not in v:
                        if any(k.startswith(p) for p in ("scsi", "virtio", "ide", "sata", "efidisk")):
                            storage = v.split(":")[0]
                            stype = src.get("storage_types", {}).get(storage, "unknown")
                            on_zfs = stype in ("zfspool", "zfs")
                            disk_info.append({"disk": k, "storage": storage, "type": stype, "zfs": on_zfs})
                            if on_zfs:
                                disks_on_zfs = True

            vm_list.append({"vmid": vmid, "name": vm_name, "status": vm.get("status"),
                           "disks": disk_info, "replicable": disks_on_zfs})

    ready = blockers == 0
    summary = "Pret pour la replication" if ready else f"{blockers} probleme(s) bloquant(s)"
    if not ready and not src.get("zfs_available") and not tgt.get("zfs_available"):
        summary = "BLOQUANT: La replication Proxmox necessite ZFS sur les deux noeuds. Vos noeuds utilisent LVM-thin."

    return jsonify({
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "ready": ready,
        "summary": summary,
        "vms": vm_list,
        "source": source,
        "target": target,
    })


# ── Ceph Wizard ─────────────────────────────────────────────────────────────

@app.route("/ceph-wizard")
def ceph_wizard():
    return render_template("ceph-wizard.html")


@app.route("/api/ceph/scan")
def api_ceph_scan():
    """Scan complet du cluster pour le wizard Ceph."""
    host_api, ticket, _ = get_ticket()
    if not host_api:
        return jsonify({"error": "Non connecte"}), 503

    result = {"nodes": [], "cluster": {}, "checks": [], "ready": True}

    # Cluster info
    cluster_status = proxmox_api(host_api, "/cluster/status")
    node_ips = {}
    if isinstance(cluster_status, list):
        for item in cluster_status:
            if item.get("type") == "cluster":
                result["cluster"]["name"] = item.get("name")
                result["cluster"]["quorate"] = bool(item.get("quorate"))
                result["cluster"]["nodes_count"] = item.get("nodes")
            elif item.get("type") == "node":
                node_ips[item.get("name")] = item.get("ip")

    nodes_list = proxmox_api(host_api, "/nodes")
    if not isinstance(nodes_list, list):
        return jsonify({"error": "Impossible de lister les noeuds"}), 503

    for node in sorted(nodes_list, key=lambda n: n.get("node", "")):
        nn = node.get("node", "")
        node_ip = node_ips.get(nn, "")
        nd = {
            "name": nn, "ip": node_ip,
            "online": node.get("status") == "online",
            "cpu_cores": 0, "ram_total_gb": 0, "ram_free_gb": 0,
            "pve_version": "", "ceph_installed": False, "ceph_version": "",
            "ntp_synced": False, "ntp_drift": "",
            "disks": [], "free_disks": [],
            "interfaces": [], "mtu": 1500,
        }

        if not nd["online"] or not node_ip:
            result["nodes"].append(nd)
            continue

        # Get node status via API
        ns = proxmox_api(host_api, f"/nodes/{nn}/status")
        if isinstance(ns, dict) and "error" not in ns:
            nd["cpu_cores"] = ns.get("cpuinfo", {}).get("cpus", 0)
            nd["ram_total_gb"] = round(ns.get("memory", {}).get("total", 0) / (1024**3), 1)
            nd["ram_free_gb"] = round(ns.get("memory", {}).get("free", 0) / (1024**3), 1)
            nd["pve_version"] = ns.get("pveversion", "")

        # Detailed scan via SSH
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(node_ip,
                        username=PROXMOX_CLUSTER["username"].split("@")[0],
                        password=PROXMOX_CLUSTER["password"], timeout=5)

            # Ceph installed?
            _, stdout, _ = ssh.exec_command("dpkg -l ceph-mon 2>/dev/null | grep -c '^ii'", timeout=5)
            nd["ceph_installed"] = stdout.read().decode().strip() == "1"

            _, stdout, _ = ssh.exec_command("ceph -v 2>/dev/null", timeout=5)
            nd["ceph_version"] = stdout.read().decode().strip()

            # NTP
            _, stdout, _ = ssh.exec_command("chronyc tracking 2>/dev/null | grep 'System time'", timeout=5)
            ntp_line = stdout.read().decode().strip()
            if ntp_line:
                nd["ntp_synced"] = True
                import re
                m = re.search(r'([\d.]+) seconds', ntp_line)
                if m:
                    drift = float(m.group(1))
                    nd["ntp_drift"] = f"{drift*1000:.1f} ms"
                    if drift > 0.5:
                        nd["ntp_synced"] = False

            # Disks
            _, stdout, _ = ssh.exec_command(
                "lsblk -J -o NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE,ROTA,MODEL -p 2>/dev/null", timeout=5)
            try:
                import json as _json
                disk_data = _json.loads(stdout.read().decode())
                for bd in disk_data.get("blockdevices", []):
                    if bd.get("type") != "disk" or bd.get("name", "").startswith("/dev/sr"):
                        continue
                    children = bd.get("children", [])
                    has_mount = any(c.get("mountpoint") for c in children)
                    has_fs = any(c.get("fstype") for c in children)
                    disk_info = {
                        "name": bd["name"],
                        "size": bd.get("size", ""),
                        "rotational": bd.get("rota", True),
                        "type": "HDD" if bd.get("rota") else "SSD/NVMe",
                        "model": bd.get("model", ""),
                        "partitions": len(children),
                        "mounted": has_mount,
                        "has_fs": has_fs,
                        "available": not has_mount and len(children) == 0,
                        "usable_for_osd": not has_mount,
                    }
                    nd["disks"].append(disk_info)
                    if disk_info["available"]:
                        nd["free_disks"].append(disk_info)
            except Exception:
                pass

            # Network interfaces
            _, stdout, _ = ssh.exec_command("ip -j addr show 2>/dev/null", timeout=5)
            try:
                import json as _json
                net_data = _json.loads(stdout.read().decode())
                for iface in net_data:
                    if iface.get("operstate") != "UP" or iface.get("ifname", "").startswith("lo"):
                        continue
                    addrs = [a["local"] for a in iface.get("addr_info", []) if a.get("family") == "inet"]
                    nd["interfaces"].append({
                        "name": iface["ifname"],
                        "ips": addrs,
                        "mtu": iface.get("mtu", 1500),
                    })
                    if iface.get("ifname") == "vmbr0":
                        nd["mtu"] = iface.get("mtu", 1500)
            except Exception:
                pass

            ssh.close()
        except Exception:
            pass

        result["nodes"].append(nd)

    # ── Pre-flight checks ──
    online_nodes = [n for n in result["nodes"] if n["online"]]

    # Min 3 nodes
    if len(online_nodes) < 3:
        result["checks"].append({"level": "critical", "msg": f"Seulement {len(online_nodes)} noeuds en ligne. Ceph requiert minimum 3."})
        result["ready"] = False
    else:
        result["checks"].append({"level": "ok", "msg": f"{len(online_nodes)} noeuds en ligne"})

    # Quorum
    if not result["cluster"].get("quorate"):
        result["checks"].append({"level": "critical", "msg": "Quorum perdu ! Impossible de deployer Ceph."})
        result["ready"] = False
    else:
        result["checks"].append({"level": "ok", "msg": "Quorum OK"})

    # NTP
    for n in online_nodes:
        if not n["ntp_synced"]:
            result["checks"].append({"level": "warning", "msg": f"{n['name']}: NTP non synchronise (drift: {n['ntp_drift']})"})

    # PVE version homogeneity
    versions = set(n["pve_version"].split("/")[1] if "/" in n.get("pve_version", "") else "" for n in online_nodes)
    versions.discard("")
    if len(versions) > 1:
        result["checks"].append({"level": "warning", "msg": f"Versions PVE heterogenes: {', '.join(versions)}"})
    else:
        result["checks"].append({"level": "ok", "msg": f"Version PVE homogene"})

    # RAM per node
    for n in online_nodes:
        if n["ram_total_gb"] < 8:
            result["checks"].append({"level": "warning", "msg": f"{n['name']}: RAM faible ({n['ram_total_gb']} GB). 8+ GB recommande."})

    # Network
    for n in online_nodes:
        if n["mtu"] < 9000:
            result["checks"].append({"level": "info", "msg": f"{n['name']}: MTU {n['mtu']} (jumbo frames 9000 recommande pour Ceph)"})

    # Available disks
    total_free = sum(len(n["free_disks"]) for n in online_nodes)
    if total_free == 0:
        result["checks"].append({"level": "warning", "msg": "Aucun disque vierge disponible. Les OSD necessitent des disques dedies."})
    else:
        result["checks"].append({"level": "ok", "msg": f"{total_free} disque(s) disponible(s) pour OSD"})

    return jsonify(result)


@app.route("/api/ceph/purge-all", methods=["POST"])
def api_ceph_purge_all():
    """Purge Ceph sur tous les noeuds du cluster."""
    host_api, ticket, _ = get_ticket()
    if not host_api:
        return jsonify({"error": "Non connecte"}), 503

    cluster_status = proxmox_api(host_api, "/cluster/status")
    node_ips = {}
    if isinstance(cluster_status, list):
        for item in cluster_status:
            if item.get("type") == "node":
                node_ips[item.get("name")] = item.get("ip")

    log = []
    for nn, node_ip in sorted(node_ips.items()):
        if not node_ip:
            continue
        log.append(f"Purge sur {nn} ({node_ip})...")
        try:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(node_ip, username=PROXMOX_CLUSTER["username"].split("@")[0],
                        password=PROXMOX_CLUSTER["password"], timeout=10)
            _, stdout, _ = ssh.exec_command(
                'systemctl stop ceph.target 2>/dev/null; sleep 2; '
                'killall -9 ceph-mon ceph-mgr ceph-osd 2>/dev/null; sleep 1; '
                'umount /var/lib/ceph/osd/* 2>/dev/null; '
                'for dm in /dev/mapper/ceph-*; do [ -b "$dm" ] && dmsetup remove "$dm" 2>/dev/null; done; '
                'dmsetup remove_all 2>/dev/null; '
                'for vg in $(vgs --noheadings -o vg_name 2>/dev/null | grep ceph); do vgremove -ff $vg 2>/dev/null; done; '
                'for DISK in /dev/sd? /dev/vd?; do '
                '  HM=0; for P in ${DISK}*; do [ "$P" = "$DISK" ] && continue; '
                '  MP=$(lsblk -n -o MOUNTPOINT "$P" 2>/dev/null | head -1); [ -n "$MP" ] && HM=1 && break; done; '
                '  [ "$HM" -eq 0 ] && { pvremove -ff "$DISK" 2>/dev/null; wipefs -af "$DISK" 2>/dev/null; '
                '  sgdisk --zap-all "$DISK" 2>/dev/null; dd if=/dev/zero of="$DISK" bs=1M count=200 2>/dev/null; }; '
                'done; '
                'rm -rf /var/lib/ceph /tmp/ceph* /tmp/monmap* 2>/dev/null; '
                'rm -f /etc/pve/ceph.conf /etc/pve/priv/ceph.* /etc/ceph/ceph.client.admin.keyring 2>/dev/null; '
                'rm -rf /etc/pve/ceph/ /etc/systemd/system/ceph-mon@.service.d '
                '/etc/systemd/system/ceph-osd@.service.d /etc/systemd/system/ceph-mgr@.service.d 2>/dev/null; '
                'apt-get purge -y ceph-mon ceph-mgr ceph-osd ceph-base ceph-volume ceph-mds 2>/dev/null; '
                'apt-get autoremove -y 2>/dev/null; apt-get install -y ceph-common ceph-fuse 2>/dev/null; '
                'partprobe 2>/dev/null; udevadm settle 2>/dev/null; systemctl daemon-reload 2>/dev/null; '
                'echo PURGED', timeout=180)
            out = stdout.read().decode("utf-8", "replace").strip()
            log.append(f"  {nn}: {'OK' if 'PURGED' in out else out[-100:]}")
            ssh.close()
        except Exception as e:
            log.append(f"  {nn}: Erreur - {e}")

    # Remove PVE storages
    cookies = {"PVEAuthCookie": ticket}
    headers = {"CSRFPreventionToken": csrf}
    for s in ["ceph-vm", "ceph-pool"]:
        try:
            requests.delete(f"https://{host_api}:{PROXMOX_CLUSTER['port']}/api2/json/storage/{s}",
                           headers=headers, cookies=cookies, verify=False, timeout=10)
        except Exception:
            pass

    log.append("Purge terminee sur tous les noeuds.")
    return jsonify({"success": True, "log": log})


@app.route("/api/ceph/install", methods=["POST"])
def api_ceph_install():
    """Execute une etape du wizard Ceph."""
    data = flask_request.get_json()
    step = data.get("step", "")
    nodes = data.get("nodes", [])
    config = data.get("config", {})

    host_api, ticket, csrf = get_ticket()
    if not host_api:
        return jsonify({"error": "Non connecte"}), 503

    # Get node IPs
    cluster_status = proxmox_api(host_api, "/cluster/status")
    node_ips = {}
    if isinstance(cluster_status, list):
        for item in cluster_status:
            if item.get("type") == "node":
                node_ips[item.get("name")] = item.get("ip")

    cookies = {"PVEAuthCookie": ticket}
    headers = {"CSRFPreventionToken": csrf}
    log = []

    try:
        if step == "install_ceph":
            # Step 1: Install Ceph on all selected nodes via SSH
            ceph_ver = config.get("ceph_version", "squid")
            for nn in nodes:
                node_ip = node_ips.get(nn)
                if not node_ip:
                    continue
                log.append(f"Installation de Ceph sur {nn} (version {ceph_ver})...")
                try:
                    ssh = paramiko.SSHClient()
                    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    ssh.connect(node_ip,
                                username=PROXMOX_CLUSTER["username"].split("@")[0],
                                password=PROXMOX_CLUSTER["password"], timeout=10)
                    # Check if already installed
                    _, stdout, _ = ssh.exec_command("dpkg -l ceph-mon 2>/dev/null | grep -c '^ii'", timeout=5)
                    already = stdout.read().decode().strip() == "1"
                    if already:
                        log.append(f"  {nn}: Ceph deja installe, skip")
                    else:
                        log.append(f"  {nn}: pveceph install --version {ceph_ver} (peut prendre quelques minutes)...")
                        # Fix repos first (ensure no-subscription + ceph repo)
                    ssh.exec_command(
                        f'echo "deb http://download.proxmox.com/debian/ceph-{ceph_ver} trixie no-subscription" > /etc/apt/sources.list.d/ceph.list && '
                        'for f in /etc/apt/sources.list.d/*enterprise*; do [ -f "$f" ] && mv "$f" "${f}.disabled"; done 2>/dev/null; '
                        'apt-get update -qq 2>/dev/null',
                        timeout=30)
                    time.sleep(2)
                    # Install via apt (more reliable than pveceph install)
                    _, stdout, stderr = ssh.exec_command(
                        "DEBIAN_FRONTEND=noninteractive apt-get install -y --allow-downgrades "
                        "ceph-mon ceph-mgr ceph-osd ceph-volume 2>&1",
                        timeout=600)
                    out = stdout.read().decode("utf-8", "replace").strip()
                    err = stderr.read().decode("utf-8", "replace").strip()
                    if "error" in (out + err).lower():
                        log.append(f"  {nn}: ERREUR - {(out + err)[:300]}")
                    else:
                        log.append(f"  {nn}: Installation terminee")
                        # Show last lines
                        for line in (out + err).split("\n")[-3:]:
                            if line.strip():
                                log.append(f"    {line.strip()}")
                    ssh.close()
                except Exception as e:
                    log.append(f"  {nn}: Erreur SSH - {e}")

        elif step == "fix_permissions":
            # Fix PVE 9 permissions bug on all nodes
            for nn in nodes:
                node_ip = node_ips.get(nn)
                if not node_ip:
                    continue
                log.append(f"Fix permissions sur {nn}...")
                try:
                    ssh = paramiko.SSHClient()
                    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    ssh.connect(node_ip, username=PROXMOX_CLUSTER["username"].split("@")[0],
                                password=PROXMOX_CLUSTER["password"], timeout=10)
                    _, stdout, _ = ssh.exec_command(
                        'usermod -aG www-data ceph 2>/dev/null; '
                        'mkdir -p /etc/systemd/system/ceph-mon@.service.d; '
                        'echo -e "[Service]\\nExecStart=\\nExecStart=/usr/bin/ceph-mon -f --cluster \\${CLUSTER} --id %i --setuser ceph --setgroup www-data" > /etc/systemd/system/ceph-mon@.service.d/override.conf; '
                        'mkdir -p /etc/systemd/system/ceph-osd@.service.d; '
                        'echo -e "[Service]\\nExecStart=\\nExecStart=/usr/bin/ceph-osd -f --cluster \\${CLUSTER} --id %i --setuser ceph --setgroup www-data" > /etc/systemd/system/ceph-osd@.service.d/override.conf; '
                        'mkdir -p /etc/systemd/system/ceph-mgr@.service.d; '
                        'echo -e "[Service]\\nExecStart=\\nExecStart=/usr/bin/ceph-mgr -f --cluster \\${CLUSTER} --id %i --setuser ceph --setgroup www-data" > /etc/systemd/system/ceph-mgr@.service.d/override.conf; '
                        'systemctl daemon-reload; echo DONE', timeout=30)
                    out = stdout.read().decode("utf-8", "replace").strip()
                    log.append(f"  {nn}: {'OK' if 'DONE' in out else out[:100]}")
                    ssh.close()
                except Exception as e:
                    log.append(f"  {nn}: Erreur - {e}")

        elif step == "init_ceph":
            # Init + bootstrap first MON (validated procedure)
            first_node = nodes[0] if nodes else ""
            node_ip = node_ips.get(first_node, host_api)
            network = config.get("public_network", "192.168.100.0/24")

            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(node_ip, username=PROXMOX_CLUSTER["username"].split("@")[0],
                            password=PROXMOX_CLUSTER["password"], timeout=10)

                # pveceph init
                log.append(f"pveceph init --network {network}...")
                _, stdout, _ = ssh.exec_command(f'pveceph init --network {network} 2>&1', timeout=30)
                log.append(f"  {stdout.read().decode('utf-8', 'replace').strip()[:200]}")
                time.sleep(2)

                # Get FSID
                _, stdout, _ = ssh.exec_command('python3 -c "import re; c=open(\'/etc/pve/ceph.conf\').read(); m=re.search(r\'[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}\', c); print(m.group(0) if m else \'\')"', timeout=5)
                fsid = stdout.read().decode().strip()
                log.append(f"  FSID: {fsid}")
                if not fsid:
                    ssh.close()
                    return jsonify({"success": False, "log": log, "error": "FSID vide"})

                # Bootstrap MON manually
                log.append(f"Bootstrap MON sur {first_node}...")
                bootstrap_cmds = [
                    f'rm -rf /var/lib/ceph/mon/ceph-{first_node}; mkdir -p /var/lib/ceph/mon/ceph-{first_node}',
                    'rm -f /tmp/ceph.mon.keyring /tmp/monmap',
                    'ceph-authtool --create-keyring /tmp/ceph.mon.keyring --gen-key -n mon. --cap mon "allow *" 2>&1',
                    'ceph-authtool /tmp/ceph.mon.keyring --import-keyring /etc/pve/priv/ceph.client.admin.keyring 2>&1',
                    'chmod 600 /tmp/ceph.mon.keyring',
                    f'monmaptool --create --add {first_node} {node_ip} --fsid {fsid} /tmp/monmap 2>&1',
                    f'ceph-mon --mkfs -i {first_node} --monmap /tmp/monmap --keyring /tmp/ceph.mon.keyring 2>&1',
                    f'chown -R ceph:www-data /var/lib/ceph/mon/ceph-{first_node}',
                    'ln -sf /etc/pve/ceph.conf /etc/ceph/ceph.conf',
                    'cp /etc/pve/priv/ceph.client.admin.keyring /etc/ceph/ceph.client.admin.keyring 2>/dev/null',
                ]
                for cmd in bootstrap_cmds:
                    ssh.exec_command(cmd, timeout=30)
                    time.sleep(1)

                # Fix ceph.conf
                _, stdout, _ = ssh.exec_command(
                    f'python3 -c "'
                    f'import re; c=open(\"/etc/pve/ceph.conf\").read(); '
                    f'c=re.sub(r\"^tmon_host.*\\n\",\"\",c,flags=re.MULTILINE); '
                    f'c=c.replace(\"[global]\",\"[global]\\n\\tmon_host = {node_ip}\") if \"mon_host\" not in c else c; '
                    f'c+=f\"\\n[mon.{first_node}]\\n\\tpublic_addr = {node_ip}\\n\" if \"mon.{first_node}\" not in c else \"\"; '
                    f'open(\"/etc/pve/ceph.conf\",\"w\").write(c)"', timeout=10)

                # Start MON with retries
                log.append(f"  Demarrage MON (avec retry)...")
                for attempt in range(4):
                    ssh.exec_command(f'systemctl reset-failed ceph-mon@{first_node} 2>/dev/null; systemctl enable ceph-mon@{first_node} 2>/dev/null; systemctl start ceph-mon@{first_node} 2>/dev/null')
                    time.sleep(15)
                    _, stdout, _ = ssh.exec_command(f'systemctl is-active ceph-mon@{first_node}')
                    status = stdout.read().decode().strip()
                    if status == 'active':
                        break

                if status == 'active':
                    log.append(f"  MON {first_node}: ACTIF")
                    ssh.exec_command('ceph config set mon auth_allow_insecure_global_id_reclaim false 2>/dev/null; ceph mon enable-msgr2 2>/dev/null')
                    time.sleep(3)
                else:
                    log.append(f"  MON {first_node}: ECHOUE ({status})")

                ssh.close()
            except Exception as e:
                log.append(f"  Erreur: {e}")

        elif step == "create_mon":
            # Create MON on secondary nodes (using monmap from cluster)
            for nn in nodes:
                node_ip = node_ips.get(nn)
                if not node_ip:
                    continue
                log.append(f"Creation MON sur {nn}...")
                try:
                    ssh = paramiko.SSHClient()
                    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    ssh.connect(node_ip, username=PROXMOX_CLUSTER["username"].split("@")[0],
                                password=PROXMOX_CLUSTER["password"], timeout=10)
                    ssh.exec_command(f'systemctl stop ceph-mon@{nn} 2>/dev/null; rm -rf /var/lib/ceph/mon/ceph-{nn}; mkdir -p /var/lib/ceph/mon/ceph-{nn}')
                    ssh.exec_command('ln -sf /etc/pve/ceph.conf /etc/ceph/ceph.conf; ceph auth get-or-create client.admin mon "allow *" osd "allow *" mds "allow *" mgr "allow *" > /etc/ceph/ceph.client.admin.keyring 2>/dev/null; chmod 644 /etc/ceph/ceph.client.admin.keyring')
                    ssh.exec_command('ceph mon getmap -o /tmp/monmap.bin 2>/dev/null')
                    time.sleep(2)
                    ssh.exec_command(f'ceph-mon --mkfs -i {nn} --monmap /tmp/monmap.bin --keyring /etc/ceph/ceph.client.admin.keyring 2>&1')
                    ssh.exec_command(f'chown -R ceph:www-data /var/lib/ceph/mon/ceph-{nn}')
                    # Start with retry
                    for attempt in range(3):
                        ssh.exec_command(f'systemctl reset-failed ceph-mon@{nn} 2>/dev/null; systemctl enable ceph-mon@{nn} 2>/dev/null; systemctl start ceph-mon@{nn} 2>/dev/null')
                        time.sleep(15)
                        _, stdout, _ = ssh.exec_command(f'systemctl is-active ceph-mon@{nn}')
                        status = stdout.read().decode().strip()
                        if status == 'active':
                            break
                    log.append(f"  {nn}: {status}")
                    ssh.close()
                except Exception as e:
                    log.append(f"  {nn}: Erreur - {e}")

        elif step == "create_mgr":
            # Create MGR on each node
            for nn in nodes:
                node_ip = node_ips.get(nn)
                if not node_ip:
                    continue
                log.append(f"Creation MGR sur {nn}...")
                try:
                    ssh = paramiko.SSHClient()
                    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    ssh.connect(node_ip, username=PROXMOX_CLUSTER["username"].split("@")[0],
                                password=PROXMOX_CLUSTER["password"], timeout=10)
                    ssh.exec_command(f'rm -rf /var/lib/ceph/mgr/ceph-{nn} 2>/dev/null; pveceph mgr create 2>&1', timeout=30)
                    time.sleep(10)
                    _, stdout, _ = ssh.exec_command(f'systemctl is-active ceph-mgr@{nn}')
                    status = stdout.read().decode().strip()
                    if status != 'active':
                        ssh.exec_command(f'systemctl reset-failed ceph-mgr@{nn}; systemctl start ceph-mgr@{nn} 2>/dev/null')
                        time.sleep(5)
                        _, stdout, _ = ssh.exec_command(f'systemctl is-active ceph-mgr@{nn}')
                        status = stdout.read().decode().strip()
                    log.append(f"  {nn}: {status}")
                    # Ensure bootstrap-osd keyring
                    ssh.exec_command('ceph auth get client.bootstrap-osd > /var/lib/ceph/bootstrap-osd/ceph.keyring 2>/dev/null; chown ceph:ceph /var/lib/ceph/bootstrap-osd/ceph.keyring 2>/dev/null')
                    ssh.close()
                except Exception as e:
                    log.append(f"  {nn}: Erreur - {e}")

        elif step == "create_osd":
            # Create OSD using prepare + activate (no rollback)
            osd_map = config.get("osd_map", {})
            for nn, disks in osd_map.items():
                node_ip = node_ips.get(nn)
                if not node_ip:
                    continue
                for disk in disks:
                    import re as _re
                    if not _re.match(r'^/dev/[a-z]+[0-9]*$', disk):
                        log.append(f"  {nn}:{disk}: Chemin invalide")
                        continue
                    log.append(f"OSD sur {nn}:{disk}...")
                    try:
                        ssh = paramiko.SSHClient()
                        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                        ssh.connect(node_ip, username=PROXMOX_CLUSTER["username"].split("@")[0],
                                    password=PROXMOX_CLUSTER["password"], timeout=10)
                        # Aggressive wipe
                        log.append(f"  Wipe...")
                        _, stdout, _ = ssh.exec_command(
                            f'dmsetup remove_all 2>/dev/null; '
                            f'for vg in $(pvs --noheadings -o vg_name {disk} 2>/dev/null); do vgremove -ff $vg 2>/dev/null; done; '
                            f'pvremove -ff {disk} 2>/dev/null; wipefs -af {disk} 2>/dev/null; '
                            f'sgdisk --zap-all {disk} 2>/dev/null; '
                            f'dd if=/dev/zero of={disk} bs=1M count=500 2>/dev/null; '
                            f'partprobe {disk} 2>/dev/null; udevadm settle 2>/dev/null; echo WIPE_DONE',
                            timeout=60)
                        stdout.read()
                        time.sleep(5)
                        # Prepare (no start = no rollback)
                        log.append(f"  Prepare...")
                        _, stdout, _ = ssh.exec_command(f'ceph-volume lvm prepare --data {disk} 2>&1', timeout=300)
                        out = stdout.read().decode("utf-8", "replace").strip()
                        if "successful" in out:
                            log.append(f"  Prepare: OK")
                        else:
                            log.append(f"  Prepare: {out[-100:]}")
                        time.sleep(3)
                        # Activate
                        log.append(f"  Activate...")
                        ssh.exec_command('ceph-volume lvm activate --all 2>&1', timeout=30)
                        time.sleep(5)
                        # Start with retry
                        osd_dirs = ssh.exec_command('ls /var/lib/ceph/osd/ 2>/dev/null')[1].read().decode().strip()
                        for d in osd_dirs.split():
                            oid = d.replace('ceph-', '')
                            for _ in range(3):
                                ssh.exec_command(f'systemctl reset-failed ceph-osd@{oid} 2>/dev/null; systemctl start ceph-osd@{oid} 2>/dev/null')
                                time.sleep(8)
                                _, sout, _ = ssh.exec_command(f'systemctl is-active ceph-osd@{oid}')
                                if sout.read().decode().strip() == 'active':
                                    break
                            log.append(f"  OSD.{oid}: {sout.read().decode().strip() if hasattr(sout, 'read') else 'started'}")
                        ssh.close()
                    except Exception as e:
                        log.append(f"  {nn}:{disk}: Erreur - {e}")

        elif step == "create_pool":
            # Step 6: Create Ceph pool via SSH (more reliable)
            pool_name = config.get("pool_name", "ceph-pool")
            pg_num = config.get("pg_num", 64)
            size = config.get("replication_size", 3)
            min_size = config.get("min_size", 2)

            first_node = nodes[0] if nodes else ""
            node_ip = node_ips.get(first_node, host_api)

            log.append(f"Verification des OSD avant creation du pool...")
            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(node_ip,
                            username=PROXMOX_CLUSTER["username"].split("@")[0],
                            password=PROXMOX_CLUSTER["password"], timeout=10)

                # Check OSD count first
                _, stdout, _ = ssh.exec_command("ceph osd stat 2>&1", timeout=15)
                osd_stat = stdout.read().decode("utf-8", "replace").strip()
                log.append(f"  OSD stat: {osd_stat}")

                if "0 osds" in osd_stat or "0 up" in osd_stat:
                    log.append(f"  BLOQUANT: Aucun OSD actif ! Impossible de creer le pool.")
                    log.append(f"  Retournez a l'etape OSD et creez des OSD d'abord.")
                    ssh.close()
                    return jsonify({"success": False, "log": log, "error": "Aucun OSD actif"})

                # Create pool
                log.append(f"Creation du pool '{pool_name}' (pg={pg_num}, size={size}, min_size={min_size})...")
                _, stdout, _ = ssh.exec_command(
                    f"ceph osd pool create {pool_name} {pg_num} {pg_num} replicated && "
                    f"ceph osd pool set {pool_name} size {size} && "
                    f"ceph osd pool set {pool_name} min_size {min_size} && "
                    f"ceph osd pool application enable {pool_name} rbd 2>&1",
                    timeout=60)
                out = stdout.read().decode("utf-8", "replace").strip()
                log.append(f"  {out}")

                # Fix keyring for RBD access
                log.append(f"Fix keyring...")
                ssh.exec_command(
                    'ceph auth get-or-create client.admin mon "allow *" osd "allow *" mds "allow *" mgr "allow *" '
                    '> /etc/ceph/ceph.client.admin.keyring 2>/dev/null; chmod 644 /etc/ceph/ceph.client.admin.keyring',
                    timeout=10)

                # Add to Proxmox storage with explicit keyring
                log.append(f"Ajout du stockage RBD dans Proxmox...")
                _, stdout, _ = ssh.exec_command(
                    f"pvesm add rbd {pool_name} -pool {pool_name} -content images,rootdir -krbd 0 "
                    f"-keyring /etc/ceph/ceph.client.admin.keyring 2>&1",
                    timeout=15)
                out = stdout.read().decode("utf-8", "replace").strip()
                if out:
                    log.append(f"  {out}")
                else:
                    log.append(f"  Stockage '{pool_name}' ajoute a Proxmox")

                ssh.close()
            except Exception as e:
                log.append(f"  Erreur: {e}")

        elif step == "purge":
            # Purge Ceph on all nodes
            for nn in nodes:
                node_ip = node_ips.get(nn)
                if not node_ip:
                    continue
                log.append(f"Purge Ceph sur {nn}...")
                try:
                    ssh = paramiko.SSHClient()
                    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    ssh.connect(node_ip,
                                username=PROXMOX_CLUSTER["username"].split("@")[0],
                                password=PROXMOX_CLUSTER["password"], timeout=10)
                    _, stdout, _ = ssh.exec_command(
                        "systemctl stop ceph.target 2>/dev/null; "
                        "killall -9 ceph-mon ceph-mgr ceph-osd 2>/dev/null; "
                        "rm -rf /var/lib/ceph/* /tmp/ceph* /tmp/monmap 2>/dev/null; "
                        "rm -f /etc/pve/ceph.conf /etc/pve/priv/ceph.* 2>/dev/null; "
                        "rm -rf /etc/pve/ceph/ 2>/dev/null; "
                        "wipefs -a /dev/sdc 2>/dev/null; "
                        "dd if=/dev/zero of=/dev/sdc bs=1M count=10 2>/dev/null; "
                        "mkdir -p /var/lib/ceph/{mon,mgr,osd,tmp,crash,bootstrap-osd,bootstrap-mgr}; "
                        "chown -R ceph:ceph /var/lib/ceph/; "
                        "echo PURGED", timeout=30)
                    out = stdout.read().decode("utf-8", "replace").strip()
                    log.append(f"  {nn}: {out}")
                    ssh.close()
                except Exception as e:
                    log.append(f"  {nn}: Erreur - {e}")

            # Remove Ceph storage from Proxmox
            try:
                for sname in ["ceph-vm", "ceph-pool"]:
                    requests.delete(
                        f"https://{host_api}:{PROXMOX_CLUSTER['port']}/api2/json/storage/{sname}",
                        headers=headers, cookies=cookies, verify=False, timeout=10)
            except Exception:
                pass
            log.append("Purge terminee.")

        elif step == "health_check":
            # Step 7: Health check
            first_node = nodes[0] if nodes else ""
            node_ip = node_ips.get(first_node, host_api)

            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh.connect(node_ip,
                            username=PROXMOX_CLUSTER["username"].split("@")[0],
                            password=PROXMOX_CLUSTER["password"], timeout=5)

                for cmd_name, cmd in [
                    ("Ceph Status", "ceph -s 2>&1"),
                    ("Health Detail", "ceph health detail 2>&1"),
                    ("OSD Tree", "ceph osd tree 2>&1"),
                    ("Pool List", "ceph osd pool ls detail 2>&1"),
                    ("DF", "ceph df 2>&1"),
                ]:
                    _, stdout, _ = ssh.exec_command(cmd, timeout=10)
                    log.append(f"--- {cmd_name} ---")
                    log.append(stdout.read().decode("utf-8", "replace").strip())

                ssh.close()
            except Exception as e:
                log.append(f"Erreur SSH: {e}")

        else:
            return jsonify({"success": False, "error": f"Etape inconnue: {step}"}), 400

        return jsonify({"success": True, "log": log})

    except Exception as e:
        log.append(f"Erreur: {e}")
        return jsonify({"success": False, "log": log, "error": str(e)})


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
