"""
测试 validators
"""
import pytest
from mech_kernel.validators import (
    require_positive, require_non_negative, require_finite,
    require_tuple3, require_tuple2, require_non_empty_str,
    require_in, require_positive_int
)
from mech_kernel.errors import InvalidRequestError


def test_require_positive():
    assert require_positive("x", 5) == 5
    assert require_positive("x", 0.1) == 0.1
    with pytest.raises(InvalidRequestError):
        require_positive("x", 0)
    with pytest.raises(InvalidRequestError):
        require_positive("x", -1)


def test_require_non_negative():
    assert require_non_negative("x", 0) == 0
    assert require_non_negative("x", 5) == 5
    with pytest.raises(InvalidRequestError):
        require_non_negative("x", -1)


def test_require_finite():
    assert require_finite("x", 1.5) == 1.5
    with pytest.raises(InvalidRequestError):
        require_finite("x", float("inf"))


def test_require_tuple3():
    assert require_tuple3("v", (1, 2, 3)) == (1.0, 2.0, 3.0)
    assert require_tuple3("v", [1, 2, 3]) == (1.0, 2.0, 3.0)
    with pytest.raises(InvalidRequestError):
        require_tuple3("v", (1, 2))
    with pytest.raises(InvalidRequestError):
        require_tuple3("v", (1, 2, "a"))


def test_require_tuple2():
    assert require_tuple2("v", (1, 2)) == (1.0, 2.0)
    with pytest.raises(InvalidRequestError):
        require_tuple2("v", (1, 2, 3))


def test_require_non_empty_str():
    assert require_non_empty_str("n", "base") == "base"
    with pytest.raises(InvalidRequestError):
        require_non_empty_str("n", "")
    with pytest.raises(InvalidRequestError):
        require_non_empty_str("n", "   ")


def test_require_in():
    assert require_in("op", "add", ["add", "cut"]) == "add"
    with pytest.raises(InvalidRequestError):
        require_in("op", "other", ["add", "cut"])


def test_require_positive_int():
    assert require_positive_int("c", 5) == 5
    with pytest.raises(InvalidRequestError):
        require_positive_int("c", 0)
    with pytest.raises(InvalidRequestError):
        require_positive_int("c", -1)
