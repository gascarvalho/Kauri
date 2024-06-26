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

def plot_hist(fname, x, y):
    plt.rcParams["figure.figsize"] = (6, 3.9)
    
    plt.xlabel(r"tempo (s)")

    if args.blksize is None:
        plt.ylabel(r"blocos/s")
        #plt.suptitle("Débito ao longo to tempo (bloco/s)", fontsize=12)
        #plt.title("Sliding Window Size: {}s, Block size: {}txs".format(args.window_size, args.blksize), fontsize=10)
    else:
        plt.ylabel(r"tx/s")
        #plt.suptitle("Débito ao longo to tempo (transação/s)", fontsize=12)
        #plt.title("Sliding Window Size: {}s, Block size: {}txs".format(args.window_size, args.blksize), fontsize=10)


    plt.plot(x, y, marker='o', markersize=3.5, label='bloco/s')
    plt.xlim(left=0)
    plt.ylim(bottom=0)

    # Reconfiguration lines
    # for reconfig in reconfig_x:
    #     plt.axvline(x=reconfig, color='r', linestyle='--', linewidth=0.5)
    #     # plt.text(reconfig, -.05, 'x='+str(reconfig), color='red', ha='center', va='top')

    # Reconfiguration markers
    # Interpolate to find the y-values at the reconfiguration points
    interp_func = interp1d(x, y, kind='linear', fill_value='extrapolate')
    reconfig_y = interp_func(reconfig_x)
    plt.plot(reconfig_x, reconfig_y, 'cD', label='reconfiguração')

    # Average transaction per second for whole experiment
    plt.axhline(y=(total / total_time), color='g', linestyle='-', linewidth=1)
    plt.text(0, avg_tx_sec, 'Média=' + str(round(float(avg_tx_sec), 3)), color='green', ha='left', va='bottom', bbox=dict(facecolor='white', alpha=0.75), fontsize=10)

    plt.legend()
    plt.savefig(fname)
    plt.show()

def plot_cdf(fname, x, y):
    x = np.array(x)
    y = np.array(y)

    # finding the PDF of the histogram using count values 
    pdf = y / sum(y) 
    
    # We can also find using the PDF values by looping and adding 
    cdf = np.cumsum(pdf) 

    print("PDF: ", pdf)
    print("CDF: ", cdf)
    print("Values: ", y)
    print("Bin edges: ", x)

    # Reconfiguration lines
    for reconfig in reconfig_x:
        plt.axvline(x=reconfig, color='r', linestyle='--', linewidth=0.5)
        # plt.text(reconfig, -.05, 'x='+str(reconfig), color='red', ha='center', va='top')

    plt.plot(x, pdf, color="red", label="PDF") 
    plt.plot(x, cdf, label="CDF") 
    plt.legend()
    plt.savefig(fname) 
    plt.show()

import numpy as np

def sliding_window_average(events, window_size, step_size):
    """
    Compute the sliding window average of events over time.

    Parameters:
    events (list of tuples): A list of (timestamp, value) tuples.
    window_size (float): The size of the sliding window.
    step_size (float): The step size for moving the window.

    Returns:
    list of tuples: A list of (timestamp, average_value) tuples representing the sliding window averages.
    """
    if not events:
        return []

    events.sort()  # Ensure events are sorted by timestamp
    timestamps = [event[0] for event in events]
    values = [event[1] for event in events]
    
    start_time = timestamps[0]
    end_time = timestamps[-1]
    
    averages = []
    current_time = start_time
    
    while current_time + window_size <= end_time:
        window_end = current_time + window_size
        window_values = [value for timestamp, value in events if current_time <= timestamp < window_end]
        if window_values:
            window_average = np.mean(window_values)
            averages.append((current_time, window_average))
        current_time += step_size
    
    return averages

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
    args = parser.parse_args()
    commit_pat = re.compile('([^[].*) \[hotstuff proto\] Core deliver') #To count in special cases where chain is enforced later
    #commit_pat = re.compile('([^[].*) \[hotstuff proto\] commit (.*)')
    reconfig_pat = re.compile('([^[].*) \[hotstuff proto\] \[PMAKER\] Timeout reached!!!')
    
    window_size = args.window_size
    begin_time = None
    blksize = args.blksize
    timestamps = []
    rcf_timestamps = []
    reconfig_x = []
    values = []

    if blksize is None:
        blksize = 1 #Graph with blocks committed instead

    with open(args.file, 'r') as file:
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

    timestamps = [ts for ts in timestamps if ts > (timestamps[0] + timedelta(seconds=args.warmup))]
    rcf_timestamps = [ts for ts in rcf_timestamps if ts > timestamps[0]]

    start_time = timestamps[0]
    end_time = timestamps[-1]

    begin_time = None
    i = 0
    j = 0
    cnt = 0
    total = 0
    cutoff_time = None
    values = []
    x = []

    # Initialize begin_time and cutoff_time
    for timestamp in timestamps:
        if begin_time is None:
            begin_time = timestamp
            cutoff_time = begin_time + timedelta(seconds=args.cutoff)

        if timestamp > cutoff_time:
            break

        # Move to the next window if current timestamp exceeds window end time
        while timestamp >= begin_time + timedelta(seconds=window_size):
            x += [(begin_time - start_time).total_seconds()]
            values.append(cnt / window_size)
            begin_time += timedelta(seconds=window_size)
            j += 1
            cnt = 0
        cnt += blksize
        total += blksize

    # Add the final value after the loop ends
    
    values.append(cnt / window_size)
    x += [(begin_time - start_time).total_seconds()]

    if end_time > cutoff_time:
        end_time = cutoff_time

    total_time = (end_time - start_time).total_seconds()
    avg_tx_sec = total / total_time

    # Calculate reconfiguration positions
    for rcf_timestamp in rcf_timestamps:
        if rcf_timestamp > cutoff_time:
            break
        elapsed_time = (rcf_timestamp - start_time).total_seconds()
        # Check if it's better to use find nearest or direct elapsed time
        reconfig_x.append(find_nearest(x, elapsed_time))

    if(args.plot == "hist"):
        plot_hist(args.output, x, values)
    elif(args.plot == "cdf"):
        plot_cdf(args.output, x, values)

