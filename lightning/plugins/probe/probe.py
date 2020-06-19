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
import csv
import itertools
import traceback
from concurrent.futures._base import ALL_COMPLETED, wait, as_completed
from concurrent.futures.thread import ThreadPoolExecutor
from copy import deepcopy
from csv import DictReader
from datetime import datetime, timedelta
from functools import partial
from itertools import groupby, repeat
from os import listdir
from os.path import isfile, join, exists
from random import choice
from typing import List, Union
from uuid import uuid4

import requests
from pause import until
from pip._vendor.distlib.util import CSVReader
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
from collections import Counter, OrderedDict
import math

Base = declarative_base()
plugin = Plugin()

exclusions = []
temporary_exclusions = {}

results_dir = "/root/results/"

SUCCESS_ERROR_MESSAGE = "WIRE_INCORRECT_OR_UNKNOWN_PAYMENT_DETAILS"

def next_experiment_id():
    if not os.path.exists(results_dir):
        os.makedirs(results_dir)

    experiment_files = [f for f in listdir(results_dir) if isfile(join(results_dir, f)) and f.endswith('.log')]
    ids = [int(file.split('.')[0]) for file in experiment_files]
    if not ids:
        ids = [0]
    return max(ids) + 1


def log(eid, msg):
    if not isinstance(msg, str):
        msg = repr(msg)
    file = join(results_dir, '{}.log'.format(eid))
    with open(file, 'a+') as f:
        print("[{}]: {}".format(datetime.utcnow().isoformat(), msg.encode('ascii', 'ignore').decode('ascii')), file=f)


def experiment_csv(eid, name=None):
    filename = str(eid)
    if name:
        filename += '-' + name
    return join(results_dir, '{}.csv'.format(filename))


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


overloaded_channels = dict()

@plugin.method('probe_all')
def probe_all(plugin, progress_file=None, parallel_probes=3, probes=100000, **kwargs):

    with Experiment('routing', ['planned_payments', 'no_route', 'payments', 'progress']) as (eid, print, write_planned, write_noroute, write_payment, write_progress):

        our_node_id = plugin.rpc.getinfo()['id']
        connected_peers = plugin.rpc.listpeers()['peers']
        nodes = plugin.rpc.listnodes()['nodes']

        # First, let's to some safety checks.
        # We only want to start sending money around once we have gathered enough gossip.

        print("max probes={}".format(probes, probes))
        print("There are {} connected peers, {} visible nodes".format(len(connected_peers), len(nodes)))
        print("Current node id: {}".format(our_node_id))

        if len(connected_peers) == 0:
            raise Exception("Please connect to some peers and wait a bit to collect the gossip")

        if len(nodes) < 5000:
            raise Exception("It seems that you don't have all the nodes in the network view yet. You have " + \
                   str(len(nodes)) +\
                   ", there are around 5500 (June 2020). Please connect to some peers and wait for more gossip.")

        amounts = { # mSatoshi -> USD
            106000: 0.01, # 0.01 USD
            10601000: 1, # 1 USD
            106015000: 10 # 10 USD
        }

        print("Loop done!")
        #return "Exiting for now"

        class Payment:
            def __init__(self, dest_node_id, amount_msat):
                self.dest_node_id = dest_node_id
                self.amount_msat = amount_msat
                self.amount_usd = amounts[amount_msat]

                self.latest_route = None
                self.route_length = None

                self.start_time = None
                self.end_time = None
                self.failcodename = None
                self.response = None
                self.success = None
                self.attempts = 0
                self.duration = 0

                self.failing_channels = []

            def __str__(self):
                return "Payment of {} ({}) to {}".format(self.amount_msat, self.amount_usd, self.dest_node_id)

            def add_failing_channel(self, error_data):
                """
                Call this whenever sendpay fails because of WIRE_TEMPORARY_CHANNEL_FAILURE.
                We need to remember which channel is faulty to re-attempt the connection along a different route next time.
                :param error_data: RpcError.error['data'] (thrown by sendpay)
                """
                entry = "{}/{}".format(error_data['erring_channel'], error_data['erring_direction'])
                self.failing_channels.append(entry)

            def add_overloaded_channel(self, error_data, duration=timedelta(minutes=10)):
                entry = "{}/{}".format(error_data['erring_channel'], error_data['erring_direction'])
                overloaded_channels[entry] = datetime.now() + duration

            def get_overloaded_channels(self):
                for ch in overloaded_channels.keys():
                    if overloaded_channels[ch] < datetime.now():
                        del overloaded_channels[ch]

                return list(overloaded_channels.keys())

            def find_route(self):
                try:
                    # The experiment is performed with 3 outgoing channels. We need to wait if they are all overloaded.
                    while len(self.get_overloaded_channels()) >= 3:
                        sleep(10)
                    route = plugin.rpc.getroute(self.dest_node_id, self.amount_msat, 1, exclude=self.failing_channels + self.get_overloaded_channels())
                    self.latest_route = route['route']
                    self.route_length = len(self.latest_route)
                    return True
                except RpcError as e:
                    if 'code' in e.error:
                        if e.error['code'] == 205:
                            if self.attempts == 0:
                                print("No route for amount {} found to {}".format(self.amount_msat, self.dest_node_id))
                                write_noroute(dict(
                                    nodeid=self.dest_node_id,
                                    amount_msat=self.amount_msat,
                                    amount_usd=self.amount_usd,
                                    timestamp=datetime.utcnow(),
                                    code=e.error['code'],
                                    response=str(e.error)
                                ))
                    print(str(e.error))
                    return False

            def send_with_retry(self, max_attempts=25):
                self.failing_channels.clear()
                while self.attempts < max_attempts:
                    new_route_found = self.find_route()
                    if not new_route_found:
                        if not self.latest_route:
                            # We can't even find 1 route to perform the payment
                            # Don't do anything with this payment.
                            # The find_route marks this payment as one with no known route.
                            return
                        self.failcodename = "NO ROUTES LEFT"
                        self.start_time = datetime.utcnow()
                        self.end_time = datetime.utcnow()
                        self.success = False
                        return
                    self.success = self.send()
                    if self.success or (not self.success and self.response == "Timeout"):
                        break
                    sleep(1)
                self.duration = self.end_time - self.start_time
                write_payment(self.__dict__)

            def send(self):
                self.attempts += 1
                print("Attempt {}: {}".format(self.attempts, self))
                payment_hash = ''.join(choice(string.hexdigits) for _ in range(64))
                self.start_time = datetime.utcnow()
                try:
                    plugin.rpc.sendpay(self.latest_route, payment_hash)
                    plugin.rpc.waitsendpay(payment_hash, 120)
                    self.response = "Timeout"
                    return False
                except RpcError as e:
                    self.response = e.error
                    print("Payment complete! Error: {}".format(e.error))
                    if e.error['message'] == "Timed out while waiting":
                        # If this is a timeout, we don't want to retry because that can overload our channels.
                        self.response = "Timeout"
                        self.failcodename = e.error['message']
                        return False
                    if "data" not in e.error:
                        return False
                    else:
                        resp = e.error["data"]
                        self.failcodename = resp["failcodename"]
                        if self.failcodename == SUCCESS_ERROR_MESSAGE:
                            return True
                        elif self.failcodename == "WIRE_UNKNOWN_NEXT_PEER":
                            return False
                        elif self.failcodename == "WIRE_TEMPORARY_CHANNEL_FAILURE":
                            if 'Too many HTLCs' in self.response['message']:
                                # Looks like we have overloaded (our?) channel. Let's not use this channel for a while
                                self.add_overloaded_channel(resp)
                                print("Too many HTLCs reported! Not using this channel for 10 minutes: {}".format(resp['erring_channel']))
                                self.attempts -= 1 # Don't count this attempt
                                return False
                            else:
                                self.add_failing_channel(resp)
                        elif self.failcodename == "WIRE_FEE_INSUFFICIENT":
                            self.add_failing_channel(resp)
                        elif self.failcodename == "WIRE_REQUIRED_NODE_FEATURE_MISSING":
                            # Channel is likely overloaded. Try another one.
                            self.add_failing_channel(resp)
                        return False
                finally:
                    self.end_time = datetime.utcnow()



        def send_one(payment):
            payment.send_with_retry()
            write_progress(Progress(payment.dest_node_id, payment.amount_msat).__dict__)
            return payment


        progress = set()
        class Progress:
            def __init__(self, nodeid, amount_msat):
                self.nodeid = nodeid
                self.amount_msat = int(amount_msat)

            def __eq__(self, other):
                return isinstance(other, Progress) \
                       and self.nodeid == other.nodeid and self.amount_msat == other.amount_msat

            def __hash__(self):
                return hash((self.nodeid, self.amount_msat))

            def __repr__(self):
                return "Progress {nodeid=" + self.nodeid + "; amount_msat=" + str(self.amount_msat) + "}"


        if progress_file:
            print("Reading progress from {}".format(progress_file))
            with open(progress_file, 'r') as f:
                r = DictReader(f, delimiter=',',
                                quotechar='"', quoting=csv.QUOTE_ALL, fieldnames=['amount_msat', 'nodeid'])
                next(r, None) # Skip headers
                for row in r:
                    p = Progress(row['nodeid'], row['amount_msat'])
                    progress.add(p)
            print("Found {} completed payments in past progress file {}".format(len(progress), progress_file))
            write_progress([p.__dict__ for p in progress])


        # Send Payments
        with ThreadPoolExecutor(max_workers=parallel_probes) as pool:
            payments = []
            for node in nodes[:probes]:
                for amount_msat in amounts.keys():
                    if Progress(node['nodeid'], amount_msat) in progress:
                        continue

                    payments.append(Payment(node['nodeid'], amount_msat))

            results = pool.map(send_one, payments)
            for completed in results:
                print("completed {}".format(completed))

        return "end of experiment"


# return [paths, len(paths)]

running_experiments = dict() # name => experiment object
class Experiment(object):
    """
    A wrapper class for performing per-experiment logging.
    Usage:
    with Experiment('experiment_name', ['csv1', 'csv2']) as eid, print, write_csv1, write_csv2:
        print("This is an experiment with experiment ID: " + eid)
        print("This print function will log to a separate file with name {eid}.log")
        write_csv1(dict(col1='value1', col2='value2')) # Writes 1 row into file `{eid}-csv1.csv', with headers
        write_csv1(rows) # Can also write many rows, if a list of dicts is supplied.
    """
    def __init__(self, name, csv_names, allow_attach=False):
        """
        Start or attach to an experiment. There can never be 2 experiments with the same name running at the same time.
        If :allow_attach is False and there is already an experiment with this name running, the method will raise an Exception.
        If :allow_attach is True in the same circumstances, we will attach to the running experiment.

        Starting a new experiment assigns a new Experiment ID (eid) to it. The log file and all CSVs will have the eid in their name.
        Attaching to an already running experiment will not create a new eid, but instead re-use all the files.

        :param name: Unique name of an experiment.
        :param csv_names: A list of csv names that this experiment is going to write to. Not paths, just names.
        Example: ['csv1', 'csv2'].
        :param allow_attach: whether we want to allow attaching to the experiment if it is already running.
        """
        self.name = name
        self.csv_names = csv_names
        self.allow_attach = allow_attach

    def __enter__(self):
        if self.name not in running_experiments:
            self.eid = next_experiment_id()
        else:
            if not self.allow_attach:
                msg = "Experiment with name {} is already running with EID {}".format(self.name, running_experiments[self.name].eid)
                raise Exception(msg)
            else:
                # Attach
                self.eid = running_experiments[self.name].eid

        # Redefine print to write to the file we want ({eid}.log)
        print = lambda msg="": log(eid=self.eid, msg=msg)
        csvs = []
        if not self.csv_names:
            self.csv_names = []
        for csv_name in self.csv_names:
            # Define each CSV as lambda that can write one or more rows to the corresponding file.
            csvfile = lambda rows, name=csv_name, eid=self.eid: write_csv_rows(eid, name, rows)
            csvs.append(csvfile)

        running_experiments[self.eid] = self
        print("Starting experiment {}, EID: {}".format(self.name, self.eid))
        return (self.eid, print, *csvs) # After this line the Experiment block is executed.

    def __exit__(self, exc_type, exc_val, exc_tb):
        # The experiment block finished executing (either naturally or because of an exception)
        log(self.eid, "Experiment completed!")
        del running_experiments[self.eid]
        if exc_val:
            # There had been an exception in Experiment block
            formatted = traceback.format_exception(exc_type, exc_val, exc_tb)
            log(self.eid, "Because an exception occurred: {}".format(repr(exc_val)))
            log(self.eid, "\n".join(formatted))
        return False


def write_csv_rows(eid, name, row_or_rows):
    csvfile = experiment_csv(eid, name=name)
    new = not os.path.exists(csvfile)
    if isinstance(row_or_rows, dict):
        rows = [row_or_rows]
    else:
        rows = row_or_rows

    rows = [OrderedDict(sorted(r.items())) for r in rows]

    with open(csvfile, 'a+', newline='') as csvfile:
        fieldnames = list(rows)[0].keys()
        writer = csv.DictWriter(csvfile, delimiter=',',
                                quotechar='"', quoting=csv.QUOTE_ALL, fieldnames=fieldnames)
        if new:
            writer.writeheader()
        writer.writerows(rows)


@plugin.method('only_connect')
def only_connect(plugin, nodes_file):
    """
    Connect to nodes (without opening any channels) and see if they gossip us and if they disconnect.
    Takes nodes from a file specified by nodes_file. Make sure to disconnect from all peers and clearing the local
    network view before running this!
    :param nodes_file That file must be in the same JSON format as what is returned by 'listnodes' RPC call.
    Can be acquired directly from listnodes. Alternatively, if you want to run this for nodes with specific channel degree,
    'lightning-cli nodes_with_degree min max' will produce a list of nodes in a similar format. See that call for more details.
    """

    if len(plugin.rpc.listpeers()['peers']) > 0:
        pass
        #return "ERROR: To run the connect_only experiment, " \
        #       " there should be no nodes in the local network view AND no peers connected."

    N_PEERS = -1 # number of peers to connect to. -1 For all on 1ml.com

    with Experiment('only_connect', ['peers', 'gossip']) as (eid, print, write_peers, write_gossip):

        print("nodes_file = {}".format(nodes_file))
        if not exists(nodes_file):
            raise Exception('File not found')
        with open(nodes_file, 'r') as f:
            nodes = json.loads(f.read())['nodes']
        print("Found {} nodes in nodes_file".format(len(nodes)))


        print("First {} best connected nodes: {}".format(N_PEERS, nodes))
        print()

        disconnects = dict()

        for node in nodes:
            nodeid = node['nodeid']
            host = node['addresses'][0]['address']
            port = node['addresses'][0]['port']
            connectstart = datetime.utcnow()
            try:
                result = plugin.rpc.connect(nodeid, host=host, port=port)
                success = True
            except Exception as e:
                result = repr(e)
                success = False
            connectend = datetime.utcnow()
            print("Connection to {}: {}".format(nodeid, result))
            write_peers(dict(nodeid=nodeid, address="{}:{}".format(host, port), connectstart=connectstart, connected_after=str(connectend - connectstart), success=success, connectresult=result))

        delays = []
        delays += map(lambda s: timedelta(seconds=s), [2, 4, 8, 12, 16, 20, 30, 60])
        delays += map(lambda m: timedelta(minutes=m), [1, 2, 3, 4, 5, 6, 10, 20, 30, 45, 60])
        delays += map(lambda h: timedelta(hours=h), [2, 4, 8, 12, 16, 20, 40])

        def listnodeids():
            """List ids of all nodes in our local network view"""
            return {n['nodeid'] for n in plugin.rpc.listnodes()['nodes']}

        start = datetime.utcnow()
        seen_node_ids = set()
        while delays:
            next_delay = delays.pop(0)
            until(start + next_delay) # Wait
            print("{} after start ({})!".format(next_delay, start))

            visible_nodes = listnodeids()
            new_nodes = visible_nodes - seen_node_ids
            seen_node_ids.update(new_nodes)

            write_gossip(dict(delay=str(next_delay),
                              timestamp=(start + next_delay).isoformat(),
                              n_peers=len(plugin.rpc.listpeers()['peers']),
                              new_nodes_since_last_time=len(new_nodes),
                              total_nodes=len(visible_nodes)))


def on_disconnect(id, **kwargs):
    nodeid = id
    if 'only_connect' in running_experiments:
        with Experiment('only_connect', ['disconnects'], allow_attach=False) as (eid, print, write_disconnect):
            print("Node disconnected! {}".format(nodeid))

            write_disconnect(dict(nodeid=nodeid, timestamp=datetime.utcnow().isoformat()))


@plugin.method('nodes_with_degree')
def nodes_with_degree(channel_count_min, channel_count_max, limit=50):
    """
    Finds nodes that have a number of channels open between channel_count_min and channel_count_max.
    Uses listnodes RPC method as data source.
    Returns the result to stdout and writes to a file in /root/results/{min}-{max}.degreenodes.
    :param channel_count_min: Nodes with less channels than this number won't be included.
    :param channel_count_max: Nodes with more channels that this number won't be included.
    :param limit: At most this many nodes are going to be returned.
    :return:
    """
    nodes = [(n, len(plugin.rpc.listchannels(source=n['nodeid'])['channels'])) for n in plugin.rpc.listnodes()['nodes']]

    with_degree = [n for n, degree in nodes if channel_count_min <= degree <= channel_count_max and 'addresses' in n]
    with_degree = with_degree[:min(limit, len(with_degree))]
    with open('/root/results/{}-{}.degreenodes'.format(channel_count_min, channel_count_max), 'w+') as f:
        f.write(json.dumps(dict(nodes=with_degree)))
    return with_degree


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
                    return "{}:{}".format(addr, a['port']) if 'port' in a else addr

        session = plugin.Session()
        public = list()

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
                raise Exception("Found unknown address types: " + str(other_types))

            session.add(node)
        print("Saving {} nodes with exposed network addresses.".format(len(public)))
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
                connections = [c for f in futures for c in f.result()]
                session.add_all(connections)
                session.flush()

        offsets = [timedelta(minutes=10), timedelta(minutes=30), timedelta(minutes=60),
                   timedelta(hours=2), timedelta(hours=4), timedelta(hours=8), timedelta(hours=12), timedelta(hours=16), timedelta(hours=20), timedelta(hours=24),
                   timedelta(hours=36), timedelta(hours=48), timedelta(hours=60), timedelta(hours=72)]

        iteration = 1
        while offsets:
            start = datetime.utcnow()
            print("Iteration {}. Trying to connect to {} nodes".format(iteration, len(public)))
            try_all_nodes()
            print("Completed in {}".format(str((datetime.utcnow() - start).total_seconds())))
            delta = offsets.pop(0)
            print("Will try again in {}".format(str(delta)))
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
plugin.add_subscription('disconnect', on_disconnect)
plugin.run()
