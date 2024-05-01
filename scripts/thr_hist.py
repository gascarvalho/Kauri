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
    if args.mode == "normal":
        plt.ylabel(r"tx")
        plt.suptitle("Transaction commands per time interval", fontsize=12)
    elif args.mode == "avg":
        plt.ylabel(r"avg_tx")
        plt.suptitle("Average Transaction commands per time interval", fontsize=12)
    plt.title("Interval: {}s, Block size: {}txs".format(interval, args.blksize), fontsize=10)
    plt.plot(x, y)
    plt.xlim(left=0)
    plt.ylim(bottom=0)
    for reconfig in reconfig_x:
        plt.axvline(x=reconfig, color='r', linestyle='--', linewidth=0.5)
        #plt.text(reconfig, -.05, 'x='+str(reconfig), color='red', ha='center', va='top')

    plt.savefig(fname)
    plt.show()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--interval', type=float, default=1, required=False)
    parser.add_argument('--blksize', type=float, required=True);
    parser.add_argument('--output', type=str, default="hist.png", required=False)
    parser.add_argument('--plot', action='store_true')
    parser.add_argument('--mode', type=str, default="normal", required=False)
    parser.add_argument('--file', type=str, required=True)
    args = parser.parse_args()
    commit_pat = re.compile('([^[].*) \[hotstuff proto\] commit (.*)')
    reconfig_pat = re.compile('([^[].*) \[hotstuff proto\] \[PMAKER\] Timeout reached!!!')
    interval = args.interval
    begin_time = None
    next_begin_time = None
    cnt = 0
    timestamps = []
    rcf_timestamps = []
    reconfig_x = []
    values = []

    # Graph will be plotted as Tx/time interval
    if args.mode == "normal":

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
            i = 0
            j = 0
            for timestamp in timestamps:
                if begin_time is None:
                    begin_time = timestamp
                    next_begin_time = timestamp + timedelta(seconds=interval)
                while timestamp >= next_begin_time:

                    if i < len(rcf_timestamps) and timestamp > rcf_timestamps[i]:
                        reconfig_x.append(j-0.5)
                        i += 1

                    begin_time = next_begin_time
                    next_begin_time += timedelta(seconds=interval)
                    values.append(cnt)
                    j += 1
                    cnt = 0
                cnt += args.blksize
            values.append(cnt)

    # Graph will be plotted as Avg_Tx/time interval
    elif args.mode == "avg":

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
            i = 0
            j = 0
            for timestamp in timestamps:
                if begin_time is None:
                    begin_time = timestamp
                    next_begin_time = begin_time + timedelta(seconds=interval)
                while timestamp >= next_begin_time:

                    if i < len(rcf_timestamps) and timestamp > rcf_timestamps[i]:
                        reconfig_x.append(j-0.5)
                        i += 1

                    elapsed_time = (next_begin_time - begin_time).total_seconds()
                    values.append(cnt / elapsed_time)
                    begin_time = next_begin_time
                    next_begin_time += timedelta(seconds=interval)
                    j += 1
                    cnt = 0
                cnt += args.blksize
            
            # Add the final value after the loop ends
            elapsed_time = (next_begin_time - begin_time).total_seconds()
            values.append(cnt / elapsed_time)
    else:
        print("Invalid mode")
        sys.exit(1)

    start_time = timestamps[0]
    end_time = timestamps[-1]

    #x = np.linspace(0, end_time - start_time, len(values))
    
    #values = remove_outliers(values)[0]
    x = [i * interval for i in range(len(values))]
    reconfig_x = [i * interval for i in reconfig_x]
    #x = range(len(values))
    print(values)
    
    if args.plot:
        plot_thr(args.output, x, values)
