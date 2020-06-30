from pandas import DataFrame, to_numeric
import matplotlib.pyplot as plt
from util.db import session
from util.plot import save_plot


def success_rate_combined():
    total = total_attempts_by_amount()
    attempt1_success = session.execute(
        "select amount_usd, count(*) from payments where success=True and attempts=1 group by amount_usd order by amount_usd asc")
    successes = DataFrame(attempt1_success, index=total.index)[[1]]
    successes /= total / 100
    successes.columns = ['Success on first attempt']

    attemptany_success = session.execute(
        "select amount_usd, count(*) from payments where success=True group by amount_usd order by amount_usd asc")
    successes_retries = DataFrame(attemptany_success, index=total.index)[[1]]
    successes_retries /= total / 100
    successes_retries.columns = ['Success after at most 25 attempts']

    combined = successes.join(successes_retries)
    plot = combined.plot(kind='bar')
    save_plot(plot, 'payment_success_rate', tilt_x_labels=True)

    print(combined)

#    combined = DataFrame({'1st attempt': successes, 'After at most 25 attempts': successes_retries}, index=total.index)
 #   print(combined)

def total_attempts_by_amount():
    total = session.execute("select amount_usd, count(*) from payments group by amount_usd order by amount_usd asc")
    total = DataFrame(total, index=['USD 0.01', 'USD 1', 'USD 10'])[[1]]
    return total


def attempts_hist():
    avgs = session.execute("select amount_usd, round(avg(attempts), 2) from payments where success=True group by amount_usd order by amount_usd asc")
    df = DataFrame(avgs)
    df[[1]] = df[[1]].astype(float)
    df[[1]] -= 1 # Display retries instead of attempts
    df.columns = ['', 'Average number of retires before success']
    df.index = ['USD 0.01', 'USD 1.00', 'USD 10.00']
    plot = df.plot(kind='bar')
    print(df)
    save_plot(plot, 'avg_attempts', tilt_x_labels=True)

def failures():
    data = session.execute("select failcodename from payments where success=False and failcodename is not null")
    df = DataFrame(data)
    df = df.groupby([0]).size()

    errors = {
        'WIRE_UNKNOWN_NEXT_PEER': 'Unknown next peer',
        'WIRE_TEMPORARY_CHANNEL_FAILURE': 'Temporary channel failure',
        'WIRE_TEMPORARY_NODE_FAILURE': 'Temporary node failure',
        'WIRE_FEE_INSUFFICIENT': 'Insufficient fee',
        'WIRE_REQUIRED_NODE_FEATURE_MISSING': 'Insufficient channel capacity',
        'WIRE_CHANNEL_DISABLED': 'Channel disabled'
    }
    df.index = [errors.get(e, e) for e in df.index]
    plt.pie(df)
    plt.legend(labels=df.index)
    plt.savefig(fname='payment_errors.png')

def success_by_route_length():
    data = session.execute(
        "select route_length, count(case success when True then 1 end)::float/count(*)*100 as success_rate, count(case route_length when route_length then 1 end) as count from payments group by route_length "
        "order by route_length asc")
    data = list(data)
    data = {
        'Hop count': [d['route_length'] for d in data],
        'Success rate, %': [d['success_rate'] for d in data],
        'Number of probes with this route length': [d['count'] for d in data]
    }
    df = DataFrame(data)
    df = df.set_index(['Hop count'])

    plot = df.plot(kind='bar', secondary_y='Number of probes with this route length')
    save_plot(plot, 'success_by_hops')


if __name__ == "__main__":
    failures()
    success_rate_combined()
    attempts_hist()
    success_by_route_length()
