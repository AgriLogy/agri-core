"""General sensor calibration: a user-written equation fitted to reference points.

agri-api #460. The affine ``real = raw * a + b`` of :mod:`agri.core.calibration`
covers decoding and unit conversion, but not field calibration: a capacitive
soil-moisture probe is quadratic or exponential against reference VWC, a 4-20 mA
transmitter maps through ``(x - 4) / 16 * span``, a wind vane needs a circular
offset. No fixed formula covers every sensor, so this module lets the formula
itself be the input.

The calibration is described by two things:

* **the equation** — any arithmetic expression in ``x``, the value the sensor
  *captured*, e.g. ``a*x^2 + b*x + c``, ``a*exp(b*x)``, ``(x + a) % 360``.
  Every other name in it is an unknown **coefficient**;
* **reference points** — pairs ``(captured, real)``: what the sensor reported
  next to what a reference instrument / certified standard says it should have
  reported.

:func:`fit_equation` solves for the coefficients that make the equation pass
closest to the reference points (least squares), and reports how good that
fit is (R², RMSE, max error, per-point residuals). An equation with no
coefficient (a fixed formula such as ``(x - 4) / 16 * 100``) is accepted too:
its reference points are then used for *verification* only.

**Safety.** The equation is user input that ends up evaluated on every reading
of the read path, so it is never handed to ``eval``. It is parsed with
:mod:`ast` against a whitelist (numbers, ``x``, coefficient names, ``+ - * /
% ^ **``, a fixed set of math functions) and compiled into a tree of closures.
Anything else — attribute access, subscripts, comparisons, lambdas, keyword
arguments, strings — is a :class:`EquationSyntaxError`. Its length and node
count are capped so a pathological expression cannot become a CPU sink.

**Fitting.** Levenberg–Marquardt with a numerical Jacobian. Each step solves the
damped linear least-squares problem by Householder QR (not the normal
equations, which would square the condition number of a polynomial in raw ADC
counts). A model linear in its coefficients — the common case — converges in
the first steps to the exact least-squares solution; non-linear models
(exponentials, power laws) need a sensible initial guess, which the caller may
pass.

Pure Python, no numpy: the problem is tiny (a handful of coefficients, a few
dozen points) and agri-core stays dependency-light.
"""

from __future__ import annotations

import ast
import functools
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from agri.core.calibration import CalibrationError

# ---------------------------------------------------------------------------
# 1. Errors
# ---------------------------------------------------------------------------


class EquationSyntaxError(CalibrationError):
    """The equation does not parse, or uses something outside the whitelist."""


class EquationEvaluationError(CalibrationError):
    """The equation could not be evaluated at a given point: a domain error
    (``log`` of a negative, ``sqrt`` of a negative, division by zero), an
    overflow, a non-finite result, or a coefficient with no value."""


class CalibrationFitError(CalibrationError):
    """The coefficients cannot be fitted from the given reference points."""


class InsufficientReferencePointsError(CalibrationFitError):
    """Fewer reference points than unknown coefficients: the system is
    under-determined, so infinitely many coefficient sets fit equally well."""


# ---------------------------------------------------------------------------
# 2. The grammar
# ---------------------------------------------------------------------------

VARIABLE = "x"
"""The captured (raw) sensor value inside an equation."""

MAX_EQUATION_LENGTH = 500
MAX_EQUATION_NODES = 300
MAX_COEFFICIENTS = 10
MAX_REFERENCE_POINTS = 500


def _safe_pow(base: float, exponent: float) -> float:
    # ``math.pow`` (unlike ``**``) raises ValueError for a negative base with a
    # fractional exponent instead of returning a complex, and OverflowError on
    # overflow — both surface as EquationEvaluationError.
    return math.pow(base, exponent)


FUNCTIONS: dict[str, tuple[Callable[..., float], int | tuple[int, int]]] = {
    # name: (implementation, arity) — arity is an int or an inclusive range.
    "sqrt": (math.sqrt, 1),
    "exp": (math.exp, 1),
    "ln": (math.log, 1),
    "log": (math.log, 1),
    "log10": (math.log10, 1),
    "log2": (math.log2, 1),
    "abs": (abs, 1),
    "sin": (math.sin, 1),
    "cos": (math.cos, 1),
    "tan": (math.tan, 1),
    "asin": (math.asin, 1),
    "acos": (math.acos, 1),
    "atan": (math.atan, 1),
    "atan2": (math.atan2, 2),
    "sinh": (math.sinh, 1),
    "cosh": (math.cosh, 1),
    "tanh": (math.tanh, 1),
    "radians": (math.radians, 1),
    "degrees": (math.degrees, 1),
    "floor": (math.floor, 1),
    "ceil": (math.ceil, 1),
    "round": (round, 1),
    "pow": (_safe_pow, 2),
    "min": (min, (2, 8)),
    "max": (max, (2, 8)),
}
"""Whitelisted functions. Trigonometry is in radians, as everywhere in maths;
``radians()`` / ``degrees()`` convert. ``log`` and ``ln`` are both the natural
logarithm."""

CONSTANTS: dict[str, float] = {"pi": math.pi, "e": math.e}
"""Reserved names that are constants, not coefficients."""

RESERVED_NAMES = frozenset({VARIABLE, *FUNCTIONS, *CONSTANTS})

_BINARY_OPS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Pow: _safe_pow,
    # Python's ``%`` takes the sign of the divisor, so ``(x + a) % 360`` is
    # always in [0, 360) — exactly the circular offset a wind vane needs.
    ast.Mod: lambda a, b: math.fmod(math.fmod(a, b) + b, b),
}

_UNARY_OPS: dict[type[ast.unaryop], Callable[[float], float]] = {
    ast.UAdd: lambda a: +a,
    ast.USub: lambda a: -a,
}

# An evaluator takes (x, coefficients) and returns a float.
_Evaluator = Callable[[float, Mapping[str, float]], float]


def normalize_equation(equation: str) -> str:
    """Canonical text of an equation: trimmed, ``^`` as power, ``×``/``÷``/``−``
    and a decimal comma inside a number left to the caller (they are ambiguous
    with argument separators). Also accepts a leading ``y =`` / ``real =``.
    """
    text = (equation or "").strip()
    for prefix in ("y=", "real=", "f(x)="):
        compact = text.replace(" ", "")
        if compact.lower().startswith(prefix):
            text = text.split("=", 1)[1].strip()
            break
    return (
        text.replace("^", "**")
        .replace("×", "*")
        .replace("·", "*")
        .replace("÷", "/")
        .replace("−", "-")
    )


@dataclass(frozen=True)
class CompiledEquation:
    """A parsed, whitelisted, ready-to-evaluate equation.

    ``coefficients`` are the unknown names, in order of first appearance — the
    order a UI should list them in.
    """

    source: str
    coefficients: tuple[str, ...]
    _evaluator: _Evaluator = field(repr=False, compare=False)

    def evaluate(self, x: float, coefficients: Mapping[str, float] | None = None) -> float:
        """``f(x; coefficients)``. Raises :class:`EquationEvaluationError` on any
        domain error or non-finite result — never returns NaN/inf, which would
        otherwise be stored or charted as if it were a measurement."""
        values = coefficients or {}
        missing = [name for name in self.coefficients if name not in values]
        if missing:
            raise EquationEvaluationError(
                f"No value for coefficient(s) {', '.join(missing)} in {self.source!r}."
            )
        try:
            result = self._evaluator(float(x), values)
        except (ValueError, ZeroDivisionError, OverflowError, TypeError) as exc:
            raise EquationEvaluationError(
                f"{self.source!r} cannot be evaluated at x={x!r}: {exc}"
            ) from exc
        if isinstance(result, complex) or not math.isfinite(result):
            raise EquationEvaluationError(f"{self.source!r} gives a non-finite result at x={x!r}.")
        return float(result)


def compile_equation(equation: str) -> CompiledEquation:
    """Parse and whitelist-check ``equation``.

    >>> compile_equation("a*x^2 + b*x + c").coefficients
    ('a', 'b', 'c')
    """
    source = normalize_equation(equation)
    if not source:
        raise EquationSyntaxError("The equation is empty.")
    if len(source) > MAX_EQUATION_LENGTH:
        raise EquationSyntaxError(f"The equation is longer than {MAX_EQUATION_LENGTH} characters.")
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise EquationSyntaxError(f"Invalid equation {equation!r}: {exc.msg}.") from exc

    node_count = sum(1 for _ in ast.walk(tree))
    if node_count > MAX_EQUATION_NODES:
        raise EquationSyntaxError("The equation is too complex.")

    coefficients: list[str] = []
    uses_variable = False

    def build(node: ast.AST) -> _Evaluator:
        nonlocal uses_variable
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, int | float):
                raise EquationSyntaxError(f"Unsupported literal {node.value!r}.")
            value = float(node.value)
            return lambda _x, _c: value
        if isinstance(node, ast.Name):
            name = node.id
            if name == VARIABLE:
                uses_variable = True
                return lambda x, _c: x
            if name in CONSTANTS:
                constant = CONSTANTS[name]
                return lambda _x, _c: constant
            if name in FUNCTIONS:
                raise EquationSyntaxError(f"{name!r} is a function; call it as {name}(...).")
            if name.startswith("_"):
                raise EquationSyntaxError(f"Invalid coefficient name {name!r}.")
            if name not in coefficients:
                coefficients.append(name)
            return lambda _x, c: c[name]
        if isinstance(node, ast.BinOp):
            op = _BINARY_OPS.get(type(node.op))
            if op is None:
                raise EquationSyntaxError(f"Operator {type(node.op).__name__} is not allowed.")
            left, right = build(node.left), build(node.right)
            return lambda x, c: op(left(x, c), right(x, c))
        if isinstance(node, ast.UnaryOp):
            unary = _UNARY_OPS.get(type(node.op))
            if unary is None:
                raise EquationSyntaxError(f"Operator {type(node.op).__name__} is not allowed.")
            operand = build(node.operand)
            return lambda x, c: unary(operand(x, c))
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                name = node.func.id if isinstance(node.func, ast.Name) else "?"
                allowed = ", ".join(sorted(FUNCTIONS))
                raise EquationSyntaxError(f"Unknown function {name!r}. Allowed: {allowed}.")
            if node.keywords or any(isinstance(a, ast.Starred) for a in node.args):
                raise EquationSyntaxError("Function arguments must be plain expressions.")
            impl, arity = FUNCTIONS[node.func.id]
            low, high = (arity, arity) if isinstance(arity, int) else arity
            if not low <= len(node.args) <= high:
                raise EquationSyntaxError(
                    f"{node.func.id}() takes {low if low == high else f'{low}-{high}'} "
                    f"argument(s), got {len(node.args)}."
                )
            args = [build(arg) for arg in node.args]
            return lambda x, c: impl(*(arg(x, c) for arg in args))
        raise EquationSyntaxError(
            f"{type(node).__name__} is not allowed in a calibration equation."
        )

    evaluator = build(tree.body)
    if not uses_variable:
        raise EquationSyntaxError(f"The equation must use {VARIABLE!r}, the captured sensor value.")
    if len(coefficients) > MAX_COEFFICIENTS:
        raise EquationSyntaxError(
            f"At most {MAX_COEFFICIENTS} coefficients can be fitted, got {len(coefficients)}."
        )
    return CompiledEquation(source=source, coefficients=tuple(coefficients), _evaluator=evaluator)


# ---------------------------------------------------------------------------
# 3. Small dense linear algebra (Householder QR least squares)
# ---------------------------------------------------------------------------


def _lstsq(matrix: list[list[float]], rhs: list[float]) -> tuple[list[float], list[float]]:
    """Least-squares solution of ``matrix @ v ≈ rhs`` (rows ≥ cols) by
    Householder QR. Returns ``(v, diag(R))``; a tiny ``|R_ii|`` flags a
    rank-deficient column, i.e. a coefficient the data cannot determine."""
    rows, cols = len(matrix), len(matrix[0])
    a = [row[:] for row in matrix]
    b = rhs[:]
    for k in range(cols):
        norm = math.sqrt(sum(a[i][k] ** 2 for i in range(k, rows)))
        if norm == 0.0:
            continue
        alpha = -norm if a[k][k] >= 0 else norm
        v = [0.0] * rows
        v[k] = a[k][k] - alpha
        for i in range(k + 1, rows):
            v[i] = a[i][k]
        vnorm2 = sum(v[i] ** 2 for i in range(k, rows))
        if vnorm2 == 0.0:
            continue
        for j in range(k, cols):
            dot = sum(v[i] * a[i][j] for i in range(k, rows))
            factor = 2.0 * dot / vnorm2
            for i in range(k, rows):
                a[i][j] -= factor * v[i]
        dot = sum(v[i] * b[i] for i in range(k, rows))
        factor = 2.0 * dot / vnorm2
        for i in range(k, rows):
            b[i] -= factor * v[i]
    diag = [a[k][k] for k in range(cols)]
    scale = max((abs(d) for d in diag), default=0.0)
    solution = [0.0] * cols
    for k in range(cols - 1, -1, -1):
        if abs(a[k][k]) <= 1e-12 * max(scale, 1e-300):
            solution[k] = 0.0  # undetermined direction: leave it unmoved
            continue
        total = b[k] - sum(a[k][j] * solution[j] for j in range(k + 1, cols))
        solution[k] = total / a[k][k]
    return solution, diag


# ---------------------------------------------------------------------------
# 4. Reference points, metrics and the fit
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferencePoint:
    """One field / bench measurement: the sensor reported ``captured`` while the
    reference instrument said the truth was ``real``."""

    captured: float
    real: float

    @classmethod
    def coerce(cls, value: object) -> ReferencePoint:
        """Accept a :class:`ReferencePoint`, a ``(captured, real)`` pair, or a
        mapping with ``captured``/``real`` (``raw``/``reference`` also accepted)."""
        if isinstance(value, ReferencePoint):
            return value
        if isinstance(value, Mapping):
            captured = value.get("captured", value.get("raw"))
            real = value.get("real", value.get("reference"))
        elif isinstance(value, Sequence) and not isinstance(value, str) and len(value) == 2:
            captured, real = value
        else:
            raise CalibrationFitError(f"Invalid reference point {value!r}.")
        try:
            point = cls(captured=float(captured), real=float(real))  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise CalibrationFitError(f"Invalid reference point {value!r}.") from exc
        if not (math.isfinite(point.captured) and math.isfinite(point.real)):
            raise CalibrationFitError(f"Reference point {value!r} is not finite.")
        return point


@dataclass(frozen=True)
class PointResidual:
    captured: float
    real: float
    predicted: float
    residual: float  # predicted - real


@dataclass(frozen=True)
class FitResult:
    """Outcome of :func:`fit_equation`.

    ``coefficients`` is what gets stored and applied. The quality metrics let a
    technician judge the calibration before activating it: ``r2`` (``None``
    when every reference value is identical, so variance is zero), ``rmse``,
    ``mae`` and ``max_abs_error`` in the unit of the real value, and the
    per-point residuals to spot an outlier reading.
    """

    equation: str
    coefficients: dict[str, float]
    points: tuple[PointResidual, ...]
    rmse: float
    mae: float
    max_abs_error: float
    r2: float | None
    degrees_of_freedom: int
    converged: bool
    iterations: int
    warnings: tuple[str, ...] = ()

    def metrics(self) -> dict[str, object]:
        """The JSON-ready summary stored alongside a calibration."""
        return {
            "n_points": len(self.points),
            "rmse": self.rmse,
            "mae": self.mae,
            "max_abs_error": self.max_abs_error,
            "r2": self.r2,
            "degrees_of_freedom": self.degrees_of_freedom,
            "converged": self.converged,
            "iterations": self.iterations,
            "warnings": list(self.warnings),
        }


def _residuals(
    compiled: CompiledEquation,
    points: Sequence[ReferencePoint],
    values: Mapping[str, float],
) -> list[float]:
    return [compiled.evaluate(p.captured, values) - p.real for p in points]


def _cost(residuals: Sequence[float]) -> float:
    return sum(r * r for r in residuals)


def _jacobian(
    compiled: CompiledEquation,
    points: Sequence[ReferencePoint],
    names: Sequence[str],
    values: dict[str, float],
) -> list[list[float]]:
    """Central-difference Jacobian ``∂f(x_i)/∂θ_j``. Exact (to rounding) for a
    model linear in its coefficients, which is most real calibrations."""
    columns: list[list[float]] = []
    for name in names:
        theta = values[name]
        step = 1e-6 * max(1.0, abs(theta))
        plus = dict(values, **{name: theta + step})
        minus = dict(values, **{name: theta - step})
        try:
            f_plus = [compiled.evaluate(p.captured, plus) for p in points]
            f_minus = [compiled.evaluate(p.captured, minus) for p in points]
            columns.append(
                [(fp - fm) / (2.0 * step) for fp, fm in zip(f_plus, f_minus, strict=True)]
            )
        except EquationEvaluationError:
            # One-sided difference when the coefficient sits on a domain edge.
            base = [compiled.evaluate(p.captured, values) for p in points]
            f_plus = [compiled.evaluate(p.captured, plus) for p in points]
            columns.append([(fp - fb) / step for fp, fb in zip(f_plus, base, strict=True)])
    return [[columns[j][i] for j in range(len(names))] for i in range(len(points))]


def _summarize(
    compiled: CompiledEquation,
    points: Sequence[ReferencePoint],
    values: Mapping[str, float],
    *,
    converged: bool,
    iterations: int,
    warnings: list[str],
) -> FitResult:
    predicted = [compiled.evaluate(p.captured, values) for p in points]
    residuals = [f - p.real for f, p in zip(predicted, points, strict=True)]
    n = len(points)
    sse = _cost(residuals)
    mean_real = sum(p.real for p in points) / n
    sst = sum((p.real - mean_real) ** 2 for p in points)
    r2 = None if sst == 0.0 else 1.0 - sse / sst
    dof = n - len(compiled.coefficients)
    if dof == 0 and compiled.coefficients:
        warnings.append(
            "As many reference points as coefficients: the curve passes through "
            "every point exactly, so the metrics say nothing about accuracy. "
            "Add at least one more point to verify the calibration."
        )
    return FitResult(
        equation=compiled.source,
        coefficients={name: float(values[name]) for name in compiled.coefficients},
        points=tuple(
            PointResidual(p.captured, p.real, f, r)
            for p, f, r in zip(points, predicted, residuals, strict=True)
        ),
        rmse=math.sqrt(sse / n),
        mae=sum(abs(r) for r in residuals) / n,
        max_abs_error=max(abs(r) for r in residuals),
        r2=r2,
        degrees_of_freedom=dof,
        converged=converged,
        iterations=iterations,
        warnings=tuple(warnings),
    )


def fit_equation(
    equation: str | CompiledEquation,
    reference_points: Sequence[object],
    *,
    initial: Mapping[str, float] | None = None,
    max_iterations: int = 200,
) -> FitResult:
    """Fit the coefficients of ``equation`` to ``reference_points``.

    Minimises ``Σ (f(captured_i; θ) - real_i)²`` by Levenberg–Marquardt.
    ``initial`` seeds the search (missing names start at ``1.0``); only
    non-linear models are sensitive to it.

    With no coefficient in the equation nothing is fitted and the result is a
    pure verification of the fixed formula against the points.

    Raises :class:`InsufficientReferencePointsError` when there are fewer
    points than coefficients, :class:`CalibrationFitError` when the equation
    cannot even be evaluated at the starting coefficients, and
    :class:`EquationSyntaxError` for an invalid equation.

    >>> fit = fit_equation("a*x + b", [(0, 1), (1, 3), (2, 5)])
    >>> round(fit.coefficients["a"], 9), round(fit.coefficients["b"], 9)
    (2.0, 1.0)
    """
    compiled = equation if isinstance(equation, CompiledEquation) else compile_equation(equation)
    points = [ReferencePoint.coerce(p) for p in reference_points]
    if not points:
        raise InsufficientReferencePointsError("At least one reference point is required.")
    if len(points) > MAX_REFERENCE_POINTS:
        raise CalibrationFitError(f"At most {MAX_REFERENCE_POINTS} reference points are accepted.")
    names = list(compiled.coefficients)
    if len(points) < len(names):
        raise InsufficientReferencePointsError(
            f"{len(names)} coefficient(s) ({', '.join(names)}) need at least "
            f"{len(names)} reference points; got {len(points)}."
        )

    seed = dict(initial or {})
    values = {name: float(seed.get(name, 1.0)) for name in names}
    warnings: list[str] = []

    try:
        residuals = _residuals(compiled, points, values)
    except EquationEvaluationError as exc:
        raise CalibrationFitError(
            f"The equation cannot be evaluated at the starting coefficients {values}: "
            f"{exc} Provide initial values closer to the expected result."
        ) from exc

    if not names:
        return _summarize(compiled, points, values, converged=True, iterations=0, warnings=warnings)

    cost = _cost(residuals)
    damping = 1e-3
    converged = False
    iterations = 0
    diag: list[float] = []
    for iterations in range(1, max_iterations + 1):  # noqa: B007 - read after loop
        jac = _jacobian(compiled, points, names, values)
        # Marquardt scaling: damp each direction by its own curvature so the
        # step is invariant to the coefficient's magnitude (a ~1e-4, c ~1e2).
        col_norms = [
            math.sqrt(sum(jac[i][j] ** 2 for i in range(len(points)))) or 1.0
            for j in range(len(names))
        ]
        improved = False
        while damping < 1e16:
            augmented = [row[:] for row in jac] + [
                [math.sqrt(damping) * col_norms[j] if k == j else 0.0 for k in range(len(names))]
                for j in range(len(names))
            ]
            rhs = [-r for r in residuals] + [0.0] * len(names)
            step, _ = _lstsq(augmented, rhs)
            candidate = {name: values[name] + step[j] for j, name in enumerate(names)}
            try:
                new_residuals = _residuals(compiled, points, candidate)
            except EquationEvaluationError:
                damping *= 10.0
                continue
            new_cost = _cost(new_residuals)
            if new_cost <= cost:
                step_size = max(
                    abs(step[j]) / max(1.0, abs(values[name])) for j, name in enumerate(names)
                )
                relative_drop = (cost - new_cost) / max(cost, 1e-300)
                values, residuals, cost = candidate, new_residuals, new_cost
                damping = max(damping / 10.0, 1e-15)
                improved = True
                if step_size < 1e-12 or relative_drop < 1e-15 or cost < 1e-24:
                    converged = True
                break
            damping *= 10.0
        if not improved:
            # No step reduces the cost even with heavy damping: a (local) minimum.
            converged = True
            break
        if converged:
            break

    _, diag = _lstsq(_jacobian(compiled, points, names, values), [0.0] * len(points))
    scale = max((abs(d) for d in diag), default=0.0)
    for name, d in zip(names, diag, strict=True):
        if abs(d) <= 1e-9 * max(scale, 1e-300):
            warnings.append(
                f"Coefficient {name!r} is not determined by these points "
                "(redundant with another coefficient, or the points do not vary "
                "enough). Its value is arbitrary."
            )
    if not converged:
        warnings.append(
            f"The fit did not converge in {max_iterations} iterations; "
            "try different initial values."
        )
    return _summarize(
        compiled, points, values, converged=converged, iterations=iterations, warnings=warnings
    )


def verify_equation(
    equation: str | CompiledEquation,
    coefficients: Mapping[str, float],
    reference_points: Sequence[object],
) -> FitResult:
    """Score a calibration with *given* coefficients against reference points,
    without refitting — e.g. re-checking a stored calibration against a fresh
    bench measurement."""
    compiled = equation if isinstance(equation, CompiledEquation) else compile_equation(equation)
    points = [ReferencePoint.coerce(p) for p in reference_points]
    if not points:
        raise InsufficientReferencePointsError("At least one reference point is required.")
    return _summarize(
        compiled,
        points,
        {name: float(v) for name, v in coefficients.items()},
        converged=True,
        iterations=0,
        warnings=[],
    )


__all__ = [
    "EquationSyntaxError",
    "EquationEvaluationError",
    "CalibrationFitError",
    "InsufficientReferencePointsError",
    "VARIABLE",
    "FUNCTIONS",
    "CONSTANTS",
    "RESERVED_NAMES",
    "MAX_EQUATION_LENGTH",
    "MAX_COEFFICIENTS",
    "MAX_REFERENCE_POINTS",
    "normalize_equation",
    "CompiledEquation",
    "compile_equation",
    "ReferencePoint",
    "PointResidual",
    "FitResult",
    "fit_equation",
    "verify_equation",
]


@functools.lru_cache(maxsize=1024)
def compile_equation_cached(equation: str) -> CompiledEquation:
    """:func:`compile_equation`, memoised by text. The read path applies the
    same few stored equations to thousands of readings; parsing once per
    distinct equation keeps that cost flat."""
    return compile_equation(equation)


__all__ += ["compile_equation_cached"]
