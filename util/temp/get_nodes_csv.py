import json
from csv import DictWriter

# This file generates a CSV file with information about each node. All fields from RPC `listnodes`
# except 'addresses' are included.
# Field address_types is added, which is either equal to `ip`, `tor` or `both`. Shows which types of addresses this node exposes.

# The CSV can then be imported into a SQL database for further analysis.


# nodes.json should have a format as if it was generated with:
# lightning-cli listnodes > nodes.json
with open('/home/dshatz/Downloads/all_results/nodes.json', 'r') as f:
    nodes = json.loads(f.read())['nodes']

nodes_with_addresses = []
for n in nodes:
    addrs = n.get('addresses', [])
    types = [a['type'] for a in addrs]
    ip = False
    tor = False
    if 'ipv4' in types or 'ipv6' in types:
        ip = True
    if 'torv2' in types or 'torv3' in types:
        tor = True
    if ip and tor:
        both = True
        n['address_types'] = 'both'
    elif ip:
        n['address_types'] = 'ip'
    elif tor:
        n['address_types'] = 'tor'
    else:
        n['address_types'] = 'none'
    if 'features' in n:
        del n['features']
    if 'addresses' in n:
        del n['addresses']
    nodes_with_addresses.append(n)

with open('/home/dshatz/Downloads/all_results/all_nodes.csv', 'w') as f:
    fields = list(nodes_with_addresses[0].keys())
    print(fields)
    write = DictWriter(f, fieldnames=fields)
    write.writeheader()
    write.writerows(nodes_with_addresses)