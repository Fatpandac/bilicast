# -*- coding: utf-8 -*-
from src.main import sum


def test_sum():
    assert sum(0, 0) == 0
    assert sum(1, 16) == 17
