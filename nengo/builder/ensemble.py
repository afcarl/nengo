import collections
import warnings

import numpy as np

import nengo.utils.numpy as npext
from nengo.builder.builder import Builder
from nengo.builder.operator import Copy, DotInc, Reset
from nengo.builder.signal import Signal
from nengo.dists import Distribution
from nengo.ensemble import Ensemble
from nengo.neurons import Direct
from nengo.utils.builder import default_n_eval_points

BuiltEnsemble = collections.namedtuple(
    'BuiltEnsemble', ['eval_points', 'encoders', 'intercepts', 'max_rates',
                      'scaled_encoders', 'gain', 'bias'])


def sample(dist, n, d=None, rng=None):
    if isinstance(dist, Distribution):
        return dist.sample(n, d=d, rng=rng).astype(np.float64)
    return np.array(dist, dtype=np.float64)


def gen_eval_points(ens, eval_points, rng, scale_eval_points=True):
    if isinstance(eval_points, Distribution):
        n_points = ens.n_eval_points
        if n_points is None:
            n_points = default_n_eval_points(ens.n_neurons, ens.dimensions)
        eval_points = eval_points.sample(n_points, ens.dimensions, rng)
    else:
        if (ens.n_eval_points is not None
                and eval_points.shape[0] != ens.n_eval_points):
            warnings.warn("Number of eval_points doesn't match "
                          "n_eval_points. Ignoring n_eval_points.")
        eval_points = np.array(eval_points, dtype=np.float64)
        assert eval_points.ndim == 2

    if scale_eval_points:
        eval_points *= ens.radius  # scale by ensemble radius
    return eval_points


def get_activities(model, ens, eval_points):
    x = np.dot(eval_points, model.params[ens].encoders.T / ens.radius)
    return ens.neuron_type.rates(
        x, model.params[ens].gain, model.params[ens].bias)


def get_gain_bias(ens, rng=np.random):
    if ens.gain is not None and ens.bias is not None:
        gain = sample(ens.gain, ens.n_neurons, rng=rng)
        bias = sample(ens.bias, ens.n_neurons, rng=rng)
        max_rates, intercepts = None, None  # TODO: determine from gain & bias
    elif ens.gain is not None or ens.bias is not None:
        # TODO: handle this instead of error
        raise NotImplementedError("gain or bias set for %s, but not both. "
                                  "Solving for one given the other is not "
                                  "implemented yet." % ens)
    else:
        max_rates = sample(ens.max_rates, ens.n_neurons, rng=rng)
        intercepts = sample(ens.intercepts, ens.n_neurons, rng=rng)
        gain, bias = ens.neuron_type.gain_bias(max_rates, intercepts)

    return gain, bias, max_rates, intercepts


@Builder.register(Ensemble)  # noqa: C901
def build_ensemble(model, ens):
    """Builds an `.Ensemble` object into a model.

    A brief of summary of what happens in the ensemble build process, in order:

    1. Generate evaluation points and encoders.
    2. Normalize encoders to unit length.
    3. Determine bias and gain.
    4. Create neuron input signal
    5. Add operator for injecting bias.
    6. Call build function for neuron type.
    7. Scale encoders by gain and radius.
    8. Add operators for mulitplying decoded input signal by encoders and
       incrementing the result in the neuron input signal.
    9. Call build function for injected noise.

    Some of these steps may be altered or omitted depending on the parameters
    of the ensemble, in particular the neuron type. For example, most steps are
    omitted for the `.Direct` neuron type.

    Parameters
    ----------
    model : Model
        The model to build into.
    ens : Ensemble
        The ensemble to build.

    Notes
    -----
    Sets ``model.params[ens]`` to a `.BuiltEnsemble` instance.
    """

    # Create random number generator
    rng = np.random.RandomState(model.seeds[ens])

    eval_points = gen_eval_points(ens, ens.eval_points, rng=rng)

    # Set up signal
    model.sig[ens]['in'] = Signal(np.zeros(ens.dimensions),
                                  name="%s.signal" % ens)
    model.add_op(Reset(model.sig[ens]['in']))

    # Set up encoders
    if isinstance(ens.neuron_type, Direct):
        encoders = np.identity(ens.dimensions)
    elif isinstance(ens.encoders, Distribution):
        encoders = sample(ens.encoders, ens.n_neurons, ens.dimensions, rng=rng)
    else:
        encoders = npext.array(ens.encoders, min_dims=2, dtype=np.float64)
    encoders /= npext.norm(encoders, axis=1, keepdims=True)

    # Build the neurons
    gain, bias, max_rates, intercepts = get_gain_bias(ens, rng)

    if isinstance(ens.neuron_type, Direct):
        model.sig[ens.neurons]['in'] = Signal(
            np.zeros(ens.dimensions), name='%s.neuron_in' % ens)
        model.sig[ens.neurons]['out'] = model.sig[ens.neurons]['in']
        model.add_op(Reset(model.sig[ens.neurons]['in']))
    else:
        model.sig[ens.neurons]['in'] = Signal(
            np.zeros(ens.n_neurons), name="%s.neuron_in" % ens)
        model.sig[ens.neurons]['out'] = Signal(
            np.zeros(ens.n_neurons), name="%s.neuron_out" % ens)
        bias_sig = Signal(bias, name="%s.bias" % ens, readonly=True)
        model.add_op(Copy(src=bias_sig, dst=model.sig[ens.neurons]['in']))
        # This adds the neuron's operator and sets other signals
        model.build(ens.neuron_type, ens.neurons)

    # Scale the encoders
    if isinstance(ens.neuron_type, Direct):
        scaled_encoders = encoders
    else:
        scaled_encoders = encoders * (gain / ens.radius)[:, np.newaxis]

    model.sig[ens]['encoders'] = Signal(
        scaled_encoders, name="%s.scaled_encoders" % ens, readonly=True)

    # Inject noise if specified
    if ens.noise is not None:
        model.build(ens.noise, sig_out=model.sig[ens.neurons]['in'], inc=True)

    # Create output signal, using built Neurons
    model.add_op(DotInc(
        model.sig[ens]['encoders'],
        model.sig[ens]['in'],
        model.sig[ens.neurons]['in'],
        tag="%s encoding" % ens))

    # Output is neural output
    model.sig[ens]['out'] = model.sig[ens.neurons]['out']

    model.params[ens] = BuiltEnsemble(eval_points=eval_points,
                                      encoders=encoders,
                                      intercepts=intercepts,
                                      max_rates=max_rates,
                                      scaled_encoders=scaled_encoders,
                                      gain=gain,
                                      bias=bias)
