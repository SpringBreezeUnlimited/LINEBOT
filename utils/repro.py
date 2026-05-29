"""再現性のためのユーティリティ

使い方:
```
from utils.repro import set_seed
set_seed(1234)
```
"""
from __future__ import annotations

import os
import random
from typing import Optional

import numpy as np


def set_seed(seed: Optional[int]) -> None:
    """全体の乱数シードを固定するユーティリティ。

    Args:
        seed: 任意の整数。None の場合は何もしない。
    """
    if seed is None:
        return
    os.environ["PYTHONHASHSEED"] = str(seed)
    random.seed(seed)
    np.random.seed(seed)
    try:
        import torch

        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False
    except Exception:
        # PyTorch が無くても問題ない
        pass
