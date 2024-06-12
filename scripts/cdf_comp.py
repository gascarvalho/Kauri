import sys
import re
import argparse
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, timedelta

def remove_outliers(x, outlierConstant=1.5):
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

def plot_hist(fname, x, y, avg_tx_sec, reconfig_x):
    plt.xlabel(r"time (s)")
    plt.ylabel(r"tx")
    plt.suptitle("Average Transactions committed per time interval", fontsize=12)
    plt.title("Sliding Window Size: {}s, Block size: {}txs".format(args.window_size, args.blksize), fontsize=10)
    plt.plot(x, y, marker='o', markersize=3.5)
    plt.xlim(left=0)
    plt.ylim(bottom=0)

    for reconfig in reconfig_x:
        plt.axvline(x=reconfig, color='r', linestyle='--', linewidth=0.5)

    plt.axhline(y=avg_tx_sec, color='g', linestyle='-', linewidth=1)
    plt.text(0, avg_tx_sec, 'Average tx/sec=' + str(int(avg_tx_sec)), color='green', ha='left', va='bottom')

    plt.savefig(fname)
    plt.show()

def plot_cdf(fname, x1, y1, x2=None, y2=None):
    x1 = np.array(x1)
    y1 = np.array(y1)

    pdf1 = y1 / sum(y1)
    cdf1 = np.cumsum(pdf1)

    #plt.plot(x1, pdf1, color="red", label="PDF File 1")
    plt.plot(x1, cdf1, color="blue", label=args.labels[0])
    
    if x2 is not None and y2 is not None:
        x2 = np.array(x2)
        y2 = np.array(y2)

        pdf2 = y2 / sum(y2)
        cdf2 = np.cumsum(pdf2)

        #plt.plot(x2, pdf2, color="green", label="PDF File 2")
        plt.plot(x2, cdf2, color="orange", label=args.labels[1])

    plt.legend()
    plt.savefig(fname)
    plt.show()

def process_file(file_path, window_size, blksize, cutoff):
    commit_pat = re.compile('([^[].*) \[hotstuff proto\] commit (.*)')
    reconfig_pat = re.compile('([^[].*) \[hotstuff proto\] \[PMAKER\] Timeout reached!!!')
    
    timestamps = []
    rcf_timestamps = []
    reconfig_x = []
    values = []

    with open(file_path, 'r') as file:
        lines = file.readlines()
        for line in lines:
            m = commit_pat.match(line)
            rcf = reconfig_pat.match(line)
            if m:
                timestamps.append(str2datetime(m.group(1)))
            elif rcf:
                rcf_timestamps.append(str2datetime(rcf.group(1)))
        
        timestamps.sort()
        rcf_timestamps.sort()

    begin_time = None
    cnt = 0
    total = 0
    cutoff_time = None

    for timestamp in timestamps:
        if begin_time is None:
            begin_time = timestamp
            cutoff_time = begin_time + timedelta(seconds=cutoff)

        if timestamp > cutoff_time:
            break

        while timestamp >= begin_time + timedelta(seconds=window_size):
            elapsed_time = (begin_time + timedelta(seconds=window_size) - begin_time).total_seconds()
            values.append(cnt / elapsed_time)
            begin_time += timedelta(seconds=window_size)
            cnt = 0
        cnt += blksize
        total += blksize

    elapsed_time = (begin_time + timedelta(seconds=window_size) - begin_time).total_seconds()
    values.append(cnt / elapsed_time)

    start_time = timestamps[0]
    end_time = timestamps[-1]

    if end_time > cutoff_time:
        end_time = cutoff_time

    total_time = (end_time - start_time).total_seconds()
    avg_tx_sec = total / total_time

    for rcf_timestamp in rcf_timestamps:
        if rcf_timestamp > cutoff_time:
            break
        elapsed_time = (rcf_timestamp - start_time).total_seconds()
        reconfig_x.append(elapsed_time)

    x = [i * window_size for i in range(len(values))]
    return x, values, avg_tx_sec, reconfig_x

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--window-size', type=float, default=1, required=False)
    parser.add_argument('--blksize', type=float, required=True)
    parser.add_argument('--output', type=str, default="hist.png", required=False)
    parser.add_argument('--file', type=str, required=True)
    parser.add_argument('--file2', type=str, required=False)
    parser.add_argument('--labels', type=str, nargs='+', required=True)
    parser.add_argument('--plot', type=str, default="hist", required=False)
    parser.add_argument('--cutoff', type=float, default=9999, required=False)
    args = parser.parse_args()

    if (len(args.labels) != 2):
        print("The number of files and labels must match.")
        sys.exit(1)
    
    x1, values1, avg_tx_sec1, reconfig_x1 = process_file(args.file, args.window_size, args.blksize, args.cutoff)

    if args.file2:
        x2, values2, avg_tx_sec2, reconfig_x2 = process_file(args.file2, args.window_size, args.blksize, args.cutoff)
    else:
        x2, values2, avg_tx_sec2, reconfig_x2 = None, None, None, None

    plot_cdf(args.output, x1, values1, x2, values2)
