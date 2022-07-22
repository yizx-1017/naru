"""Evaluate estimators (Naru or others) on queries."""
import argparse
import collections
import csv
import glob
import itertools
import json
import logging
import os
import pickle
import re
import time
import ast
from datetime import datetime

import numpy as np
import pandas as pd
import torch

import common
import datasets
import estimators as estimators_lib
import made
import transformer

# For inference speed.
torch.backends.cudnn.deterministic = False
torch.backends.cudnn.benchmark = True

DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'
print('Device', DEVICE)

logging.basicConfig(filename='test.log', filemode='a', format='%(asctime)s - %(levelname)s - %(message)s',
                    level=logging.INFO)

parser = argparse.ArgumentParser()

parser.add_argument('--query', type=bool, default=False, help='specific query')
parser.add_argument('--groupby_col', type=str, help='columns for group by')
parser.add_argument('--agg_col', type=str, help='columns for aggregate result')
parser.add_argument('--where_col', type=str, help='format example: [col1, col2, col3]')
parser.add_argument('--where_ops', type=str, help='format example: [[<],[=],[>,<]]')
parser.add_argument('--where_val', type=str, help='format example: [[10], [2], [1, 1000]]')

parser.add_argument('--inference-opts',
                    action='store_true',
                    help='Tracing optimization for better latency.')

parser.add_argument('--num_queries', type=int, default=20, help='# queries.')
parser.add_argument('--dataset', type=str, default='dmv-tiny', help='Dataset.')
parser.add_argument('--col', nargs='+', help='Column names in the dataset that you want to use.')
parser.add_argument('--err-csv',
                    type=str,
                    default='results.csv',
                    help='Save result csv to what path?')
parser.add_argument('--glob',
                    type=str,
                    help='Checkpoints to glob under models/.')
parser.add_argument('--blacklist',
                    type=str,
                    help='Remove some globbed checkpoint files.')
parser.add_argument('--psample',
                    type=int,
                    default=2000,
                    help='# of progressive samples to use per query.')
parser.add_argument(
    '--column_masking',
    action='store_true',
    help='Turn on wildcard skipping.  Requires checkpoints be trained with ' \
         'column masking.')
parser.add_argument('--order',
                    nargs='+',
                    type=int,
                    help='Use a specific order?')

# MADE.
parser.add_argument('--fc-hiddens',
                    type=int,
                    default=128,
                    help='Hidden units in FC.')
parser.add_argument('--layers', type=int, default=4, help='# layers in FC.')
parser.add_argument('--residual', action='store_true', help='ResMade?')
parser.add_argument('--direct-io', action='store_true', help='Do direct IO?')
parser.add_argument(
    '--inv_order',
    action='store_true',
    help='Set this flag iff using MADE and specifying --order. Flag --order' \
         'lists natural indices, e.g., [0 2 1] means variable 2 appears second.' \
         'MADE, however, is implemented to take in an argument the inverse ' \
         'semantics (element i indicates the position of variable i).  Transformer' \
         ' does not have this issue and thus should not have this flag on.')
parser.add_argument(
    '--input-encoding',
    type=str,
    default='binary',
    help='Input encoding for MADE/ResMADE, {binary, one_hot, embed}.')
parser.add_argument(
    '--output-encoding',
    type=str,
    default='one_hot',
    help='Iutput encoding for MADE/ResMADE, {one_hot, embed}.  If embed, '
         'then input encoding should be set to embed as well.')

# Transformer.
parser.add_argument(
    '--heads',
    type=int,
    default=0,
    help='Transformer: num heads.  A non-zero value turns on Transformer' \
         ' (otherwise MADE/ResMADE).'
)
parser.add_argument('--blocks',
                    type=int,
                    default=2,
                    help='Transformer: num blocks.')
parser.add_argument('--dmodel',
                    type=int,
                    default=32,
                    help='Transformer: d_model.')
parser.add_argument('--dff', type=int, default=128, help='Transformer: d_ff.')
parser.add_argument('--transformer-act',
                    type=str,
                    default='gelu',
                    help='Transformer activation.')

# Estimators to enable.
parser.add_argument('--run-sampling',
                    action='store_true',
                    help='Run a materialized sampler?')
parser.add_argument('--run-maxdiff',
                    action='store_true',
                    help='Run the MaxDiff histogram?')
parser.add_argument('--run-bn',
                    action='store_true',
                    help='Run Bayes nets? If enabled, run BN only.')

# Bayes nets.
parser.add_argument('--bn-samples',
                    type=int,
                    default=200,
                    help='# samples for each BN inference.')
parser.add_argument('--bn-root',
                    type=int,
                    default=0,
                    help='Root variable index for chow liu tree.')
# Maxdiff
parser.add_argument(
    '--maxdiff-limit',
    type=int,
    default=30000,
    help='Maximum number of partitions of the Maxdiff histogram.')

# Save Result
parser.add_argument(
    '--save_result',
    type=str,
    default='results/1G/query.json',
    help='Turn on to write results in results/'
)

args = parser.parse_args()


def InvertOrder(order):
    if order is None:
        return None
    # 'order'[i] maps nat_i -> position of nat_i
    # Inverse: position -> natural idx.  This it the "true" ordering -- it's how
    # heuristic orders are generated + (less crucially) how Transformer works.
    nin = len(order)
    inv_ordering = [None] * nin
    for natural_idx in range(nin):
        inv_ordering[order[natural_idx]] = natural_idx
    return inv_ordering


def MakeTable():
    # assert args.dataset in ['dmv-tiny', 'dmv']
    if args.dataset == 'dmv-tiny':
        table = datasets.LoadDmv('dmv-tiny.csv')
    elif args.dataset == 'dmv':
        table = datasets.LoadDmv()
    else:
        table = datasets.LoadMyDataset(args.dataset, args.col)
    oracle_est = estimators_lib.Oracle(table)

    real_result = estimators_lib.RealResult(table)
    if args.run_bn:
        return table, common.TableDataset(table), real_result
    return table, None, oracle_est, real_result


def ErrorMetric(est_card, card):
    if card == 0 and est_card != 0:
        return est_card
    if card != 0 and est_card == 0:
        return card
    if card == 0 and est_card == 0:
        return 1.0
    return max(est_card / card, card / est_card)


def SampleTupleThenRandom(all_cols,
                          num_filters,
                          rng,
                          table,
                          return_col_idx=False):
    s = table.data.iloc[rng.randint(0, table.cardinality)]
    vals = s.values

    if args.dataset in ['dmv', 'dmv-tiny']:
        # Giant hack for DMV.
        vals[5] = vals[5].to_datetime64()

    idxs = rng.choice(len(all_cols) - 2, replace=False, size=num_filters)
    cols = np.take(all_cols, idxs)

    # If dom size >= 10, okay to place a range filter.
    # Otherwise, low domain size columns should be queried with equality.
    ops = rng.choice(['<=', '>=', '='], size=num_filters)
    ops_all_eqs = ['='] * num_filters
    sensible_to_do_range = [c.DistributionSize() >= 10 for c in cols]
    ops = np.where(sensible_to_do_range, ops, ops_all_eqs)

    if num_filters == len(all_cols):
        if return_col_idx:
            return np.arange(len(all_cols)), ops, vals
        return all_cols, ops, vals

    vals = vals[idxs]
    if return_col_idx:
        return idxs, ops, vals

    return cols, ops, vals


def GenerateQuery(all_cols, rng, table, return_col_idx=False):
    """Generate a random query."""
    num_filters = rng.randint(5, 10)
    cols, ops, vals = SampleTupleThenRandom(all_cols,
                                            num_filters,
                                            rng,
                                            table,
                                            return_col_idx=return_col_idx)
    return cols, ops, vals


def Query(estimators,
          do_print=True,
          oracle_card=None,
          query=None,
          table=None,
          oracle_est=None):
    assert query is not None
    cols, ops, vals = query

    ### Actually estimate the query.

    def pprint(*args, **kwargs):
        if do_print:
            print(*args, **kwargs)

    # Actual.
    card = oracle_est.Query(cols, ops,
                            vals) if oracle_card is None else oracle_card
    if card == 0:
        return

    pprint('Q(', end='')
    for c, o, v in zip(cols, ops, vals):
        pprint('{} {} {}, '.format(c.name, o, str(v)), end='')
    pprint('): ', end='')

    pprint('\n  actual {} ({:.3f}%) '.format(card,
                                             card / table.cardinality * 100),
           end='')

    for est in estimators:
        est_card = est.Query(cols, ops, vals)
        err = ErrorMetric(est_card, card)
        est.AddError(err, est_card, card)
        pprint('{} {} (err={:.3f}) '.format(str(est), est_card, err), end='')
    pprint()


def ReportEsts(estimators):
    v = -1
    for est in estimators:
        print(est.name, 'max', np.max(est.errs), '99th',
              np.quantile(est.errs, 0.99), '95th', np.quantile(est.errs, 0.95),
              'median', np.quantile(est.errs, 0.5))
        v = max(v, np.max(est.errs))
    return v


def GenerateRandomQuery(table):
    rng = np.random.RandomState()
    ncol = len(args.col)
    key_cols = [table.ColumnIndex('ss_sold_date_sk'), table.ColumnIndex('ss_store_sk')]
    select_cols = [*range(ncol)]
    for col in key_cols:
        select_cols.remove(col)
    agg_col = rng.choice(select_cols, size=1)[0]
    all_cols = [*range(ncol)]
    all_cols.remove(agg_col)
    nselect = len(all_cols)
    if nselect > 2:
        p = [0.5, 0.3]
        p.extend([0.2 / (nselect - 2)] * (nselect - 2))
    elif nselect == 2:
        p = [0.6, 0.4]
    else:
        p = [1]
    num_cols = rng.choice(range(1, nselect+1), size=1, p=p)[0]
    col_idxs = rng.choice(all_cols, replace=False, size=num_cols).tolist()
    col_idxs.sort()
    cols = np.take(table.columns, col_idxs)

    # If dom size >= 10, okay to place a range filter.
    # Otherwise, low domain size columns should be queried with equality.
    ops = rng.choice([['<='], ['>='], ['>=', '<='], ['=']], size=num_cols, p=[0.3, 0.3, 0.3, 0.1])
    ops_all_eqs = ['='] * len(col_idxs)
    sensible_to_do_range = [c.DistributionSize() >= 10 for c in cols]
    ops = np.where(sensible_to_do_range, ops, ops_all_eqs)
    vals = []
    for i, op in enumerate(ops):
        if op == ['>=', '<=']:
            val = rng.choice(table.columns[col_idxs[i]].all_distinct_values, size=2).tolist()
            val.sort()
            vals.append(val)
        else:
            val = rng.choice(table.columns[col_idxs[i]].all_distinct_values, size=1).tolist()
            vals.append(val)

    query = {
        "agg_col": table.columns[agg_col].Name(),
        "where_col": cols,
        "where_ops": ops,
        "where_val": vals
    }
    print(query)
    return query


def RunSingleQuery(est, est_avg, real, agg_col, where_col, where_ops, where_val, groupby_col):
    # Actual.
    real_result = real.Query(agg_col, where_col, where_ops, where_val, groupby_col)

    est_result_avg = est_avg.Query(agg_col, where_col, where_ops, where_val, groupby_col, count=False)
    est_result_count = est.Query(agg_col, where_col, where_ops, where_val, groupby_col, count=True)
    est_result_sum = est_result_count*est_result_avg
    est_result = [est_result_avg, est_result_count, est_result_sum]
    return est_result, real_result


def RunN(table,
         cols,
         estimators,
         rng=None,
         num=20,
         log_every=50,
         num_filters=11,
         oracle_cards=None,
         oracle_est=None):
    if rng is None:
        rng = np.random.RandomState()

    last_time = None
    for i in range(num):
        do_print = False
        if i % log_every == 0:
            if last_time is not None:
                print('{:.1f} queries/sec'.format(log_every /
                                                  (time.time() - last_time)))
            do_print = True
            print('Query {}:'.format(i), end=' ')
            last_time = time.time()
        query = GenerateQuery(cols, rng, table)
        Query(estimators,
              do_print,
              oracle_card=oracle_cards[i]
              if oracle_cards is not None and i < len(oracle_cards) else None,
              query=query,
              table=table,
              oracle_est=oracle_est)

        max_err = ReportEsts(estimators)
    return False


def RunNParallel(estimator_factory,
                 parallelism=2,
                 rng=None,
                 num=20,
                 num_filters=11,
                 oracle_cards=None):
    """RunN in parallel with Ray.  Useful for slow estimators e.g., BN."""
    import ray
    ray.init(redis_password='xxx')

    @ray.remote
    class Worker(object):

        def __init__(self, i):
            self.estimators, self.table, self.oracle_est = estimator_factory()
            self.columns = np.asarray(self.table.columns)
            self.i = i

        def run_query(self, query, j):
            col_idxs, ops, vals = pickle.loads(query)
            Query(self.estimators,
                  do_print=True,
                  oracle_card=oracle_cards[j]
                  if oracle_cards is not None else None,
                  query=(self.columns[col_idxs], ops, vals),
                  table=self.table,
                  oracle_est=self.oracle_est)

            print('=== Worker {}, Query {} ==='.format(self.i, j))
            for est in self.estimators:
                est.report()

        def get_stats(self):
            return [e.get_stats() for e in self.estimators]

    print('Building estimators on {} workers'.format(parallelism))
    workers = []
    for i in range(parallelism):
        workers.append(Worker.remote(i))

    print('Building estimators on driver')
    estimators, table, _ = estimator_factory()
    cols = table.columns

    if rng is None:
        rng = np.random.RandomState(1234)
    queries = []
    for i in range(num):
        col_idxs, ops, vals = GenerateQuery(cols,
                                            rng,
                                            table=table,
                                            return_col_idx=True)
        queries.append((col_idxs, ops, vals))

    cnts = 0
    for i in range(num):
        query = queries[i]
        print('Queueing execution of query', i)
        workers[i % parallelism].run_query.remote(pickle.dumps(query), i)

    print('Waiting for queries to finish')
    stats = ray.get([w.get_stats.remote() for w in workers])

    print('Merging and printing final results')
    for stat_set in stats:
        for e, s in zip(estimators, stat_set):
            e.merge_stats(s)
    time.sleep(1)

    print('=== Merged stats ===')
    for est in estimators:
        est.report()
    return estimators


def MakeBnEstimators():
    table, train_data, real_result = MakeTable()
    estimators = [
        estimators_lib.BayesianNetwork(train_data,
                                       args.bn_samples,
                                       'chow-liu',
                                       topological_sampling_order=True,
                                       root=args.bn_root,
                                       max_parents=2,
                                       use_pgm=False,
                                       discretize=100,
                                       discretize_method='equal_freq')
    ]

    for est in estimators:
        est.name = str(est)
    return estimators, table, real_result


def MakeMade(scale, cols_to_train, seed, fixed_ordering=None, natural_ordering=False):
    if fixed_ordering:
        print('Inverting order!')
        ordering = InvertOrder(fixed_ordering)

    model = made.MADE(
        nin=len(cols_to_train),
        hidden_sizes=[scale] *
                     args.layers if args.layers > 0 else [512, 256, 512, 128, 1024],
        nout=sum([c.DistributionSize() for c in cols_to_train]),
        input_bins=[c.DistributionSize() for c in cols_to_train],
        input_encoding=args.input_encoding,
        output_encoding=args.output_encoding,
        embed_size=32,
        seed=seed,
        do_direct_io_connections=args.direct_io,
        natural_ordering=natural_ordering,
        residual_connections=args.residual,
        fixed_ordering=ordering if fixed_ordering else None,
        column_masking=args.column_masking,
    ).to(DEVICE)

    return model


def MakeTransformer(cols_to_train, fixed_ordering, seed=None):
    return transformer.Transformer(
        num_blocks=args.blocks,
        d_model=args.dmodel,
        d_ff=args.dff,
        num_heads=args.heads,
        nin=len(cols_to_train),
        input_bins=[c.DistributionSize() for c in cols_to_train],
        use_positional_embs=True,
        activation=args.transformer_act,
        fixed_ordering=fixed_ordering,
        column_masking=args.column_masking,
        seed=seed,
    ).to(DEVICE)


def ReportModel(model, blacklist=None):
    ps = []
    for name, p in model.named_parameters():
        if blacklist is None or blacklist not in name:
            ps.append(np.prod(p.size()))
    num_params = sum(ps)
    mb = num_params * 4 / 1024 / 1024
    # print('Number of model parameters: {} (~= {:.1f}MB)'.format(num_params, mb))
    # print(model)
    return mb


def SaveEstimators(path, estimators, return_df=False):
    # name, query_dur_ms, errs, est_cards, true_cards
    results = pd.DataFrame()
    for est in estimators:
        data = {
            'est': [est.name] * len(est.errs),
            'err': est.errs,
            'est_card': est.est_cards,
            'true_card': est.true_cards,
            'query_dur_ms': est.query_dur_ms,
        }
        results = results.append(pd.DataFrame(data))
    if return_df:
        return results
    results.to_csv(path, index=False)


def LoadOracleCardinalities():
    ORACLE_CARD_FILES = {
        'dmv': 'datasets/dmv-2000queries-oracle-cards-seed1234.csv'
    }
    path = ORACLE_CARD_FILES.get(args.dataset, None)
    if path and os.path.exists(path):
        df = pd.read_csv(path)
        assert len(df) == 2000, len(df)
        return df.values.reshape(-1)
    return None


def err(est, real):
    if real == 0:
        return 'result=0!'
    return abs(est - real) / real


def saveResults(est, real, est_result, real_result, query, order, filename):
    if len(est_result) == 3 and est_result[0] is not list:
        result = {
            'timestamp': str(datetime.now()),
            'dataset': args.dataset,
            'model': args.glob,
            'query': query,
            'avg_est': est_result[0],
            'avg_real': real_result[0],
            'avg_err': err(est_result[0], real_result[0]),
            'count_est': est_result[1],
            'count_real': real_result[1],
            'count_err': err(est_result[1], real_result[1]),
            'sum_est': est_result[2],
            'sum_real': real_result[2],
            'sum_err': err(est_result[2], real_result[2]),
            'query_dur_ms_est': est.query_dur_ms[0],
            'query_dur_ms_real': real.query_dur_ms[0],
            'query_dur_ms_err': err(est.query_dur_ms[0], real.query_dur_ms[0]),
            'order': order,
            'groupby': False
        }

    else:
        data = {
            'avg_est': [row[1] for row in est_result],
            'count_est': [row[2] for row in est_result],
            'sum_est': [row[3] for row in est_result],
        }
        est_df = pd.DataFrame(data, index=[row[0] for row in est_result])
        data = {
            'avg_real': [row[1] for row in real_result],
            'count_real': [row[2] for row in real_result],
            'sum_real': [row[3] for row in real_result],
        }
        real_df = pd.DataFrame(data, index=[row[0] for row in real_result])
        results = pd.concat([est_df, real_df], axis=1)
        avg_error = []
        cnt_error = []
        sum_error = []
        for index, row in results.iterrows():
            if not row.isnull().any():
                avg_error.append(abs(row[0] - row[3]) / row[3])
                cnt_error.append(abs(row[1] - row[4]) / row[4])
                sum_error.append(abs(row[2] - row[5]) / row[5])
        print(np.mean(avg_error), np.mean(cnt_error), np.mean(sum_error))
        result = results.to_dict()
        result.update({
            'timestamp': str(datetime.now()),
            'dataset': args.dataset,
            'model': args.glob,
            'query': query,
            'avg_err': np.mean(avg_error),
            'count_err': np.mean(cnt_error),
            'sum_err': np.mean(sum_error),
            'query_dur_ms_est': est.query_dur_ms[0],
            'query_dur_ms_real': real.query_dur_ms[0],
            'query_dur_ms_err': err(est.query_dur_ms[0], real.query_dur_ms[0]),
            'order': order,
            'groupby': True
        })
    json_object = json.dumps(result, indent=4)

    ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
    RESULT_PATH = os.path.join(ROOT_DIR, filename)
    with open(RESULT_PATH, "w") as outfile:
        outfile.write(json_object)


def toQuery(agg_col, where_col, where_ops, where_val, groupby_col=None):
    where_str = ' WHERE '
    for i, col in enumerate(where_col):
        if i != 0:
            where_str += ' AND '
        where_str += where_col[i]
        for j, op in enumerate(where_ops[i]):
            where_str += str(where_ops[i][j]) + str(where_val[i][j])
    query = '\'SELECT ' + agg_col + ' FROM ' + args.dataset + where_str
    if groupby_col is not None:
        query += ' GROUP BY '
        for col in groupby_col:
            query += str(col)
    query += '\''
    return query


def generateOrder(table, agg_col, groupby_col):
    agg_idx = table.ColumnIndex(agg_col)
    ncol = len(table.columns)
    order = [*range(ncol)]
    if groupby_col is not None:
        groupby_idx = [table.ColumnIndex(n) for n in groupby_col]
        order = [i for i in order if i not in groupby_idx]
        order.extend(groupby_idx)
    order.remove(agg_idx)
    order.append(agg_idx)
    return order


def loadEstimators(table, order, natural_ordering):
    all_ckpts = glob.glob('./models/{}'.format(args.glob))
    if args.blacklist:
        all_ckpts = [ckpt for ckpt in all_ckpts if args.blacklist not in ckpt]

    selected_ckpts = all_ckpts
    oracle_cards = LoadOracleCardinalities()
    print('ckpts', selected_ckpts)

    Ckpt = collections.namedtuple(
        'Ckpt', 'epoch model_bits bits_gap path loaded_model seed')
    parsed_ckpts = []

    for s in selected_ckpts:
        z = re.match('.+model([\d\.]+)-data([\d\.]+).+seed([\d\.]+).*.pt',
                     s)
        # if args.order is None:
        #     z = re.match('.+model([\d\.]+)-data([\d\.]+).+seed([\d\.]+).*.pt',
        #                  s)
        # else:
        #     z = re.match(
        #         '.+model([\d\.]+)-data([\d\.]+).+seed([\d\.]+)-order.*.pt', s)
        assert z
        model_bits = float(z.group(1))
        data_bits = float(z.group(2))
        seed = int(z.group(3))
        bits_gap = model_bits - data_bits

        if args.heads > 0:
            model = MakeTransformer(cols_to_train=table.columns,
                                    fixed_ordering=order,
                                    seed=seed)
        else:
            # if args.dataset in ['dmv-tiny', 'dmv']:
            model = MakeMade(
                scale=args.fc_hiddens,
                cols_to_train=table.columns,
                seed=seed,
                fixed_ordering=order if not natural_ordering else None,
                natural_ordering=natural_ordering
            )
            # else:
            #     assert False, args.dataset

        assert order is None or len(order) == model.nin, order
        ReportModel(model)
        print('Loading ckpt:', s)
        model.load_state_dict(torch.load(s))
        model.eval()

        print(s, bits_gap, seed)

        parsed_ckpts.append(
            Ckpt(path=s,
                 epoch=None,
                 model_bits=model_bits,
                 bits_gap=bits_gap,
                 loaded_model=model,
                 seed=seed))

    # Estimators to run.
    if args.run_bn:
        estimators = RunNParallel(estimator_factory=MakeBnEstimators,
                                  parallelism=50,
                                  rng=np.random.RandomState(1234),
                                  num=args.num_queries,
                                  num_filters=None,
                                  oracle_cards=oracle_cards)
    else:
        estimators = [
            estimators_lib.ProgressiveSampling(c.loaded_model,
                                               table,
                                               args.psample,
                                               device=DEVICE,
                                               shortcircuit=args.column_masking)
            for c in parsed_ckpts
        ]
        for est, ckpt in zip(estimators, parsed_ckpts):
            est.name = str(est) + '_{}_{:.3f}'.format(ckpt.seed, ckpt.bits_gap)

        if args.inference_opts:
            print('Tracing forward_with_encoded_input()...')
            for est in estimators:
                encoded_input = est.model.EncodeInput(
                    torch.zeros(args.psample, est.model.nin, device=DEVICE))

                # NOTE: this line works with torch 1.0.1.post2 (but not 1.2).
                # The 1.2 version changes the API to
                # torch.jit.script(est.model) and requires an annotation --
                # which was found to be slower.
                est.traced_fwd = torch.jit.trace(
                    est.model.forward_with_encoded_input, encoded_input)

        if args.run_sampling:
            SAMPLE_RATIO = {'dmv': [0.0013]}  # ~1.3MB.
            for p in SAMPLE_RATIO.get(args.dataset, [0.01]):
                estimators.append(estimators_lib.Sampling(table, p=p))

        if args.run_maxdiff:
            estimators.append(
                estimators_lib.MaxDiffHistogram(table, args.maxdiff_limit))

        # Other estimators can be appended as well.

        # if len(estimators):
        #     RunN(table,
        #          cols_to_train,
        #          estimators,
        #          rng=np.random.RandomState(),
        #          num=args.num_queries,
        #          log_every=1,
        #          num_filters=None,
        #          oracle_cards=oracle_cards,
        #          oracle_est=oracle_est)
    return estimators


def Main():
    if args.query:
        agg_col = args.agg_col
        if args.where_col is not None:
            where_col = ast.literal_eval(args.where_col)
            where_ops = ast.literal_eval(args.where_ops)
            where_val = ast.literal_eval(args.where_val)
        else:
            where_col = where_ops = where_val = None
        if args.groupby_col is not None:
            groupby_col = ast.literal_eval(args.groupby_col)
        else:
            groupby_col = None
        querystr = toQuery(agg_col, where_col, where_ops, where_val, groupby_col)
        where_col = [table.ColumnIndex(i) for i in where_col]
        where_col = [table.columns[i] for i in where_col]
        if not args.run_bn:
            # OK to load tables now
            table, train_data, oracle_est, real = MakeTable()
        if args.order:
            order = args.order
        else:
            order = generateOrder(table, agg_col, groupby_col)
        orders = list(itertools.permutations([0, 1, 2, 3]))
        cnt = 0
        for order in orders:
            order.append(4)
            estimators1 = loadEstimators(table, order, natural_ordering=True)[0]
            estimators2 = loadEstimators(table, order, natural_ordering=False)[0]

            logging.info('query ' + querystr)
            est_result, real_result = RunSingleQuery(estimators1, estimators2, real, agg_col, where_col, where_ops, where_val,
                                                     groupby_col)
            save_result = "results/1G/query" + str(cnt) + '.json'
            saveResults(estimators1, real, est_result, real_result, querystr, order, save_result)
            print('...Done, result:', save_result)
            logging.info('write results in ' + save_result)
            cnt += 1
    else:
        if not args.run_bn:
            # OK to load tables now
            table, train_data, oracle_est, real = MakeTable()
        cnt = 0
        for i in range(args.num_queries):
            query = GenerateRandomQuery(table)
            agg_col = query['agg_col']
            where_col = query['where_col']
            where_ops = query['where_ops']
            where_val = query['where_val']
            groupby_col = [None]
            for g in groupby_col:
                order = generateOrder(table, agg_col, g)
                print(order)
                querystr = toQuery(agg_col, [c.Name() for c in where_col],
                                   where_ops, where_val, g)
                print(querystr)
                estimators1 = loadEstimators(table, order, natural_ordering=True)[0]
                estimators2 = loadEstimators(table, order, natural_ordering=False)[0]
                est_result, real_result = RunSingleQuery(estimators1, estimators2, real, agg_col, where_col,
                                                             where_ops, where_val, g)

                if args.save_result is not None:
                    save_result = "results/1G/query" + str(cnt) + '.json'
                    saveResults(estimators1, real, est_result, real_result, querystr, order, save_result)
                    print('...Done, result:', save_result)
                    logging.info('write results in ' + save_result)
                    cnt += 1

    # SaveEstimators(args.err_csv, estimators)
    # print('...Done, result:', args.err_csv)


if __name__ == '__main__':
    Main()
