#!/bin/bash

CURRENT_DATE=$(date -v-1d +%Y-%m-%d)
#CURRENT_DATE=$(date -d "yesterday" +%Y-%m-%d)

# daily 1m-kline
python -m src.fetcher.lb_kline_fetcher --mode=day --value day=${CURRENT_DATE}

# option 1m-kline
python -m src.fetcher.futu_option_fetcher --start ${CURRENT_DATE} --end ${CURRENT_DATE}
