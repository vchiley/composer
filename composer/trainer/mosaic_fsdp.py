# Copyright 2022 MosaicML Composer authors
# SPDX-License-Identifier: Apache-2.0

import functools
import warnings
from typing import Any, Callable, Dict, Set, Tuple, cast

import torch
import torch.nn as nn
from torch.distributed import ProcessGroup
from torch.distributed.fsdp import FullyShardedDataParallel, ShardingStrategy
from torch.distributed.fsdp._utils import _contains_batchnorm, _override_batchnorm_mixed_precision
from torch.distributed.fsdp.wrap import _or_policy, _wrap, _wrap_batchnorm_individually

from composer.utils.dist import get_global_rank, get_local_rank, get_local_world_size, get_node_rank, get_world_size

sharding_map = {
    'NO_SHARD': ShardingStrategy.NO_SHARD,
    'SHARD_GRAD_OP': ShardingStrategy.SHARD_GRAD_OP,
    'FULL_SHARD': ShardingStrategy.FULL_SHARD,
}

def _pro_recursive_wrap(module: nn.Module,
                        auto_wrap_policy: Callable,
                        wrapper_cls: Callable,
                        ignored_modules: Set[nn.Module],
                        ignored_params: Set[nn.Parameter],
                        only_wrap_children: bool = False,
                        **kwargs: Any) -> Tuple[nn.Module, int]:
    """
    Automatically wrap child modules of *module* that meet the given
    criteria with :func:`auto_wrap`. Does not rely on _ConfigAutoWrap.

    Args:
        module (nn.Module):
            module to recursively wrap
        auto_wrap_policy (Callable):
            A callable specifying a policy to recursively wrap layers with FSDP.
        ignored_modules (Set[torch.nn.Module]): Modules to ignore when
            wrapping.
        ignored_params (Set[torch.nn.Parameter]): Parameters to ignore when
            wrapping; these should be the parameters contained in the modules
            in ``ignored_modules``.
    Returns:
        (nn.Module, int):
            Wrapped module and the number parameters wrapped recursively.
    """
    assert auto_wrap_policy is not None, 'Must specify auto_wrap_policy.'
    assert wrapper_cls is not None, 'Must specify wrapper_cls'
    # Make sure no child is already wrapped.
    for _, child in module.named_modules():
        if child in ignored_modules:
            continue
        try:
            assert not isinstance(child, cast(type, wrapper_cls))
        except TypeError:
            # wrapper_cls is a function as opposed to a class type, just bypass above check.
            pass

    # We count all params, assuming none of them are already wrapped.
    num_params = sum(p.numel() for p in module.parameters() if p not in ignored_params)

    assert auto_wrap_policy is not None
    if auto_wrap_policy(module=module, recurse=True, unwrapped_params=num_params):
        total_wrapped_params = 0
        # Iterate through the children, recursively wrap if necessary
        for name, child in module.named_children():
            if child in ignored_modules:
                continue
            wrapped_child, num_wrapped_params = _pro_recursive_wrap(
                module=child,
                auto_wrap_policy=auto_wrap_policy,
                wrapper_cls=wrapper_cls,
                ignored_modules=ignored_modules,
                ignored_params=ignored_params,
                **kwargs,
            )
            setattr(module, name, wrapped_child)
            # Keep track of how many parameters have been wrapped
            total_wrapped_params += num_wrapped_params
        # decide if we need to wrap the current module,
        # since the left over parameters exceed the number of params to wrap
        remainder = num_params - total_wrapped_params
        module_kwargs = auto_wrap_policy(module=module, recurse=False, unwrapped_params=remainder)
        if not only_wrap_children and module_kwargs:
            module_kwargs = module_kwargs if isinstance(module_kwargs, dict) else {}
            if 'sharding_strategy' in module_kwargs:
                module_kwargs['sharding_strategy'] = sharding_map[module_kwargs['sharding_strategy'].upper()]
            final_kwargs = {**kwargs, **module_kwargs}

            print(module.__class__, final_kwargs)

            # Leaf node or final wrapping of the remainder both happen here.
            return _wrap(module, wrapper_cls, **final_kwargs), num_params
        else:
            return module, total_wrapped_params
    return module, 0


class MosaicFullyShardedDataParallel(FullyShardedDataParallel):

    def _auto_wrap(
        self,
        auto_wrap_kwargs: Dict[str, Any],
        fsdp_kwargs: Dict[str, Any],
    ) -> None:
        """
        Recursively auto wraps the root module given by the key "module" in
        ``auto_wrap_kwargs`` with the arguments in ``auto_wrap_kwargs`` and
        ``fsdp_kwargs``.
        Precondition: ``auto_wrap_policy`` contains the arguments expected by
        ``_recursive_wrap()``, where ``auto_wrap_policy`` is not ``None``.
        ``fsdp_kwargs`` contains all FSDP arguments except ``module``.
        """
        auto_wrap_policy = auto_wrap_kwargs['auto_wrap_policy']
        root_module = auto_wrap_kwargs['module']
        assert auto_wrap_policy is not None
        # For auto wrapping, submodules should not already be wrapped with FSDP
        # since double wrapping is not supported
        for module_name, module in root_module.named_modules():
            if isinstance(module, FullyShardedDataParallel):
                raise ValueError(f'Expected {module_name} to NOT be FullyShardedDataParallel '
                                 'if using an `auto_wrap_policy`')
        mixed_precision = fsdp_kwargs['mixed_precision']
        if mixed_precision is not None and _contains_batchnorm(root_module):
            _override_batchnorm_mixed_precision(root_module)
            auto_wrap_policy = functools.partial(_or_policy, policies=[_wrap_batchnorm_individually, auto_wrap_policy])
            warnings.warn('Both mixed precision and an `auto_wrap_policy` were specified '
                          'for FSDP, where the wrapped module has batch norm submodules. '
                          'The batch norm submodules will be wrapped as separate FSDP '
                          'instances with mixed precision disabled since some batch norm '
                          'kernels do not support low precision.')
            auto_wrap_kwargs['auto_wrap_policy'] = auto_wrap_policy
        _pro_recursive_wrap(**auto_wrap_kwargs, **fsdp_kwargs)
