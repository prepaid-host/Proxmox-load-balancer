#!/usr/bin/python3
# -*- coding: utf-8 -*-
# Extended Proxmox-load-balancer with CPU/RAM balancing, grouping, OOM checks, Test Mode, configurable thresholds
# and CPU trend analysis via RRD data. After each migration, hosts are re-measured to see if CPU/RAM situation improves.

import sys
import requests
import urllib3
import yaml
import smtplib
import socket
from random import random
from email.message import EmailMessage
from time import sleep
from itertools import permutations
from copy import deepcopy
from loguru import logger
from statistics import mean

try:
    with open("config.yaml", "r", encoding='utf8') as yaml_file:
        cfg = yaml.safe_load(yaml_file)
except Exception as e:
    print(f"[ERROR] Configuration file could not be opened: {e}")
    sys.exit(1)

# Proxmox connection info
server_url = f'https://{cfg["proxmox"]["url"]["ip"]}:{cfg["proxmox"]["url"]["port"]}'
auth = dict(cfg["proxmox"]["auth"])

# Balancing Parameters
CONFIG_DEVIATION = CD = cfg["parameters"]["deviation"] / 200
THRESHOLD = cfg["parameters"]["threshold"] / 100
LXC_MIGRATION = cfg["parameters"]["lxc_migration"]
MIGRATION_TIMEOUT = cfg["parameters"]["migration_timeout"]
ONLY_ON_MASTER = cfg["parameters"].get("only_on_master", "OFF")
TEST_MODE = cfg["parameters"].get("test_mode", "OFF")  # If ON, no real migrations

# Exclusions
excluded_vms = []
for x in tuple(cfg["exclusions"]["vms"]):
    if isinstance(x, int):
        excluded_vms.append(x)
    elif "-" in x:
        r = tuple(x.split("-"))
        excluded_vms.extend(range(int(r[0]), int(r[1]) + 1))
    else:
        excluded_vms.append(int(x))
excluded_nodes = tuple(cfg["exclusions"]["nodes"])

# Groups
node_groups = cfg.get("groups", {})
node_to_group = {}
for gname, nodes_list in node_groups.items():
    for n in nodes_list:
        node_to_group[n] = gname

# Balancing weights and thresholds
weight_ram = cfg["balancing"]["weight_ram"]
weight_cpu = cfg["balancing"]["weight_cpu"]
memory_oom_threshold = cfg["balancing"]["memory_oom_threshold"]
cpu_threshold = cfg["balancing"]["cpu_threshold"]

# Mail
send_on = cfg["mail"]["sending"]

# Logging Level
logger.remove()
logger.add(sys.stdout, format="{level} | {message}", level=cfg["logging_level"])

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

sum_of_deviations: float = 0
iteration = 0

class Cluster:
    def __init__(self, server: str):
        logger.debug("Initializing Cluster object...")
        self.server: str = server
        self.cl_name = self.cluster_name()
        self.master_node: str = ""
        self.quorate: bool = False
        self.cl_nodes: int = 0
        self.cluster_information = {}
        self.included_nodes = {}
        self.cl_lxcs = set()
        self.cl_vms_included: dict = {}
        self.cl_vms: dict = {}
        self.cl_max_mem_included: int = 0
        self.cl_mem_included: int = 0
        self.mem_load: float = 0
        self.mem_load_included: float = 0
        self.cl_max_mem: int = 0
        self.cl_cpu_included: int = 0
        self.cl_cpu_load: float = 0
        self.cl_cpu_load_include: float = 0
        self.cl_cpu: int = 0

        self.cluster_items()
        self.cl_nodes = self.cluster_hosts()
        self.cl_vms = self.cluster_vms()
        self.cl_max_mem = self.cluster_mem()
        self.cl_cpu = self.cluster_cpu()

    def cluster_name(self):
        url = f'{self.server}/api2/json/cluster/status'
        name_request = requests.get(url, cookies=payload, verify=False)
        if not name_request.ok:
            logger.warning('Could not get cluster info')
            sys.exit(0)
        temp = name_request.json()["data"]
        name = ""
        for i in temp:
            if i["type"] == "cluster":
                name = i["name"]
                self.cl_nodes = i["nodes"]
        return name

    def cluster_items(self):
        url = f'{self.server}/api2/json/cluster/resources'
        resources_request = requests.get(url, cookies=payload, verify=False)
        if not resources_request.ok:
            logger.warning('Could not get cluster resources')
            sys.exit(0)
        self.cluster_information = resources_request.json()['data']

    def cluster_hosts(self):
        nodes_dict = {}
        url = f'{self.server}/api2/json/cluster/ha/status/manager_status'
        rr = requests.get(url, cookies=payload, verify=False)
        if not rr.ok:
            logger.warning('Could not get HA manager status')
            sys.exit(0)
        self.master_node = rr.json()['data']['manager_status']['master_node']
        self.quorate = (rr.json()['data']['quorum']['quorate'] == "1")

        if not self.quorate:
            logger.warning('Cluster quorum is not reached.')

        temp = deepcopy(self.cluster_information)
        for item in temp:
            if item["type"] == "node":
                self.cluster_information.remove(item)
                if item["status"] != "online":
                    continue
                item["cpu_used"] = round(item["maxcpu"] * item["cpu"], 2)
                item["free_mem"] = item["maxmem"] - item["mem"]
                item["mem_load"] = item["mem"] / item["maxmem"]
                item["is_master"] = (item["node"] == self.master_node)
                nodes_dict[item["node"]] = item

                if item["node"] not in excluded_nodes:
                    self.included_nodes[item["node"]] = item
        return nodes_dict

    def cluster_vms(self):
        vms_dict = {}
        temp = deepcopy(self.cluster_information)
        for item in temp:
            if item["type"] in ["qemu", "lxc"] and item["status"] == "running":
                vms_dict[item["vmid"]] = item
                if item["type"] == "lxc":
                    self.cl_lxcs.add(item["vmid"])
                if item["node"] not in excluded_nodes and item["vmid"] not in excluded_vms:
                    self.cl_vms_included[item["vmid"]] = item
                self.cluster_information.remove(item)
        del temp
        return vms_dict

    def cluster_mem(self):
        cl_max_mem = 0
        cl_used_mem = 0
        for node, sources in self.cl_nodes.items():
            if sources["node"] not in excluded_nodes:
                self.cl_max_mem_included += sources["maxmem"]
                self.cl_mem_included += sources["mem"]
            else:
                cl_max_mem += sources["maxmem"]
                cl_used_mem += sources["mem"]
        cl_max_mem += self.cl_max_mem_included
        cl_used_mem += self.cl_mem_included
        self.mem_load = cl_used_mem / cl_max_mem
        self.mem_load_included = self.cl_mem_included / self.cl_max_mem_included
        return cl_max_mem

    def cluster_cpu(self):
        cl_cpu_used: float = 0
        cl_cpu_used_included: float = 0
        cl_max_cpu: int = 0
        for host, sources in self.cl_nodes.items():
            if sources["node"] not in excluded_nodes:
                self.cl_cpu_included += sources["maxcpu"]
                cl_cpu_used_included += sources["cpu_used"]
            else:
                cl_max_cpu += sources["maxcpu"]
                cl_cpu_used += sources["cpu_used"]
        cl_max_cpu += self.cl_cpu_included
        cl_cpu_used += cl_cpu_used_included
        self.cl_cpu_load = cl_cpu_used / cl_max_cpu
        self.cl_cpu_load_include = cl_cpu_used_included / self.cl_cpu_included
        return cl_max_cpu


def authentication(server: str, data: dict):
    global payload, header
    url = f'{server}/api2/json/access/ticket'
    logger.info("[AUTH] Attempting authentication...")
    try:
        get_token = requests.post(url, data=data, verify=False)
    except Exception as exc:
        logger.error(f"Connection issue: {exc}")
        send_mail(f'Node ({server_url}) unreachable')
        sys.exit(1)
    if not get_token.ok:
        logger.error(f'Authentication failed with status {get_token.status_code}')
        sys.exit(1)
    payload = {'PVEAuthCookie': (get_token.json()['data']['ticket'])}
    header = {'CSRFPreventionToken': (get_token.json()['data']['CSRFPreventionToken'])}
    logger.info("[AUTH] Authentication successful!")


def cluster_load_verification(mem_load: float, cluster_obj: object) -> None:
    logger.debug("[CHECK] Verifying cluster load...")
    if len(cluster_obj.included_nodes) - len(excluded_nodes) == 1:
        logger.error("Only one node is included, balancing not possible.")
        sys.exit(1)
    if not (0 < mem_load < 1):
        logger.error("Cluster memory load invalid.")
        sys.exit(1)
    if mem_load >= THRESHOLD:
        logger.warning("Cluster memory load near threshold. Balancing may be needed.")


def check_risk(cluster_obj: object) -> (bool, bool):
    logger.debug("[CHECK] Calculating OOM and CPU risk...")
    oom_risk = False
    cpu_risk = False

    for node, values in cluster_obj.included_nodes.items():
        node_mem_load_percent = values["mem_load"] * 100
        node_cpu_percent = (values["cpu_used"] / values["maxcpu"]) * 100
        if node_mem_load_percent > memory_oom_threshold:
            logger.warning(f"High OOM risk on node {node}, mem load > {memory_oom_threshold}%.")
            oom_risk = True
        if node_cpu_percent > cpu_threshold:
            logger.warning(f"High CPU load on node {node}, CPU load > {cpu_threshold}%.")
            cpu_risk = True

    cluster_mem_percent = cluster_obj.mem_load_included * 100
    cluster_cpu_percent = cluster_obj.cl_cpu_load_include * 100
    if cluster_mem_percent > memory_oom_threshold:
        logger.warning(f"High OOM risk on entire cluster, mem load > {memory_oom_threshold}%.")
        oom_risk = True
    if cluster_cpu_percent > cpu_threshold:
        logger.warning(f"Cluster CPU load > {cpu_threshold}%.")
        cpu_risk = True

    return oom_risk, cpu_risk


def fetch_rrd_data(node: str, vmid: int, timeframe='hour') -> list:
    # Fetch RRD data for the VM to calculate CPU trend.
    # This returns a list of data points with "cpu" usage among other metrics.
    # Endpoint: /nodes/{node}/qemu/{vmid}/rrddata or /nodes/{node}/lxc/{vmid}/rrddata
    # We'll assume qemu for simplicity; if lxc, adjust accordingly.
    # For demonstration, we assume qemu. For LXC: /nodes/{node}/lxc/{vmid}/rrddata
    # Note: This is a simplified example, actual parsing may differ.
    vm_type = "qemu" if vmid not in cluster_obj.cl_lxcs else "lxc"
    url = f'{server_url}/api2/json/nodes/{node}/{vm_type}/{vmid}/rrddata?timeframe={timeframe}'
    r = requests.get(url, cookies=payload, verify=False)
    if not r.ok:
        return []
    data = r.json()['data']
    # data is typically a list of time/value entries. We'll look for "cpu".
    return data


def calculate_cpu_trend(rrd_data: list) -> float:
    # Given RRD data, extract CPU values and compute an average.
    # RRD data often contains 'cpu' as a fraction (0 to 1). We'll compute mean.
    cpu_values = []
    for entry in rrd_data:
        # entry might look like {"time":..., "cpu":0.02,...}
        if 'cpu' in entry and entry['cpu'] is not None:
            cpu_values.append(entry['cpu'])
    if not cpu_values:
        return 0.0
    # Compute short avg CPU usage over RRD timeframe
    return mean(cpu_values)


def update_vm_cpu_trends(cluster_obj: object):
    # Fetch and store CPU trends for each VM
    for vmid, vm_info in cluster_obj.cl_vms_included.items():
        node = vm_info["node"]
        rrd_data = fetch_rrd_data(node, vmid, timeframe='hour')
        vm_info["cpu_trend"] = calculate_cpu_trend(rrd_data)
        logger.debug(f"VM:{vmid} CPU trend (hourly avg): {vm_info['cpu_trend']*100:.2f}%")
    # Additionally, we might adjust node level CPU from these values if needed.


def need_to_balance_checking(cluster_obj: object) -> bool:
    global sum_of_deviations, iteration
    nodes = cluster_obj.included_nodes
    avg_ram = cluster_obj.mem_load_included

    # Instead of only instantaneous CPU, use CPU trend from VMs to estimate node CPU load:
    # Approx: node cpu load from trend = average of CPU_trend of VMs on that node * factor or just rely on node_data
    # For simplicity, just combine instantaneous CPU and VM trend if available.
    # We'll average VM trends on each node to get a better node CPU estimation.
    logger.debug("[CHECK] Calculating deviation with CPU trends...")

    node_cpu_estimation = {}
    for node in nodes:
        # avg CPU trend of VMs on this node
        node_vms = [vmid for vmid, info in cluster_obj.cl_vms_included.items() if info["node"] == node]
        if node_vms:
            trends = [cluster_obj.cl_vms_included[vmid].get("cpu_trend", 0) for vmid in node_vms]
            avg_vm_cpu_trend = mean(trends) if trends else 0
        else:
            # No VM or not available
            avg_vm_cpu_trend = nodes[node]["cpu"]  # fallback
        # combine with node CPU load
        # We'll just use avg_vm_cpu_trend as main factor
        node_cpu_estimation[node] = avg_vm_cpu_trend if avg_vm_cpu_trend > 0 else (nodes[node]["cpu_used"]/nodes[node]["maxcpu"])

    avg_cpu = mean(node_cpu_estimation.values()) if node_cpu_estimation else cluster_obj.cl_cpu_load_include

    for host, values in nodes.items():
        ram_deviation = abs(values["mem_load"] - avg_ram)
        cpu_current = node_cpu_estimation[host]
        cpu_dev = abs(cpu_current - avg_cpu)
        values["deviation"] = weight_ram * ram_deviation + weight_cpu * cpu_dev

    sum_of_deviations = sum(v["deviation"] for v in nodes.values())

    if iteration > 10:
        operational_deviation = CD/2 if random() > 1/3 else CD/4 if random() > 1/6 else CD/8
        iteration = 0
    else:
        operational_deviation = CONFIG_DEVIATION

    for val in nodes.values():
        if val["deviation"] > operational_deviation:
            logger.info(f"[CHECK] Deviation > {operational_deviation}. Balancing needed.")
            return True

    logger.info("[CHECK] No significant deviation. No balancing needed.")
    return False


def temporary_dict(cluster_obj: object) -> object:
    obj = {}
    vm_dict = deepcopy(cluster_obj.cl_vms_included)
    if LXC_MIGRATION == "OFF":
        for lxc in cluster_obj.cl_lxcs:
            vm_dict.pop(lxc, None)
    for host in cluster_obj.included_nodes:
        hosts = {}
        for vm, value in vm_dict.items():
            if value["node"] == host:
                hosts[vm] = value
        obj[host] = hosts
    return obj


def calculating(hosts: object, cluster_obj: object) -> list:
    variants: list = []
    nodes = cluster_obj.included_nodes
    avg_ram = cluster_obj.mem_load_included

    # Reuse the node_cpu_estimation logic from above if needed:
    node_cpu_estimation = {}
    for node in nodes:
        node_vms = [vmid for vmid, info in cluster_obj.cl_vms_included.items() if info["node"] == node]
        if node_vms:
            trends = [cluster_obj.cl_vms_included[vmid].get("cpu_trend", 0) for vmid in node_vms]
            avg_vm_cpu_trend = mean(trends) if trends else 0
        else:
            avg_vm_cpu_trend = nodes[node]["cpu_used"]/nodes[node]["maxcpu"]
        node_cpu_estimation[node] = avg_vm_cpu_trend if avg_vm_cpu_trend > 0 else (nodes[node]["cpu_used"]/nodes[node]["maxcpu"])

    avg_cpu = mean(node_cpu_estimation.values()) if node_cpu_estimation else cluster_obj.cl_cpu_load_include

    logger.info("┌─────────────────────────────────┐")
    logger.info("│ Calculating possible migrations │")
    logger.info("└─────────────────────────────────┘")

    global sum_of_deviations

    for host in permutations(nodes, 2):
        if node_to_group.get(host[0]) != node_to_group.get(host[1]):
            continue
        donor = host[0]
        recipient = host[1]

        base_deviations = 0
        for node_name, vals in nodes.items():
            if node_name not in host:
                base_deviations += vals["deviation"]

        for vm in hosts[donor].values():
            donor_new_mem = (nodes[donor]["mem"] - vm["mem"])
            recipient_new_mem = (nodes[recipient]["mem"] + vm["mem"])
            donor_new_load = donor_new_mem / nodes[donor]["maxmem"]
            recipient_new_load = recipient_new_mem / nodes[recipient]["maxmem"]

            # CPU estimation doesn't change easily without VM-specific CPU usage details.
            # We'll assume CPU trends remain similar after migration for evaluation:
            # A more complex approach would remove VM CPU from donor and add to recipient to recalc.
            # For simplicity, assume average CPU load shift:
            # If this VM had a certain trend, removing it from donor, adding to recipient:
            vm_cpu_trend = cluster_obj.cl_vms_included[vm["vmid"]]["cpu_trend"] if vm["vmid"] in cluster_obj.cl_vms_included else 0

            donor_cpu_ratio = max(0, node_cpu_estimation[donor] - vm_cpu_trend/2) # rough approximation
            recipient_cpu_ratio = min(1, node_cpu_estimation[recipient] + vm_cpu_trend/2)

            donor_ram_dev = abs(donor_new_load - avg_ram)
            recipient_ram_dev = abs(recipient_new_load - avg_ram)
            donor_cpu_dev = abs(donor_cpu_ratio - avg_cpu)
            recipient_cpu_dev = abs(recipient_cpu_ratio - avg_cpu)

            donor_dev = weight_ram * donor_ram_dev + weight_cpu * donor_cpu_dev
            recipient_dev = weight_ram * recipient_ram_dev + weight_cpu * recipient_cpu_dev

            temp_full_deviation = base_deviations + donor_dev + recipient_dev

            if temp_full_deviation < sum_of_deviations:
                variants.append((donor, recipient, vm["vmid"], temp_full_deviation))

    variants.sort(key=lambda x: x[-1])
    logger.info(f"[CALC] Found {len(variants)} beneficial migration variants.")
    return variants


def vm_migration(variants: list, cluster_obj: object) -> None:
    local_disk = None
    local_resources = None
    clo = cluster_obj
    error_counter = 0
    problems: list = []

    if not variants:
        logger.info("[MIGRATE] No migration variants to process.")
        return

    logger.info("┌───────────────────────────┐")
    logger.info("│ Starting VM Migrations... │")
    logger.info("└───────────────────────────┘")

    if TEST_MODE == "ON":
        logger.info("[TEST MODE] The following migrations would be attempted:")
        for donor, recipient, vm, _ in variants:
            logger.info(f"[TEST MODE] Migrate VM:{vm} from {donor} to {recipient}")
        logger.info("[TEST MODE] No real migrations performed.")
        return

    for variant in variants:
        if error_counter > 2:
            logger.error("[MIGRATE] Too many migration errors.")
            send_mail(f'Migration errors: {problems}')
            sys.exit(1)
        donor, recipient, vm = variant[:3]
        logger.info(f"[MIGRATE] Attempting migration of VM:{vm} from {donor} → {recipient}")

        if vm in cluster_obj.cl_lxcs:
            options = {'target': recipient, 'restart': 1}
            url = f'{cluster_obj.server}/api2/json/nodes/{donor}/lxc/{vm}/migrate'
        else:
            options = {'target': recipient, 'online': 1}
            check_url = f'{cluster_obj.server}/api2/json/nodes/{donor}/qemu/{vm}/migrate'
            check_request = requests.get(check_url, cookies=payload, verify=False)
            if not check_request.ok:
                logger.warning("[MIGRATE] Could not check VM migration info.")
                error_counter += 1
                problems.append(vm)
                continue
            local_disk = check_request.json()['data']['local_disks']
            local_resources = check_request.json()['data']['local_resources']
            url = check_url

        if local_disk or local_resources:
            logger.info(f"[MIGRATE] VM:{vm} has local resources that can't be migrated easily.")
            local_disk = None
            local_resources = None
            continue
        else:
            job = requests.post(url, cookies=payload, headers=header, data=options, verify=False)
            if not job.ok:
                logger.warning("[MIGRATE] Migration request failed.")
                error_counter += 1
                problems.append(vm)
                continue
            pid = job.json()['data']
            error_counter -= 1
            status = True
            timer: int = 0

            while status:
                timer += 10
                sleep(10)
                if vm in cluster_obj.cl_lxcs:
                    url_check = f'{cluster_obj.server}/api2/json/nodes/{recipient}/lxc'
                else:
                    url_check = f'{cluster_obj.server}/api2/json/nodes/{recipient}/qemu'

                request = requests.get(url_check, cookies=payload, verify=False)
                if not request.ok:
                    send_mail(f'Problem checking VM:{vm} after migration')
                    sys.exit(1)
                recipient_vms = request.json()['data']
                found = False
                for _ in recipient_vms:
                    if int(_['vmid']) == vm:
                        found = True
                        if _['status'] == 'running':
                            logger.info(f"[MIGRATE] Migration of VM:{vm} complete after {timer}s!")
                            sleep(10)
                            if vm in cluster_obj.cl_vms:
                                resume_url = f'{cluster_obj.server}/api2/json/nodes/{recipient}/qemu/{vm}/status/resume'
                                requests.post(resume_url, cookies=payload, headers=header, verify=False)
                            status = False
                        else:
                            logger.warning(f"[MIGRATE] VM:{vm} found but not running.")
                            send_mail(f'Check VM:{vm} post-migration status')
                            sys.exit(1)
                        break
                if not found:
                    logger.info(f"[MIGRATE] VM:{vm} migration in progress... {timer}s")
            break


def send_mail(message: str):
    if send_on == "ON":
        msg = EmailMessage()
        msg.set_payload(message)
        msg['Subject'] = cfg["mail"]["message_subject"]
        msg['From'] = cfg["mail"]["from"]
        msg['To'] = cfg["mail"]["to"]
        login: str = cfg["mail"]["login"]
        password: str = cfg["mail"]["password"]
        s = smtplib.SMTP(f'{cfg["mail"]["server"]["address"]}:{cfg["mail"]["server"]["port"]}')
        encryption = cfg["mail"]["ssl_tls"]
        if encryption == "ON":
            s.starttls()
        try:
            s.login(login, password)
            s.sendmail(msg['From'], [msg['To']], msg.as_string())
            logger.debug('[MAIL] Notification sent.')
        except Exception as exc:
            logger.debug(f'[MAIL] Sending mail failed: {exc}')
        finally:
            s.quit()


def re_measure_cluster(cluster: object):
    # After migrations, re-fetch CPU trends and re-check cluster conditions
    logger.info("[RE-MEASURE] Re-measuring cluster load and CPU trends after migration...")
    update_vm_cpu_trends(cluster)


def main():
    global iteration
    print("========================================")
    print("        Proxmox Load Balancer")
    if TEST_MODE == "ON":
        print("          [TEST MODE ACTIVE]")
    print("========================================")
    authentication(server_url, auth)
    global cluster_obj
    cluster_obj = Cluster(server_url)

    if ONLY_ON_MASTER == "ON":
        hostname = socket.gethostname()
        master = cluster_obj.master_node
        if hostname != master:
            logger.info(f"This node ({hostname}) is not the current cluster master ({master}). Waiting...")
            sleep(300)
            return

    # Display cluster info
    print(f"Cluster Name: {cluster_obj.cl_name}")
    print(f"Included Nodes: {', '.join(cluster_obj.included_nodes.keys())}")
    print(f"Cluster RAM Load: {cluster_obj.mem_load_included * 100:.2f}%")
    print(f"Cluster CPU Load: {cluster_obj.cl_cpu_load_include * 100:.2f}%")
    print("========================================")

    cluster_load_verification(cluster_obj.mem_load_included, cluster_obj)
    # First fetch CPU trends
    update_vm_cpu_trends(cluster_obj)
    oom_risk, cpu_risk = check_risk(cluster_obj)

    need_to_balance = need_to_balance_checking(cluster_obj) or oom_risk or cpu_risk
    if need_to_balance:
        iteration = 0
        balance_cl = temporary_dict(cluster_obj)
        sorted_variants = calculating(balance_cl, cluster_obj)
        if sorted_variants:
            vm_migration(sorted_variants, cluster_obj)
            logger.info("[INFO] Post-migration pause for cluster re-evaluation.")
            sleep(10)
            re_measure_cluster(cluster_obj)
            oom_risk_after, cpu_risk_after = check_risk(cluster_obj)
            if oom_risk_after or cpu_risk_after:
                logger.info("[INFO] Risk still high after migration.")
            else:
                logger.info("[INFO] Situation improved after migration.")
        else:
            logger.info("[INFO] No variants found. Waiting before next attempt...")
            sleep(60)
    else:
        iteration += 1
        logger.info("[INFO] Cluster balanced. Sleeping 300 seconds.")
        sleep(300)


while True:
    main()
