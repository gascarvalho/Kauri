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

def plot_hist(fname, data, labels, reconfig_data, avg_tx_secs):
    plt.xlabel(r"time (s)")
    plt.ylabel(r"tx")
    plt.suptitle("Average Transactions committed per time interval", fontsize=12)
    plt.title("Sliding Window Size: {}s, Block size: {}txs".format(args.window_size, args.blksize), fontsize=10)

    for i, (x, y) in enumerate(data):
        color = plt.plot(x, y, marker='o', markersize=3.5, label=labels[i])[0].get_color()
        plt.axhline(y=avg_tx_secs[i], color=color, linestyle='--', linewidth=1)
        plt.text(-5, avg_tx_secs[i], 'Avg=' + str(int(avg_tx_secs[i])), color=color, ha='left', va='bottom', bbox=dict(facecolor='white', alpha=0.75), fontsize=8)
        if args.reconfig_lines and reconfig_data[i]:
            for reconfig in reconfig_data[i]:
                plt.axvline(x=reconfig, color=color, linestyle='--', linewidth=0.5)
    
    plt.legend()
    plt.savefig(fname)
    plt.show()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--window-size', type=float, default=1, required=False)
    parser.add_argument('--blksize', type=float, required=True)
    parser.add_argument('--output', type=str, default="hist.png", required=False)
    parser.add_argument('--files', type=str, nargs='+', required=True)
    parser.add_argument('--labels', type=str, nargs='+', required=True)
    parser.add_argument('--cutoff', type=float, default=9999, required=False)
    parser.add_argument('--reconfig-lines', action='store_true')
    args = parser.parse_args()

    if len(args.files) != len(args.labels):
        print("The number of files and labels must match.")
        sys.exit(1)

    commit_pat = re.compile(r'([^[].*) \[hotstuff proto\] commit (.*)')
    reconfig_pat = re.compile(r'([^[].*) \[hotstuff proto\] \[PMAKER\] Timeout reached!!!')
    
    window_size = args.window_size

    all_values = []
    all_reconfig_x = []
    avg_tx_secs = []

    for file in args.files:
        begin_time = None
        timestamps = []
        rcf_timestamps = []
        reconfig_x = []
        values = []

        with open(file, 'r') as f:
            lines = f.readlines()
            for line in lines:
                m = commit_pat.match(line)
                rcf = reconfig_pat.match(line)
                if m:
                    timestamps.append(str2datetime(m.group(1)))
                elif rcf:
                    rcf_timestamps.append(str2datetime(rcf.group(1)))
            
            timestamps.sort()
            rcf_timestamps.sort()

        i = 0
        j = 0
        cnt = 0
        total = 0
        cutoff_time = None

        for timestamp in timestamps:
            if begin_time is None:
                begin_time = timestamp
                cutoff_time = begin_time + timedelta(seconds=args.cutoff)

            if timestamp > cutoff_time:
                break

            while timestamp >= begin_time + timedelta(seconds=window_size):
                elapsed_time = (begin_time + timedelta(seconds=window_size) - begin_time).total_seconds()
                values.append(cnt / elapsed_time)
                begin_time += timedelta(seconds=window_size)
                j += 1
                cnt = 0
            cnt += args.blksize
            total += args.blksize

        elapsed_time = (begin_time + timedelta(seconds=window_size) - begin_time).total_seconds()
        values.append(cnt / elapsed_time)

        start_time = timestamps[0]
        end_time = timestamps[-1]

        if end_time > cutoff_time:
            end_time = cutoff_time

        total_time = (end_time - start_time).total_seconds()
        avg_tx_sec = total / total_time
        avg_tx_secs.append(avg_tx_sec)

        for rcf_timestamp in rcf_timestamps:
            if rcf_timestamp > cutoff_time:
                break
            elapsed_time = (rcf_timestamp - start_time).total_seconds()
            reconfig_x.append(elapsed_time)

        x = [i * window_size for i in range(len(values))]

        all_values.append((x, values))
        all_reconfig_x.append(reconfig_x)

    plot_hist(args.output, all_values, args.labels, all_reconfig_x, avg_tx_secs)
