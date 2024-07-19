import sys
import re
import argparse
import math
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

def calculate_trailing_moving_average(values, window_size):
    averages = []
    for i in range(len(values)):
        if i < window_size:
            averages.append(np.mean(values[:i+1]))
        else:
            averages.append(np.mean(values[i-window_size+1:i+1]))
    return averages

def plot_hist(fname, x, y, reconfig_x, total, total_time, avg_tx_sec, lat_x, lat_y):
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 8))

    # First subplot: original histogram
    ax1.set_xlabel(r"tempo (s)")
    if args.blksize is None:
        ax1.set_ylabel(r"blocos/s")
    else:
        ax1.set_ylabel(r"tx/s")

    ax1.plot(x, y, marker='o', markersize=3.5, label='bloco/s')
    ax1.set_xlim(left=0)
    ax1.set_ylim(bottom=0)

    # Reconfiguration markers for first subplot
    interp_func = interp1d(x, y, kind='linear', fill_value='extrapolate')
    reconfig_y = interp_func(reconfig_x)
    ax1.plot(reconfig_x, reconfig_y, 'cD', label='reconfiguração')

    # Average transaction per second for whole experiment
    ax1.axhline(y=(total / total_time), color='g', linestyle='-', linewidth=1)
    ax1.text(0, avg_tx_sec, 'Média=' + str(round(float(avg_tx_sec), 3)), color='green', ha='left', va='bottom', bbox=dict(facecolor='white', alpha=0.75), fontsize=10)

    ax1.legend()

    # Second subplot: latency plot
    ax2.set_xlabel(r"tempo (s)")
    ax2.set_ylabel(r"latência (ms)")

    ax2.plot(lat_x, lat_y, marker='o', markersize=3.5, color='r', label='latência')
    ax2.set_xlim(left=0)
    ax2.set_ylim(bottom=0)

    # Reconfiguration markers for second subplot
    interp_func = interp1d(lat_x, lat_y, kind='linear', fill_value='extrapolate')
    reconfig_lat_y = interp_func(reconfig_x)
    ax2.plot(reconfig_x, reconfig_lat_y, 'cD', label='reconfiguração')

    ax2.legend()

    plt.tight_layout()
    plt.savefig(fname)
    plt.show()

def plot_cdf(fname, x, y, reconfig_x):
    x = np.array(x)
    y = np.array(y)

    pdf = y / sum(y)
    cdf = np.cumsum(pdf)

    print("PDF: ", pdf)
    print("CDF: ", cdf)
    print("Values: ", y)
    print("Bin edges: ", x)

    for reconfig in reconfig_x:
        plt.axvline(x=reconfig, color='r', linestyle='--', linewidth=0.5)

    plt.plot(x, pdf, color="red", label="PDF") 
    plt.plot(x, cdf, label="CDF") 
    plt.legend()
    plt.savefig(fname) 
    plt.show()

def find_nearest(array, value):
    array = np.asarray(array)
    idx = (np.abs(array - value)).argmin()
    return array[idx]

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--window-size', type=float, default=1, required=False)
    parser.add_argument('--blksize', type=float, required=False)
    parser.add_argument('--output', type=str, default="hist.png", required=False)
    parser.add_argument('--file', type=str, required=True)
    parser.add_argument('--plot', type=str, default="hist", required=False)
    parser.add_argument('--cutoff', type=float, default=9999, required=False)
    parser.add_argument('--warmup', type=float, default=0, required=False)
    parser.add_argument('--moving-average-window', type=int, default=1, required=False)
    args = parser.parse_args()

    commit_pat = re.compile('([^[].*) \[hotstuff proto\] Core deliver')
    #commit_pat = re.compile(r'([^[].*) \[hotstuff proto\] commit (.*)')
    reconfig_pat = re.compile('([^[].*) \[hotstuff proto\] \[PMAKER\] Timeout reached!!!')
    lat_pat = re.compile("([^[].*) \[hotstuff proto\] \[TIME\] Average latency per block \((\d+) tx\): (\d+) ms")
    lat_pat_2 = re.compile("([^[].*) \[hotstuff proto\] \[TIME\] Stats for last block: wall: ([\d.]+), cpu: ([\d.]+)")
   
    window_size = args.window_size
    begin_time = None
    blksize = args.blksize
    timestamps = []
    rcf_timestamps = []
    lat_timestamps = []
    reconfig_x = []
    lat_values = []
    values = []

    if blksize is None:
        blksize = 1

    with open(args.file, 'r') as file:
        lines = file.readlines()
        for line in lines:
            m = commit_pat.match(line)
            rcf = reconfig_pat.match(line)
            lat = lat_pat.match(line)
            lat_2 = lat_pat_2.match(line)

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

    ######################################### Throughput ############################################

    begin_time = None
    i = 0
    cnt = 0
    total = 0
    cutoff_time = None
    values = []
    x = []

    lat_x = []
    lat_y = []
    last_ts = None
    lat_acum = 0

    for timestamp in timestamps:

        if begin_time is None:
            begin_time = timestamp
            cutoff_time = begin_time + timedelta(seconds=args.cutoff)
            last_ts = timestamp

        lat_x += [(begin_time - start_time).total_seconds()]
        lat_y += [(timestamp - last_ts).total_seconds() * 1000]

        if timestamp > cutoff_time:
            break

        while timestamp >= begin_time + timedelta(seconds=window_size):
            x += [(begin_time - start_time).total_seconds()]
            values.append(cnt / window_size)
            begin_time += timedelta(seconds=window_size)
            cnt = 0
            lat_acum = 0

        cnt += blksize
        total += blksize

        last_ts = timestamp

    values.append(cnt / window_size)
    x += [(begin_time - start_time).total_seconds()]

    lat_x += [(begin_time - start_time).total_seconds()]
    lat_y.append(lat_acum / window_size)

    if end_time > cutoff_time:
        end_time = cutoff_time

    total_time = (end_time - start_time).total_seconds()
    avg_tx_sec = total / total_time

    ################################################################################################

    for rcf_timestamp in rcf_timestamps:
        if rcf_timestamp > cutoff_time:
            break
        elapsed_time = (rcf_timestamp - start_time).total_seconds()
        #reconfig_x.append(find_nearest(x, elapsed_time))
        reconfig_x.append(elapsed_time)

    moving_average_window = args.moving_average_window
    values = calculate_trailing_moving_average(values, moving_average_window)
    lat_y = calculate_trailing_moving_average(lat_y, moving_average_window)

    if args.plot == "hist":
        plot_hist(args.output, x, values, reconfig_x, total, total_time, avg_tx_sec, lat_x, lat_y)
    elif args.plot == "cdf":
        plot_cdf(args.output, x, values, reconfig_x)
