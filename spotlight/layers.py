"""
Embedding layers useful for recommender models.
"""

import numpy as np
from sklearn.utils import murmurhash3_32
import torch
import torch.nn as nn

from torch.autograd import Variable


PRIMES = [
    179424941, 179425457, 179425907, 179426369,
    179424977, 179425517, 179425943, 179426407,
    179424989, 179425529, 179425993, 179426447,
    179425003, 179425537, 179426003, 179426453,
    179425019, 179425559, 179426029, 179426491,
    179425027, 179425579, 179426081, 179426549
]


class ScaledEmbedding(nn.Embedding):
    """
    Embedding layer that initialises its values
    to using a normal variable scaled by the inverse
    of the emedding dimension.
    """

    def reset_parameters(self):
        """
        Initialize parameters.
        """

        self.weight.data.normal_(0, 1.0 / self.embedding_dim)
        if self.padding_idx is not None:
            self.weight.data[self.padding_idx].fill_(0)


class ZeroEmbedding(nn.Embedding):
    """
    Embedding layer that initialises its values
    to using a normal variable scaled by the inverse
    of the emedding dimension.

    Used for biases.
    """

    def reset_parameters(self):
        """
        Initialize parameters.
        """

        self.weight.data.zero_()
        if self.padding_idx is not None:
            self.weight.data[self.padding_idx].fill_(0)


class BloomEmbedding(nn.Module):
    """
    An embedding layer that compresses the number of embedding
    parameters required by using bloom filter-like hashing.

    Parameters
    ----------

    num_embeddings: int
        Number of entities to be represented.
    embedding_dim: int
        Latent dimension of the embedding.
    bag: boolean, optional
        Whether to use the EmbeddingBag layer.
        Faster, but not available for sequence problems.
    compression_ratio: float, optional
        The underlying number of rows in the embedding layer
        after compression. Numbers below 1.0 will use more
        and more compression, reducing the number of parameters
        in the layer.
    num_hash_functions: int, optional
        Number of hash functions used to compute the bloom filter indices.

    Notes
    -----

    Large embedding layers are a performance problem for fitting models:
    even though the gradients are sparse (only a handful of user and item
    vectors need parameter updates in every minibatch), PyTorch updates
    the entire embedding layer at every backward pass. Computation time
    is then wasted on applying zero gradient steps to whole embedding matrix.

    To alleviate this problem, we can use a smaller underlying embedding layer,
    and probabilistically hash users and items into that smaller space. With
    good hash functions, collisions should be rare, and we should observe
    fitting speedups without a decrease in accuracy.

    The idea follows the RecSys 2017 "Getting recommenders fit"[1]_
    paper. The authors use a bloom-filter-like approach to hashing. Their approach
    uses one-hot encoded inputs followed by fully connected layers as
    well as softmax layers for the output, and their hashing reduces the
    size of the fully connected layers rather than embedding layers as
    implemented here; mathematically, however, the two formulations are
    identical.

    The hash function used is simple multiplicative hashing with a
    different prime for every hash function, modulo the size of the
    compressed embedding layer.

    References
    ----------

    .. [1] Serra, Joan, and Alexandros Karatzoglou.
       "Getting deep recommenders fit: Bloom embeddings
       for sparse binary input/output networks."
       arXiv preprint arXiv:1706.03993 (2017).
    """

    def __init__(self, num_embeddings, embedding_dim,
                 compression_ratio=0.2,
                 num_hash_functions=2,
                 padding_idx=0):

        super(BloomEmbedding, self).__init__()

        self.num_embeddings = num_embeddings
        self.embedding_dim = embedding_dim
        self.compression_ratio = compression_ratio
        self.compressed_num_embeddings = int(compression_ratio *
                                             num_embeddings)
        self.num_hash_functions = num_hash_functions
        self.padding_idx = padding_idx

        if num_hash_functions > len(PRIMES):
            raise ValueError('Can use at most {} hash functions ({} requested)'
                             .format(len(PRIMES), num_hash_functions))

        self._masks = PRIMES[:self.num_hash_functions]

        self.embeddings = ScaledEmbedding(self.compressed_num_embeddings,
                                          self.embedding_dim,
                                          padding_idx=padding_idx)

        # Hash cache. We pre-hash all the indices, and then just
        # map the indices to their pre-hashed values as we go
        # through the minibatches.
        self._hashes = None

    def __repr__(self):

        return ('<BloomEmbedding (compression_ratio: {}): {}>'
                .format(self.compression_ratio,
                        repr(self.embeddings)))

    def _get_hashed_indices(self, original_indices):

        def _hash(x, seed):

            # TODO: integrate with padding index
            result = murmurhash3_32(indices, seed=seed)
            result[self.padding_idx] = 0

            return result % self.compressed_num_embeddings

        if self._hashes is None:
            indices = np.arange(self.num_embeddings, dtype=np.int32)
            hashes = np.stack([_hash(indices, seed)
                               for seed in self._masks],
                              axis=1).astype(np.int64)
            assert hashes[self.padding_idx].sum() == 0

            self._hashes = torch.from_numpy(hashes)

            if original_indices.is_cuda:
                self._hashes = self._hashes.cuda()

        return torch.index_select(self._hashes, 0, original_indices.squeeze())

    def forward(self, indices):
        """
        Retrieve embeddings corresponding to indices.

        See documentation on PyTorch ``nn.Embedding`` for details.
        """

        if indices.dim() == 2:
            batch_size, seq_size = indices.size()
        else:
            batch_size, seq_size = indices.size(0), 1

        if not indices.is_contiguous():
            indices = indices.contiguous()
        indices = indices.data.view(batch_size * seq_size, 1)

        indices = self._get_hashed_indices(indices)

        masked_indices = indices.remainder(self.compressed_num_embeddings)
        masked_indices = Variable(masked_indices)

        embedding = self.embeddings(masked_indices)
        embedding = embedding.sum(1)
        embedding = embedding.view(batch_size, seq_size, -1)

        return embedding
