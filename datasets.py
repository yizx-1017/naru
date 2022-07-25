"""Dataset registrations."""
import os

import numpy as np

import common


def LoadDmv(filename='dmv.csv'):
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


def LoadMyDataset(filename, cols):
    # Make sure that this loads data correctly.
    csv_file = './datasets/{}'.format(filename)
    return common.CsvTable('TPCDS', csv_file, cols)
