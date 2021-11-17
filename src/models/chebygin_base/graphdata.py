import numpy as np
import os
from os.path import join as pjoin
import pickle
import copy
import torch
import torch.utils
import torch.utils.data
import torch.nn.functional as F
import torchvision
from scipy.spatial.distance import cdist
from .utils import *


def compute_adjacency_matrix_images(coord, sigma=0.1):
    coord = coord.reshape(-1, 2)
    dist = cdist(coord, coord)
    A = np.exp(- dist / (sigma * np.pi) ** 2)
    A[np.diag_indices_from(A)] = 0
    return A


def precompute_graph_images(img_size):
    col, row = np.meshgrid(np.arange(img_size), np.arange(img_size))
    coord = np.stack((col, row), axis=2) / img_size  # 28,28,2
    A = torch.from_numpy(compute_adjacency_matrix_images(coord)).float().unsqueeze(0)
    coord = torch.from_numpy(coord).float().unsqueeze(0).view(1, -1, 2)
    mask = torch.ones(1, img_size * img_size, dtype=torch.uint8)
    return A, coord, mask


def collate_batch_images(batch, A, mask, use_mean_px=True, coord=None,
                         gt_attn_threshold=0, replicate_features=True):
    B = len(batch)
    C, H, W = batch[0][0].shape
    N_nodes = H * W
    params_dict = {'N_nodes': torch.zeros(B, dtype=torch.long) + N_nodes, 'node_attn_eval': None}
    has_WS_attn = len(batch[0]) > 2
    if has_WS_attn:
        WS_attn = torch.from_numpy(np.stack([batch[b][2].reshape(N_nodes) for b in range(B)]).astype(np.float32)).view(B, N_nodes)
        WS_attn = normalize_batch(WS_attn)
        params_dict.update({'node_attn': WS_attn})  # use these scores for training

    if use_mean_px:
        x = torch.stack([batch[b][0].view(C, N_nodes).t() for b in range(B)]).float()
        if gt_attn_threshold == 0:
            GT_attn = (x > 0).view(B, N_nodes).float()
        else:
            GT_attn = x.view(B, N_nodes).float().clone()
            GT_attn[GT_attn < gt_attn_threshold] = 0
        GT_attn = normalize_batch(GT_attn)

        params_dict.update({'node_attn_eval': GT_attn})  # use this for evaluation of attention
        if not has_WS_attn:
            params_dict.update({'node_attn': GT_attn})  # use this to train attention
    else:
        raise NotImplementedError('this case is not well supported')

    if coord is not None:
        if use_mean_px:
            x = torch.cat((x, coord.expand(B, -1, -1)), dim=2)
        else:
            x = coord.expand(B, -1, -1)
    if x is None:
        x = torch.ones(B, N_nodes, 1)  # dummy features

    if replicate_features:
        x = F.pad(x, (2, 0), 'replicate')

    try:
        labels = torch.Tensor([batch[b][1] for b in range(B)]).long()
    except:
        labels = torch.stack([batch[b][1] for b in range(B)]).long()

    return [x, A.expand(B, -1, -1), mask.expand(B, -1), labels, params_dict]


def collate_batch(batch):
    '''
    Creates a batch of same size graphs by zero-padding node features and adjacency matrices up to
    the maximum number of nodes in the current batch rather than in the entire dataset.
    '''

    B = len(batch)
    N_nodes = [batch[b][2] for b in range(B)]
    C = batch[0][0].shape[1]
    N_nodes_max = int(np.max(N_nodes))

    mask = torch.zeros(B, N_nodes_max, dtype=torch.bool)  # use byte for older PyTorch
    A = torch.zeros(B, N_nodes_max, N_nodes_max)
    x = torch.zeros(B, N_nodes_max, C)
    has_GT_attn = len(batch[0]) > 4 and batch[0][4] is not None
    if has_GT_attn:
        GT_attn = torch.zeros(B, N_nodes_max)
    has_WS_attn = len(batch[0]) > 5 and batch[0][5] is not None
    if has_WS_attn:
        WS_attn = torch.zeros(B, N_nodes_max)

    for b in range(B):
        x[b, :N_nodes[b]] = batch[b][0]
        A[b, :N_nodes[b], :N_nodes[b]] = batch[b][1]
        mask[b][:N_nodes[b]] = 1  # mask with values of 0 for dummy (zero padded) nodes, otherwise 1
        if has_GT_attn:
            GT_attn[b, :N_nodes[b]] = batch[b][4].squeeze()
        if has_WS_attn:
            WS_attn[b, :N_nodes[b]] = batch[b][5].squeeze()

    N_nodes = torch.from_numpy(np.array(N_nodes)).long()

    params_dict = {'N_nodes': N_nodes}
    if has_WS_attn:
        params_dict.update({'node_attn': WS_attn})  # use this to train attention
    if has_GT_attn:
        params_dict.update({'node_attn_eval': GT_attn})  # use this for evaluation of attention
        if not has_WS_attn:
            params_dict.update({'node_attn': GT_attn})  # use this to train attention
    elif has_WS_attn:
        params_dict.update({'node_attn_eval': WS_attn})  # use this for evaluation of attention

    labels = torch.from_numpy(np.array([batch[b][3] for b in range(B)])).long()
    return [x, A, mask, labels, params_dict]


class MNIST(torchvision.datasets.MNIST):
    '''
    Wrapper around MNIST to use predefined attention coefficients
    '''
    def __init__(self, root, train=True, transform=None, target_transform=None, download=False, attn_coef=None):
        super(MNIST, self).__init__(root, train, transform, target_transform, download)
        self.alpha_WS = None
        if attn_coef is not None and train:
            print('loading weakly-supervised labels from %s' % attn_coef)
            with open(attn_coef, 'rb') as f:
                self.alpha_WS = pickle.load(f)
            print(train, len(self.alpha_WS))

    def __getitem__(self, index):
        img, target = super(MNIST, self).__getitem__(index)
        if self.alpha_WS is None:
            return img, target
        else:
            return img, target, self.alpha_WS[index]


class MNIST75sp(torch.utils.data.Dataset):
    def __init__(self,
                 data_dir,
                 split,
                 use_mean_px=True,
                 use_coord=True,
                 gt_attn_threshold=0,
                 attn_coef=None):

        self.data_dir = data_dir
        self.split = split
        self.is_test = split.lower() in ['test', 'val']
        with open(pjoin(data_dir, 'mnist_75sp_%s.pkl' % split), 'rb') as f:
            self.labels, self.sp_data = pickle.load(f)

        self.use_mean_px = use_mean_px
        self.use_coord = use_coord
        self.n_samples = len(self.labels)
        self.img_size = 28
        self.gt_attn_threshold = gt_attn_threshold

        self.alpha_WS = None
        if attn_coef is not None and not self.is_test:
            with open(attn_coef, 'rb') as f:
                self.alpha_WS = pickle.load(f)
            print('using weakly-supervised labels from %s (%d samples)' % (attn_coef, len(self.alpha_WS)))

    def train_val_split(self, samples_idx):
        self.sp_data = [self.sp_data[i] for i in samples_idx]
        self.labels = self.labels[samples_idx]
        self.n_samples = len(self.labels)

    def precompute_graph_data(self, replicate_features, threads=0):
        print('precompute all data for the %s set...' % self.split.upper())
        self.Adj_matrices, self.node_features, self.GT_attn, self.WS_attn = [], [], [], []
        for index, sample in enumerate(self.sp_data):
            mean_px, coord = sample[:2]
            coord = coord / self.img_size
            A = compute_adjacency_matrix_images(coord)
            N_nodes = A.shape[0]
            x = None
            if self.use_mean_px:
                x = mean_px.reshape(N_nodes, -1)
            if self.use_coord:
                coord = coord.reshape(N_nodes, 2)
                if self.use_mean_px:
                    x = np.concatenate((x, coord), axis=1)
                else:
                    x = coord
            if x is None:
                x = np.ones(N_nodes, 1)  # dummy features
            if replicate_features:
                x = np.pad(x, ((0, 0), (2, 0)), 'edge')  # replicate features to make it possible to test on colored images
            if self.gt_attn_threshold == 0:
                gt_attn = (mean_px > 0).astype(np.float32)
            else:
                gt_attn = mean_px.copy()
                gt_attn[gt_attn < self.gt_attn_threshold] = 0
            self.GT_attn.append(normalize(gt_attn))

            if self.alpha_WS is not None:
                self.WS_attn.append(normalize(self.alpha_WS[index]))

            self.node_features.append(x)
            self.Adj_matrices.append(A)


    def __len__(self):
        return self.n_samples

    def __getitem__(self, index):
        data = [self.node_features[index],
                self.Adj_matrices[index],
                self.Adj_matrices[index].shape[0],
                self.labels[index],
                self.GT_attn[index]]

        if self.alpha_WS is not None:
            data.append(self.WS_attn[index])

        data = list_to_torch(data)  # convert to torch

        return data


class SyntheticGraphs(torch.utils.data.Dataset):
    def __init__(self,
                 data_dir,
                 dataset,
                 split,
                 degree_feature=True,
                 attn_coef=None,
                 threads=12):

        self.is_test = split.lower() in ['test', 'val']
        self.split = split
        self.degree_feature = degree_feature

        if dataset.find('colors') >= 0:
            dim = int(dataset.split('-')[1])
            data_file = 'random_graphs_colors_dim%d_%s.pkl' % (dim, split)
            is_triangles = False
            self.feature_dim = dim + 1
        if dataset.find('triangles') >= 0:
            data_file = 'random_graphs_triangles_%s.pkl' % split
            is_triangles = True
        else:
            NotImplementedError(dataset)

        with open(pjoin(data_dir, data_file), 'rb') as f:
            data = pickle.load(f)

        for key in data:
            if not isinstance(data[key], list) and not isinstance(data[key], np.ndarray):
                print(split, key, data[key])
            else:
                print(split, key, len(data[key]))

        self.Node_degrees = [np.sum(A, 1).astype(np.int32) for A in data['Adj_matrices']]

        if is_triangles:
            # use one-hot degree features as node features
            self.feature_dim = data['Max_degree'] + 1
            self.node_features = []
            for i in range(len(data['Adj_matrices'])):
                N = data['Adj_matrices'][i].shape[0]
                if degree_feature:
                    D_onehot = np.zeros((N, self.feature_dim ))
                    D_onehot[np.arange(N), self.Node_degrees[i]] = 1
                else:
                    D_onehot = np.zeros((N, 1))
                self.node_features.append(D_onehot)
            if not degree_feature:
                self.feature_dim = 1
        else:
            # Add 1 feature to support new colors at test time
            self.node_features = []
            for i in range(len(data['node_features'])):
                features = data['node_features'][i]
                if features.shape[1] < self.feature_dim:
                    features = np.pad(features, ((0, 0), (0, 1)), 'constant')
                self.node_features.append(features)

        self.alpha_WS = None
        if attn_coef is not None and not self.is_test:
            with open(attn_coef, 'rb') as f:
                self.alpha_WS = pickle.load(f)
            print('using weakly-supervised labels from %s (%d samples)' % (attn_coef, len(self.alpha_WS)))
            self.WS_attn = []
            for index in range(len(self.alpha_WS)):
                self.WS_attn.append(normalize(self.alpha_WS[index]))

        N_nodes = np.array([A.shape[0] for A in data['Adj_matrices']])
        self.Adj_matrices = data['Adj_matrices']
        self.GT_attn = data['GT_attn']
        # Normalizing ground truth attention so that it sums to 1
        for i in range(len(self.GT_attn)):
            self.GT_attn[i] = normalize(self.GT_attn[i])
            #assert np.sum(self.GT_attn[i]) == 1, (i, np.sum(self.GT_attn[i]), self.GT_attn[i])
        self.labels = data['graph_labels'].astype(np.int32)
        self.classes = np.unique(self.labels)
        self.n_classes = len(self.classes)
        R = np.corrcoef(self.labels, N_nodes)[0, 1]

        degrees = []
        for i in range(len(self.Node_degrees)):
            degrees.extend(list(self.Node_degrees[i]))
        degrees = np.array(degrees, np.int32)

        print('N nodes avg/std/min/max: \t{:.2f}/{:.2f}/{:d}/{:d}'.format(*stats(N_nodes)))
        print('N edges avg/std/min/max: \t{:.2f}/{:.2f}/{:d}/{:d}'.format(*stats(data['N_edges'])))
        print('Node degree avg/std/min/max: \t{:.2f}/{:.2f}/{:d}/{:d}'.format(*stats(degrees)))
        print('Node features dim: \t\t%d' % self.feature_dim)
        print('N classes: \t\t\t%d' % self.n_classes)
        print('Correlation of labels with graph size: \t%.2f' % R)
        print('Classes: \t\t\t%s' % str(self.classes))
        for lbl in self.classes:
            idx = self.labels == lbl
            print('Class {}: \t\t\t{} samples, N_nodes: avg/std/min/max: \t{:.2f}/{:.2f}/{:d}/{:d}'.format(lbl, np.sum(idx), *stats(N_nodes[idx])))

    def __len__(self):
        return len(self.Adj_matrices)

    def __getitem__(self, index):
        data = [self.node_features[index],
                self.Adj_matrices[index],
                self.Adj_matrices[index].shape[0],
                self.labels[index],
                self.GT_attn[index]]

        if self.alpha_WS is not None:
            data.append(self.WS_attn[index])

        data = list_to_torch(data)  # convert to torch

        return data


class GraphData(torch.utils.data.Dataset):
    def __init__(self,
                 datareader,
                 fold_id,
                 split,  # train, val, train_val, test
                 degree_feature=True,
                 attn_labels=None):
        self.fold_id = fold_id
        self.split = split
        self.w_sup_signal_attn = None
        print('''The degree_feature argument is ignored for this dataset. 
        It will automatically be set to True if nodes do not have any features. Otherwise it will be set to False''')
        if attn_labels is not None:
            if isinstance(attn_labels, str) and os.path.isfile(attn_labels):
                with open(attn_labels, 'rb') as f:
                    self.w_sup_signal_attn = pickle.load(f)
            else:
                self.w_sup_signal_attn = attn_labels
            for i in range(len(self.w_sup_signal_attn)):
                alpha = self.w_sup_signal_attn[i]
                alpha[alpha < 1e-3] = 0  # assuming that some nodes should have zero importance
                self.w_sup_signal_attn[i] = normalize(alpha)
            print(('!!!using weakly supervised labels (%d samples)!!!' % len(self.w_sup_signal_attn)).upper())

        self.set_fold(datareader.data, fold_id)

    def set_fold(self, data, fold_id):

        self.total = len(data['targets'])
        self.N_nodes_max = data['N_nodes_max']
        self.num_classes = data['num_classes']
        self.num_features = data['num_features']
        if self.split in ['train', 'val']:
            self.idx = data['splits'][self.split][fold_id]
        else:
            assert self.split in ['train_val', 'test'], ('unexpected split', self.split)
            self.idx = data['splits'][self.split]

        # use deepcopy to make sure we don't alter objects in folds
        self.labels = np.array(copy.deepcopy([data['targets'][i] for i in self.idx]))
        self.adj_list = copy.deepcopy([data['adj_list'][i] for i in self.idx])
        self.features_onehot = copy.deepcopy([data['features_onehot'][i] for i in self.idx])
        self.N_edges = np.array([A.sum() // 2 for A in self.adj_list])  # assume undirected graph with binary edges
        print('%s: %d/%d' % (self.split.upper(), len(self.labels), len(data['targets'])))
        classes = np.unique(self.labels)
        for lbl in classes:
            print('Class %d: \t\t\t%d samples' % (lbl, np.sum(self.labels == lbl)))

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, index):
        if isinstance(index, str):
            # To make data format consistent with SyntheticGraphs
            if index == 'Adj_matrices':
                return self.adj_list
            elif index == 'GT_attn':
                print('Ground truth attention is unavailable for this dataset: weakly-supervised labels will be returned')
                return self.w_sup_signal_attn
            elif index == 'graph_labels':
                return self.labels
            elif index == 'node_features':
                return self.features_onehot
            elif index == 'N_edges':
                return self.N_edges
            else:
                raise KeyError(index)
        else:
            data = [self.features_onehot[index],
                    self.adj_list[index],
                    self.adj_list[index].shape[0],
                    self.labels[index],
                    None]  # no GT attention
            if self.w_sup_signal_attn is not None:
                data.append(self.w_sup_signal_attn[index])
            data = list_to_torch(data)  # convert to torch

            return data


class DataReader():
    '''
    Class to read the txt files containing all data of the dataset
    Should work for any dataset from https://ls11-www.cs.tu-dortmund.de/staff/morris/graphkerneldatasets
    '''

    def __init__(self,
                 data_dir,  # folder with txt files
                 N_nodes=None,  # maximum number of nodes in the training set
                 rnd_state=None,
                 use_cont_node_attr=False, # use or not additional float valued node attributes available in some datasets
                 folds=10,
                 fold_id=None):

        self.data_dir = data_dir
        self.rnd_state = np.random.RandomState() if rnd_state is None else rnd_state
        self.use_cont_node_attr = use_cont_node_attr
        self.N_nodes = N_nodes
        if os.path.isfile('%s/data.pkl' % data_dir):
            print('loading data from %s/data.pkl' % data_dir)
            with open('%s/data.pkl' % data_dir, 'rb') as f:
                data = pickle.load(f)
        else:
            files = os.listdir(self.data_dir)
            data = {}
            nodes, graphs = self.read_graph_nodes_relations(
                list(filter(lambda f: f.find('graph_indicator') >= 0, files))[0])
            lst = list(filter(lambda f: f.find('node_labels') >= 0, files))
            if len(lst) > 0:
                data['features'] = self.read_node_features(lst[0], nodes, graphs, fn=lambda s: int(s.strip()))
            else:
                data['features'] = None
            data['adj_list'] = self.read_graph_adj(list(filter(lambda f: f.find('_A') >= 0, files))[0], nodes, graphs)
            data['targets'] = np.array(
                self.parse_txt_file(list(filter(lambda f: f.find('graph_labels') >= 0, files))[0],
                                    line_parse_fn=lambda s: int(float(s.strip()))))

            if self.use_cont_node_attr:
                data['attr'] = self.read_node_features(list(filter(lambda f: f.find('node_attributes') >= 0, files))[0],
                                                       nodes, graphs,
                                                       fn=lambda s: np.array(list(map(float, s.strip().split(',')))))

            features, n_edges, degrees = [], [], []
            for sample_id, adj in enumerate(data['adj_list']):
                N = len(adj)  # number of nodes
                if data['features'] is not None:
                    assert N == len(data['features'][sample_id]), (N, len(data['features'][sample_id]))
                n = np.sum(adj)  # total sum of edges
                # assert n % 2 == 0, n
                n_edges.append(int(n / 2))  # undirected edges, so need to divide by 2
                if not np.allclose(adj, adj.T):
                    print(sample_id, 'not symmetric')
                degrees.extend(list(np.sum(adj, 1)))
                if data['features'] is not None:
                    features.append(np.array(data['features'][sample_id]))

            # Create features over graphs as one-hot vectors for each node
            if data['features'] is not None:
                features_all = np.concatenate(features)
                features_min = features_all.min()
                num_features = int(features_all.max() - features_min + 1)  # number of possible values

                features_onehot = []
                for i, x in enumerate(features):
                    feature_onehot = np.zeros((len(x), num_features))
                    for node, value in enumerate(x):
                        feature_onehot[node, value - features_min] = 1
                    if self.use_cont_node_attr:
                        feature_onehot = np.concatenate((feature_onehot, np.array(data['attr'][i])), axis=1)
                    features_onehot.append(feature_onehot)

                if self.use_cont_node_attr:
                    num_features = features_onehot[0].shape[1]
            else:
                degree_max = int(np.max([np.sum(A, 1).max() for A in data['adj_list']]))
                num_features = degree_max + 1
                features_onehot = []
                for A in data['adj_list']:
                    n = A.shape[0]
                    D = np.sum(A, 1).astype(np.int)
                    D_onehot = np.zeros((n, num_features))
                    D_onehot[np.arange(n), D] = 1
                    features_onehot.append(D_onehot)

            shapes = [len(adj) for adj in data['adj_list']]
            labels = data['targets']  # graph class labels
            labels -= np.min(labels)  # to start from 0

            classes = np.unique(labels)
            num_classes = len(classes)

            if not np.all(np.diff(classes) == 1):
                print('making labels sequential, otherwise pytorch might crash')
                labels_new = np.zeros(labels.shape, dtype=labels.dtype) - 1
                for lbl in range(num_classes):
                    labels_new[labels == classes[lbl]] = lbl
                labels = labels_new
                classes = np.unique(labels)
                assert len(np.unique(labels)) == num_classes, np.unique(labels)

            def stats(x):
                return (np.mean(x), np.std(x), np.min(x), np.max(x))

            print('N nodes avg/std/min/max: \t%.2f/%.2f/%d/%d' % stats(shapes))
            print('N edges avg/std/min/max: \t%.2f/%.2f/%d/%d' % stats(n_edges))
            print('Node degree avg/std/min/max: \t%.2f/%.2f/%d/%d' % stats(degrees))
            print('Node features dim: \t\t%d' % num_features)
            print('N classes: \t\t\t%d' % num_classes)
            print('Classes: \t\t\t%s' % str(classes))
            for lbl in classes:
                print('Class %d: \t\t\t%d samples' % (lbl, np.sum(labels == lbl)))

            if data['features'] is not None:
                for u in np.unique(features_all):
                    print('feature {}, count {}/{}'.format(u, np.count_nonzero(features_all == u), len(features_all)))

            N_graphs = len(labels)  # number of samples (graphs) in data
            assert N_graphs == len(data['adj_list']) == len(features_onehot), 'invalid data'

            data['features_onehot'] = features_onehot
            data['targets'] = labels

            data['N_nodes_max'] = np.max(shapes)  # max number of nodes
            data['num_features'] = num_features
            data['num_classes'] = num_classes

            # Save preprocessed data for faster loading
            with open('%s/data.pkl' % data_dir, 'wb') as f:
                pickle.dump(data, f, protocol=2)

        labels = data['targets']
        # Create test sets first
        N_graphs = len(labels)
        shapes = np.array([len(adj) for adj in data['adj_list']])
        train_ids, val_ids, train_val_ids, test_ids = self.split_ids_shape(np.arange(N_graphs), shapes, N_nodes, folds=folds)

        # Create train sets
        splits = {'train': [], 'val': [], 'train_val': train_val_ids, 'test': test_ids}
        for fold in range(folds):
            splits['train'].append(train_ids[fold])
            splits['val'].append(val_ids[fold])

        data['splits'] = splits

        self.data = data


    def split_ids_shape(self, ids_all, shapes, N_nodes, folds=1, fold_id=0):
        if N_nodes > 0:
            small_graphs_ind = np.where(shapes <= N_nodes)[0]
            print('{}/{} graphs with at least {} nodes'.format(len(small_graphs_ind), len(shapes), N_nodes))
            idx = self.rnd_state.permutation(len(small_graphs_ind))
            if len(idx) > 1000:
                n = 1000
            else:
                n = 500
            train_val_ids = small_graphs_ind[idx[:n]]
            test_ids = small_graphs_ind[idx[n:]]
            large_graphs_ind = np.where(shapes > N_nodes)[0]
            test_ids = np.concatenate((test_ids, large_graphs_ind))
        else:
            idx = self.rnd_state.permutation(len(ids_all))
            n = len(ids_all) // folds  # number of test samples
            test_ids = ids_all[idx[fold_id * n: (fold_id + 1) * n if fold_id < folds - 1 else -1]]
            train_val_ids = []
            for i in ids_all:
                if i not in test_ids:
                    train_val_ids.append(i)
            train_val_ids = np.array(train_val_ids)

        assert np.all(
            np.unique(np.concatenate((train_val_ids, test_ids))) == sorted(ids_all)), 'some graphs are missing in the test sets'
        if folds > 0:
            print('generating %d-fold cross-validation splits' % folds)
            train_ids, val_ids = self.split_ids(train_val_ids, folds=folds)
            # Sanity checks
            for fold in range(folds):
                ind = np.concatenate((train_ids[fold], val_ids[fold]))
                print(fold, len(train_ids[fold]), len(val_ids[fold]))
                assert len(train_ids[fold]) + len(val_ids[fold]) == len(np.unique(ind)) == len(ind) == len(train_val_ids), 'invalid splits'
        else:
            train_ids, val_ids = [], []

        return train_ids, val_ids, train_val_ids, test_ids

    def split_ids(self, ids, folds=10):
        n = len(ids)
        stride = int(np.ceil(n / float(folds)))
        test_ids = [ids[i: i + stride] for i in range(0, n, stride)]
        assert np.all(
            np.unique(np.concatenate(test_ids)) == sorted(ids)), 'some graphs are missing in the test sets'
        assert len(test_ids) == folds, 'invalid test sets'
        train_ids = []
        for fold in range(folds):
            train_ids.append(np.array([e for e in ids if e not in test_ids[fold]]))
            assert len(train_ids[fold]) + len(test_ids[fold]) == len(
                np.unique(list(train_ids[fold]) + list(test_ids[fold]))) == n, 'invalid splits'

        return train_ids, test_ids

    def parse_txt_file(self, fpath, line_parse_fn=None):
        with open(pjoin(self.data_dir, fpath), 'r') as f:
            lines = f.readlines()
        data = [line_parse_fn(s) if line_parse_fn is not None else s for s in lines]
        return data

    def read_graph_adj(self, fpath, nodes, graphs):
        edges = self.parse_txt_file(fpath, line_parse_fn=lambda s: s.split(','))
        adj_dict = {}
        for edge in edges:
            node1 = int(edge[0].strip()) - 1  # -1 because of zero-indexing in our code
            node2 = int(edge[1].strip()) - 1
            graph_id = nodes[node1]
            assert graph_id == nodes[node2], ('invalid data', graph_id, nodes[node2])
            if graph_id not in adj_dict:
                n = len(graphs[graph_id])
                adj_dict[graph_id] = np.zeros((n, n))
            ind1 = np.where(graphs[graph_id] == node1)[0]
            ind2 = np.where(graphs[graph_id] == node2)[0]
            assert len(ind1) == len(ind2) == 1, (ind1, ind2)
            adj_dict[graph_id][ind1, ind2] = 1

        adj_list = [adj_dict[graph_id] for graph_id in sorted(list(graphs.keys()))]

        return adj_list

    def read_graph_nodes_relations(self, fpath):
        graph_ids = self.parse_txt_file(fpath, line_parse_fn=lambda s: int(s.rstrip()))
        nodes, graphs = {}, {}
        for node_id, graph_id in enumerate(graph_ids):
            if graph_id not in graphs:
                graphs[graph_id] = []
            graphs[graph_id].append(node_id)
            nodes[node_id] = graph_id
        graph_ids = np.unique(list(graphs.keys()))
        for graph_id in graph_ids:
            graphs[graph_id] = np.array(graphs[graph_id])
        return nodes, graphs

    def read_node_features(self, fpath, nodes, graphs, fn):
        node_features_all = self.parse_txt_file(fpath, line_parse_fn=fn)
        node_features = {}
        for node_id, x in enumerate(node_features_all):
            graph_id = nodes[node_id]
            if graph_id not in node_features:
                node_features[graph_id] = [None] * len(graphs[graph_id])
            ind = np.where(graphs[graph_id] == node_id)[0]
            assert len(ind) == 1, ind
            assert node_features[graph_id][ind[0]] is None, node_features[graph_id][ind[0]]
            node_features[graph_id][ind[0]] = x
        node_features_lst = [node_features[graph_id] for graph_id in sorted(list(graphs.keys()))]
        return node_features_lst
