"""Tests for equation-based calibration (agri-api #460).

Three families:

* **the grammar** — what parses, what is refused (anything that could escape
  into Python), and how coefficients are discovered;
* **the fit** — exact recovery of known coefficients for linear-in-parameter
  and genuinely non-linear models, metrics, and the under-determined / domain
  failure modes;
* **integration** with :class:`agri.core.calibration.Calibration` — the read
  path's ``apply_calibration`` / ``calibrated_value`` / unit retargeting, and
  that affine calibrations stored before #460 are untouched.
"""

from __future__ import annotations

import math

import pytest

from agri.core.calibration import (
    Calibration,
    NonInvertibleCalibrationError,
    apply_calibration,
    calibrated_value,
    calibration_in_unit,
    invert_calibration,
)
from agri.core.calibration_equation import (
    CalibrationFitError,
    EquationEvaluationError,
    EquationSyntaxError,
    InsufficientReferencePointsError,
    ReferencePoint,
    compile_equation,
    compile_equation_cached,
    fit_equation,
    normalize_equation,
    verify_equation,
)

# ---------------------------------------------------------------------------
# Grammar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("equation", "coefficients"),
    [
        ("a*x + b", ("a", "b")),
        ("a*x^2 + b*x + c", ("a", "b", "c")),
        ("c + b*x + a*x**2", ("c", "b", "a")),  # order of first appearance
        ("(x - 4) / 16 * 100", ()),
        ("a*exp(b*x)", ("a", "b")),
        ("(x + offset) % 360", ("offset",)),
        ("gain * log10(x) + zero", ("gain", "zero")),
        ("k * pi * x + e", ("k",)),  # pi and e are constants, not coefficients
        ("y = 2*x", ()),
        ("real = a × x − b", ("a", "b")),
        ("max(x, 0) * a", ("a",)),
    ],
)
def test_parses_and_discovers_coefficients(equation, coefficients):
    assert compile_equation(equation).coefficients == coefficients


@pytest.mark.parametrize(
    "equation",
    [
        "",
        "   ",
        "__import__('os').system('true')",
        "x.real",
        "x[0]",
        "lambda: x",
        "x if a else b",
        "x > 1",
        "open('f')",
        "'abc' + x",
        "sqrt(x=1)",
        "sqrt(x, x)",
        "a*b",  # no x — not a calibration of the captured value
        "x + _hidden",
        "sqrt * x",
        "x +",
        "True * x",
        "x" + " + x" * 400,  # too long
    ],
)
def test_refuses_anything_outside_the_whitelist(equation):
    with pytest.raises(EquationSyntaxError):
        compile_equation(equation)


def test_too_many_coefficients_is_refused():
    equation = " + ".join(f"c{i}*x**{i}" for i in range(11))
    with pytest.raises(EquationSyntaxError, match="At most"):
        compile_equation(equation)


def test_normalize_equation_handles_caret_and_prefix():
    assert normalize_equation(" y = a*x^2 ") == "a*x**2"


def test_evaluate_known_values():
    eq = compile_equation("(x - 4) / 16 * 100")
    assert eq.evaluate(4) == 0.0
    assert eq.evaluate(12) == 50.0
    assert eq.evaluate(20) == 100.0


def test_modulo_is_circular_for_wind_direction():
    eq = compile_equation("(x + a) % 360")
    assert eq.evaluate(350, {"a": 20}) == pytest.approx(10.0)
    assert eq.evaluate(5, {"a": -20}) == pytest.approx(345.0)


@pytest.mark.parametrize(
    ("equation", "x"),
    [("log(x)", -1.0), ("sqrt(x)", -4.0), ("1 / x", 0.0), ("exp(x)", 1e6), ("x ^ 0.5", -2.0)],
)
def test_domain_errors_raise_instead_of_returning_nan(equation, x):
    with pytest.raises(EquationEvaluationError):
        compile_equation(equation).evaluate(x)


def test_missing_coefficient_value_raises():
    with pytest.raises(EquationEvaluationError, match="b"):
        compile_equation("a*x + b").evaluate(1.0, {"a": 1.0})


def test_compile_cache_returns_same_object():
    assert compile_equation_cached("a*x+b") is compile_equation_cached("a*x+b")


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------


def test_linear_fit_recovers_exact_coefficients():
    points = [(x, 2.5 * x - 3.0) for x in (0, 1, 2, 5, 10)]
    fit = fit_equation("a*x + b", points)
    assert fit.coefficients["a"] == pytest.approx(2.5, abs=1e-9)
    assert fit.coefficients["b"] == pytest.approx(-3.0, abs=1e-9)
    assert fit.rmse == pytest.approx(0.0, abs=1e-9)
    assert fit.r2 == pytest.approx(1.0)
    assert fit.converged
    assert fit.degrees_of_freedom == 3


def test_quadratic_fit_on_raw_adc_counts_is_well_conditioned():
    # Capacitive soil-moisture probe: raw counts in the thousands, VWC in %.
    a, b, c = -1.2e-6, 0.021, -18.0
    xs = [1200, 1800, 2500, 3100, 3700, 4200, 4800]
    fit = fit_equation("a*x^2 + b*x + c", [(x, a * x * x + b * x + c) for x in xs])
    assert fit.coefficients["a"] == pytest.approx(a, rel=1e-6)
    assert fit.coefficients["b"] == pytest.approx(b, rel=1e-6)
    assert fit.coefficients["c"] == pytest.approx(c, rel=1e-6)
    assert fit.max_abs_error < 1e-6


def test_least_squares_with_noise_matches_closed_form():
    points = [(0, 1.1), (1, 2.9), (2, 5.2), (3, 6.8), (4, 9.1)]
    fit = fit_equation("a*x + b", points)
    # Closed-form ordinary least squares.
    n = len(points)
    sx = sum(x for x, _ in points)
    sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points)
    sxy = sum(x * y for x, y in points)
    slope = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    intercept = (sy - slope * sx) / n
    assert fit.coefficients["a"] == pytest.approx(slope, abs=1e-9)
    assert fit.coefficients["b"] == pytest.approx(intercept, abs=1e-9)
    assert 0.99 < fit.r2 < 1.0
    assert fit.rmse > 0
    assert sum(p.residual for p in fit.points) == pytest.approx(0.0, abs=1e-9)


def test_nonlinear_exponential_fit():
    a, b = 3.0, 0.4
    points = [(x / 2, a * math.exp(b * x / 2)) for x in range(10)]
    fit = fit_equation("a*exp(b*x)", points, initial={"a": 1.0, "b": 0.1})
    assert fit.coefficients["a"] == pytest.approx(a, rel=1e-6)
    assert fit.coefficients["b"] == pytest.approx(b, rel=1e-6)
    assert fit.converged


def test_power_law_fit():
    points = [(x, 0.5 * x**1.7) for x in (1, 2, 3, 5, 8, 13)]
    fit = fit_equation("k * x^n", points, initial={"k": 1, "n": 1})
    assert fit.coefficients["k"] == pytest.approx(0.5, rel=1e-6)
    assert fit.coefficients["n"] == pytest.approx(1.7, rel=1e-6)


def test_circular_offset_fit_for_wind_vane():
    true_offset = 12.0
    points = [(x, (x + true_offset) % 360) for x in (10, 90, 180, 270)]
    fit = fit_equation("(x + a) % 360", points, initial={"a": 0})
    assert fit.coefficients["a"] == pytest.approx(true_offset, abs=1e-6)


def test_single_point_offset_correction():
    fit = fit_equation("x + b", [(20.4, 20.0)])
    assert fit.coefficients["b"] == pytest.approx(-0.4)
    assert fit.degrees_of_freedom == 0
    assert any("exactly" in w for w in fit.warnings)


def test_fixed_formula_is_verified_not_fitted():
    fit = fit_equation("(x - 4) / 16 * 100", [(4, 0.0), (12, 51.0), (20, 100.0)])
    assert fit.coefficients == {}
    assert fit.iterations == 0
    assert fit.max_abs_error == pytest.approx(1.0)
    assert [p.predicted for p in fit.points] == [0.0, 50.0, 100.0]


def test_too_few_points_raises():
    with pytest.raises(InsufficientReferencePointsError, match="3"):
        fit_equation("a*x^2 + b*x + c", [(1, 1), (2, 2)])
    with pytest.raises(InsufficientReferencePointsError):
        fit_equation("(x-4)/16", [])


def test_unidentifiable_coefficients_are_flagged():
    fit = fit_equation("a*b*x", [(1, 2), (2, 4), (3, 6)])
    # a*b is determined, a and b individually are not.
    assert fit.coefficients["a"] * fit.coefficients["b"] == pytest.approx(2.0, rel=1e-6)
    assert any("not determined" in w for w in fit.warnings)


def test_identical_reference_values_have_no_r2():
    fit = fit_equation("x + b", [(1, 5), (2, 5), (3, 5)])
    assert fit.r2 is None


def test_bad_starting_point_raises_a_fit_error():
    with pytest.raises(CalibrationFitError, match="initial"):
        fit_equation("log(x - a)", [(1, 0), (2, 1)], initial={"a": 5})


@pytest.mark.parametrize(
    "point",
    [{"captured": 1, "real": 2}, {"raw": 1, "reference": 2}, (1, 2), ReferencePoint(1, 2)],
)
def test_reference_point_shapes(point):
    assert ReferencePoint.coerce(point) == ReferencePoint(1.0, 2.0)


@pytest.mark.parametrize("point", [{"captured": "x"}, (1,), (1, float("nan")), "12"])
def test_invalid_reference_points(point):
    with pytest.raises(CalibrationFitError):
        ReferencePoint.coerce(point)


def test_verify_equation_scores_without_refitting():
    result = verify_equation("a*x + b", {"a": 2.0, "b": 0.0}, [(1, 2.5), (2, 4.0)])
    assert result.coefficients == {"a": 2.0, "b": 0.0}
    assert result.max_abs_error == pytest.approx(0.5)


def test_metrics_are_json_ready():
    metrics = fit_equation("a*x + b", [(0, 0), (1, 1), (2, 2)]).metrics()
    assert set(metrics) >= {"n_points", "rmse", "mae", "max_abs_error", "r2", "warnings"}
    assert metrics["n_points"] == 3


# ---------------------------------------------------------------------------
# Integration with Calibration
# ---------------------------------------------------------------------------


QUADRATIC = Calibration(
    equation="a*x^2 + b*x + c", coefficients={"a": 0.01, "b": 1.0, "c": -2.0}, unit="°C"
)


def test_apply_equation_calibration():
    assert apply_calibration(10.0, QUADRATIC) == pytest.approx(9.0)


def test_equation_replaces_affine_coefficients():
    cal = Calibration(scale_a=5.0, offset_b=5.0, equation="x + 1")
    assert apply_calibration(1.0, cal) == 2.0


def test_inactive_equation_returns_raw():
    from dataclasses import replace

    assert apply_calibration(10.0, replace(QUADRATIC, is_active=False)) == 10.0


def test_none_stays_none():
    assert apply_calibration(None, QUADRATIC) is None


def test_equation_is_never_identity():
    assert not Calibration(equation="x").is_identity
    assert Calibration().is_identity


def test_affine_calibrations_unchanged():
    cal = Calibration(scale_a=2.0, offset_b=1.0)
    assert not cal.is_equation
    assert apply_calibration(10.0, cal) == 21.0


def test_equation_domain_error_propagates_from_apply():
    with pytest.raises(EquationEvaluationError):
        apply_calibration(-1.0, Calibration(equation="sqrt(x)"))


def test_equation_calibration_in_unit_composes_exactly():
    in_f = calibration_in_unit(QUADRATIC, "°F")
    assert in_f.unit == "°F"
    assert in_f.coefficients == QUADRATIC.coefficients
    assert apply_calibration(10.0, in_f) == pytest.approx(9.0 * 1.8 + 32.0)
    # Same unit: the equation is left alone, not wrapped.
    assert calibration_in_unit(QUADRATIC, "°C").equation == QUADRATIC.equation


def test_calibrated_value_converts_equation_output():
    assert calibrated_value(10.0, QUADRATIC, target_unit="K") == pytest.approx(9.0 + 273.15)


def test_equation_calibration_is_not_invertible():
    with pytest.raises(NonInvertibleCalibrationError):
        invert_calibration(9.0, QUADRATIC)


class _Row:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_from_row_reads_equation_and_json_coefficients():
    row = _Row(
        scale_a=1.0,
        offset_b=0.0,
        unit="%",
        is_active=True,
        equation="a*x + b",
        coefficients='{"a": 0.5, "b": 1}',
    )
    cal = Calibration.from_row(row)
    assert cal.is_equation
    assert cal.coefficients == {"a": 0.5, "b": 1.0}
    assert apply_calibration(4.0, cal) == 3.0


def test_from_row_without_equation_columns_is_affine():
    cal = Calibration.from_row(_Row(scale_a=2.0, offset_b=0.0, unit="", is_active=True))
    assert not cal.is_equation
    assert cal.coefficients == {}
