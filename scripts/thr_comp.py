import sys
import re
import argparse
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
from scipy.interpolate import interp1d

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

def plot_hist(fnames, labels, window_size, blksize, cutoff):
    plt.rcParams["figure.figsize"] = (6, 3.9)
    
    plt.xlabel(r"tempo (s)")
    plt.ylabel(r"tx" if args.blksize else r"blocos")

    colors = plt.cm.get_cmap('tab10').colors  # Get a colormap with 10 colors
    color_idx = 0

    for fname, label in zip(fnames, labels):
        timestamps, rcf_timestamps = parse_file(fname)
        begin_time, values, reconfig_x, total, total_time, avg_tx_sec = process_data(timestamps, rcf_timestamps, window_size, blksize, cutoff)

        x = [i * window_size for i in range(len(values))]
        color = colors[color_idx % len(colors)]
        color_idx += 1

        plt.plot(x, values, marker='o', markersize=3.5, label=label, color=color)

        # Reconfiguration markers
        interp_func = interp1d(x, values, kind='linear', fill_value='extrapolate')
        reconfig_y = interp_func(reconfig_x)
        plt.plot(reconfig_x, reconfig_y, 'D', color=color)

        # Average transaction per second for whole experiment
        plt.axhline(y=(total / total_time), color=color, linestyle='-', linewidth=1)
        plt.text(0, avg_tx_sec, 'Média=' + str(round(float(avg_tx_sec), 3)), color=color, ha='left', va='bottom', bbox=dict(facecolor='white', alpha=0.75), fontsize=10)

    plt.legend()
    plt.savefig("hist.png")
    plt.show()

def parse_file(fname):
    commit_pat = re.compile('([^[].*) \[hotstuff proto\] commit (.*)')
    reconfig_pat = re.compile('([^[].*) \[hotstuff proto\] \[PMAKER\] Timeout reached!!!')

    timestamps = []
    rcf_timestamps = []

    with open(fname, 'r') as file:
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

    return timestamps, rcf_timestamps

def process_data(timestamps, rcf_timestamps, window_size, blksize, cutoff):
    begin_time = None
    cnt = 0
    total = 0
    values = []
    reconfig_x = []

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

    return begin_time, values, reconfig_x, total, total_time, avg_tx_sec

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--window-size', type=float, default=1, required=False)
    parser.add_argument('--blksize', type=float, required=False)
    parser.add_argument('--output', type=str, default="hist.png", required=False)
    parser.add_argument('--files', type=str, nargs='+', required=True)
    parser.add_argument('--labels', type=str, nargs='+', required=True)
    parser.add_argument('--cutoff', type=float, default=9999, required=False)
    args = parser.parse_args()

    if len(args.files) != len(args.labels):
        print("The number of files must match the number of labels.")
        sys.exit(1)

    blksize = args.blksize if args.blksize else 1
    plot_hist(args.files, args.labels, args.window_size, blksize, args.cutoff)

