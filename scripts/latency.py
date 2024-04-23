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


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('--file', type=str, required=True)
    args = parser.parse_args()

    transactions = {}

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

    lats = []
    for key, value in transactions.items():
        if value.completed:
            lats.append(value.lat)

    lats = remove_outliers(lats)
    
    print("Average latency = {:.3f} ms".format(np.mean(lats)))