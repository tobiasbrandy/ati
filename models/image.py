import itertools
import os
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, List, Tuple, Callable, Union, Any, Optional, Dict

from models.draw_cmd import DrawCmd

from .path_utils import get_extension, strip_extension, lower_extension
from repositories import metadata_repo

import numpy as np

from PIL import Image as PImage

COLOR_DEPTH: int = 256
MAX_COLOR: int = COLOR_DEPTH - 1

CIRCLE_IMAGE_NAME: str = 'circle.pgm'
SQUARE_IMAGE_NAME: str = 'square.pgm'
RESERVED_IMAGE_NAMES: Tuple[str, ...] = (CIRCLE_IMAGE_NAME, SQUARE_IMAGE_NAME)

# (hist, bins)
Hist = Tuple[np.ndarray, np.ndarray]

class ImageFormat(Enum):
    PGM     = 'pgm'
    PPM     = 'ppm'
    JPEG    = 'jpeg'
    JPG     = 'jpg'
    PNG     = 'png'
    RAW     = 'raw'

    @classmethod
    def values(cls):
        return list(map(lambda c: c.value, cls))

    @classmethod
    def from_str(cls, fmt):
        if fmt not in ImageFormat.values():
            raise ValueError(f'"{fmt}" is not a supported image format')
        return cls(fmt)

    @classmethod
    def from_extension(cls, ext):
        return cls.from_str((ext[1:] if len(ext) > 0 and ext[0] == '.' else ext).lower())

    def to_extension(self) -> str:
        return '.' + self.value

@dataclass
class ImageChannelTransformation:
    public_results:     Dict[str, Any]
    internal_results:   Dict[str, Any]
    overlay:            List[DrawCmd]

    def __init__(self, public_results: Dict[str, Any], internal_results: Dict[str, Any], overlay: Optional[List[DrawCmd]] = None):
        self.public_results     = public_results
        self.internal_results   = internal_results
        self.overlay            = overlay if overlay is not None else []

    def __str__(self) -> str:
        return ''.join((f'\t\t{k}: {str_round(v)}\n' for k, v in self.public_results.items()))

    def all_results(self) -> Dict[str, Any]:
        return {**self.public_results, **self.internal_results}

@dataclass
class ImageTransformation:
    name:                       str
    major_inputs:               Dict[str, Any]
    minor_inputs:               Dict[str, Any]
    channel_transformations:    List[ImageChannelTransformation]

    def __init__(self, name: str, major_inputs: Dict[str, Any], minor_inputs: Dict[str, Any], channel_transformations: Optional[List[ImageChannelTransformation]] = None):
        self.name                       = name
        self.major_inputs               = major_inputs
        self.minor_inputs               = minor_inputs
        self.channel_transformations    = channel_transformations if channel_transformations is not None else []

    def __str__(self) -> str:
        ret = f'Transformation {self.name}:\n'

        if self.major_inputs:
            ret += '\tMayor inputs:\n' + ''.join((f'\t\t{k}: {str_round(v)}\n' for k, v in self.major_inputs.items())) + '\n'

        channel_tr_len = len(self.channel_transformations)
        if channel_tr_len == 0:
            pass  # No hacemos nada
        elif channel_tr_len == 1:
            ret += '\tResults:\n' + str(self.channel_transformations[0]) + '\n\n'
        elif channel_tr_len == 3:
            ret += ''.join((f'\tChannel {i} Results:\n{channel_tr}\n' for i, channel_tr in enumerate(self.channel_transformations)))
        else:
            ret += 'More than 3 channels (???)'

        return ret

@dataclass
class Image:
    name:               str
    format:             ImageFormat
    data:               np.ndarray
    movie:              Optional[str]
    transformations:    List[ImageTransformation]

    # No le ponemos tipo para que dataclass no lo agarre como field
    RED_CHANNEL     = 0
    GREEN_CHANNEL   = 1
    BLUE_CHANNEL    = 2

    def __init__(self, name: str, fmt: ImageFormat, data: np.ndarray, allow_reserved: bool = False, movie: Optional[str] = None, transformations: Optional[List[ImageTransformation]] = None):
        if not allow_reserved and name in RESERVED_IMAGE_NAMES:
            raise ValueError(f'name cannot be any of this names: {RESERVED_IMAGE_NAMES}')

        self.name               = name
        self.format             = fmt
        self.data               = data
        self.movie              = movie
        self.transformations    = transformations if transformations else []

    def valid_pixel(self, pixel: Tuple[int, int]) -> bool:
        x, y = pixel
        return 0 <= x < self.width and 0 <= y < self.height

    def get_pixel(self, pixel: Tuple[int, int]) -> bool:
        x, y = pixel
        return self.data[y, x]

    def get_channel(self, channel: int) -> np.ndarray:
        return self.data[:, :, channel] if self.channels > 1 else self.data

    def apply_over_channels(self, fn: Callable[[np.ndarray, Any], Union[np.ndarray, Tuple[np.ndarray, ImageChannelTransformation]]], *args, **kwargs) -> Tuple[np.ndarray, List[ImageChannelTransformation]]:
        new_data: np.ndarray
        channels_tr: List[ImageChannelTransformation] = []

        if self.channels == 1:
            fn_ret = fn(self.data, *args, **kwargs)
            if isinstance(fn_ret, tuple):
                new_data = fn_ret[0]
                channels_tr.append(fn_ret[1])
            else:
                new_data = fn_ret
        else:
            new_data = np.empty(self.shape)
            for channel in range(self.channels):
                fn_ret = fn(self.get_channel(channel), *args, **kwargs)
                if isinstance(fn_ret, tuple):
                    new_data[:, :, channel] = fn_ret[0]
                    channels_tr.append(fn_ret[1])
                else:
                    new_data[:, :, channel] = fn_ret

        return new_data, channels_tr

    def combine_over_channels(self, other: 'Image', fn: Callable[[np.ndarray, np.ndarray, Any], Union[np.ndarray, Tuple[np.ndarray, ImageChannelTransformation]]], *args, **kwargs) -> Tuple[np.ndarray, List[ImageChannelTransformation]]:
        if self.channels != other.channels:
            raise ValueError(f'Cannot combine images of different channel count. Img 1 has {self.channels} channels, and img 2 has {other.channels} channels.')

        new_data: np.ndarray
        channels_tr: List[ImageChannelTransformation] = []

        if self.channels == 1:
            fn_ret = fn(self.data, other.data, *args, **kwargs)
            if isinstance(fn_ret, tuple):
                new_data = fn_ret[0]
                channels_tr.append(fn_ret[1])
            else:
                new_data = fn_ret
        else:
            data0: np.ndarrray
            # Primera iteracion para obtener shape
            fn_ret = fn(self.get_channel(0), other.get_channel(0), *args, **kwargs)
            if isinstance(fn_ret, tuple):
                data0 = fn_ret[0]
                channels_tr.append(fn_ret[1])
            else:
                data0 = fn_ret

            new_data = np.empty((*data0.shape, 3))
            new_data[:, :, 0] = data0

            for channel in range(1, self.channels):
                fn_ret = fn(self.get_channel(channel), other.get_channel(channel), *args, **kwargs)
                if isinstance(fn_ret, tuple):
                    new_data[:, :, channel] = fn_ret[0]
                    channels_tr.append(fn_ret[1])
                else:
                    new_data[:, :, channel] = fn_ret

        return new_data, channels_tr

    def get_histograms(self) -> Union[Tuple[Hist], Tuple[Hist, Hist, Hist]]:
        if self.channels == 1:
            return channel_histogram(self.data),
        else:
            return (
                channel_histogram(self.get_channel(Image.RED_CHANNEL)),
                channel_histogram(self.get_channel(Image.GREEN_CHANNEL)),
                channel_histogram(self.get_channel(Image.BLUE_CHANNEL))
            )

    def transform(self, new_name: str, new_data: np.ndarray, transformation: ImageTransformation):
        return Image(new_name, self.format, new_data, movie=self.movie, transformations=self.transformations + [transformation])

    def input(self, input_name: str) -> Any:
        tr = self.last_transformation
        return tr.major_inputs.get(input_name, tr.minor_inputs[input_name])

    def all_inputs(self) -> Dict[str, Any]:
        tr = self.last_transformation
        return {**tr.major_inputs, **tr.minor_inputs}

    @property
    def shape(self) -> Tuple[int]:
        return self.data.shape

    @property
    def height(self) -> int:
        return self.data.shape[0]

    @property
    def width(self) -> int:
        return self.data.shape[1]

    @property
    def type(self) -> int:
        return self.data.dtype

    @property
    def channels(self) -> int:
        shape = self.data.shape
        return 1 if len(shape) == 2 else shape[2]

    def is_multi_channel(self) -> bool:
        return self.channels > 1

    @property
    def movie_frame(self) -> bool:
        return self.movie is not None

    @property
    def last_transformation(self) -> ImageTransformation:
        if not self.transformations:
            raise ValueError('Image has no transformations')
        return self.transformations[-1]

    @property
    def is_transformed(self) -> bool:
        return self.transformations and len(self.transformations) > 0

    @property
    def major_inputs(self) -> Dict[str, Any]:
        return self.last_transformation.major_inputs

    @property
    def minor_inputs(self) -> Dict[str, Any]:
        return self.last_transformation.minor_inputs

    @staticmethod
    def name_from_path(path: str) -> str:
        return lower_extension(path)

def str_round(n: Any) -> str:
    return f'{n:.2f}' if isinstance(n, float) else str(n)

def valid_image_formats() -> Iterable[str]:
    formats = list(map(lambda fmt: fmt.to_extension(), ImageFormat))
    return itertools.chain(formats, map(lambda f: f.upper(), formats))

def _grayscale_to_rgba(data: np.ndarray) -> np.ndarray:
    # Tenemos que repetir el valor por cada canal de color, y agregar 1 por el canal del alpha
    return _color_to_rgba(np.repeat(data.reshape((*data.shape, 1)), 3, axis=2))

def _color_to_rgba(data: np.ndarray) -> np.ndarray:
    # Solamente hace falta agregar el canal del alpha (que siempre es 1)
    return np.insert(data, 3, 255, axis=2).flatten() / 255

def image_to_rgba_array(image: Image) -> np.ndarray:
    normalized_data = normalize(image.data)
    if image.channels == 1:
        return _grayscale_to_rgba(normalized_data)
    elif image.channels == 3:
        return _color_to_rgba(normalized_data)

# height x width x channel
def load_image(path: str, movie: Optional[str] = None) -> Image:
    name = Image.name_from_path(path)
    fmt = ImageFormat.from_extension(get_extension(name))
    data: np.ndarray
    if fmt == ImageFormat.RAW:
        metadata = metadata_repo.get_metadata(name)
        data = np.fromfile(path, dtype=np.uint8)
        data = data.reshape((metadata.height, metadata.width))
    else:
        data = np.asarray(PImage.open(path), dtype=np.uint8) # noqa

    return Image(name, fmt, data.astype(np.float64), movie=movie)

def save_image(image: Image, dir_path: str) -> None:
    normalized_data = normalize(image.data)
    path = os.path.join(dir_path, strip_extension(image.name)) + image.format.to_extension()
    if image.format == ImageFormat.RAW:
        # Write bytes from data
        with open(path, 'wb') as fp:
            for b in normalized_data.flatten():
                fp.write(b)
        # Write metadata
        metadata_repo.persist_image_metadata(image.name, image.width, image.height)
    else:
        PImage.fromarray(normalized_data).save(path)

# Normalizes to uint8 ndarray
def normalize(data: np.ndarray, as_type=np.uint8) -> np.ndarray:
    if data.dtype == np.uint8:
        return data.astype(as_type, copy=False)
    elif np.can_cast(data.dtype, np.uint8, casting='safe'):
        return data.astype(as_type, copy=False)
    else:
        amax = data.max()
        amin = data.min()
        if amax - amin == 0:
            return np.full(data.shape, min(abs(int(data[0, 0])), 255))
        else:
            ret = (data - amin) / (amax - amin) * 255
            return ret.astype(as_type, copy=False)

def channel_histogram(channel: np.ndarray) -> Hist:
    channel = normalize(channel, np.float64)
    hist, bins = np.histogram(channel.flatten(), bins=COLOR_DEPTH, range=(0, COLOR_DEPTH))
    return hist / channel.size, bins

# ***************************** Default Images ******************************** #

CREATED_IMAGE_LEN: int = 200
CIRCLE_RADIUS: int = 100
def create_circle_image() -> Image:
    mask = create_circular_mask(CREATED_IMAGE_LEN, CREATED_IMAGE_LEN, radius=CIRCLE_RADIUS)
    data = np.zeros((CREATED_IMAGE_LEN, CREATED_IMAGE_LEN), dtype=np.float64)
    data[mask] = 255
    return Image(CIRCLE_IMAGE_NAME, ImageFormat.PGM, data, allow_reserved=True)

# https://stackoverflow.com/a/44874588
def create_circular_mask(h, w, center=None, radius=None):
    if center is None:  # use the middle of the image
        center = (int(w/2), int(h/2))
    if radius is None:  # use the smallest distance between the center and image walls
        radius = min(center[0], center[1], w-center[0], h-center[1])

    y, x = np.ogrid[:h, :w]
    dist_from_center = np.sqrt((x - center[0])**2 + (y-center[1])**2)

    mask = dist_from_center <= radius
    return mask


SQUARE_LEN: int = 160
def create_square_image() -> Image:
    diff = (CREATED_IMAGE_LEN - SQUARE_LEN) // 2
    min_square = diff
    max_square = CREATED_IMAGE_LEN - diff
    data = np.zeros((CREATED_IMAGE_LEN, CREATED_IMAGE_LEN), dtype=np.float64)
    data[min_square:max_square, min_square:max_square] = 255

    return Image(SQUARE_IMAGE_NAME, ImageFormat.PGM, data, allow_reserved=True)
