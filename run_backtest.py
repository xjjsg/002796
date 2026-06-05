"""Run the default V6 historical backtest.

The implementation lives in sz002796.backtest; this wrapper keeps command-line
usage stable after the refactor.
"""
from sz002796.backtest import main

if __name__ == "__main__":
    main()
