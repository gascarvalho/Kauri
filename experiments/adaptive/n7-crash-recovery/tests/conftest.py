"""Import paths for the standalone N=7 experiment tools."""

from pathlib import Path
import sys


SCENARIO_DIRECTORY = Path(__file__).resolve().parents[1]
TEST_DIRECTORY = Path(__file__).resolve().parent
for directory in (SCENARIO_DIRECTORY, TEST_DIRECTORY):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
