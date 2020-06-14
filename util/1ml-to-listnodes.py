import json
from os.path import join

"""
This scripts takes json from https://1ml.com/node?order=channelcount&json=true and converts it to 
a json format similar to one returned by c-lightning listnodes RPC method. 
"""

WORKDIR = '../'
INPUT_FILE = 'best-connected-nodes.json'
OUTPUT_FILE = 'best-connected-nodes.json'

with open(join(WORKDIR, INPUT_FILE), 'r') as f:
    data = f.read()
    nodes = json.loads(data)
    for n in nodes:
        if 'last_update' in n:
            n['last_timestamp'] = n['last_update']
            del n['last_update']
        if 'pub_key' in n:
            n['nodeid'] = n['pub_key']
            del n['pub_key']
        n['addresses'] = [dict(
            address=a['addr'],
            type=
            'ipv6' if '::' in a['addr'] else (
                'ipv4' if 'onion' not in a['addr'] else (
                    'torv3' if len(a['addr']) > 30 else 'torv2'
                ))) for a in n['addresses'] if 'addr' in a]

print(nodes[0])

with open(join(WORKDIR, OUTPUT_FILE), 'w') as f:
    f.write(json.dumps(nodes))
