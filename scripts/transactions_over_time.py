import sys
import re
import argparse
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, timedelta

def str2datetime(s):
    parts = s.split('.')
    dt = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
    return dt.replace(microsecond=int(parts[1]))

def plot_transactions_over_time(fname, x, y):
    plt.figure(figsize=(10, 6))
    plt.plot(x, y, marker='o', linestyle='-')
    plt.xlabel('Time')
    plt.ylabel('Total Transactions')
    plt.title('Total Transactions Over Time')
    plt.grid(True)
    plt.savefig(fname)
    plt.show()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--file', type=str, required=True,
                        help='Path to the file containing transaction timestamps')
    args = parser.parse_args()

    commit_pat = re.compile('([^[].*) \[hotstuff proto\] commit (.*)')
    interval = 5

    timestamps = []

    with open(args.file, 'r') as file:
        lines = file.readlines()
        for line in lines:
            m = commit_pat.match(line)
            if m:
                timestamps.append(str2datetime(m.group(1)))

    timestamps.sort()
    start_time = timestamps[0]
    end_time = timestamps[-1]

    total_transactions = []
    current_total = 0
    current_index = 0

    for timestamp in timestamps:
        while timestamp - start_time >= current_index * timedelta(seconds=interval):
            total_transactions.append(current_total)
            current_index += 1
        current_total += 1

    # Append remaining zeros if necessary
    while current_index * timedelta(seconds=interval) <= end_time - start_time:
        total_transactions.append(current_total)
        current_index += 1

    # Normalize time axis
    x = np.linspace(0, 380, len(total_transactions))

    plot_transactions_over_time("transactions_over_time.png", x, total_transactions)
