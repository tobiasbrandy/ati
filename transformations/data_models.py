from dataclasses import dataclass
from typing import List, Tuple, Union

import numpy as np


@dataclass
class LinRange:
    start:  float
    end:    float
    count:  int

    def __init__(self, start: float, end: float, count: int) -> None:
        if count < 1:
            raise ValueError('Count must be at least 1')

        self.start  = start
        self.end    = end
        self.count  = count

    def to_linspace(self) -> np.ndarray:
        return np.linspace(self.start, self.end, self.count)


@dataclass
class Measurement:
    magnitude:  Union[int, float]
    unit:       str

    def __str__(self):
        return f'{self.magnitude} {self.unit}'

@dataclass
class ActiveOutlineMetrics:
    section_number = int
    lout = List[Tuple[int, int]]
    lin  = List[Tuple[int, int]]
    sigma = Union[float, np.ndarray]
    lout_color = Tuple[int, int, int]
    lin_color = Tuple[int, int, int]

    def __init__(self, section_number: int, lout: List[Tuple[int, int]], lin: List[Tuple[int, int]], sigma: Union[float, np.ndarray], lout_color: Tuple[int, int, int], lin_color: Tuple[int, int, int]):
        self.section_number     = section_number
        self.lout     = lout
        self.lin   = lin
        self.sigma   = sigma
        self.lout_color   = lout_color
        self.lin_color   = lin_color
