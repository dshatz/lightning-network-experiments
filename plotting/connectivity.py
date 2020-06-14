import os
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
    print(proportion_ip, proportion_tor, proportion_both)



    df = DataFrame([proportion_ip, proportion_tor, proportion_both]).transpose()
    df.index = node_types
    df.columns = node_types

    plot = df.plot(kind='bar',
            stacked=True)

    xticks(rotation=45)
    saveplot(plot, 'address_types.png')




def saveplot(plot, name):
    tight_layout()
    plot.get_figure().savefig(name)

if __name__ == "__main__":
    address_types()