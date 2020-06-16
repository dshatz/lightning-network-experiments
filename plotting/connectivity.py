import itertools
import os
from datetime import timedelta
from itertools import repeat

from matplotlib.pyplot import tight_layout, xticks
from pandas import DataFrame, np
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

"""
This script takes data from postgresql and plots nice charts.
You need to manually import the CSVs produced by the experiment to Postgres."""

engine = create_engine(os.environ['SQLALCHEMY_URL'])

session = Session(bind=engine)

def address_types():
    ip_only_1 = session.execute(
        "select count(*) from nodes_1_channel where total_ip > 0 and total_addresses=total_ip").scalar()
    ip_only_5_10 = session.execute(
        "select count(*) from nodes_5_10_channels where total_ip > 0 and total_addresses=total_ip").scalar()
    ip_only_best_connected = session.execute(
        "select count(*) from nodes_best_connected where total_ip > 0 and total_addresses=total_ip").scalar()

    tor_only_1 = session.execute(
        "select count(*) from nodes_1_channel where total_tor > 0 and total_addresses=total_tor").scalar()
    tor_only_5_10 = session.execute(
        "select count(*) from nodes_5_10_channels where total_tor > 0 and total_addresses=total_tor").scalar()
    tor_only_best_connected = session.execute(
        "select count(*) from nodes_best_connected where total_tor > 0 and total_addresses=total_tor").scalar()

    both_1 = session.execute(
        "select count(*) from nodes_1_channel where total_tor > 0 and total_ip > 0").scalar()
    both_5_10 = session.execute(
        "select count(*) from nodes_5_10_channels where total_tor > 0 and total_ip > 0").scalar()
    both_best_connected = session.execute(
        "select count(*) from nodes_best_connected where total_tor > 0 and total_ip > 0").scalar()


    node_types = np.array(['Nodes with 1 channel', 'Nodes with 5-10 channels', 'Nodes with a lot of channels'])
    ip_only = np.array([ip_only_1, ip_only_5_10, ip_only_best_connected])
    tor_only = np.array([tor_only_1, tor_only_5_10, tor_only_best_connected])
    both = np.array([both_1, both_5_10, both_best_connected])

    total_1 = ip_only_1 + tor_only_1 + both_1
    total_5_10 = ip_only_5_10 + tor_only_5_10 + both_5_10
    total_best = ip_only_best_connected + tor_only_best_connected + both_best_connected
    total = [total_1, total_5_10, total_best]

    proportion_ip = np.true_divide(ip_only, total) * 100
    proportion_tor = np.true_divide(tor_only, total) * 100
    proportion_both = np.true_divide(both, total) * 100



    df = DataFrame([proportion_ip, proportion_tor, proportion_both]).transpose()
    df.index = node_types
    df.columns = ['IP addresses only', 'Onion addresses only', 'Both kinds of addresses']

    plot = df.plot(kind='bar',
            stacked=True)

    saveplot(plot, 'address_types.png', tilt_x_labels=True)


def connection_times():
    durations = list(session.execute("select extract(second from connected_after) from peers"))
    df = DataFrame(durations, columns=['Connection time (s)'])
    plot = df.plot(kind='hist')
    saveplot(plot, 'connection_times')


def connection_successes():
    successes = dict(list(session.execute("select category, count(*) as successes from peers where success=True group by category")))
    failure = dict(list(session.execute("select category, count(*) as total from peers where success=False group by category")))

    df = DataFrame(successes.items(), columns=['category', 'Connection succeeded'])
    df2 = DataFrame(failure.items(), columns=['category', 'Connection failed'])
    df3 = df.merge(df2, how='left')
    df3.index = ['Nodes with 1 open channel', 'Nodes with 5-10 open channels', 'Nodes with a lot of open channels']
    df3 = df3.fillna(0)

    def normalize(x):
        total = sum(x[1:])
        x[1:] *= 100
        x[1:] /= total
        return x

    df3 = df3.apply(normalize, axis=1)
    plot = df3.plot(kind='bar', stacked=True)
    saveplot(plot, 'connection_success', tilt_x_labels=True)


def visible_nodes():
    nodes_over_time = list(session.execute("select extract (epoch from delay) as secs, to_char(delay, 'HH24 hrs, MI mnutes, ss secs') as time, total_nodes from gossip_most order by time asc"))
    df = DataFrame(nodes_over_time, columns=['secs', 'time', 'total_nodes'])
    df.index = df[['secs']]
    print(df)
    plot = df.plot(kind='line', x_compat=True)
    saveplot(plot, 'gossip', tilt_x_labels=60)

def saveplot(plot, name, tilt_x_labels=False):
    if tilt_x_labels:
        degrees = 45 if isinstance(tilt_x_labels, bool) else tilt_x_labels
        xticks(rotation=degrees)
    tight_layout()
    plot.get_figure().savefig(name)

if __name__ == "__main__":
    address_types()
    connection_times()
    connection_successes()
    visible_nodes()