from matplotlib import ticker, lines
from matplotlib.pyplot import tight_layout, xticks, bar
from matplotlib.ticker import Locator, IndexLocator, LinearLocator
from pandas import DataFrame, np
import matplotlib.pylab as plt
from util.db import session
from util.plot import save_plot

"""
This script takes data from postgresql and plots nice charts.
You need to manually import the CSVs produced by the experiment to Postgres."""


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


    node_types = np.array(['Nodes with 1 channel', 'Nodes with 5-10 channels',
                           'Nodes with higest number\n of channels in the network'])
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

    print(df)
    plot = df.plot(kind='bar',
            stacked=True)

    save_plot(plot, 'address_types.png', tilt_x_labels=True)


def connection_times():
    durations = list(session.execute("select extract(second from connected_after) from connects"))
    df = DataFrame(durations, columns=['Connection time (s)'])
    plot = df.plot(kind='hist')
    save_plot(plot, 'connection_times')


def connection_successes():
    successes = dict(list(session.execute("select category, count(*) as successes from connects where success=True group by category")))
    #failure = dict(list(session.execute("select category, count(*) as total from connects where success=False group by category")))


    refused = dict(list(session.execute("select category, count(*) from connects where connectresult like '%refused%' group by category")))
    no_tor_circuit = dict(list(session.execute("select category, count(*) from connects where connectresult like '%in progress%' group by category")))
    timeout = dict(list(session.execute("select category, count(*) from connects where connectresult like '%timed out%' group by category")))
    df_refused = DataFrame(refused.items(), columns=['category', 'Connection refused'])
    df_timeout = DataFrame(timeout.items(), columns=['category', 'Timeout'])
    df_tor = DataFrame(no_tor_circuit.items(), columns=['category', 'Unable to establish Tor circuit'])

    df = DataFrame(successes.items(), columns=['category', 'Connection succeeded'])
    dfe = df.merge(df_refused, how='left').merge(df_timeout, how='left').merge(df_refused, how='left').merge(df_tor, how='left')
    dfe = dfe.fillna(0)
    dfe.index = ['Nodes with 1 open channel', 'Nodes with 5-10 open channels',
                 'Nodes with higest number\n of channels in the network']


    def normalize(x):
        total = sum(x[1:])
        x[1:] *= 100
        x[1:] /= total
        return x

    df3 = dfe.apply(normalize, axis=1)
    plot = df3.plot(kind='bar', stacked=True, color='gryb')
    save_plot(plot, 'connection_success', tilt_x_labels=True)


def visible_nodes():
    nodes_over_time = list(session.execute("select extract (epoch from delay) as secs, to_char(delay, 'HH24 hrs, MI mnutes, ss secs') as time, total_nodes from gossip_most order by time asc"))
    df = DataFrame(nodes_over_time, columns=['secs', 'Time', 'Nodes in the local network view'])
    df = df.set_index(['secs'])
    def human_readable_interval(x):
        time = x['Time']
        components = time.split(', ')
        nonzero = [c for c in components if not c.startswith('00 ')]
        x['Time'] = ", ".join(nonzero)
        return x


    df = df.apply(human_readable_interval, axis=1)
    df = df.set_index(['Time'])
    print(df)
    plot = df.plot(kind='line', x_compat=True)
    # plot.get_figure().savefig('gossip.png')
    save_plot(plot, 'gossip', tilt_x_labels=60)

def connection_failure_reasons():

    refused = session.execute("select count(*) from connects where connectresult like '%refused%'").scalar()
    no_tor_circuit = session.execute("select count(*) from connects where connectresult like '%in progress%'").scalar()
    timeout = session.execute("select count(*) from connects where connectresult like '%timed out%'").scalar()
    index = ['TCP Connection refused', 'Unable to establish Tor circuit', 'Timeout', 'Other error']
    df = DataFrame({'Connection errors': [refused, no_tor_circuit, timeout, 1]}, index=index)
    plot = df.plot(kind='pie', y='Connection errors', labels=None)
    plot.set_ylabel('')
    save_plot(plot, 'connection_errors')

if __name__ == "__main__":
    #address_types()
    #connection_times()
    #connection_successes()
    visible_nodes()
    #connection_failure_reasons()