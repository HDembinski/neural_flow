"""Bijectors used in conditional normalizing flows."""

from typing import Tuple, Sequence, Callable, Union, Optional, Dict, TypeGuard
from abc import ABC, abstractmethod
from jax import numpy as jnp
from .utils import (
    normalize_spline_params,
    rational_quadratic_spline_forward,
    rational_quadratic_spline_inverse,
)
from flax import linen as nn
from flax.typing import Array
from flax.linen.normalization import _compute_stats
import numpy as np
from jax import lax


__all__ = [
    "Bijector",
    "ShiftBounds",
    "Roll",
    "NeuralSplineCoupling",
    "Chain",
    "chain",
    "rolling_spline_coupling",
]


class Bijector(nn.Module, ABC):
    """
    Bijector base class.

    A bijector is a basic element that defines the normalizing flow. The bijector is
    learned during training to transform a simple base distribution to the target
    distribution.
    """

    @abstractmethod
    def __call__(self, x: Array, c: Array, train: bool = False) -> Tuple[Array, Array]:
        """
        Transform samples from the target distribution to the base distribution.

        Parameters
        ----------
        x : Array of shape (N, D)
            N samples from a D-dimensional target distribution. It is not necessary to
            standardize it or transform it to look more gaussian, but doing so might
            accelerate convergence or allow one to use a simpler bijector.
        c : Array of shape (N, K) or None
            N values from a K-dimensional vector of variables which determines the shape
            of the D-dimensional distribution.
        train : bool, optional (default = False)
            Whether to run in training mode (update BatchNorm statistics, etc.).

        Returns
        -------
        y : Array of shape (N, D)
            N samples of the base distribution.
        log_det : Array of shape (N,)
            Logarithm of the determinant of the transformation.

        """
        raise NotImplementedError

    @abstractmethod
    def inverse(self, x: Array, c: Array) -> Array:
        """
        Transform samples from the base distribution to the target distribution.

        The log-determinant is not returned in the inverse pass, since it is not needed.

        Parameters
        ----------
        x : Array of shape (N, D)
            N samples from the D-dimensional base distribution.
        c : Array of shape (N, K) or None
            N values from a K-dimensional vector of variables which determines the shape
            of the D-dimensional target distribution.

        Returns
        -------
        y : Array of shape (N, D)
            N samples of the target distribution.

        """
        raise NotImplementedError


class Chain(Bijector, Sequence):
    """
    Chain of other bjiectors.

    The forward transform calls bijectors in order and applies the forward transform of
    each and accumulates the log-determinants.

    The inverse transform calls the bijectors in reverse order and applies the inverse
    transform of each.
    """

    bijectors: Sequence[Bijector]

    @nn.compact
    def __call__(self, x: Array, c: Array, train: bool = False) -> Tuple[Array, Array]:
        log_det = jnp.zeros(x.shape[0])
        for bijector in self.bijectors:
            x, ld = bijector(x, c, train)
            log_det += ld
        return x, log_det

    def inverse(self, x: Array, c: Array) -> Array:
        for bijector in self.bijectors[::-1]:
            x = bijector.inverse(x, c)
        return x

    def __getitem__(self, idx: Union[int, slice]):
        """Get bijector at location idx."""
        return self.bijectors[idx]

    def __len__(self):
        """Return number of bijectors in the chain."""
        return len(self.bijectors)


def chain(*bijectors):
    """Create a chain directly from a variable number of bijector arguments."""
    return Chain(bijectors)


class ShiftBounds(Bijector):
    """
    Shift values into the interval [margin, 1 - margin].

    This bijector keeps track of the smallest and largest inputs along each dimension of
    the target distribution and applies an affine transformation so that all values are
    inside a hypercube where each side starts at `margin` and ends at `1 - margin`.

    This transformation is necessary before applying the first NeuralSplineCoupling,
    which only transforms samples inside a hypercube with coordinates in the interval
    (0, 1) along each dimension. A value margin > 0 guarantees that values are never
    mapped to the boundary values, where the latent distribution may be exactly zero.

    It is possible to declare the bounds of a bounded variable. If the variable is
    fully bounded, ShiftBounds will not try to estimate the minimum and maximum value from the sample and use the known bounds instead. For samples which are bounded
    on one side, a log-transform is applied to make the variable unbounded before
    the usual processing.
    """

    margin: float = 1e-5
    bounds: Sequence[Tuple[int, Optional[float], Optional[float]]] = ()

    def setup(self):
        if self.margin < 0:
            msg = f"margin = {self.margin} is less than zero, it must be positive"
            raise ValueError(msg)
        if self.margin >= 0.5:
            msg = f"margin = {self.margin} must be much smaller than 0.5"
            raise ValueError(msg)

    @nn.compact
    def __call__(self, x: Array, c: Array, train: bool = False) -> Tuple[Array, Array]:
        if self.is_initializing():
            if self.margin != 0 and not (jnp.array(1, x.dtype) - self.margin < 1):
                msg = f"margin = {self.margin} is too small for dtype={x.dtype}, use a larger value"
                raise ValueError(msg)

            for i, a, b in self.bounds:
                if i >= x.shape[1]:
                    msg = f"index {i} is out of bounds"
                    raise ValueError(msg)
                if _is_set(a) and _is_set(b):
                    if b < a:
                        raise ValueError("upper bound must be larger than lower bound")

        bounds = {i: (a, b) for (i, a, b) in self.bounds}

        if x.dtype.kind == "i":
            x = x.astype(jnp.float32)

        z = jnp.empty_like(x)
        log_det = jnp.zeros(x.shape[0], x.dtype)
        for i in range(x.shape[1]):
            xi = x[:, i]
            a, b = bounds.get(i, (None, None))
            if _is_set(a):
                if _is_set(b):
                    # fully bounded
                    mul = 1 / (b - a)
                    assert mul > 0
                    zi = (xi - a) * mul
                    ld = jnp.log(mul)
                else:
                    # only lower bound
                    ti = jnp.log(xi - a)
                    zi, ld = self._transform_to_unit_interval(i, ti, train)
                    ld -= ti
            elif _is_set(b):
                # only upper bound
                ti = jnp.log(b - xi)
                zi, ld = self._transform_to_unit_interval(i, ti, train)
                ld -= ti
            else:
                # no bounds
                zi, ld = self._transform_to_unit_interval(i, xi, train)
            z = z.at[:, i].set(zi)
            log_det += ld

        # Now we shrink the interval [0, 1] to [margin, 1 - margin]
        y = (1 - self.margin) * z + (1 - z) * self.margin
        log_det += y.shape[1] * jnp.log((1 - 2 * self.margin))
        return y, log_det

    def inverse(self, y: Array, c: Array) -> Array:
        bounds = {i: (a, b) for (i, a, b) in self.bounds}

        z = (y - self.margin) / (1 - 2 * self.margin)

        x = jnp.empty_like(y)
        for i in range(y.shape[1]):
            zi = z[:, i]
            a, b = bounds.get(i, (None, None))
            if _is_set(a):
                if _is_set(b):
                    # fully bounded
                    xi = zi * b + (1 - zi) * a
                else:
                    # only lower bound
                    xmin = self.get_variable("batch_stats", f"xmin_{i}")
                    xmax = self.get_variable("batch_stats", f"xmax_{i}")
                    ti = zi * xmax + (1 - zi) * xmin
                    xi = jnp.exp(ti) + a
            elif _is_set(b):
                # only upper bound
                xmin = self.get_variable("batch_stats", f"xmin_{i}")
                xmax = self.get_variable("batch_stats", f"xmax_{i}")
                ti = zi * xmax + (1 - zi) * xmin
                xi = b - jnp.exp(ti)
            else:
                # no bounds
                xmin = self.get_variable("batch_stats", f"xmin_{i}")
                xmax = self.get_variable("batch_stats", f"xmax_{i}")
                xi = zi * xmax + (1 - zi) * xmin
            x = x.at[:, i].set(xi)

        return x

    def _transform_to_unit_interval(self, i: int, x: Array, train: bool):
        ra_min = self.variable(
            "batch_stats", f"xmin_{i}", lambda s: jnp.full(s, np.inf), (1,)
        )
        ra_max = self.variable(
            "batch_stats", f"xmax_{i}", lambda s: jnp.full(s, -np.inf), (1,)
        )

        if train:
            xmin = jnp.minimum(ra_min.value, x.min())
            xmax = jnp.maximum(ra_max.value, x.max())
            if not self.is_initializing():
                ra_min.value = xmin
                ra_max.value = xmax
        else:
            xmin = ra_min.value
            xmax = ra_max.value

        mul = 1 / (xmax - xmin)
        z = (x - xmin) * mul
        ld = jnp.log(mul)
        # If test sample has more extreme values than train sample, it is possible to
        # get z values outside of the interval [0, 1], which may cause the latent
        # distribution to be evaluated outside of its non-zero domain. We clip the
        # values as a workaround.
        z = jnp.clip(z, 0, 1)
        return z, ld


class TransformToBound(Bijector):
    """
    Transform all values into the interval [0, 1].

    This bijector does not work very well and should not be used.
    """

    bounds: Sequence[Tuple[int, Optional[float], Optional[float]]] = ()
    momentum: float = 0.99
    var_epsilon: float = 0.0
    use_fast_variance: bool = True
    force_float32_reductions: bool = True

    @nn.compact
    def __call__(self, x: Array, c: Array, train: bool = False) -> Tuple[Array, Array]:
        if self.is_initializing():
            for i, a, b in self.bounds:
                if i >= x.shape[1]:
                    msg = f"index {i} is out of bounds"
                    raise ValueError(msg)
                if _is_set(a) and _is_set(b):
                    if b < a:
                        raise ValueError("upper bound must be larger than lower bound")
        bounds = {i: (a, b) for (i, a, b) in self.bounds}
        y = jnp.empty(x.shape, x.dtype)
        log_det = jnp.zeros(x.shape[0], x.dtype)
        for i in range(y.shape[1]):
            xi = x[:, i]
            a, b = bounds.get(i, (None, None))
            if _is_set(a):
                if _is_set(b):
                    # fully bounded
                    scale = 1 / (b - a)
                    yi = (xi - a) * scale
                    ld = jnp.log(scale)
                else:
                    # only lower bound
                    lxi = jnp.log(xi - a)
                    yi, ld = self._transform(i, lxi, train)
                    ld -= lxi
            elif _is_set(b):
                # only upper bound
                lxi = jnp.log(b - xi)
                yi, ld = self._transform(i, lxi, train)
                ld -= lxi
            else:
                # no bounds
                yi, ld = self._transform(i, xi, train)
            y = y.at[:, i].set(yi)
            log_det += ld

        return y, log_det

    def inverse(self, y: Array, c: Array) -> Array:
        bounds = {i: (a, b) for (i, a, b) in self.bounds}
        x = jnp.empty(y.shape, y.dtype)
        for i in range(y.shape[1]):
            yi = y[:, i]
            a, b = bounds.get(i, (None, None))
            if _is_set(a):
                if _is_set(b):
                    # fully bounded
                    scale = b - a
                    xi = yi * scale + a
                else:
                    # only lower bound
                    xi = jnp.exp(self._inverse_transform(i, yi)) + a
            elif _is_set(b):
                # only upper bound
                xi = b - jnp.exp(self._inverse_transform(i, yi))
            else:
                # no bounds
                xi = self._inverse_transform(i, yi)
            x = x.at[:, i].set(xi)
        return x

    def _transform(self, i: int, x: Array, train: bool) -> Tuple[Array, Array]:
        ra_mean = self.variable(
            "batch_stats", f"mean_{i}", lambda s: jnp.zeros(s, jnp.float32), (1,)
        )
        ra_var = self.variable(
            "batch_stats", f"var_{i}", lambda s: jnp.ones(s, jnp.float32), (1,)
        )

        if not train:
            mean, var = ra_mean.value, ra_var.value
        else:
            mean, var = _compute_stats(
                x,
                (0,),
                dtype=None,
                use_fast_variance=self.use_fast_variance,
                force_float32_reductions=self.force_float32_reductions,
            )

            if not self.is_initializing():
                ra_mean.value = (
                    self.momentum * ra_mean.value + (1 - self.momentum) * mean
                )
                ra_var.value = self.momentum * ra_var.value + (1 - self.momentum) * var

        scale = lax.rsqrt(var + self.var_epsilon)
        z = scale * (x - mean)
        y = 0.5 * (jnp.tanh(z) + 1)
        ld = jnp.log(0.5 * scale) - 2 * jnp.log(jnp.cosh(z))
        return y, ld

    def _inverse_transform(self, i: int, y: Array) -> Tuple[Array, Array]:
        loc = self.get_variable("batch_stats", f"mean_{i}")
        var = self.get_variable("batch_stats", f"var_{i}")
        scale = lax.sqrt(var + self.var_epsilon)
        z = jnp.atanh(2 * y - 1)
        x = z * scale + loc
        return x


class Roll(Bijector):
    """
    Roll inputs along their last column.

    This bijector should be used together with a NeuralSplineCoupling. Couplings use the
    upper dimensions of the input sample and the conditional variables to transform the
    lower dimensions of the input sample. Roll mixes the upper and lower dimensions. One
    should apply at least D-1 Rolls for D dimensional input to transform all dimensions.
    """

    shift: int = 1

    def __call__(self, x: Array, c: Array, train: bool = False) -> Tuple[Array, Array]:
        x = jnp.roll(x, shift=self.shift, axis=-1)
        log_det = jnp.zeros(x.shape[0])
        return x, log_det

    def inverse(self, x: Array, c: Array) -> Array:
        x = jnp.roll(x, shift=-self.shift, axis=-1)
        return x


class NeuralSplineCoupling(Bijector):
    """
    Coupling layer with transforms with rational quadratic splines.

    This coupling transform uses a rational quadratic spline, which is analytically
    invertible. Couplings use the upper dimensions of the input sample and the
    conditional variables to transform the lower dimensions of the input sample.

    The spline only transform values in a hypercube with side intervals [0, 1]. For
    values outside of the hypercube the identity transform is applied.

    For a derivation, discussion, and more information, see:

    Durkan, C., Bekasov, A., Murray, I., and Papamakarios, G. (2019). “Neural Spline
    Flows,” In: Advances in Neural Information Processing Systems, pp. 7509–7520.
    """

    knots: int = 16
    layers: Sequence[int] = (128, 128)
    act: Callable[[Array], Array] = nn.swish

    @nn.nowrap
    @staticmethod
    def _split(x: Array):
        x_dim = x.shape[1]
        x_split = x_dim // 2
        assert x_split > 0 and x_split < x_dim
        return x[:, :x_split], x[:, x_split:]

    @nn.compact
    def _spline_params(
        self, x: Array, c: Array, train: bool
    ) -> Tuple[Array, Array, Array, Array, Array]:
        # xt are transformed conditionally based on values xc
        xt, xc = self._split(x)

        dim = xt.shape[1]
        spline_dim = 3 * self.knots - 1

        # calculate spline parameters as a function of xc variables
        # and external conditional variables c
        x = jnp.hstack((xc, c))
        x = nn.BatchNorm(use_running_average=not train)(x)
        for width in self.layers:
            x = nn.Dense(width)(x)
            x = self.act(x)
        x = nn.Dense(dim * spline_dim)(x)
        x = x.reshape((xt.shape[0], dim, spline_dim))

        return (
            xt,
            xc,
            *normalize_spline_params(
                x[..., : self.knots],
                x[..., self.knots : 2 * self.knots],
                x[..., 2 * self.knots :],
            ),
        )

    def __call__(self, x: Array, c: Array, train: bool = False) -> Tuple[Array, Array]:
        xt, xc, dx, dy, sl = self._spline_params(x, c, train)
        yt, log_det = rational_quadratic_spline_forward(xt, dx, dy, sl)
        y = jnp.hstack((yt, xc))
        return y, log_det

    def inverse(self, y: Array, c: Array) -> Array:
        yt, yc, dx, dy, sl = self._spline_params(y, c, False)
        xt = rational_quadratic_spline_inverse(yt, dx, dy, sl)
        x = jnp.hstack((xt, yc))
        return x


def rolling_spline_coupling(
    dim: int,
    knots: int = 16,
    layers: Sequence[int] = (128, 128),
    margin: Optional[float] = None,
    bounds: Sequence[Tuple[int, Optional[float], Optional[float]]] = (),
    preprocessing: Optional[Sequence[Bijector]] = None,
) -> Chain:
    """
    Create a chain of rolling spline couplings.

    The chain starts with ShiftBounds and then alternates between
    NeuralSplineCoupling and Roll once for each dimension in the input.
    The input must be at least two-dimensional for this to work.

    Parameters
    ----------
    dim: int
        The dimension of the target distribution.
    knots : int (default = 16)
        Number of knots used by the spline.
    layers: sequence of int (default = (128, 128))
        Sequence of neurons per hidden layer in the feed-forward network which computes
        the spline parameters from the upper dimensions of the input and the conditional
        variables.
    margin : float or None (default is None)
        Safety margin for ShiftBounds. See ShiftBounds for details. The parameter is
        ignored, if preprocessing is set.
    preprocessing: sequence of bijectors or None (default is None)
        Specify an alternative preprocessing chain. The default is to use ShiftBounds.
    """
    if dim < 2:
        raise ValueError("dim must be at least 2")
    if preprocessing is not None:
        bijectors = list(preprocessing)
    else:
        kwargs: Dict[
            "str", Union[float, Sequence[Tuple[int, Optional[float], Optional[float]]]]
        ] = {}
        if margin is not None:
            kwargs["margin"] = margin
        if bounds is not None:
            kwargs["bounds"] = bounds
        bijectors = [ShiftBounds(**kwargs)]
    for _ in range(dim - 1):
        bijectors.append(NeuralSplineCoupling(knots=knots, layers=layers))
        bijectors.append(Roll())
    bijectors.append(NeuralSplineCoupling(knots=knots, layers=layers))
    # we can skip last Roll, latent distribution is invariant to Roll
    return Chain(bijectors)


def _is_set(x: Optional[float]) -> TypeGuard[float]:
    return x is not None and np.isfinite(x)
