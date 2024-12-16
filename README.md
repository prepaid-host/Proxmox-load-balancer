## Proxmox-load-balancer v1

This script is designed to automatically balance both RAM and CPU load across the nodes of a Proxmox cluster. It introduces additional enhancements such as CPU trend analysis, configurable thresholds, test mode for safe simulation, and improved handling of VM migrations within predefined node groups. The script constantly monitors your cluster and attempts to redistribute VMs to maintain a defined deviation range, ensuring that no single node becomes a bottleneck.

### Key Features:
1. **RAM and CPU Balancing**:  
   Balances the cluster based on both RAM and CPU load. CPU trend data (via RRD) and momentary values are considered, ensuring a more holistic resource distribution.

2. **Configurable Parameters**:  
   Set thresholds for CPU and RAM (e.g. OOM risk percentage, CPU usage percentage) as well as a deviation range for balancing operations in `config.yaml`.

3. **Exclusions & Groups**:  
   Define which VMs and nodes should be excluded. Group nodes together so that VMs only migrate within the same group.

4. **LXC Migration Toggle**:  
   Optionally disable LXC migrations if necessary.

5. **Continuous Operation**:  
   The script runs constantly, sleeping between attempts. If balance is achieved, it waits before re-checking. If thresholds are exceeded, it tries to restore balance more aggressively.

6. **Test Mode**:  
   Run the script in test mode to simulate migrations without applying them, useful for verifying configuration and logic before going live.

7. **Integration with HA**:  
   Deploy automatically (e.g. via Ansible) to all nodes. Activate `only_on_master` in the config to ensure it only runs on the current HA master node.

### Requirements & Recommendations:
- **Shared Storage**:  
  A shared or distributed storage (e.g. CEPH) accessible to all nodes is required for online migrations.

- **Deviation Setting**:  
  A recommended minimum deviation is 1% for large clusters and around 3-5% for medium/small clusters. A value of 0% leads to constant migrations.

- **Proxmox Access**:  
  Ensure continuous access to the Proxmox host. The script can be run directly on a Proxmox node or within a VM/LXC in the cluster. Use systemd to set it as a service.

1. **For the migration mechanism to work correctly, a shared storage is required. This can be a CEPH (or other distributed storage) or a storage system connected to all Proxmox nodes.**
2. For a cluster similar in size and composition to the one in the screenshot, the normal value of "deviation" is 4%. This means that with an average load of the cluster (or part of it) the maximum deviation of the RAM load of each node can be 2% in a larger or smaller direction.
Example: cluster load is 50%, the minimum loaded node is 48%, the maximum loaded node is 52%.
Moreover, it does not matter at all how much RAM the node has.
3. Do not set the "deviation" value to 0. This will result in a permanent VM migration at the slightest change to the VM["mem"]. The recommended minimum value is 1% for large clusters with many different VMs. For medium and small clusters 3-5%
4. For the script to work correctly, you need constant access to the Proxmox host. Therefore, I recommend running the script on one of the Proxmox nodes or creating a VM/Lxc in a balanced cluster and configuring the script autorun.
5. To autorun the script on Linux (ubuntu):  
	 `touch /etc/systemd/system/load-balancer.service`  
	 `chmod 664 /etc/systemd/system/load-balancer.service`  
		Add the following lines to it, replacing USERNAME with the name of your Linux user:  
			
		[Unit]  
  		Description=Proxmor cluster load-balancer Service  
  		After=network.target  

  		[Service]  
  		Type=simple  
  		User=USERNAME  
		NoNewPrivileges=yes  
  		ExecStart=/home/USERNAME/plb.py  
		WorkingDirectory=/home/USERNAME/  
  		Restart=always  
  		RestartSec=300  

  		[Install]  
 		WantedBy=multi-user.target  
				
```systemctl daemon-reload```  
```systemctl start load-balancer.service```  
```systemctl status load-balancer.service```  
```systemctl enable load-balancer.service```  

<i>Tested on Proxmox 8.2-2 virtual environment with more than 1500 virtual machines</i>  
**Before using the script, please read the Supplement to the license**


**If you have any exceptions, please write about them in https://github.com/prepaid-host/Proxmox-load-balancer/issues. I'll try to help you.**


