"""Launch the realtime V6 monitor GUI.

The GUI restores position from the V6 simulated-account trade replay and then
continues with live ticks. This wrapper stays minimal so packaging can point at
one stable entry script.
"""
import multiprocessing
from sz002796.gui import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
