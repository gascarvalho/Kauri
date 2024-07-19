import sys
import re
import argparse
import math
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
from scipy.interpolate import interp1d

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--file', type=str, required=True)
    args = parser.parse_args()

    sigs_pat = re.compile('Aggregating Sigs: (\d+) us to execute.')
    pubs_pat = re.compile('Aggregating Pubs: (\d+) us to execute.')
    verify_pat = re.compile('FastAggVerify: (\d+) us to execute.')
    
    counter = 0
    total_time = 0


    with open(args.file, 'r') as file:
        lines = file.readlines()
        for line in lines:
            sigs = sigs_pat.match(line)
            pubs = pubs_pat.match(line)
            verify = verify_pat.match(line)

            if sigs:
                total_time += int(sigs.group(1))
                
            elif pubs:
                total_time += int(pubs.group(1))

            elif verify:
                total_time += int(verify.group(1))
                counter += 1
    
    print(counter)
    print("Total time: {} microseconds (us) or {} milliseconds (ms)".format( total_time, round(total_time / 1000, 2)))
    print("Average Processing Time: {} us, or {} ms".format(round(total_time / counter, 2), round(total_time / (counter*1000), 2)))