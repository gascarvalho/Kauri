import sys
import re
import argparse
import numpy as np
from datetime import datetime, timedelta

def remove_outliers(x, outlierConstant = 1.5):
    a = np.array(x)
    upper_quartile = np.percentile(a, 75)
    lower_quartile = np.percentile(a, 25)
    IQR = (upper_quartile - lower_quartile) * outlierConstant
    quartileSet = (lower_quartile - IQR, upper_quartile + IQR)
    resultList = []
    removedList = []
    for y in a.tolist():
        if y >= quartileSet[0] and y <= quartileSet[1]:
            resultList.append(y)
        else:
            removedList.append(y)
    return (resultList, removedList)

def str2datetime(s):
    parts = s.split('.')
    dt = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
    return dt.replace(microsecond=int(parts[1]))

def plot_thr(fname, x, y):
    import matplotlib.pyplot as plt
    plt.xlabel(r"time (s)")
    plt.ylabel(r"average tx/sec")
    plt.plot(x, y)
    plt.xlim(left=0)
    plt.ylim(bottom=0)
    plt.savefig(fname)
    plt.show()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--interval', type=float, default=1, required=False)
    parser.add_argument('--blksize', type=float, required=True);
    parser.add_argument('--output', type=str, default="hist.png", required=False)
    parser.add_argument('--plot', action='store_true')
    parser.add_argument('--file', type=str, required=True)
    args = parser.parse_args()
    commit_pat = re.compile('([^[].*) \[hotstuff proto\] commit (.*)')
    interval = args.interval
    begin_time = None
    next_begin_time = None
    cnt = 0
    timestamps = []
    values = []
    with open(args.file, 'r') as file:
        lines = file.readlines()
        for line in lines:
            m = commit_pat.match(line)
            if m:
                timestamps.append(str2datetime(m.group(1)))
        timestamps.sort()
        for timestamp in timestamps:
            if begin_time is None:
                begin_time = timestamp
                next_begin_time = begin_time + timedelta(seconds=interval)
            while timestamp >= next_begin_time:
                elapsed_time = (next_begin_time - begin_time).total_seconds()
                values.append(cnt / elapsed_time)
                begin_time = next_begin_time
                next_begin_time += timedelta(seconds=interval)
                cnt = 0
            cnt += args.blksize
        
        # Add the final value after the loop ends
        elapsed_time = (next_begin_time - begin_time).total_seconds()
        values.append(cnt / elapsed_time)
        
        x = [i * interval for i in range(len(values))]
        
        print(values)
        
        if args.plot:
            plot_thr(args.output, x, values)
