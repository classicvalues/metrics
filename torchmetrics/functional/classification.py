# Copyright The PyTorch Lightning team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from functools import wraps
from typing import Callable, Optional, Tuple

import torch
from pytorch_lightning.utilities import rank_zero_warn

from torchmetrics.functional.auc import auc
from torchmetrics.utils import class_reduce, get_num_classes, reduce, to_categorical


def stat_scores(
    pred: torch.Tensor,
    target: torch.Tensor,
    class_index: int,
    argmax_dim: int = 1,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    Calculates the number of true positive, false positive, true negative
    and false negative for a specific class

    Args:
        pred: prediction tensor
        target: target tensor
        class_index: class to calculate over
        argmax_dim: if pred is a tensor of probabilities, this indicates the
            axis the argmax transformation will be applied over

    Return:
        True Positive, False Positive, True Negative, False Negative, Support

    Example:

        >>> x = torch.tensor([1, 2, 3])
        >>> y = torch.tensor([0, 2, 3])
        >>> tp, fp, tn, fn, sup = stat_scores(x, y, class_index=1)
        >>> tp, fp, tn, fn, sup
        (tensor(0), tensor(1), tensor(2), tensor(0), tensor(0))

    """
    if pred.ndim == target.ndim + 1:
        pred = to_categorical(pred, argmax_dim=argmax_dim)

    tp = ((pred == class_index) * (target == class_index)).to(torch.long).sum()
    fp = ((pred == class_index) * (target != class_index)).to(torch.long).sum()
    tn = ((pred != class_index) * (target != class_index)).to(torch.long).sum()
    fn = ((pred != class_index) * (target == class_index)).to(torch.long).sum()
    sup = (target == class_index).to(torch.long).sum()

    return tp, fp, tn, fn, sup


def stat_scores_multiple_classes(
    pred: torch.Tensor,
    target: torch.Tensor,
    num_classes: Optional[int] = None,
    argmax_dim: int = 1,
    reduction: str = 'none',
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
    """
    Calculates the number of true positive, false positive, true negative
    and false negative for each class

    .. warning :: Deprecated in favor of :func:`~torchmetrics.functional.stat_scores`

    """

    rank_zero_warn(
        "This `stat_scores_multiple_classes` was deprecated in v1.2.0 in favor of"
        " `from torchmetrics.functional import stat_scores`."
        " It will be removed in v1.4.0", DeprecationWarning
    )
    if pred.ndim == target.ndim + 1:
        pred = to_categorical(pred, argmax_dim=argmax_dim)

    num_classes = get_num_classes(pred=pred, target=target, num_classes=num_classes)

    if pred.dtype != torch.bool:
        pred = pred.clamp_max(max=num_classes)
    if target.dtype != torch.bool:
        target = target.clamp_max(max=num_classes)

    possible_reductions = ('none', 'sum', 'elementwise_mean')
    if reduction not in possible_reductions:
        raise ValueError("reduction type %s not supported" % reduction)

    if reduction == 'none':
        pred = pred.view((-1, )).long()
        target = target.view((-1, )).long()

        tps = torch.zeros((num_classes + 1, ), device=pred.device)
        fps = torch.zeros((num_classes + 1, ), device=pred.device)
        fns = torch.zeros((num_classes + 1, ), device=pred.device)
        sups = torch.zeros((num_classes + 1, ), device=pred.device)

        match_true = (pred == target).float()
        match_false = 1 - match_true

        tps.scatter_add_(0, pred, match_true)
        fps.scatter_add_(0, pred, match_false)
        fns.scatter_add_(0, target, match_false)
        tns = pred.size(0) - (tps + fps + fns)
        sups.scatter_add_(0, target, torch.ones_like(match_true))

        tps = tps[:num_classes]
        fps = fps[:num_classes]
        tns = tns[:num_classes]
        fns = fns[:num_classes]
        sups = sups[:num_classes]

    elif reduction == 'sum' or reduction == 'elementwise_mean':
        count_match_true = (pred == target).sum().float()
        oob_tp, oob_fp, oob_tn, oob_fn, oob_sup = stat_scores(pred, target, num_classes, argmax_dim)

        tps = count_match_true - oob_tp
        fps = pred.nelement() - count_match_true - oob_fp
        fns = pred.nelement() - count_match_true - oob_fn
        tns = pred.nelement() * (num_classes + 1) - (tps + fps + fns + oob_tn)
        sups = pred.nelement() - oob_sup.float()

        if reduction == 'elementwise_mean':
            tps /= num_classes
            fps /= num_classes
            fns /= num_classes
            tns /= num_classes
            sups /= num_classes

    return tps.float(), fps.float(), tns.float(), fns.float(), sups.float()


def _confmat_normalize(cm):
    """ Normalization function for confusion matrix """
    cm = cm / cm.sum(-1, keepdim=True)
    nan_elements = cm[torch.isnan(cm)].nelement()
    if nan_elements != 0:
        cm[torch.isnan(cm)] = 0
        rank_zero_warn(f'{nan_elements} nan values found in confusion matrix have been replaced with zeros.')
    return cm


def precision_recall(
    pred: torch.Tensor,
    target: torch.Tensor,
    num_classes: Optional[int] = None,
    class_reduction: str = 'micro',
    return_support: bool = False,
    return_state: bool = False
) -> Tuple[torch.Tensor, torch.Tensor]:
    """
    Computes precision and recall for different thresholds

    .. warning :: Deprecated in favor of
     :func:`~torchmetrics.functional.precision_recall`.
     Will be removed in v1.4.0.

    Args:
        pred: estimated probabilities
        target: ground-truth labels
        num_classes: number of classes
        class_reduction: method to reduce metric score over labels

            - ``'micro'``: calculate metrics globally (default)
            - ``'macro'``: calculate metrics for each label, and find their unweighted mean.
            - ``'weighted'``: calculate metrics for each label, and find their weighted mean.
            - ``'none'``: returns calculated metric per class

        return_support: returns the support for each class, need for fbeta/f1 calculations
        return_state: returns a internal state that can be ddp reduced
            before doing the final calculation

    Return:
        Tensor with precision and recall

    Example:

        >>> x = torch.tensor([0, 1, 2, 3])
        >>> y = torch.tensor([0, 2, 2, 2])
        >>> precision_recall(x, y, class_reduction='macro')
        (tensor(0.5000), tensor(0.3333))

    """
    rank_zero_warn(
        "This `precision_recall` was deprecated in v1.2.0 in favor of"
        " `from torchmetrcs.functional import precision_recall`."
        " It will be removed in v1.4.0", DeprecationWarning
    )

    tps, fps, tns, fns, sups = stat_scores_multiple_classes(pred=pred, target=target, num_classes=num_classes)

    precision = class_reduce(tps, tps + fps, sups, class_reduction=class_reduction)
    recall = class_reduce(tps, tps + fns, sups, class_reduction=class_reduction)
    if return_state:
        return {'tps': tps, 'fps': fps, 'fns': fns, 'sups': sups}
    if return_support:
        return precision, recall, sups
    return precision, recall


def precision(
    pred: torch.Tensor,
    target: torch.Tensor,
    num_classes: Optional[int] = None,
    class_reduction: str = 'micro',
) -> torch.Tensor:
    """
    Computes precision score.

    .. warning :: Deprecated in favor of
     :func:`~torchmetrics.functional.recall`. Will be removed in v1.4.0.

    Args:
        pred: estimated probabilities
        target: ground-truth labels
        num_classes: number of classes
        class_reduction: method to reduce metric score over labels

            - ``'micro'``: calculate metrics globally (default)
            - ``'macro'``: calculate metrics for each label, and find their unweighted mean.
            - ``'weighted'``: calculate metrics for each label, and find their weighted mean.
            - ``'none'``: returns calculated metric per class

    Return:
        Tensor with precision.

    Example:

        >>> x = torch.tensor([0, 1, 2, 3])
        >>> y = torch.tensor([0, 1, 2, 2])
        >>> precision(x, y)
        tensor(0.7500)

    """
    rank_zero_warn(
        "This `precision` was deprecated in v1.2.0 in favor of"
        " `from torchmetrics.functional import precision`."
        " It will be removed in v1.4.0", DeprecationWarning
    )

    return precision_recall(pred=pred, target=target, num_classes=num_classes, class_reduction=class_reduction)[0]


def recall(
    pred: torch.Tensor,
    target: torch.Tensor,
    num_classes: Optional[int] = None,
    class_reduction: str = 'micro',
) -> torch.Tensor:
    """
    Computes recall score.

    .. warning :: Deprecated in favor of
     :func:`~torchmetrics.functional.recall`. Will be removed in v1.4.0.

    Args:
        pred: estimated probabilities
        target: ground-truth labels
        num_classes: number of classes
        class_reduction: method to reduce metric score over labels

            - ``'micro'``: calculate metrics globally (default)
            - ``'macro'``: calculate metrics for each label, and find their unweighted mean.
            - ``'weighted'``: calculate metrics for each label, and find their weighted mean.
            - ``'none'``: returns calculated metric per class

    Return:
        Tensor with recall.

    Example:

        >>> x = torch.tensor([0, 1, 2, 3])
        >>> y = torch.tensor([0, 1, 2, 2])
        >>> recall(x, y)
        tensor(0.7500)
    """
    rank_zero_warn(
        "This `recall` was deprecated in v1.2.0 in favor of"
        " `from torchmetrics.functional import recall`."
        " It will be removed in v1.4.0", DeprecationWarning
    )

    return precision_recall(pred=pred, target=target, num_classes=num_classes, class_reduction=class_reduction)[1]


def auc_decorator() -> Callable:
    rank_zero_warn("This `auc_decorator` was deprecated in v1.2.0." " It will be removed in v1.4.0", DeprecationWarning)

    def wrapper(func_to_decorate: Callable) -> Callable:

        @wraps(func_to_decorate)
        def new_func(*args, **kwargs) -> torch.Tensor:
            x, y = func_to_decorate(*args, **kwargs)[:2]

            return auc(x, y)

        return new_func

    return wrapper


def multiclass_auc_decorator() -> Callable:
    rank_zero_warn(
        "This `multiclass_auc_decorator` was deprecated in v1.2.0."
        " It will be removed in v1.4.0", DeprecationWarning
    )

    def wrapper(func_to_decorate: Callable) -> Callable:

        @wraps(func_to_decorate)
        def new_func(*args, **kwargs) -> torch.Tensor:
            results = []
            for class_result in func_to_decorate(*args, **kwargs):
                x, y = class_result[:2]
                results.append(auc(x, y))

            return torch.stack(results)

        return new_func

    return wrapper


def dice_score(
    pred: torch.Tensor,
    target: torch.Tensor,
    bg: bool = False,
    nan_score: float = 0.0,
    no_fg_score: float = 0.0,
    reduction: str = 'elementwise_mean',
) -> torch.Tensor:
    """
    Compute dice score from prediction scores

    Args:
        pred: estimated probabilities
        target: ground-truth labels
        bg: whether to also compute dice for the background
        nan_score: score to return, if a NaN occurs during computation
        no_fg_score: score to return, if no foreground pixel was found in target
        reduction: a method to reduce metric score over labels.

            - ``'elementwise_mean'``: takes the mean (default)
            - ``'sum'``: takes the sum
            - ``'none'``: no reduction will be applied

    Return:
        Tensor containing dice score

    Example:

        >>> pred = torch.tensor([[0.85, 0.05, 0.05, 0.05],
        ...                      [0.05, 0.85, 0.05, 0.05],
        ...                      [0.05, 0.05, 0.85, 0.05],
        ...                      [0.05, 0.05, 0.05, 0.85]])
        >>> target = torch.tensor([0, 1, 3, 2])
        >>> dice_score(pred, target)
        tensor(0.3333)

    """
    num_classes = pred.shape[1]
    bg = (1 - int(bool(bg)))
    scores = torch.zeros(num_classes - bg, device=pred.device, dtype=torch.float32)
    for i in range(bg, num_classes):
        if not (target == i).any():
            # no foreground class
            scores[i - bg] += no_fg_score
            continue

        tp, fp, tn, fn, sup = stat_scores(pred=pred, target=target, class_index=i)
        denom = (2 * tp + fp + fn).to(torch.float)
        # nan result
        score_cls = (2 * tp).to(torch.float) / denom if torch.is_nonzero(denom) else nan_score

        scores[i - bg] += score_cls
    return reduce(scores, reduction=reduction)