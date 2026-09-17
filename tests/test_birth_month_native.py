import pytest

from scripts.roxy_native_register import _birth_month_index


def test_august_is_month_8():
    assert _birth_month_index("August") == 8
    assert _birth_month_index("august") == 8


def test_invalid_month():
    with pytest.raises(Exception):
        _birth_month_index("NotAMonth")
