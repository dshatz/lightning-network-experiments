#!/usr/bin/env python3
"""Plugin that probes the network for failed channels.

This plugin regularly performs a random probe of the network by sending a
payment to a random node in the network, with a random `payment_hash`, and
observing how the network reacts. The random `payment_hash` results in the
payments being rejected at the destination, so no funds are actually
transferred. The error messages however allow us to gather some information
about the success probability of a payment, and the stability of the channels.

The random selection of destination nodes is a worst case scenario, since it's
likely that most of the nodes in the network are leaf nodes that are not
well-connected and often offline at any point in time. Expect to see a lot of
errors about being unable to route these payments as a result of this.

The probe data is stored in a sqlite3 database for later inspection and to be
able to eventually draw pretty plots about how the network stability changes
over time. For now you can inspect the results using the `sqlite3` command
line utility:

```bash
sqlite3  ~/.lightning/probes.db "select destination, erring_channel, failcode from probes"
```

Failcode -1 and 16399 are special:

 - -1 indicates that we were unable to find a route to the destination. This
    usually indicates that this is a leaf node that is currently offline.

 - 16399 is the code for unknown payment details and indicates a successful
   probe. The destination received the incoming payment but could not find a
   matching `payment_key`, which is expected since we generated the
   `payment_hash` at random :-)

"""
from concurrent.futures._base import ALL_COMPLETED, wait
from concurrent.futures.thread import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
from itertools import groupby, repeat
from random import choice
from typing import List, Union

from pause import until
from pyln.client import Plugin, RpcError
from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey, PrimaryKeyConstraint
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from time import sleep, time
import heapq
import json
import os
import random
import string
import threading
from collections import Counter
import math

Base = declarative_base()
plugin = Plugin()

exclusions = []
temporary_exclusions = {}


class Probe(Base):
    __tablename__ = "probes"
    id = Column(Integer, primary_key=True)
    destination = Column(String)
    route = Column(String)
    error = Column(String)
    erring_channel = Column(String)
    failcode = Column(Integer)
    payment_hash = Column(String)
    started_at = Column(DateTime)
    finished_at = Column(DateTime)

    def jsdict(self):
        return {
            'id': self.id,
            'destination': self.destination,
            'route': self.route,
            'erring_channel': self.erring_channel,
            'failcode': self.failcode,
            'started_at': str(self.started_at),
            'finished_at': str(self.finished_at),
        }


class Node(Base):
    __tablename__ = "nodes"
    id = Column(String, primary_key=True)
    ip4 = Column(String)
    ip6 = Column(String)
    tor2 = Column(String)
    tor3 = Column(String)

class Connection(Base):
    __tablename__ = "connections"
    node = Column(String, ForeignKey(Node.id, ondelete='RESTRICT'), primary_key=True)
    time = Column(DateTime, primary_key=True, default=datetime.utcnow)
    success = Column(Boolean)
    error = Column(String, nullable=True)

def start_probe(plugin):
    t = threading.Thread(target=probe, args=[plugin])
    t.daemon = True
    t.start()


@plugin.async_method('probe')
def probe(request, plugin, node_id=None, **kwargs):
    res = None
    if node_id is None:
        nodes = plugin.rpc.listnodes()['nodes']
        node_id = choice(nodes)['nodeid']

    s = plugin.Session()
    p = Probe(destination=node_id, started_at=datetime.now())
    s.add(p)
    try:
        route = plugin.rpc.getroute(
            node_id,
            msatoshi=10000,
            riskfactor=1,
            exclude=exclusions + list(temporary_exclusions.keys())
        )['route']
        p.route = ','.join([r['channel'] for r in route])
        p.payment_hash = ''.join(choice(string.hexdigits) for _ in range(64))
    except RpcError:
        p.failcode = -1
        res = p.jsdict()
        s.commit()
        return request.set_result(res)

    s.commit()
    plugin.rpc.sendpay(route, p.payment_hash)
    plugin.pending_probes.append({
        'request': request,
        'probe_id': p.id,
        'payment_hash': p.payment_hash,
        'callback': complete_probe,
        'plugin': plugin,
    })


@plugin.method('traceroute')
def traceroute(plugin, node_id, **kwargs):
    traceroute = {
        'destination': node_id,
        'started_at': str(datetime.now()),
        'probes': [],
    }
    try:
        traceroute['route'] = plugin.rpc.getroute(
            traceroute['destination'],
            msatoshi=10000,
            riskfactor=1,
        )['route']
        traceroute['payment_hash'] = ''.join(random.choice(string.hexdigits) for _ in range(64))
    except RpcError:
        traceroute['failcode'] = -1
        return traceroute

    # For each prefix length, shorten the route and attempt the payment
    for l in range(1, len(traceroute['route']) + 1):
        probe = {
            'route': traceroute['route'][:l],
            'payment_hash': ''.join(random.choice(string.hexdigits) for _ in range(64)),
            'started_at': str(datetime.now()),
        }
        probe['destination'] = probe['route'][-1]['id']
        plugin.rpc.sendpay(probe['route'], probe['payment_hash'])

        try:
            plugin.rpc.waitsendpay(probe['payment_hash'], timeout=30)
            raise ValueError("The recipient guessed the preimage? Cryptography is broken!!!")
        except RpcError as e:
            probe['finished_at'] = str(datetime.now())
            if e.error['code'] == 200:
                probe['error'] = "Timeout"
                break
            else:
                probe['error'] = e.error['data']
                probe['failcode'] = e.error['data']['failcode']

        traceroute['probes'].append(probe)

    return traceroute


@plugin.method('probe-stats')
def stats(plugin):
    return {
        'pending_probes': len(plugin.pending_probes),
        'exclusions': len(exclusions),
        'temporary_exclusions': len(temporary_exclusions),
    }


def complete_probe(plugin, request, probe_id, payment_hash):
    s = plugin.Session()
    p = s.query(Probe).get(probe_id)
    try:
        plugin.rpc.waitsendpay(p.payment_hash)
    except RpcError as e:
        error = e.error['data']
        p.erring_channel = e.error['data']['erring_channel']
        p.failcode = e.error['data']['failcode']
        p.error = json.dumps(error)

    if p.failcode in [16392, 16394]:
        exclusion = "{erring_channel}/{erring_direction}".format(**error)
        print('Adding exclusion for channel {} ({} total))'.format(
            exclusion, len(exclusions))
        )
        exclusions.append(exclusion)

    if p.failcode in [21, 4103]:
        exclusion = "{erring_channel}/{erring_direction}".format(**error)
        print('Adding temporary exclusion for channel {} ({} total))'.format(
            exclusion, len(temporary_exclusions))
        )
        expiry = time() + plugin.probe_exclusion_duration
        temporary_exclusions[exclusion] = expiry

    p.finished_at = datetime.now()
    res = p.jsdict()
    s.commit()
    s.close()
    request.set_result(res)


def poll_payments(plugin):
    """Iterate through all probes and complete the finalized ones.
    """
    for probe in plugin.pending_probes:
        p = plugin.rpc.listpayments(None, payment_hash=probe['payment_hash'])
        if p['payments'][0]['status'] == 'pending':
            continue

        plugin.pending_probes.remove(probe)
        cb = probe['callback']
        del probe['callback']
        cb(**probe)


def clear_temporary_exclusion(plugin):
    timed_out = [k for k, v in temporary_exclusions.items() if v < time()]
    for k in timed_out:
        del temporary_exclusions[k]

    print("Removed {}/{} temporary exclusions.".format(
        len(timed_out), len(temporary_exclusions))
    )


def hi(plugin):
    return "hi"


def schedule(plugin):
    # List of scheduled calls with next runtime, function and interval
    # next_runs = [
    #   (time() + 300, clear_temporary_exclusion, 300),
    #   (time() + plugin.probe_interval, start_probe, plugin.probe_interval),
    #   (time() + 1, poll_payments, 1),
    # ]
    # heapq.heapify(next_runs)
    next_run = time() + 60 * 60 * 24
    # with open("/root/.lightning/probe_log.txt", "w") as f:
    # f.write("HI!")
    while True:
        # break
        t = next_run - time()
        if t > 0:
            sleep(t)
        next_run = time() + plugin.probe_interval
        fname = "/root/.lightning/attempts/fullprobe_" + str(int(time()))
        probe_two(plugin, depth=-1, file_name=fname)
        # with open("/root/.lightning/results.txt","a") as f:
        # for row in res:
        # f.write(str(row))
        # f.write("\n")


@plugin.method('probe_two')
def probe_two(plugin, depth=-1, amount=50000000, file_name=None, **kwargs):
    paths = [
        {"channels": [], "route": [], "start": "033f12b6786951cc2f5084c6db6390c152240bb5ee1bbc9a0ed0f18038df97ea76",
         "prev_base_fee": 0, "prev_fee_rate": 0}]

    d = 0
    results = []
    att_chan = set()
    SUCCESS_ERROR_MESSAGE = "WIRE_INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS"

    # for i in range(depth):
    while len(paths) > 0:
        if d == depth:
            break
        else:
            d += 1
        hashes = []
        new_paths = []
        for path in paths:
            for channel in plugin.rpc.listchannels(source=path["start"])["channels"]:
                # return channel
                dir = 1
                if channel["destination"] > channel["source"]:
                    dir = 0
                if len(path["channels"]) == 0 or (channel["short_channel_id"], dir) not in att_chan:
                    stop = {}
                    stop["id"] = channel["destination"]
                    stop["channel"] = channel["short_channel_id"]
                    stop["direction"] = dir
                    # stop["source"] = channel["source"]
                    # stop["destination"] = channel["destination"]

                    cloned_path = deepcopy(path)

                    # CLTV Expiries & Fees
                    if len(cloned_path["route"]) != 0:
                        # stop["msatoshi"] = amount
                        for s in cloned_path["route"]:  # channel["delay"]??
                            s["delay"] += channel["delay"]
                        prev_payment = path["route"][-1]["msatoshi"]
                        # stop["msatoshi"] = prev_payment -  math.ceil(path["prev_fee_rate"]* prev_payment / 1000) - path["prev_base_fee"]
                        stop["msatoshi"] = prev_payment - round(channel["fee_per_millionth"] * prev_payment / 1000000) - \
                                           channel["base_fee_millisatoshi"]
                        if stop["msatoshi"] < 0:
                            # TODO better handling
                            continue
                    else:
                        stop["msatoshi"] = amount

                    stop["delay"] = 9

                    stop["amount_msat"] = str(stop["msatoshi"]) + "msat"

                    new_route = {}
                    new_route["route"] = cloned_path["route"] + [stop]
                    new_route["start"] = channel["destination"]
                    new_route["channels"] = cloned_path["channels"] + [stop["channel"]]
                    # new_route["prev_delay"] = channel["delay"]
                    # new_route["prev_base_fee"] = channel["base_fee_millisatoshi"]
                    # new_route["prev_fee_rate"] = channel["fee_per_millionth"]
                    # return plugin.rpc.getroute("02ad6fb8d693dc1e4569bcedefadf5f72a931ae027dc0f0$
                    new_paths.append(new_route)
                    # if len(new_route["route"]) == 3:
                    # return new_route

                    # Send Payments
                    payment_hash = ''.join(choice(string.hexdigits) for _ in range(64))
                    hashes.append(payment_hash)
                    while True:
                        try:
                            # sleep(0.25)
                            # plugin.rpc.sendpay(new_route["route"], payment_hash)
                            att_chan.add((stop["channel"], dir))
                            break
                        except Exception as e:
                            results.append([None, e.error, "RPC__Error", None, None, None, None])

                            return str(e)
                            break

        # Get Payments Back
        paths = new_paths
        new_paths = []
        for idx, hash in enumerate(hashes):
            resp = None
            try:
                plugin.rpc.sendpay(paths[idx]["route"], hash)

                plugin.rpc.waitsendpay(hash, 240)
            except RpcError as e:
                if "data" not in e.error:
                    results.append(
                        [time(), paths[idx]["channels"][-1], ",".join(paths[idx]["channels"]), "Payment_Error",
                         paths[idx]["start"], 0, None])
                    # continue
                    # return e.error
                else:
                    resp = e.error["data"]
                    if resp["failcodename"] == SUCCESS_ERROR_MESSAGE:
                        new_paths.append(paths[idx])
                        # succ_chan.add(paths[idx]["route"][-1]["channel"]))
                    elif resp["failcodename"] == "WIRE_UNKNOWN_NEXT_PEER":
                        pass
                        # return paths[idx]["channels"]

                    # fail_chan.add(paths[idx]["channels"][-1])
                    # att_chan.add(paths[idx]["channels"][-1])
                    results.append(
                        [time(), paths[idx]["channels"][-1], ",".join(paths[idx]["channels"]), resp["failcodename"],
                         paths[idx]["start"], resp["erring_index"], paths[idx]["route"][-1]["msatoshi"]])
                if file_name is not None:
                    with open("/root/.lightning/attempts/" + file_name, "a+") as f:
                        f.write(str(results[-1]))
                        f.write("\n")
        paths = new_paths

    # res = Counter([r[3] for r in results])

    return results


@plugin.method('probe_all')
def probe_all(plugin, depth=1, probes=2500, **kwargs):
    paths = [{"route": [], "dest": "033f12b6786951cc2f5084c6db6390c152240bb5ee1bbc9a0ed0f18038df97ea76"}]

    for i in range(depth):
        new_paths = []
        for path in paths:
            for channel in plugin.rpc.listchannels(source=path["dest"])["channels"]: # Channels going from path['dest']
                # return channel
                if len(path["route"]) == 0 or path["route"][-1]["channel"] != channel["short_channel_id"]:
                    stop = {}
                    stop["id"] = channel["destination"]
                    stop["channel"] = channel["short_channel_id"]
                    stop["direction"] = 1
                    stop["msatoshi"] = 500000 - 1000 * i
                    stop["amount_msat"] = str(stop["msatoshi"]) + "msat"
                    stop["delay"] = 500 - 150 * i
                    new_route = {}
                    new_route["route"] = path["route"] + [stop]
                    new_route["dest"] = channel["destination"]
                    # return plugin.rpc.getroute("02ad6fb8d693dc1e4569bcedefadf5f72a931ae027dc0f0c544b34c1c6f3b9a02b", msatoshi=10000, riskfactor=1)
                    new_paths.append(new_route)
                    paths = new_paths

    # Send Payments
    hashes = []
    for path in paths[:probes]:
        payment_hash = ''.join(choice(string.hexdigits) for _ in range(64))
        hashes.append(payment_hash)
        plugin.rpc.sendpay(path["route"], payment_hash)

    # Get Payments Back
    results = []
    for hash in hashes:
        try:
            plugin.rpc.waitsendpay(hash, 10)
        except RpcError as e:
            if "data" not in e.error:
                return e.error
            results.append(e.error["data"])
    return [results, Counter([r["failcodename"] for r in results])]


# return [paths, len(paths)]

@plugin.async_method('connect_all')
def connect_all(request, plugin):

    CONNECTION_TIMEOUT = 10 # seconds
    PARALLEL_CONNECTIONS = 4

    try:

        visible_nodes = plugin.rpc.listnodes()['nodes']

        def address_by_type(addresses, type) -> Union[str, None]:
            for a in addresses:
                if a['type'] == type:
                    addr = a['addr']
                    return f"{addr}:{a['port']}" if 'port' in a else addr

        session = plugin.Session()
        public: List[Node] = list()

        for n in visible_nodes:
            node = Node(n['nodeid'])
            addresses = n['addresses']
            node.ip4 = address_by_type(addresses, 'ipv4')
            node.ip6 = address_by_type(addresses, 'ipv6')
            node.tor2 = address_by_type(addresses, 'torv2')
            node.tor3 = address_by_type(addresses, 'torv3')

            if node.ip4 or node.ip6 or node.tor2 or node.tor3:
                public.append(node)


            other_types = [address for address in addresses if address['type'] not in ['ipv4', 'ipv6', 'tor2', 'tor3']]

            if other_types:
                raise Exception(f"Found unknown address types: {other_types}")

            session.add(node)
        print(f"Saving {len(public)} nodes with exposed network addresses.")
        session.flush()

        def try_connect(node, dt) -> List[Connection]:
            connections = []
            for addr in [node.ip4, node.ip6, node.tor2, node.tor3]:
                if addr:
                    result = plugin.rpc.connect(addr)
                    conn = Connection(node=node.id, time=dt)
                    if result['code'] and result['message']:  # Error !
                        conn.success = False
                        conn.error = result['code'] + ': ' + result['message']
                    else:
                        conn.success = True
                    plugin.rpc.disconnect(node.id)  # Don't forget to disconnect
                    connections.append(conn)
            return connections

        def try_all_nodes():
            with ThreadPoolExecutor(max_workers=PARALLEL_CONNECTIONS) as executor:
                dt = datetime.utcnow()
                futures = [executor.submit(try_connect, *args) for args in zip(public, repeat(dt))]
                wait(futures, timeout=CONNECTION_TIMEOUT, return_when=ALL_COMPLETED)
                connections: List[Connection] = [c for f in futures for c in f.result()]
                session.add_all(connections)
                session.flush()

        offsets = [timedelta(minutes=10), timedelta(minutes=30), timedelta(minutes=60),
                   timedelta(hours=2), timedelta(hours=4), timedelta(hours=8), timedelta(hours=12), timedelta(hours=16), timedelta(hours=20), timedelta(hours=24),
                   timedelta(hours=36), timedelta(hours=48), timedelta(hours=60), timedelta(hours=72)]

        iteration = 1
        while offsets:
            start = datetime.utcnow()
            print(f"Iteration {iteration}. Trying to connect to {len(public)} nodes")
            try_all_nodes()
            print(f"Completed in {str((datetime.utcnow() - start).total_seconds())}")
            delta = offsets.pop(0)
            print(f"Will try again in {str(delta)}")
            until(datetime.utcnow() + delta) # sleep until it's time to repeat the connections

        return request.set_result("OK")

    except Exception as e:
        return request.set_result(str(e))


@plugin.init()
def init(configuration, options, plugin):
    plugin.probe_interval = int(options['probe-interval'])
    plugin.probe_exclusion_duration = int(options['probe-exclusion-duration'])

    db_filename = 'sqlite:///' + os.path.join(
        configuration['lightning-dir'],
        'probe_all.db'
    )

    engine = create_engine(db_filename, echo=True)
    Base.metadata.create_all(engine)
    plugin.Session = sessionmaker()
    plugin.Session.configure(bind=engine)
    t = threading.Thread(target=schedule, args=[plugin])
    t.daemon = True
    t.start()

    # Probes that are still pending and need to be checked against.
    plugin.pending_probes = []


plugin.add_option(
    'probe-interval',
    '60',
    'How many seconds should we wait between probes?'
)
plugin.add_option(
    'probe-exclusion-duration',
    '1800',
    'How many seconds should temporarily failed channels be excluded?'
)
plugin.run()
