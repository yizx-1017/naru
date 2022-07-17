#!/bin/bash
rm -f models/*
declare -a DATASET="store_sales_1G.csv"
python3 train_model.py \
--dataset=$DATASET \
--col 'ss_sold_date_sk' 'ss_store_sk' 'ss_sales_price' 'ss_quantity' \
--epochs=10 --warmups=8000 --bs=2048 \
--residual --layers=5 --fc-hiddens=256 --direct-io \
--order 0 1 2 3 --inv_order

mkdir -p results/1G
python eval_model.py \
--glob=$DATASET* \
--dataset=$DATASET \
--col 'ss_sold_date_sk' 'ss_store_sk' 'ss_sales_price' 'ss_quantity' \
--query=True \
--agg_col='ss_sales_price' \
--where_col="['ss_sold_date_sk']" \
--where_ops="[['>=', '<=']]" \
--where_val="[[2451119, 2451483]]" \
--residual --layers=5 --fc-hiddens=256 --direct-io \
--order 0 3 1 2 --inv_order \
--save_result='results/1G/query.json'