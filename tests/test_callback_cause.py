"""Tests for callback exception chaining via ``pycurl.error.__cause__``.

When a regular ``Exception`` subclass is raised inside a PycURL callback, the
callback returns its libcurl-specific failure value and PycURL still raises
``pycurl.error`` from the driving call. The original exception is now attached
as the ``__cause__`` of that ``pycurl.error`` instead of being printed to
stderr.

``KeyboardInterrupt``, ``SystemExit``, and other ``BaseException`` subclasses
still propagate unchanged — they are not wrapped in ``pycurl.error``.
"""

from __future__ import annotations

import builtins
import os.path
import select
import sys
import time
import urllib.request

import pycurl
import pytest

from . import util


# BaseExceptionGroup is a 3.11+ builtin; fall back to an empty tuple on
# 3.10 so isinstance(x, _BaseExceptionGroup) is harmlessly False.
_BaseExceptionGroup = getattr(builtins, "BaseExceptionGroup", ())

HAS_MIME = hasattr(pycurl, "Mime") or hasattr(pycurl, "CurlMime")


def _make_mime(curl):
    # CurlMime is the public class name; older builds may expose Mime.
    cls = getattr(pycurl, "CurlMime", None) or getattr(pycurl, "Mime")
    return cls(curl)


def _assert_cause_includes(err, exc_type, message=None):
    """Assert ``err.__cause__`` either is an instance of ``exc_type`` (single
    capture) or, on Python 3.11+, an :class:`ExceptionGroup` whose leaves
    include at least one matching exception (multi-capture). Optional
    ``message`` matches ``str(exc)`` exactly on at least one matching entry.
    """
    cause = err.__cause__
    assert cause is not None, f"expected a __cause__, got None on {err!r}"
    if isinstance(cause, _BaseExceptionGroup):
        match, _rest = cause.split(exc_type)
        assert match is not None, f"no {exc_type.__name__} in {cause!r}"
        leaves = list(_walk_leaves(match))
        assert leaves, f"empty match group from {cause!r}"
        if message is not None:
            assert any(str(e) == message for e in leaves), (
                f"expected {exc_type.__name__}({message!r}) in {leaves!r}"
            )
    else:
        assert isinstance(cause, exc_type), (
            f"expected {exc_type.__name__}, got {cause!r}"
        )
        if message is not None:
            assert str(cause) == message


def _walk_leaves(exc):
    """Yield all non-group leaves of an ExceptionGroup tree."""
    if isinstance(exc, _BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from _walk_leaves(sub)
    else:
        yield exc


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def file_url() -> str:
    """A file:// URL guaranteed to produce some bytes / trigger callbacks."""
    return "file:" + urllib.request.pathname2url(os.path.abspath(__file__))


@pytest.fixture
def file_curl(file_url):
    c = util.DefaultCurl()
    c.setopt(pycurl.URL, file_url)
    yield c
    c.close()


# --------------------------------------------------------------------------- #
# Easy-handle callbacks
# --------------------------------------------------------------------------- #


def _raise_runtime(*_a, **_kw):
    raise RuntimeError("boom")


def test_write_cb_exception_becomes_cause(file_curl):
    file_curl.setopt(pycurl.WRITEFUNCTION, _raise_runtime)
    with pytest.raises(pycurl.error) as excinfo:
        file_curl.perform()
    _assert_cause_includes(excinfo.value, RuntimeError, "boom")
    assert excinfo.value.__cause__.__traceback__ is not None


def test_header_cb_exception_becomes_cause(app, curl):
    curl.setopt(pycurl.URL, f"{app}/success")
    curl.setopt(pycurl.HEADERFUNCTION, _raise_runtime)
    # Need a write callback so the body bytes don't go to stdout.
    curl.setopt(pycurl.WRITEFUNCTION, lambda _: None)
    with pytest.raises(pycurl.error) as excinfo:
        curl.perform()
    _assert_cause_includes(excinfo.value, RuntimeError, "boom")


def test_read_cb_exception_becomes_cause(app, curl):
    def read_cb(_size):
        raise RuntimeError("boom")

    curl.setopt(pycurl.URL, f"{app}/postfields")
    curl.setopt(pycurl.POST, 1)
    curl.setopt(pycurl.POSTFIELDSIZE, 16)
    curl.setopt(pycurl.READFUNCTION, read_cb)
    curl.setopt(pycurl.WRITEFUNCTION, lambda _: None)
    with pytest.raises(pycurl.error) as excinfo:
        curl.perform()
    _assert_cause_includes(excinfo.value, RuntimeError, "boom")


def test_progress_cb_exception_becomes_cause(file_curl):
    def progress(_dlt, _dln, _ult, _uln):
        raise RuntimeError("boom")

    file_curl.setopt(pycurl.WRITEFUNCTION, lambda _: None)
    file_curl.setopt(pycurl.NOPROGRESS, 0)
    file_curl.setopt(pycurl.PROGRESSFUNCTION, progress)
    with pytest.raises(pycurl.error) as excinfo:
        file_curl.perform()
    # Progress callback fires multiple times during a transfer; on 3.11+
    # the captures wrap in an ExceptionGroup.
    _assert_cause_includes(excinfo.value, RuntimeError, "boom")


def test_xferinfo_cb_exception_becomes_cause(file_curl):
    def xferinfo(_dlt, _dln, _ult, _uln):
        raise RuntimeError("boom")

    file_curl.setopt(pycurl.WRITEFUNCTION, lambda _: None)
    file_curl.setopt(pycurl.NOPROGRESS, 0)
    file_curl.setopt(pycurl.XFERINFOFUNCTION, xferinfo)
    with pytest.raises(pycurl.error) as excinfo:
        file_curl.perform()
    _assert_cause_includes(excinfo.value, RuntimeError, "boom")


def test_opensocket_cb_exception_becomes_cause(app, curl):
    def open_socket(_purpose, _address):
        raise RuntimeError("boom")

    curl.setopt(pycurl.URL, f"{app}/success")
    curl.setopt(pycurl.WRITEFUNCTION, lambda _: None)
    curl.setopt(pycurl.OPENSOCKETFUNCTION, open_socket)
    with pytest.raises(pycurl.error) as excinfo:
        curl.perform()
    _assert_cause_includes(excinfo.value, RuntimeError, "boom")


def test_sockopt_cb_exception_becomes_cause(app, curl):
    def sockopt(_curlfd, _purpose):
        raise RuntimeError("boom")

    curl.setopt(pycurl.URL, f"{app}/success")
    curl.setopt(pycurl.WRITEFUNCTION, lambda _: None)
    curl.setopt(pycurl.SOCKOPTFUNCTION, sockopt)
    with pytest.raises(pycurl.error) as excinfo:
        curl.perform()
    _assert_cause_includes(excinfo.value, RuntimeError, "boom")


# --------------------------------------------------------------------------- #
# Multi-handle callbacks
# --------------------------------------------------------------------------- #


def test_multi_socket_cb_exception_becomes_cause(app):
    easy = util.DefaultCurl()
    easy.setopt(pycurl.URL, f"{app}/success")
    easy.setopt(pycurl.WRITEFUNCTION, lambda _: None)

    multi = pycurl.CurlMulti()
    multi.setopt(pycurl.M_SOCKETFUNCTION, _raise_runtime)
    multi.setopt(pycurl.M_TIMERFUNCTION, lambda _: 0)
    try:
        with pytest.raises(pycurl.error) as excinfo:
            multi.add_handle(easy)
            multi.socket_action(pycurl.SOCKET_TIMEOUT, 0)
        _assert_cause_includes(excinfo.value, RuntimeError, "boom")
    finally:
        try:
            multi.remove_handle(easy)
        except pycurl.error:
            pass
        multi.close()
        easy.close()


def test_multi_timer_cb_exception_becomes_cause(app):
    easy = util.DefaultCurl()
    easy.setopt(pycurl.URL, f"{app}/success")
    easy.setopt(pycurl.WRITEFUNCTION, lambda _: None)

    multi = pycurl.CurlMulti()
    multi.setopt(pycurl.M_SOCKETFUNCTION, lambda *_: 0)
    multi.setopt(pycurl.M_TIMERFUNCTION, _raise_runtime)
    try:
        with pytest.raises(pycurl.error) as excinfo:
            multi.add_handle(easy)
            multi.socket_action(pycurl.SOCKET_TIMEOUT, 0)
        _assert_cause_includes(excinfo.value, RuntimeError, "boom")
    finally:
        try:
            multi.remove_handle(easy)
        except pycurl.error:
            pass
        multi.close()
        easy.close()


# --------------------------------------------------------------------------- #
# BaseException preservation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "exc, exc_type",
    [
        (KeyboardInterrupt(), KeyboardInterrupt),
        (SystemExit(7), SystemExit),
        (GeneratorExit(), GeneratorExit),
    ],
    ids=["KeyboardInterrupt", "SystemExit", "GeneratorExit"],
)
def test_base_exception_propagates_unchanged(file_curl, exc, exc_type):
    def write_cb(_data):
        raise exc

    file_curl.setopt(pycurl.WRITEFUNCTION, write_cb)
    with pytest.raises(exc_type) as excinfo:
        file_curl.perform()
    # Right type, not wrapped in pycurl.error, no __cause__ attached.
    assert excinfo.type is exc_type
    assert excinfo.value.__cause__ is None


def test_system_exit_preserves_code(file_curl):
    def write_cb(_data):
        raise SystemExit(7)

    file_curl.setopt(pycurl.WRITEFUNCTION, write_cb)
    with pytest.raises(SystemExit) as excinfo:
        file_curl.perform()
    assert excinfo.value.code == 7


# --------------------------------------------------------------------------- #
# Stale-cause prevention
# --------------------------------------------------------------------------- #


def test_no_stale_cause_after_failed_then_successful_perform(file_curl):
    # First perform raises, captures a cause.
    file_curl.setopt(pycurl.WRITEFUNCTION, _raise_runtime)
    with pytest.raises(pycurl.error) as excinfo:
        file_curl.perform()
    assert isinstance(excinfo.value.__cause__, RuntimeError)

    # Second perform succeeds; the previous cause must not leak anywhere.
    file_curl.setopt(pycurl.WRITEFUNCTION, lambda _data: None)
    file_curl.perform()  # should not raise

    # And a third perform that fails differently must not pick up the stale
    # RuntimeError as its cause.
    file_curl.setopt(pycurl.WRITEFUNCTION, lambda _data: -1)
    with pytest.raises(pycurl.error) as excinfo:
        file_curl.perform()
    assert excinfo.value.__cause__ is None


def test_pycurl_error_without_callback_exception_has_no_cause(curl, free_port):
    # No callback raises; libcurl fails on its own (connect refused). The
    # resulting pycurl.error must not carry a stale __cause__.
    curl.setopt(pycurl.URL, f"http://127.0.0.1:{free_port}/")
    curl.setopt(pycurl.CONNECTTIMEOUT, 1)
    with pytest.raises(pycurl.error) as excinfo:
        curl.perform()
    assert excinfo.value.__cause__ is None


def test_debug_cb_capture_does_not_leak_to_setopt(file_curl):
    """Regression for B3: a captured exception from a successful perform()
    (e.g. DEBUGFUNCTION whose return value is ignored) must not become the
    __cause__ of a later, unrelated CURLERROR-bearing call such as setopt."""

    def debug_cb(_type, _buf):
        raise RuntimeError("stale debug")

    file_curl.setopt(pycurl.VERBOSE, 1)
    file_curl.setopt(pycurl.DEBUGFUNCTION, debug_cb)
    file_curl.setopt(pycurl.WRITEFUNCTION, lambda _b: None)
    file_curl.perform()  # succeeds — debug return value is ignored

    file_curl.setopt(pycurl.DEBUGFUNCTION, None)
    with pytest.raises(pycurl.error) as excinfo:
        # PROXYTYPE rejects unknown ints with CURLE_BAD_FUNCTION_ARGUMENT
        # and goes through CURLERROR_RETVAL.
        file_curl.setopt(pycurl.PROXYTYPE, 999999)
    assert excinfo.value.__cause__ is None


# --------------------------------------------------------------------------- #
# Mime data callbacks (read / seek)
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(not HAS_MIME, reason="libcurl mime API not available")
def test_mime_read_cb_exception_becomes_cause(app, curl):
    def read_cb(_size, _userdata):
        raise RuntimeError("mime boom")

    mime = _make_mime(curl)
    part = mime.addpart()
    part.name("blob")
    part.data_cb(16, read_cb)

    curl.setopt(pycurl.URL, f"{app}/postfields")
    curl.setopt(pycurl.MIMEPOST, mime)
    curl.setopt(pycurl.WRITEFUNCTION, lambda _b: None)

    with pytest.raises(pycurl.error) as excinfo:
        curl.perform()
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert str(excinfo.value.__cause__) == "mime boom"


@pytest.mark.skipif(not HAS_MIME, reason="libcurl mime API not available")
def test_no_stale_mime_cause_across_performs(app, file_curl):
    """Regression: a captured exception left on a mime owner from an earlier
    perform must not become the __cause__ of a later, unrelated pycurl.error."""
    poison = pycurl.Curl()
    try:
        # Run a perform whose mime read_cb raises, deliberately captured.
        def raise_once(_size, _userdata):
            raise RuntimeError("stale mime")

        mime = _make_mime(poison)
        part = mime.addpart()
        part.name("blob")
        part.data_cb(16, raise_once)

        poison.setopt(pycurl.URL, f"{app}/postfields")
        poison.setopt(pycurl.MIMEPOST, mime)
        poison.setopt(pycurl.WRITEFUNCTION, lambda _b: None)
        with pytest.raises(pycurl.error) as ei1:
            poison.perform()
        assert isinstance(ei1.value.__cause__, RuntimeError)

        # Second perform on the same handle: succeed (replace the read_cb).
        seen = []
        mime2 = _make_mime(poison)
        part2 = mime2.addpart()
        part2.name("blob")
        part2.data(b"hello")
        poison.setopt(pycurl.MIMEPOST, mime2)
        poison.setopt(pycurl.WRITEFUNCTION, seen.append)
        poison.perform()  # must not raise
        assert seen, "expected a successful body write"

        # Third perform: fails for an unrelated reason (write returns -1).
        # The old mime exception must NOT become its cause.
        poison.setopt(pycurl.WRITEFUNCTION, lambda _b: -1)
        with pytest.raises(pycurl.error) as ei3:
            poison.perform()
        assert ei3.value.__cause__ is None, (
            f"stale mime cause leaked: {ei3.value.__cause__!r}"
        )
    finally:
        poison.close()


# --------------------------------------------------------------------------- #
# First-wins ordering and DEBUGFUNCTION special case
# --------------------------------------------------------------------------- #


def test_first_callback_to_raise_wins(app, curl):
    """When two callbacks raise during the same perform(), the FIRST one
    captured becomes __cause__. HEADERFUNCTION runs before WRITEFUNCTION."""
    seen = []

    def header_cb(_data):
        seen.append("header")
        raise ValueError("header err")

    def write_cb(_data):
        seen.append("write")
        raise RuntimeError("write err")

    curl.setopt(pycurl.URL, f"{app}/success")
    curl.setopt(pycurl.HEADERFUNCTION, header_cb)
    curl.setopt(pycurl.WRITEFUNCTION, write_cb)

    with pytest.raises(pycurl.error) as excinfo:
        curl.perform()
    assert seen[0] == "header"
    assert isinstance(excinfo.value.__cause__, ValueError)
    assert str(excinfo.value.__cause__) == "header err"


def test_debug_cb_exception_alone_does_not_abort(file_curl):
    """DEBUGFUNCTION's return value is ignored by libcurl — an exception
    raised inside it cannot, by itself, fail the perform()."""
    file_curl.setopt(pycurl.VERBOSE, 1)
    file_curl.setopt(pycurl.DEBUGFUNCTION, _raise_runtime)
    file_curl.setopt(pycurl.WRITEFUNCTION, lambda _b: None)
    file_curl.perform()  # must not raise


@pytest.mark.skipif(
    sys.version_info < (3, 11), reason="ExceptionGroup added in Python 3.11"
)
def test_multiple_captures_wrap_in_exception_group(file_curl):
    """Two or more callback exceptions captured during the same perform()
    are wrapped in an ExceptionGroup as ``__cause__`` on Python 3.11+."""
    file_curl.setopt(pycurl.VERBOSE, 1)

    def debug_cb(_type, _buf):
        raise RuntimeError("debug boom")

    file_curl.setopt(pycurl.DEBUGFUNCTION, debug_cb)
    file_curl.setopt(pycurl.WRITEFUNCTION, lambda _b: -1)

    with pytest.raises(pycurl.error) as excinfo:
        file_curl.perform()

    cause = excinfo.value.__cause__
    assert isinstance(cause, _BaseExceptionGroup)
    leaves = list(_walk_leaves(cause))
    assert len(leaves) >= 2
    assert all(isinstance(e, RuntimeError) for e in leaves)
    assert all(str(e) == "debug boom" for e in leaves)


def test_debug_cb_exception_surfaces_when_other_callback_fails(file_curl):
    """When DEBUGFUNCTION captures and another callback also fails, the
    debug exception is part of the chained cause. DEBUGFUNCTION fires
    multiple times, so on 3.11+ the cause is an ExceptionGroup of
    RuntimeErrors; on 3.10 first-wins makes it a single RuntimeError."""
    file_curl.setopt(pycurl.VERBOSE, 1)

    def debug_cb(_type, _buf):
        raise RuntimeError("debug boom")

    file_curl.setopt(pycurl.DEBUGFUNCTION, debug_cb)
    file_curl.setopt(pycurl.WRITEFUNCTION, lambda _b: -1)  # forces E_WRITE_ERROR

    with pytest.raises(pycurl.error) as excinfo:
        file_curl.perform()
    _assert_cause_includes(excinfo.value, RuntimeError, "debug boom")


# --------------------------------------------------------------------------- #
# Easy-multi interaction
# --------------------------------------------------------------------------- #


def test_easy_cb_capture_via_multi_does_not_leak_to_setopt(app):
    """Regression for B4: an easy-handle callback that captures during
    multi.perform() (e.g. DEBUGFUNCTION whose return value libcurl ignores)
    must not become the __cause__ of a later easy.setopt error."""

    def debug_cb(_type, _buf):
        raise RuntimeError("via-multi")

    easy = util.DefaultCurl()
    easy.setopt(pycurl.URL, f"{app}/success")
    easy.setopt(pycurl.VERBOSE, 1)
    easy.setopt(pycurl.DEBUGFUNCTION, debug_cb)
    easy.setopt(pycurl.WRITEFUNCTION, lambda _b: None)

    multi = pycurl.CurlMulti()
    try:
        multi.add_handle(easy)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            multi.perform()
            num_q, ok_list, err_list = multi.info_read()
            if ok_list or err_list:
                break
            rset, wset, xset = multi.fdset()
            if rset or wset or xset:
                select.select(rset, wset, xset, 0.05)
            else:
                time.sleep(0.01)
    finally:
        try:
            multi.remove_handle(easy)
        except pycurl.error:
            pass
        multi.close()

    easy.setopt(pycurl.DEBUGFUNCTION, None)
    with pytest.raises(pycurl.error) as excinfo:
        easy.setopt(pycurl.PROXYTYPE, 999999)
    assert excinfo.value.__cause__ is None
    easy.close()


def test_easy_callback_failure_during_multi_does_not_chain_to_multi(app):
    """multi.perform() does not raise pycurl.error from its own callback
    storage when an easy-handle callback (not a multi-level one) fails — the
    easy failure surfaces via info_read instead."""
    easy = util.DefaultCurl()
    easy.setopt(pycurl.URL, f"{app}/success")
    easy.setopt(pycurl.WRITEFUNCTION, _raise_runtime)

    multi = pycurl.CurlMulti()
    err_entries: list = []
    try:
        multi.add_handle(easy)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            multi.perform()  # must not raise
            while True:
                num_q, ok_list, err_list = multi.info_read()
                err_entries.extend(err_list)
                if num_q == 0:
                    break
            if err_entries:
                break
            rset, wset, xset = multi.fdset()
            if rset or wset or xset:
                select.select(rset, wset, xset, 0.05)
            else:
                time.sleep(0.01)

        assert err_entries, "expected the easy handle to be reported as failed"
    finally:
        try:
            multi.remove_handle(easy)
        except pycurl.error:
            pass
        multi.close()
        easy.close()
