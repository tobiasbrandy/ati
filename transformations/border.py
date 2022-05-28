import functools
import time
from enum import Enum
from typing import List, Optional, Tuple, Union

import numpy as np
from models.draw_cmd import CircleDrawCmd, LineDrawCmd, ScatterDrawCmd
from models.image import MAX_COLOR, Image, ImageChannelTransformation, normalize

from transformations.np_utils import gauss_kernel, index_matrix
from .input.lin_range import LinRange
from .sliding import PaddingStrategy, sliding_window, weighted_sum

RHO_RESOLUTION = 125
THETA_RESOLUTION = 91

class Direction(Enum):
    VERTICAL = [
        [ 0,  1,  0],
        [ 0,  1,  0],
        [ 0,  1,  0]
    ]
    NEGATIVE_DIAGONAL = [
        [ 1,  0,  0],
        [ 0,  1,  0],
        [ 0,  0,  1]
    ]
    HORIZONTAL = [
        [ 0,  0,  0],
        [ 1,  1,  1],
        [ 0,  0,  0]
    ]
    POSITIVE_DIAGONAL = [
        [ 0,  0,  1],
        [ 0,  1,  0],
        [ 1,  0,  0]
    ]

    @classmethod
    def names(cls):
        return list(map(lambda c: c.name, cls))

    @classmethod
    def from_str(cls, direction: str) -> 'Direction':
        direction_name = direction.upper()
        if direction_name not in Direction.names():
            raise ValueError(f'"{direction_name.title()}" is not a supported direction')
        return cls[direction_name]

    @classmethod
    def from_angle(cls, angle: int) -> 'Direction':
        if angle == 0:
            return cls.HORIZONTAL
        elif angle == 45:
            return cls.POSITIVE_DIAGONAL
        elif angle == 90:
            return cls.VERTICAL
        elif angle == 135:
            return cls.NEGATIVE_DIAGONAL
        else:
            raise ValueError(f'{angle} is not a valid direction angle')

    @property
    def kernel(self) -> np.ndarray:
        return np.array(self.value)

    # https://stackoverflow.com/a/41506120/12270520
    @staticmethod
    def _outer_slice(x):
        return np.r_[x[0], x[1:-1, -1], x[-1, :0:-1], x[-1:0:-1, 0]]

    @staticmethod
    def _rotate_matrix(x, shift):
        out = np.empty_like(x)
        N = x.shape[0]
        idx = np.arange(x.size).reshape(x.shape)
        for n in range((N + 1) // 2):
            sliced_idx = Direction._outer_slice(idx[n:N - n, n:N - n])
            out.ravel()[sliced_idx] = np.roll(np.take(x, sliced_idx), shift)
        return out

    def align_vertical_kernel(self, kernel: np.ndarray):
        return self._rotate_matrix(kernel, self.value)

class FamousKernel(Enum):
    # x derivative kernel
    PREWITT = [
        [-1,  0,  1],
        [-1,  0,  1],
        [-1,  0,  1]
    ]
    SOBEL = [
        [-1,  0,  1],
        [-2,  0,  2],
        [-1,  0,  1]
    ]
    LAPLACE = [
        [ 0, -1,  0],
        [-1,  4, -1],
        [ 0, -1,  0]
    ]
    JULIANA = [
        [ 1,  1, -1],
        [ 1, -2, -1],
        [ 1,  1, -1]
    ]
    SUSAN = [
        [0, 0, 1, 1, 1, 0, 0],
        [0, 1, 1, 1, 1, 1, 0],
        [1, 1, 1, 1, 1, 1, 1],
        [1, 1, 1, 1, 1, 1, 1],
        [1, 1, 1, 1, 1, 1, 1],
        [0, 1, 1, 1, 1, 1, 0],
        [0, 0, 1, 1, 1, 0, 0]
    ]

    @property
    def kernel(self) -> np.ndarray:
        return np.array(self.value)

class HarrisR(Enum):
    R1  = functools.partial(lambda ix2, ixy, iy2, k: calculate_r1(ix2, ixy, iy2, k))
    R2  = functools.partial(lambda ix2, ixy, iy2, k: calculate_r2(ix2, ixy, iy2, k))

    def __call__(self, *args, **kwargs) -> np.ndarray:
        return self.value(*args, **kwargs)

    @classmethod
    def names(cls):
        return list(map(lambda c: c.name, cls))

    @classmethod
    def from_str(cls, strategy: str) -> 'HarrisR':
        strategy_name = strategy.upper()
        if strategy_name not in HarrisR.names():
            raise ValueError(f'"{strategy_name.title()}" is not a supported Harris function')
        return cls[strategy_name]

def directional_channel(channel: np.ndarray, vertical_kernel: np.ndarray, border_dir: Direction, padding_str: PaddingStrategy) -> np.ndarray:
    kernel = border_dir.align_vertical_kernel(vertical_kernel)
    return weighted_sum(channel, kernel, padding_str)

def high_pass_channel(channel: np.ndarray, kernel_size: int, padding_str: PaddingStrategy) -> np.ndarray:
    kernel = np.full((kernel_size, kernel_size), -1 / kernel_size)
    kernel[kernel_size // 2, kernel_size // 2] = (kernel_size ** 2 - 1) / kernel_size
    return weighted_sum(channel, kernel, padding_str)

def gradient_modulus(img: np.ndarray, kernel: np.ndarray, padding_str: PaddingStrategy) -> np.ndarray:
    x_channel = weighted_sum(img, kernel, padding_str)
    kernel = np.rot90(kernel, k=-1)
    y_channel = weighted_sum(img, kernel, padding_str)
    return np.sqrt(y_channel ** 2 + x_channel ** 2)

def prewitt_channel(channel: np.ndarray, padding_str: PaddingStrategy) -> np.ndarray:
    return gradient_modulus(channel, FamousKernel.PREWITT.kernel, padding_str)

def sobel_channel(channel: np.ndarray, padding_str: PaddingStrategy) -> np.ndarray:
    return gradient_modulus(channel, FamousKernel.SOBEL.kernel, padding_str)

def zero_crossing_vertical(data: np.ndarray, threshold: int) -> np.ndarray:
    ans = np.empty(data.shape, dtype=np.bool8)
    # Cambios de signo directos
    ans[:-1] = (data[:-1] * data[1:] < 0) & (np.abs(data[:-1] - data[1:]) > threshold)
    # Cambios con un 0 en el medio
    ans[:-2] |= (data[:-2] * data[2:] < 0) & (data[1:-1] == 0) & (np.abs(data[:-2] - data[2:]) > threshold)
    # Ultimo nunca cruza
    ans[-1] = False

    return ans

def zero_crossing_horizontal(data: np.ndarray, threshold: int) -> np.ndarray:
    ans = np.empty(data.shape, dtype=np.bool8)
    # Cambios de signo directos
    ans[:, :-1] = (data[:, :-1] * data[:, 1:] < 0) & (np.abs(data[:, :-1] - data[:, 1:]) > threshold)
    # Cambios con un 0 en el medio
    ans[:, :-2] |= (data[:, :-2] * data[:, 2:] < 0) & (data[:, 1:-1] == 0) & (np.abs(data[:, :-2] - data[:, 2:]) > threshold)
    # Ultimo nunca cruza
    ans[:, -1] = False

    return ans

def zero_crossing_borders(data: np.ndarray, threshold: int) -> np.ndarray:
    mask = zero_crossing_vertical(data, threshold) | zero_crossing_horizontal(data, threshold)
    ret = np.zeros(data.shape)
    ret[mask] = MAX_COLOR
    return ret

def laplacian_channel(channel: np.ndarray, crossing_threshold: int, padding_str: PaddingStrategy) -> np.ndarray:
    # Derivada segunda
    channel = weighted_sum(channel, FamousKernel.LAPLACE.kernel, padding_str)

    # Queremos ver donde se hace 0, pues son los minimos/maximos de la derivada => borde
    return zero_crossing_borders(channel, crossing_threshold)

def log_kernel(sigma: float) -> np.ndarray:
    kernel_size = int(sigma * 10 + 1)
    indices = np.array(list(np.ndindex((kernel_size, kernel_size)))) - kernel_size//2 # noqa
    indices = np.reshape(indices, (kernel_size, kernel_size, 2))
    sum_squared_over_sigma = np.sum(indices**2, axis=2) / sigma**2  # (x^2 + y^2) / sigma^2
    k = (np.sqrt(2 * np.pi) * sigma**3)                             # sqrt(2pi) * sigma^3
    return - ((2 - sum_squared_over_sigma) / k) * np.exp(-sum_squared_over_sigma/2)

def log_channel(channel: np.ndarray, sigma: float, crossing_threshold: int, padding_str: PaddingStrategy) -> np.ndarray:
    # Derivada segunda con gauss
    channel = weighted_sum(channel, log_kernel(sigma), padding_str)

    # Queremos ver donde se hace 0, pues son los minimos/maximos de la derivada => borde
    return zero_crossing_borders(channel, crossing_threshold)

def susan_channel(channel: np.ndarray, padding_str: PaddingStrategy) -> np.ndarray:
    kernel = FamousKernel.SUSAN.kernel
    sw = sliding_window(channel, np.shape(kernel), padding_str)

    # Expando dimensiones para que sea compatible con el tamaño de la sliding window
    new_channel = np.expand_dims(channel, axis=(2,3))
    absolute_values = np.absolute(sw[:,:]*kernel - new_channel[:,:])
    absolute_values[absolute_values < 15] = 1
    absolute_values[absolute_values >= 15] = 0

    values = 1 - np.sum(absolute_values, axis=(2, 3)) / kernel.size
    values[(values < 0.4) | (values >= 0.85)] = 0
    values[(values >= 0.4) & (values < 0.65)] = 63
    values[(values >= 0.65) & (values < 0.85)] = 255

    return values

def hough_lines_channel(channel: np.ndarray, theta: np.ndarray, rho: np.ndarray, threshold: float, most_fitted_ratio: float) -> Tuple[np.ndarray, ImageChannelTransformation]:
    indices = np.insert(index_matrix(*channel.shape), 0, -1, axis=2)
    acum = np.empty((rho.size, theta.size))
    white_points = channel > 0

    for i in range(len(rho)):
        for j in range(len(theta)):
            params = np.hstack((rho[i], np.sin(theta[j]),  np.cos(theta[j])))
            # |rho - y*sin(theta) - x*cos(theta)|
            line = np.abs(np.sum(params * indices, axis=2)) < threshold
            acum[i, j] = np.sum(white_points & line)

    most_fitted_lines = np.argwhere(acum > most_fitted_ratio * acum.max())
    Y = np.transpose(most_fitted_lines)[0]
    X = np.transpose(most_fitted_lines)[1]

    best = np.hstack((rho[Y, None], theta[X, None]))

    lines = list(filter(lambda l: l, (get_border_points(rho, theta, channel.shape) for rho, theta in best)))
    
    return channel, ImageChannelTransformation({'best': best}, {}, lines)

def hough_circle_channel(channel: np.ndarray, radius: np.ndarray, x_axis: np.ndarray, y_axis: np.ndarray, threshold: float, most_fitted_ratio: float) -> Tuple[np.ndarray, ImageChannelTransformation]:
    indices = index_matrix(*channel.shape)
    centers = np.stack(np.meshgrid(y_axis, x_axis), -1).reshape((-1, 2))
    acum = np.empty((radius.size, len(centers)))
    white_points = channel > 0

    for i in range(len(radius)):
        for j in range(len(centers)):
            # |rho - (y - b)^2 - (x - a)^2|
            circles = (indices - centers[j]) ** 2
            circles = np.insert(circles, 0, - radius[i] ** 2, axis=2)
            circles = np.abs(np.sum(circles, axis=2)) < threshold
            acum[i, j] = np.sum(white_points & circles)

    most_fitted_lines = np.argwhere(acum > most_fitted_ratio * acum.max())
    Y = np.transpose(most_fitted_lines)[0]
    X = np.transpose(most_fitted_lines)[1]

    best = np.hstack((radius[Y, None], centers[X]))

    overlay = list(filter(lambda l: l, (CircleDrawCmd(r, y, x) for r, y, x in best)))

    return channel, ImageChannelTransformation({'best': best}, {}, overlay)

def get_border_points(rho: float, theta: float, img_shape) -> Optional[LineDrawCmd]:
    if np.isclose(theta, 0):
        return LineDrawCmd(0, round(rho), img_shape[0]-1, round(rho))

    max_y = img_shape[0] - 1
    max_x = img_shape[1] - 1

    x_0 = rho / np.cos(theta)
    x_f = (rho - max_y * np.sin(theta)) / np.cos(theta)
    y_0 = rho / np.sin(theta)
    y_f = (rho - max_x * np.cos(theta)) / np.sin(theta)
    
    ans = []

    if 0 < x_0 < max_x: ans.append((0, x_0))
    if 0 < x_f < max_x: ans.append((max_y, x_f))
    if 0 < y_0 < max_y: ans.append((y_0, 0))
    if 0 < y_f < max_y: ans.append((y_f, max_x))

    if len(ans) != 2:
        raise ValueError(f'Cantidad incorrecta de puntos {len(ans)} para rho:{rho} y theta:{theta}')
        
    return LineDrawCmd(*ans[0], *ans[1])

def canny_drag_borders(gradient_mod: np.ndarray, t1: int, t2: int, max_col: int, max_row: int, row: int, col: int) -> None:
    if t1 < gradient_mod[row, col] < t2:
        # Conectado por un borde de manera 8-conexo
        neighbour = gradient_mod[max(row - 1, 0):min(row + 2, max_row), max(col - 1, 0):min(col + 2, max_col)]
        gradient_mod[row, col] = MAX_COLOR if np.amax(neighbour) == MAX_COLOR else 0

# Asume que ya fue paso por un filtro gaussiano
def canny_channel(channel: np.ndarray, t1: int, t2: int, padding_str: PaddingStrategy) -> np.ndarray:
    # Usamos prewitt para derivar
    kernel = FamousKernel.PREWITT.kernel
    dx = weighted_sum(channel, kernel, padding_str)
    kernel = np.rot90(kernel, k=-1)
    dy = weighted_sum(channel, kernel, padding_str)
    gradient_mod = np.sqrt(dy ** 2 + dx ** 2)

    # Calculamos el angulo de la derivada en grados
    d_angle = np.arctan2(dy, dx)
    d_angle[d_angle < 0] += np.pi
    d_angle = np.pi - d_angle
    d_angle = np.rad2deg(d_angle)

    # Discretizamos dicho angulo
    dir_sw = np.empty((*d_angle.shape, *kernel.shape))
    dir_sw[((0 <= d_angle) & (d_angle < 22.5)) | ((157.5 <= d_angle) & (d_angle <= 180))]   = Direction.from_angle(0).kernel
    dir_sw[(22.5 <= d_angle) & (d_angle < 67.5)]                                            = Direction.from_angle(45).kernel
    dir_sw[(67.5 <= d_angle) & (d_angle < 112.5)]                                           = Direction.from_angle(90).kernel
    dir_sw[(112.5 <= d_angle) & (d_angle < 157.5)]                                          = Direction.from_angle(135).kernel

    # Suprimimos los valores que no son maximos
    max_suppression_sw = sliding_window(gradient_mod, kernel.shape, padding_str) * dir_sw
    max_suppression_sw = np.max(max_suppression_sw, axis=(2, 3))
    gradient_mod[max_suppression_sw != gradient_mod] = 0

    # Normalizamos la imagen antes del thresholding
    gradient_mod = normalize(gradient_mod, np.float64)

    # Thresholding con histéresis
    gradient_mod[gradient_mod >= t2] = MAX_COLOR
    gradient_mod[gradient_mod <= t1] = 0

    max_row, max_col = gradient_mod.shape

    # Arrastramos los bordes de manera vertical
    for row in range(max_row):
        for col in range(max_col):
            canny_drag_borders(gradient_mod, t1, t2, max_col, max_row, row, col)

    # Arrastramos los bordes de manera horizontal
    for col in range(max_col):
        for row in range(max_row):
            canny_drag_borders(gradient_mod, t1, t2, max_col, max_row, row, col)

    return gradient_mod


def calculate_r1(ix2: np.ndarray, ixy: np.ndarray, iy2: np.ndarray, k: float) -> np.ndarray: 
    det = np.multiply(ix2, iy2) - np.multiply(ixy, ixy)
    sum = ix2 + iy2
    trace = np.multiply(sum, sum)

    return det - k*trace

def calculate_r2(ix2: np.ndarray, ixy: np.ndarray, iy2: np.ndarray, k: float) -> np.ndarray: 
    det = ix2 * iy2 - ixy * 4
    sum = ix2 + iy2

    return det - k*sum*sum
    
def harris_channel(channel: np.ndarray, sigma:int, k: float, threshold: float, r_function: HarrisR, padding_str: PaddingStrategy) -> np.ndarray:
    # Usamos prewitt para derivar
    kernel = FamousKernel.PREWITT.kernel
    dx = weighted_sum(channel, kernel, padding_str)
    kernel = np.rot90(kernel, k=-1)
    dy = weighted_sum(channel, kernel, padding_str)
    

    ix2 = weighted_sum(np.multiply(dx, dx), gauss_kernel(sigma), padding_str)
    ixy = weighted_sum(np.multiply(dx, dy), gauss_kernel(sigma), padding_str)
    iy2 = weighted_sum(np.multiply(dy, dy), gauss_kernel(sigma), padding_str)


    r = r_function(ix2, ixy, iy2, k)
    negative_mask = r < 0
    r_abs = np.abs(r)
    # Normalizamos la imagen antes del thresholding
    
    normalized_r = normalize(r_abs, np.float64)

    normalized_r[(normalized_r < threshold)] = 0
    normalized_r[(normalized_r > threshold) & negative_mask] = 125
    normalized_r[(normalized_r > threshold) & np.logical_not(negative_mask)] = 255
    
    return normalized_r

def set_difference(new_values: np.ndarray, removed_values: np.ndarray) -> np.ndarray:
    nrows, ncols = new_values.shape
    dtype = {'names': ['f{}'.format(i) for i in range(ncols)], 'formats': ncols * [new_values.dtype]}
    C = np.setdiff1d(new_values.copy().view(dtype), removed_values.copy().view(dtype))
    return C.view(new_values.dtype).reshape(-1, ncols)

def get_rectangular_boundary(x: Tuple[int, int], y: Tuple[int, int]) -> List[Tuple[int, int]]:
    boundary = []
    boundary.extend([(y[0], x) for x in range(x[0], x[1] + 1)])
    boundary.extend([(y[1], x) for x in range(x[0], x[1] + 1)])
    boundary.extend([(y, x[0]) for y in range(y[0], y[1] + 1)])
    boundary.extend([(y, x[1]) for y in range(y[0], y[1] + 1)])

    return boundary

def calculate_sigma(image: Image, x: Tuple[int, int], y: Tuple[int, int]) -> Union[float, np.ndarray]:
    if image.channels > 1:
        return np.mean(np.array(image.data[y[0]:y[1], x[0]:x[1]]).reshape((-1, 3)), axis=0)
    else:
        return np.mean(image.data[y[0]:y[1], x[0]:x[1]])

def get_initial_boundaries(x: Tuple[int, int], y: Tuple[int, int], shape:Tuple[int, int]) -> Tuple[List[Tuple[int, int]], List[Tuple[int, int]], np.ndarray]:
    lout = get_rectangular_boundary(x, y)
    lin = get_rectangular_boundary((x[0] + 1, x[1] - 1), (y[0] + 1, y[1] - 1))
    phi = np.full(shape, 3)
    phi[y[0]:y[1] + 1, x[0]:x[1] + 1] = 1
    phi[y[0] + 1:y[1], x[0] + 1:x[1]] = -1
    phi[y[0] + 2:y[1] - 1, x[0] + 2:x[1] - 1] = -3
    return lout, lin, phi

def in_bounds(x: int, y: int, shape: Tuple[int, int]):
    return 0 <= x < shape[1] and 0 <= y < shape[0]

def new_phi_values(phi: np.ndarray, add_collection: List[Tuple[int, int]], remove_collection: List[Tuple[int, int]],  indices: np.ndarray, point: Tuple[int, int], target: int, new_value: int, delete_condition):
    for index in indices:
        phi_y = point[0] + index[0]
        phi_x = point[1] + index[1]
        if in_bounds(phi_x, phi_y, phi.shape):
            if phi[phi_y, phi_x] == target:
                add_collection.append((phi_y, phi_x))
                phi[phi_y, phi_x] = new_value
            # Si eras el borde contrario a mi tengo que revisar si seguis siendo valido como dicho borde
            elif phi[phi_y, phi_x] == -new_value:
                check_value_state((phi_y, phi_x), phi, remove_collection, indices, delete_condition, -target)

def check_value_state(point: Tuple[int, int], phi: np.ndarray, remove_collection: List[Tuple[int, int]], indices: np.ndarray, condition, new_value: int):
    neighbor: bool = True
    for index in indices:
        phi_y = point[0] + index[0]
        phi_x = point[1] + index[1]
        # Tengo que tener a alguien que sea mi borde contrario al lado
        if in_bounds(phi_x, phi_y, phi.shape) and condition(phi[phi_y, phi_x]):
            neighbor = False
    # si tengo algun vecino que es mi borde contrario y algun vecino que es mi contorno contrario no te borro
    if neighbor:
        phi[point[0], point[1]] = new_value
        remove_collection.append((point[0], point[1]))

def is_lout(val: int) -> bool:
    return val > 0

def is_lin(val: int) -> bool:
    return val < 0

def active_outline_all_channels(image: np.ndarray, threshold: float, sigma: Union[float, np.ndarray], lout: List[Tuple[int, int]], lin: List[Tuple[int, int]], phi: np.ndarray) -> Tuple[np.ndarray, ImageChannelTransformation]:
    flag = True
    indices_4 = np.array([[-1, 0], [0, -1], [1, 0], [0, 1]])
    remove_lout = []
    remove_lin = []
    while flag:
        flag = False
        for point in lout:
            norm_lout = np.linalg.norm(sigma - image[point[0], point[1]])
            if norm_lout < threshold:
                lin.append(point)
                remove_lout.append(point)
                phi[point[0], point[1]] = -1
                new_phi_values(phi, lout, remove_lin, indices_4, point, 3, 1, is_lout)
                flag = True
        for point in lin:
            norm_lin = np.linalg.norm(sigma - image[point[0], point[1]])
            if norm_lin >= threshold:
                lout.append(point)
                remove_lin.append(point)
                phi[point[0], point[1]] = 1
                new_phi_values(phi, lin, remove_lout, indices_4, point, -3, -1, is_lin)
                flag = True
        lout = list(set(lout) - set(remove_lout))
        lin = list(set(lin) - set(remove_lin))

    overlay = [ScatterDrawCmd(np.asarray(lout), (255, 0, 0)), ScatterDrawCmd(np.asarray(lin), (255, 0, 255))]
    return image, ImageChannelTransformation({'threshold': threshold, 'sigma': sigma}, {'phi': phi, 'lout': lout, 'lin': lin}, overlay)


# ******************* Export Functions ********************** #

def directional(image: Image, kernel: FamousKernel, border_dir: Direction, padding_str: PaddingStrategy) -> Tuple[np.ndarray, List[ImageChannelTransformation]]:
    return image.apply_over_channels(directional_channel, vertical_kernel=kernel.kernel, border_dir=border_dir, padding_str=padding_str)

def high_pass(image: Image, kernel_size: int, padding_str: PaddingStrategy) -> Tuple[np.ndarray, List[ImageChannelTransformation]]:
    return image.apply_over_channels(high_pass_channel, kernel_size=kernel_size, padding_str=padding_str)

def prewitt(image: Image, padding_str: PaddingStrategy) -> Tuple[np.ndarray, List[ImageChannelTransformation]]:
    return image.apply_over_channels(prewitt_channel, padding_str=padding_str)

def sobel(image: Image, padding_str: PaddingStrategy) -> Tuple[np.ndarray, List[ImageChannelTransformation]]:
    return image.apply_over_channels(sobel_channel, padding_str=padding_str)

def laplace(image: Image, crossing_threshold: int, padding_str: PaddingStrategy) -> Tuple[np.ndarray, List[ImageChannelTransformation]]:
    return image.apply_over_channels(laplacian_channel, crossing_threshold=crossing_threshold, padding_str=padding_str)

def log(image: Image, sigma: float, crossing_threshold: int, padding_str: PaddingStrategy) -> Tuple[np.ndarray, List[ImageChannelTransformation]]:
    return image.apply_over_channels(log_channel, sigma=sigma, crossing_threshold=crossing_threshold, padding_str=padding_str)

def susan(image: Image, padding_str: PaddingStrategy) -> Tuple[np.ndarray, List[ImageChannelTransformation]]:
    return image.apply_over_channels(susan_channel, padding_str=padding_str)

def hough_lines(image: Image, theta: List[int], rho: LinRange, threshold: float, most_fitted_ratio: float) -> Tuple[np.ndarray, List[ImageChannelTransformation]]:
    theta = np.deg2rad(theta)
    return image.apply_over_channels(hough_lines_channel, theta=theta, rho=rho.to_linspace(), threshold=threshold, most_fitted_ratio=most_fitted_ratio)

def hough_circles(image: Image, radius: LinRange, x_axis: LinRange, y_axis: LinRange, threshold: float, most_fitted_ratio: float) -> Tuple[np.ndarray, List[ImageChannelTransformation]]:
    return image.apply_over_channels(hough_circle_channel, radius=radius.to_linspace(), x_axis=x_axis.to_linspace(), y_axis=y_axis.to_linspace(), threshold=threshold, most_fitted_ratio=most_fitted_ratio)

def canny(image: Image, t1: int, t2: int, padding_str: PaddingStrategy) -> Tuple[np.ndarray, List[ImageChannelTransformation]]:
    return image.apply_over_channels(canny_channel, t1=t1, t2=t2, padding_str=padding_str)

def harris(image: Image, sigma: int, k: float, threshold: float, function: HarrisR, padding_str: PaddingStrategy) -> Tuple[np.ndarray, List[ImageChannelTransformation]]:
    return image.apply_over_channels(harris_channel, sigma=sigma, k=k, threshold=threshold, r_function=function, padding_str=padding_str)


def active_outline_base(image: Image, threshold: float, p1: Tuple[int, int], p2: Tuple[int, int]) -> Tuple[np.ndarray, List[ImageChannelTransformation]]:
    start_time = time.thread_time_ns() // 1000000

    x = p1[1], p2[1]
    y = p1[0], p2[0]
    sigma = calculate_sigma(image, x, y)
    lout, lin, phi = get_initial_boundaries(x, y, image.data.shape[:2])
    img, tr = active_outline_all_channels(image.data, threshold, sigma, lout, lin, phi)

    duration = time.thread_time_ns() // 1000000 - start_time
    tr.public_results['duration']           = duration
    tr.public_results['total_duration']     = duration

    return img, [tr]

def active_outline_inductive(frame: int, prev: Image, current: Image) -> Tuple[np.ndarray, List[ImageChannelTransformation]]:
    prev_results = prev.last_transformation.channel_transformations[0].all_results()
    inputs = map(prev_results.get, ('threshold', 'sigma', 'lout', 'lin', 'phi'))

    start_time = time.thread_time_ns() // 1000000
    img, tr = active_outline_all_channels(current.data, *inputs)

    duration = time.thread_time_ns() // 1000000 - start_time
    tr.public_results['duration']           = duration
    tr.public_results['total_duration']     = prev_results['total_duration'] + duration
    tr.public_results['mean_duration']      = round(tr.public_results['total_duration'] / (frame + 1), 2)

    return img, [tr]
