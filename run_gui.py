"""Compatibility alias for launching the V6 web workbench."""
import multiprocessing
from sz002796.gui import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
