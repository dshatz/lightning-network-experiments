import csv
import json
from os.path import join

"""
This script takes a json file of format like in c-lightning RPC method listnodes and converts it to a CSV for easy import into DataGrip.
Address type util is also performed.
"""

WORKDIR = '../'
FILENAME = 'best-connected-nodes'

with open(join(WORKDIR, '{}.json'.format(FILENAME)), 'r') as f:
    data = f.read()
    nodes = json.loads(data)

for n in nodes:
    addresses = n['addresses']
    n['total_addresses'] = len(addresses)
    n['total_ip'] = len([a for a in addresses if a['type'] == 'ipv4' or a['type'] == 'ipv6'])
    n['total_ipv6'] = len([a for a in addresses if a['type'] == 'ipv6'])
    n['total_ipv4'] = n['total_ip'] - n['total_ipv6']
    n['total_tor'] = len([a for a in addresses if a['type'] == 'torv2' or a['type'] == 'torv3'])
    n['total_torv2'] = len([a for a in addresses if a['type'] == 'torv2'])
    n['total_torv3'] = n['total_tor'] - n['total_torv2']
    del n['addresses']

with open(join(WORKDIR, './{}-nodes.csv'.format(FILENAME)), 'w+') as f:
    writer = csv.DictWriter(f, nodes[0].keys())
    writer.writeheader()
    writer.writerows(nodes)
