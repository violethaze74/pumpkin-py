# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.
import contextlib
import functools
from typing import Any, Dict, Optional, Tuple, Callable, Union
import torch
from torch._C import _disabled_torch_function_impl
import torch.utils._pytree as pytree
from torch.fx import Tracer, GraphModule
from torch._subclasses.fake_tensor import FakeTensorMode
import torch.fx as fx
from torch.utils._mode_utils import no_dispatch
from torch.fx.passes.shape_prop import _extract_tensor_metadata
from contextlib import contextmanager, nullcontext
import inspect

from torch.utils._python_dispatch import TorchDispatchMode, enable_torch_dispatch_mode
from torch._subclasses import FakeTensor
from .symbolic_shapes import ShapeEnv, magic_methods, reflectable_magic_methods
import torch.fx.experimental.symbolic_shapes as symbolic_shapes

__all__ = ["ProxyTensor", "PythonKeyTracer", "dispatch_trace", "make_fx", "DecompositionInterpreter"]
aten = torch.ops.aten

CURRENT_DECOMPOSITION_TABLE: Dict[torch._ops.OpOverload, Callable] = {}


def fake_signature(fn, nargs):
    """FX gets confused by varargs, de-confuse it"""
    argnames = ",".join(f"arg{i}" for i in range(nargs))
    return eval(f"lambda {argnames}: fn({argnames})", {"fn": fn})


class ProxySymInt(object):
    def __init__(self, sym_int, proxy):
        assert isinstance(sym_int, torch._C.SymIntNode) or isinstance(sym_int, int)
        self.sym_int = sym_int
        # Note, this doesn't have to be a proxy, it can also be an int
        self.proxy = proxy

    def wrap(self, num):
        return ProxySymInt(num, num)

    def __str__(self):
        return f"ProxySymInt({self.sym_int})"

    def __int__(self):
        # Not sure how to make mypy support this lol
        return int(self.sym_int)  # type: ignore[arg-type]

    def __bool__(self):
        return bool(self.sym_int)

    def to_fx_node(self):
        if isinstance(self.proxy, fx.Proxy):
            return self.proxy.node
        return self.proxy

import operator

def create_magic_impl(op):
    def magic_impl(self, other):
        def unwrap_proxy(x):
            return x.proxy if isinstance(x, ProxySymInt) else x
        out_proxy = op(unwrap_proxy(self), unwrap_proxy(other))

        def unwrap_proxyint(x):
            return x.sym_int if isinstance(x, ProxySymInt) else x
        out_sym_int = op(unwrap_proxyint(self), unwrap_proxyint(other))
        return ProxySymInt(out_sym_int, out_proxy)
    return magic_impl

for method in reflectable_magic_methods:
    method_name = f'{method}'

    op = getattr(operator, method_name)
    setattr(ProxySymInt, f'r{method_name}', create_magic_impl(op))

for method in magic_methods:
    method_name = f'{method}'

    op = getattr(operator, method_name)
    setattr(ProxySymInt, method_name, create_magic_impl(op))


@contextmanager
def decompose(decomposition_table):
    global CURRENT_DECOMPOSITION_TABLE
    old_decomposition_table = CURRENT_DECOMPOSITION_TABLE
    CURRENT_DECOMPOSITION_TABLE = decomposition_table
    try:
        yield CURRENT_DECOMPOSITION_TABLE
    finally:
        CURRENT_DECOMPOSITION_TABLE = old_decomposition_table

def wrap_output(inner_res, proxy_res, *, constant, proxy_mode):
    def wrap_with_proxy(e, proxy, constant):
        if isinstance(e, torch.Tensor):
            with no_dispatch():
                return ProxyTensor(e, proxy, constant=constant, proxy_mode=proxy_mode)
        else:
            return e

    def get_constant(idx):
        if constant is None:
            return None
        else:
            return constant[idx]

    # Unfortunately, tree_map cannot directly be used here. As the resulting
    # object may be a proxy that represents a tuple, we may need to
    # explicitly unwrap the proxy by simulating the flattening operations.
    if isinstance(inner_res, tuple):
        return tuple(wrap_with_proxy(e, proxy_res[idx], get_constant(idx)) for idx, e in enumerate(inner_res))
    elif isinstance(inner_res, list):
        return list([wrap_with_proxy(e, proxy_res[idx], get_constant(idx)) for idx, e in enumerate(inner_res)])
    elif isinstance(inner_res, torch.Tensor):
        return wrap_with_proxy(inner_res, proxy_res, constant)
    else:
        return inner_res


def maybe_disable_fake_tensor_mode():
    # TODO: figure out if this API generally makes sense and bake it into the
    # library
    mb_fake_mode = torch._C._get_torch_dispatch_mode()
    if isinstance(mb_fake_mode, FakeTensorMode):
        return enable_torch_dispatch_mode(mb_fake_mode.inner, replace=mb_fake_mode)
    else:
        return nullcontext()


def unwrap_elem(e):
    if isinstance(e, ProxyTensor):
        return e.elem
    if isinstance(e, torch._C.SymIntNode):
        if isinstance(e.get_pyobj(), ProxySymInt):
            return e.get_pyobj().sym_int
        else:
            raise RuntimeError(f"Something has gone wrong, we are trying to put SymInt {e.get_pyobj()} into the graph,"
                               f"even though it's not a ProxySymInt. This is a bug.")
    return e


def proxy_call(proxy_mode, func_overload, args, kwargs=None):
    if kwargs is None:
        kwargs = {}

    func = func_overload.overloadpacket
    if func_overload in CURRENT_DECOMPOSITION_TABLE:
        with proxy_mode.restore():
            return CURRENT_DECOMPOSITION_TABLE[func_overload](*args, **kwargs)
    with proxy_mode.restore():
        r = func_overload.decompose(*args, **kwargs)
        if r is not NotImplemented:
            return r

    all_constant = pytree.tree_all_only(ProxyTensor, lambda t: t.constant is not None, (args, kwargs))

    if torch.Tag.data_dependent_output in func_overload.tags:  # type: ignore[attr-defined]
        # Check if all of the Tensor inputs are constants
        if all_constant:
            const_args, const_kwargs = pytree.tree_map_only(
                ProxyTensor, lambda t: t.constant, (args, kwargs)
            )
            with maybe_disable_fake_tensor_mode():
                return func_overload(*const_args, **const_kwargs)
        raise RuntimeError(
            "It appears that you're trying to get value out of a tracing tensor - erroring out! "
            "It's likely that this is caused by data-dependent control flow or similar."
        )

    proxy_args, proxy_kwargs = pytree.tree_map_only(ProxyTensor, lambda e: e.proxy, (args, kwargs))
    proxy_res = func_overload(*proxy_args, **proxy_kwargs)

    # Kind of a hacky way to test if an op is in-place or not
    if func.__name__[-1] == "_" and func.__name__[0] != "_":
        args[0].proxy = proxy_res
        proxy_res.node.meta['tensor_meta'] = _extract_tensor_metadata(args[0])

    elem_args, elem_kwargs = pytree.tree_map(unwrap_elem, (args, kwargs))
    inner_res = func_overload(*elem_args, **elem_kwargs)

    # Needed to sync up metadata for in-place operators that modify metadata
    # TODO: instead forward the metadata to the inner tensor so updating
    # is not necessary
    if torch.Tag.inplace_view in func_overload.tags:  # type: ignore[attr-defined]
        with no_dispatch():
            func_overload(*args, **kwargs)

    # In some circumstances, we will be tracing in a situation where a tensor
    # is *statically* known to be a constant (currently, this only happens if
    # you run torch.tensor; deterministic factory functions like torch.arange
    # don't get this treatment).  When the tensor in question is small, it's
    # helpful to due constant propagation in case we call item() (in which
    # case we can return the constant value that is known, rather than give
    # an error.)  The logic here tests if constant propagation is possible
    # (because all of the inputs are constant).  If so, we disable fake tensor
    # mode (if it is on) and do true compute on the constant.
    #
    # It's worth highlighting that we're making a policy decision here.
    # There is a potential that the tensor is actually quite large, and we
    # don't actually want to run the compute.  The tensor being quite large
    # is one of the reasons why factory functions don't get this treatment
    # (since they can be quite large; if a parameter is initialized to a
    # constant value it will be!)  Similarly, there is also a potential
    # to run an operator that blows up the size of a small tensor; we don't
    # protect against this case, but we could force, e.g., only single
    # element constant computation by testing the numel of the result before
    # propagating const-ness.  Similarly, we don't require the constant to
    # live on CPU, but we could.
    any_constant = pytree.tree_any_only(ProxyTensor, lambda t: t.constant is not None, (args, kwargs))

    constant = None
    # NB: do NOT include factories as constants
    if all_constant and any_constant:
        with maybe_disable_fake_tensor_mode():
            const_args, const_kwargs = pytree.tree_map_only(
                ProxyTensor, lambda t: t.constant, (args, kwargs)
            )
            constant = func_overload(*const_args, **const_kwargs)

    # TODO(chilli): Enable this after it's been refactored to work with wrapper tensor subclasses in general
    # pytree.tree_map(lambda x: check_metadata_consistency(x, ProxyTensor), (inner_res, args, kwargs))
    return wrap_output(inner_res, proxy_res, constant=constant, proxy_mode=proxy_mode)


class ProxyTensor(torch.Tensor):
    proxy: fx.Proxy
    elem: torch.Tensor
    has_sym_ints: bool
    proxy_mode: "ProxyTorchDispatchMode"


    @staticmethod
    def __new__(cls, elem, proxy, *, requires_grad=None, constant=None, proxy_mode):
        def create_proxy_symint(sym_int, new_proxy):
            return torch._C.SymIntNode.new_symint(ProxySymInt(sym_int, new_proxy))

        has_sym_ints = symbolic_shapes.has_symbolic_sizes_strides(elem)
        if has_sym_ints:
            new_shape = []
            for idx, s in enumerate(elem.shape):
                if isinstance(s, torch._C.SymIntNode):
                    new_shape.append(create_proxy_symint(s, proxy.size(idx)))
                else:
                    assert isinstance(s, int)
                    # If it's not an existing SymIntNodeImpl, just pass the proxy as the int
                    # _make_wrapper_subclass requires all inputs to be SymIntNodeImpls
                    new_shape.append(create_proxy_symint(s, s))
            # TODO: hack, since we currently don't support symbolic strides
            new_strides = symbolic_shapes.create_contiguous(new_shape)
        else:
            new_shape = elem.shape
            new_strides = elem.stride()

        r = torch.Tensor._make_wrapper_subclass(  # type: ignore[attr-defined]
            cls,
            new_shape, dtype=elem.dtype, layout=elem.layout, device=elem.device,
            requires_grad=requires_grad if requires_grad is not None else False, strides=new_strides,
            storage_offset=elem.storage_offset()
        )
        r.has_sym_ints = has_sym_ints
        return r

    def __init__(self, elem, proxy, *, requires_grad=None, constant=None, proxy_mode):
        # TODO: hack since _extract_tensor_metadata currently tries to access stride
        if elem.is_sparse or self.has_sym_ints:
            proxy.node.meta['tensor_meta'] = {}
        else:
            proxy.node.meta['tensor_meta'] = _extract_tensor_metadata(self)
        # This detects situations where you accidentally put a ProxyTensor
        # inside a ProxyTensor for the same trace; this is a layering violation
        assert not (isinstance(elem, ProxyTensor) and elem.proxy.tracer is proxy.tracer)
        self.elem = elem
        self.proxy = proxy
        self.constant = constant
        self.proxy_mode = proxy_mode


    def __deepcopy__(self, memo):
        return self.clone()

    def __repr__(self):
        with no_dispatch():
            return f"ProxyTensor({self.elem}, proxy={self.proxy})"

    __torch_function__ = _disabled_torch_function_impl

    @classmethod
    def __torch_dispatch__(cls, func_overload, types, args=(), kwargs=None):
        raise RuntimeError(
            "Should not be needed as we always trace with modes. May have entered this due to redispatching from"
            "__torch_dispatch__ into another op without restoring dispatch mode"
        )


class PythonKeyTracer(Tracer):
    def __init__(self):
        super().__init__()

    # In general, we don't want to make modules leaves. In principle, users of
    # this tracer might want to override this in order to turn a couple specific
    # modules into leaves in the traced graph.
    def call_module(
            self, m: torch.nn.Module, forward: Callable[..., Any], args: Tuple[Any, ...], kwargs: Dict[str, Any]
    ) -> Any:
        return forward(*args, **kwargs)

    def create_arg(self, a: Any):
        if isinstance(a, torch.nn.Parameter):
            for n, p in self.root.named_parameters():
                if a is p:
                    return self.create_node('get_attr', n, (), {})
            qualname: Optional[str] = None

            if not qualname:
                i = 0
                while True:
                    qualname = f'_param_constant{i}'
                    if not hasattr(self.root, qualname):
                        break
                    i += 1
                setattr(self.root, qualname, a)

            return self.create_node('get_attr', qualname, (), {})
        elif isinstance(a, torch._C.SymIntNode):
            py_symint = a.get_pyobj()
            assert isinstance(py_symint, ProxySymInt)
            return py_symint.to_fx_node()
        return super().create_arg(a)


def dispatch_trace(
        root: Union[torch.nn.Module, Callable],
        tracer: Tracer,
        concrete_args: Optional[Tuple[Any, ...]] = None,
) -> GraphModule:
    graph = tracer.trace(root, concrete_args)
    name = root.__class__.__name__ if isinstance(root, torch.nn.Module) else root.__name__
    return GraphModule(tracer.root, graph, name)


def wrap_key(f, inps, proxy_mode):
    flat_inps, _ = pytree.tree_flatten(inps)

    @functools.wraps(f)
    def wrapped(*args):
        flat_args, args_spec = pytree.tree_flatten(args)
        assert (len(flat_args) == len(flat_inps))
        for idx, arg in enumerate(flat_args):
            if isinstance(flat_inps[idx], torch.Tensor):
                with no_dispatch():
                    flat_args[idx] = ProxyTensor(
                        flat_inps[idx],
                        arg,
                        requires_grad=(flat_inps[idx].is_leaf and flat_inps[idx].requires_grad),
                        proxy_mode=proxy_mode,
                    )
            else:
                flat_args[idx] = flat_inps[idx]

        tree_args = pytree.tree_unflatten(flat_args, args_spec)
        out = f(*tree_args)
        flat_outs, out_spec = pytree.tree_flatten(out)
        for idx in range(len(flat_outs)):
            if isinstance(flat_outs[idx], torch.Tensor) and isinstance(flat_outs[idx], ProxyTensor):
                flat_outs[idx] = flat_outs[idx].proxy
        return pytree.tree_unflatten(flat_outs, out_spec)

    return wrapped


class ProxyTorchDispatchMode(TorchDispatchMode):
    def __init__(self, tracer):
        self.tracer = tracer
        self.enable_tracing = True

    def __torch_dispatch__(self, func_overload, types, args=(), kwargs=None):
        if not self.enable_tracing:
            return func_overload(*args, **kwargs)

        if symbolic_shapes.is_symbolic_op(func_overload):
            with self.restore():
                return symbolic_shapes.handle_symbolic_op(func_overload, args, kwargs)

        func = func_overload.overloadpacket
        # We don't want to convert torch.tensor constants into tracing objects.
        if func_overload == aten.lift.default:
            return args[0]

        if any(tuple(isinstance(arg, ProxyTensor) for arg in pytree.tree_flatten(args)[0])):
            out = proxy_call(self, func_overload, args, kwargs)
        # When we trace through a torch.tensor invocation, you never actually
        # see a torch.ops.aten.tensor call. Instead, the way this function is
        # implemented internally is that we allocate a plain tensor (this is
        # *guaranteed* to be a plain tensor, we disable all modes when doing
        # so), and then call at::lift_fresh on it (to give modes a chance to do
        # their stuff).  Furthermore, the tensor argument to lift_fresh is guaranteed
        # to be freshly allocated, so we want lift_fresh to be a no-op (directly
        # returning the input argument).
        #
        # Here is the basic problem: when we trace this sequence of executions
        # into an FX graph, what happens to this call sequence?  Traditionally,
        # tensor constants get interned as buffers on the FX GraphModule.  But
        # this is dangerous.  Consider:
        #
        #       x = torch.tensor(1)
        #       x.add_(2)
        #
        # Naively, this traces into:
        #
        #       t = self._tensor_constant0  # initialized to torch.tensor(1)
        #       x = torch.ops.aten.lift_fresh(t)
        #       x.add_(2)
        #
        # If lift_fresh returns t directly, the subsequent add_ call will
        # modify the tensor constant. Really, the problem is we've violated
        # the invariant the the argument to lift is fresh.  So what we should
        # preserve the invariant by replacing lift_fresh with lift_fresh_copy:
        #
        #       t = self._tensor_constant0  # initialized to torch.tensor(1)
        #       x = torch.ops.aten.lift_fresh_copy(t)
        #       x.add_(2)
        #
        # This is what the overload modification does.
        else:
            flat_args = pytree.tree_flatten((args, kwargs))[0]
            handled_types = [torch.Tensor, ProxyTensor, torch.nn.Parameter]

            # If there are any tensor subclasses, we need to handle those tensor subclasses first
            if any([isinstance(arg, torch.Tensor) and type(arg) not in handled_types for arg in flat_args]):
                return NotImplemented

            if func_overload is torch.ops.aten.lift_fresh.default:
                func_overload = torch.ops.aten.lift_fresh_copy.default

            proxy_res = self.tracer.create_proxy('call_function', func_overload, args, kwargs,
                                                 name=self.tracer.graph._target_to_str(func.__name__))

            inner_res = func_overload(*args, **kwargs)

            # If this is a lift, the input tensor is guaranteed to be a
            # constant, so we keep a copy of the original argument along so
            # we can query it if we're asked to item() it at some later point
            is_lift = func_overload is torch.ops.aten.lift_fresh_copy.default
            if is_lift:
                with maybe_disable_fake_tensor_mode():
                    constant = args[0].clone()
            else:
                constant = None
            out = wrap_output(inner_res, proxy_res, constant=constant, proxy_mode=self)

        def assert_proxy_tensor(e):
            if isinstance(e, torch.Tensor):
                assert isinstance(e, ProxyTensor), \
                    f"Internal Error: ProxyTensor is incorrectly baking a tensor constant into the graph: {str(e)}"

        # When we trace factory functions, we expect that tensor outputs are *always* ProxyTensors.
        # (Except for torch.tensor() constants handled through lift(), which is handled
        # specially further up).
        pytree.tree_map(assert_proxy_tensor, out)
        return out


class DecompositionInterpreter(torch.fx.Interpreter):
    def __init__(self, module: torch.fx.GraphModule, new_graph: torch.fx.Graph, decomposition_table=None, **kwargs):
        super().__init__(module, **kwargs)
        self.new_graph = new_graph
        self.tracer = torch.fx.proxy.GraphAppendingTracer(self.new_graph)
        self.decomposition_table = decomposition_table
        if self.decomposition_table is None:
            self.decomposition_table = {}
        self.mode = ProxyTorchDispatchMode(self.tracer)

    def placeholder(self, target, args, kwargs):
        out = super().placeholder(target, args, kwargs)
        # TODO handle case where the first character of target is '*'
        return ProxyTensor(out, torch.fx.Proxy(self.new_graph.placeholder(target), self.tracer), proxy_mode=self.mode)

    def get_attr(self, target, args, kwargs):
        out = super().get_attr(target, args, kwargs)
        return ProxyTensor(out, torch.fx.Proxy(self.new_graph.get_attr(target), self.tracer), proxy_mode=self.mode)

    # call_function, call_method, call_module get traced automatically by the ProxyTensors.

    def output(self, target, args, kwargs):
        out = super().output(target, args, kwargs)

        def unwrap(e):
            return e.proxy.node if isinstance(e, ProxyTensor) else e
        self.new_graph.output(pytree.tree_map(unwrap, out))
        return out

    def run(self, *args, **kwargs):
        # Should enter the mode at least once for being able to restore it later
        # See: https://github.com/pytorch/pytorch/pull/82549#discussion_r934782025
        with decompose(self.decomposition_table), self.mode:
            return super().run(*args, **kwargs)


def wrapper_and_args_for_make_fx(func, args, kwargs):
    # make_fx doesn't support kwargs, so we need to do this flattening
    # and then unflatten the args before calling func
    flat_args, spec = pytree.tree_flatten((args, kwargs))

    def wrapped(flat_args):
        fn_args, fn_kwargs = pytree.tree_unflatten(flat_args, spec)
        return func(*fn_args, **fn_kwargs)
    return wrapped, flat_args


def make_fx(f, decomposition_table=None, tracing_mode="real"):
    assert tracing_mode in ["real", "fake", "symbolic"]

    if decomposition_table is None:
        decomposition_table = {}

    @functools.wraps(f)
    def wrapped(*args):
        phs = pytree.tree_map(lambda _: fx.PH, args)  # type: ignore[attr-defined]
        fx_tracer = PythonKeyTracer()
        fake_tensor_mode: Any = nullcontext()
        if tracing_mode == "real":
            fake_tensor_mode = nullcontext()
        elif tracing_mode == "fake":
            fake_tensor_mode = FakeTensorMode(allow_fallback_kernels=True)
        elif tracing_mode == "symbolic":
            fake_tensor_mode = FakeTensorMode(allow_fallback_kernels=False)
        else:
            raise AssertionError(f"Unexpected tracing type: {tracing_mode}")

        proxy_mode = ProxyTorchDispatchMode(fx_tracer)

        def wrap_fake_concrete(x):
            if isinstance(x, torch.Tensor):
                return fake_tensor_mode.from_tensor(x)  # type: ignore[attr-defined]

            return x

        shape_env = ShapeEnv()

        # todo: Figure out a more informative name for symints
        def wrap_fake_symbolic(x, sym_shape):
            if isinstance(x, torch.Tensor):
                val = FakeTensor(fake_tensor_mode, torch.empty(sym_shape, device="meta"), x.device)
                return val
            return x

        wrap_fn_map = {
            "real": lambda x: x,
            "fake": wrap_fake_concrete,
        }
        if tracing_mode == "symbolic":
            flat_shapes = shape_env.create_shapes_for_args(args)
            flat_args, spec = pytree.tree_flatten(args)
            args = pytree.tree_unflatten(list(map(lambda a: wrap_fake_symbolic(a[0], a[1]), zip(flat_args, flat_shapes))), spec)
        else:
            args = pytree.tree_map(wrap_fn_map[tracing_mode], args)

        if not hasattr(f, '__code__') or inspect.unwrap(f).__code__.co_flags & inspect.CO_VARARGS:
            # FX doesn't support varargs, so we gotta fake up a wrapper
            # TODO: Would be nice to fix this at the source...
            func = fake_signature(f, len(phs))
        else:
            func = f

        with decompose(decomposition_table), fake_tensor_mode, proxy_mode:  # type: ignore[attr-defined]
            t = dispatch_trace(wrap_key(func, args, proxy_mode), tracer=fx_tracer, concrete_args=tuple(phs))

        # TODO: kind of a bad way to do it, should maybe figure out a better way
        t.shape_env = shape_env  # type: ignore[assignment]
        return t

    return wrapped


def get_torch_dispatch_modes():
    modes = [torch._C._get_torch_dispatch_mode()]
    if modes[-1] is None:
        return list()
    while modes[-1].inner is not None:
        modes.append(modes[-1].inner)
    return modes


@contextlib.contextmanager
def disable_proxy_modes_tracing():
    modes = get_torch_dispatch_modes()
    proxy_tensor_modes = [m for m in modes if isinstance(m, ProxyTorchDispatchMode)]
    olds = [m.enable_tracing for m in proxy_tensor_modes]
    for proxy_mode in proxy_tensor_modes:
        proxy_mode.enable_tracing = False
    try:
        yield
    finally:
        for proxy_mode, old in zip(proxy_tensor_modes, olds):
            proxy_mode.enable_tracing = old


def get_isolated_graphmodule(func, args, kwargs):
    """A helper function used to get the GraphModule for the given func.

    It's expected to be used in the ProxyTensor tracing context.
    It detaches the args and kwargs from the current tracer so that the trace of
    the current graph module can be created without any side-effects.
    """
    wrapped, all_args = wrapper_and_args_for_make_fx(func, args, kwargs)

    unwrapped_all_args = [unwrap_elem(a) for a in all_args]

    # Current implementation doesn't support the case when ProxyTensor is
    # wrapped with another Tensor subclass
    # See: https://github.com/pytorch/pytorch/pull/81764#issuecomment-1200472068
    # TODO: Once https://github.com/pytorch/pytorch/pull/82549 is merged, we can
    # remove this
    assert all(
        getattr(a, "elem", None) is None
        for a in unwrapped_all_args
        if isinstance(a, torch.Tensor)
    ), "ProxyTensor is wrapped with another Tensor subclass"

    with disable_proxy_modes_tracing():
        gm = make_fx(wrapped)(unwrapped_all_args)
    return gm
