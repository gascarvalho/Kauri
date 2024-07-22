import sys
import re
import argparse
import math
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
from scipy.interpolate import interp1d
import matplotlib.colors as mcolors

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

def plot_hist(fname, data, labels, window_size, blksize):
    plt.rcParams["figure.figsize"] = (10, 6)
    plt.xlabel(r"tempo (s)")
    
    if args.blksize is None:
        plt.ylabel(r"blocos/s")
    else:
        plt.ylabel(r"tx/s")

    #label_offsets = [0,-0.075,0.075,0.04]
    colors = list(mcolors.TABLEAU_COLORS)
    for i, (x, y, reconfig_x, avg_tx_sec, total, total_time) in enumerate(data):
        color = colors[i]
        label = labels[i] if labels else f'Plot {i+1}'
        
        plt.plot(x, y, marker='o', markersize=3.5, label=label, color=color)
        
        interp_func = interp1d(x, y, kind='linear', fill_value='extrapolate')
        reconfig_y = interp_func(reconfig_x)
        plt.plot(reconfig_x, reconfig_y, 'D', color=color)
        
        plt.axhline(y=(total / total_time), color=color, linestyle='-', linewidth=1)
        plt.text(142, avg_tx_sec, 'Média=' + str(round(float(avg_tx_sec), 3)), color=color, ha='left', va='bottom', bbox=dict(facecolor='white', alpha=0.75), fontsize=10)

    plt.xlim(left=0)
    plt.ylim(bottom=1.5)
    plt.legend()
    plt.savefig(fname)
    plt.show()

def find_nearest(array, value):
    array = np.asarray(array)
    idx = (np.abs(array - value)).argmin()
    return array[idx]

def calculate_trailing_moving_average(values, window_size):
    averages = []
    for i in range(len(values)):
        if i < window_size:
            averages.append(np.mean(values[:i+1]))
        else:
            averages.append(np.mean(values[i-window_size+1:i+1]))
    return averages

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--window-size', type=float, default=1, required=False)
    parser.add_argument('--blksize', type=float, required=False)
    parser.add_argument('--output', type=str, default="hist.png", required=False)
    parser.add_argument('--files', type=str, nargs='+', required=True)
    parser.add_argument('--labels', type=str, nargs='+', required=False)
    parser.add_argument('--cutoff', type=float, default=9999, required=False)
    parser.add_argument('--warmup', type=float, default=0, required=False)
    parser.add_argument('--moving-average-window', type=int, default=1, required=False)
    args = parser.parse_args()
    commit_pat = re.compile('([^[].*) \[hotstuff proto\] Core deliver(.*)')
    reconfig_pat = re.compile('([^[].*) \[hotstuff proto\] \[PMAKER\] Timeout reached!!!')
    
    window_size = args.window_size
    blksize = args.blksize
    data = []

    if blksize is None:
        blksize = 1

    for file in args.files:
        timestamps = []
        rcf_timestamps = []
        values = []
        x = []

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

        timestamps = [ts for ts in timestamps if ts > (timestamps[0] + timedelta(seconds=args.warmup))]
        rcf_timestamps = [ts for ts in rcf_timestamps if ts > timestamps[0]]

        start_time = timestamps[0]
        end_time = timestamps[-1]

        begin_time = None
        cnt = 0
        total = 0
        cutoff_time = None
        values = []
        x = []

        for timestamp in timestamps:
            if begin_time is None:
                begin_time = timestamp
                cutoff_time = begin_time + timedelta(seconds=args.cutoff)

            if timestamp > cutoff_time:
                break

            while timestamp >= begin_time + timedelta(seconds=window_size):
                x += [(begin_time - start_time).total_seconds()]
                values.append(cnt / window_size)
                begin_time += timedelta(seconds=window_size)
                cnt = 0
            cnt += blksize
            total += blksize

        values.append(cnt / window_size)
        x += [(begin_time - start_time).total_seconds()]

        if end_time > cutoff_time:
            end_time = cutoff_time

        total_time = (end_time - start_time).total_seconds()
        avg_tx_sec = total / total_time

        reconfig_x = []
        for rcf_timestamp in rcf_timestamps:
            if rcf_timestamp > cutoff_time:
                break
            elapsed_time = (rcf_timestamp - start_time).total_seconds()
            #reconfig_x.append(find_nearest(x, elapsed_time))
            reconfig_x.append(elapsed_time)

        moving_average_window = args.moving_average_window
        values = calculate_trailing_moving_average(values, moving_average_window)

        data.append((x, values, reconfig_x, avg_tx_sec, total, total_time))

    plot_hist(args.output, data, args.labels, window_size, blksize)
