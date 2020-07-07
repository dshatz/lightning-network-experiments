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


def hi(plugin):
    return "hi"


overloaded_channels = dict()

@plugin.method('probe_all')
def probe_all(plugin, progress_file=None, parallel_probes=3, probes=100000, **kwargs):

    with Experiment('routing', ['no_route', 'payments', 'progress']) as (eid, print, write_noroute, write_payment, write_progress):

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
                        # Some very rare nodes are weird and don't supply proper errors.
                        self.failcodename = resp.get("failcodename", None)
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
