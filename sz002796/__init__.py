"""002796.SZ V6 strategy monitor package.

The public entry points are intentionally small:
- run_backtest.py calls sz002796.backtest.main()
- run_web.py calls sz002796.web_server.create_app()

Most modules are internal building blocks for data loading, factor calculation,
strategy decisions, persistence, and realtime monitoring.
"""
