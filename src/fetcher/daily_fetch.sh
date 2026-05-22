#!/bin/bash

# daily 1m-kline
python -m src.fetcher.lb_kline_fetcher --mode=day --value day='2026-05-20'

# option 1m-kline
python -m src.fetcher.futu_option_fetcher --start 2026-05-20 --end 2026-05-20
