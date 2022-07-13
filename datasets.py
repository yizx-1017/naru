"""Dataset registrations."""
import os

import numpy as np

import common


def LoadDmv(filename='Vehicle__Snowmobile__and_Boat_Registrations.csv'):
    csv_file = './datasets/{}'.format(filename)
    cols = [
        'Record Type', 'State', 'County', 'Body Type',
        'Fuel Type', 'Reg Valid Date', 'Scofflaw Indicator',
        'Suspension Indicator', 'Revocation Indicator', 'Registration Class', 'Model Year'
    ]
    # Note: other columns are converted to objects/strings automatically.  We
    # don't need to specify a type-cast for those because the desired order
    # there is the same as the default str-ordering (lexicographical).
    type_casts = {'Reg Valid Date': np.datetime64}
    return common.CsvTable('DMV', csv_file, cols, type_casts)


def LoadMyDataset(filename='ss.csv'):
    # Make sure that this loads data correctly.
    csv_file = './datasets/{}'.format(filename)
    cols = [
        'ss_sold_date_sk', 'ss_sold_time_sk', 'ss_item_sk', 'ss_customer_sk', 'ss_cdemo_sk', 'ss_hdemo_sk', 'ss_addr_sk',
        'ss_store_sk', 'ss_promo_sk', 'ss_ticket_number', 'ss_quantity', 'ss_wholesale_cost', 'ss_list_price',
        'ss_sales_price', 'ss_ext_discount_amt', 'ss_ext_sales_price', 'ss_ext_wholesale_cost', 'ss_ext_list_price',
        'ss_ext_tax', 'ss_coupon_amt', 'ss_net_paid', 'ss_net_paid_inc_tax', 'ss_net_profit'
    ]
    return common.CsvTable('TPCDS', csv_file, cols)
