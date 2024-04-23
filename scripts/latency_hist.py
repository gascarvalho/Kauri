import sys
import re
import argparse
import numpy as np
from datetime import datetime, timedelta


class Transaction():

    def __init__(self, hash, send):
        self.hash = hash
        self.send = send
        self.completed = False

    def set_got(self, got):
        self.completed = True
        self.got = got
        l = self.got - self.send
        self.lat = l.total_seconds()

    def __lt__(self, other):
        return self.send < other.send


def str2datetime(s):

    parts = s.split('.')
    dt = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
    return dt.replace(microsecond=int(parts[1]))

def remove_outliers(lats, outlierConstant = 1.5):

    a = np.array(lats)
    upper_quartile = np.percentile(a, 75)
    lower_quartile = np.percentile(a, 25)
    iqr = (upper_quartile - lower_quartile) * outlierConstant
    quartileSet = (lower_quartile - iqr, upper_quartile + iqr)

    resultList = []
    for lat in lats:
        if lat >= quartileSet[0] and lat <= quartileSet[1]:
            resultList.append(lat)

    return resultList

def plot_mean_lat(fname, x, y):
    import matplotlib.pyplot as plt
    plt.xlabel(r"time (s)")
    plt.ylabel(r"average latency (ms)")
    plt.plot(x, y)
    plt.xlim(left=0)
    plt.ylim(bottom=0)
    plt.savefig(fname)
    plt.show()

if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('--interval', type=float, default=1, required=False)
    parser.add_argument('--output', type=str, default="lat_hist.png", required=False)
    parser.add_argument('--plot', action='store_true')
    parser.add_argument('--file', type=str, required=True)
    args = parser.parse_args()

    transactions = {}
    completed_txs = {}
    interval = args.interval
    begin_time = None
    next_begin_time = None
    cnt = 0

    send_line = re.compile('(.*) \[hotstuff info\] send new cmd (.*)$')
    got_line = re.compile('(.*) \[hotstuff info\] got fin <fin decision=1 cmd_idx=(.*) cmd_height=(.*) cmd=(.*) blk=(.*) tid=(.*)')

    with open(args.file, 'r') as file:
        lines = file.readlines()

        for line in lines:
            is_send_line = send_line.match(line)
            is_got_line = got_line.match(line)

            if is_send_line:
                t = Transaction(is_send_line.group(2), str2datetime(is_send_line.group(1)))
                transactions[is_send_line.group(2)] = t
            
            if is_got_line:
                t = transactions[is_got_line.group(4)]
                t.set_got(str2datetime(is_got_line.group(1)))

    for key, value in transactions.items():
        if value.completed:
            completed_txs[key] = value

    completed_txs = list(completed_txs.items())  # Convert dictionary to list of tuples

    # Sort the list based on the send attribute of Transaction objects
    completed_txs.sort(key=lambda x: x[1].send)

    values = []
    for key, value in completed_txs:
        if begin_time is None:
            begin_time = value.send
            next_begin_time = begin_time + timedelta(seconds=interval)
        while value.send >= next_begin_time:
            elapsed_time = (next_begin_time - begin_time).total_seconds()
            values.append(cnt / elapsed_time / 1000)  # Mean latency for this interval
            begin_time = next_begin_time
            next_begin_time += timedelta(seconds=interval)
            cnt = 0
        cnt += value.lat
    
    # Add the final value after the loop ends
    elapsed_time = (next_begin_time - begin_time).total_seconds()
    values.append(cnt / elapsed_time / 1000)  # Mean latency for the last interval

    values = remove_outliers(values)
        
    x = [i * interval for i in range(len(values))]

    if args.plot:
        plot_mean_lat(args.output, x, values)
