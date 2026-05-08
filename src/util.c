#include "pycurl.h"

PYCURL_INTERNAL int
check_pending_python_signal(void)
{
    if (PyErr_CheckSignals() != 0) {
        return -1;
    }
    return 0;
}

PYCURL_INTERNAL int
check_pending_python_exception_or_signal(void)
{
    if (PyErr_Occurred()) {
        return -1;
    }
    return check_pending_python_signal();
}

PYCURL_INTERNAL void
warn_failed_to_acquire_thread(const char *warning_message)
{
    PyGILState_STATE tmp_warn_state = PyGILState_Ensure();
    PyErr_WarnEx(PyExc_RuntimeWarning, warning_message, 1);
    PyGILState_Release(tmp_warn_state);
}

PYCURL_INTERNAL void
pycurl_capture_callback_exception(PyObject **storage)
{
    PyObject *type = NULL;
    PyObject *value = NULL;
    PyObject *tb = NULL;
    PyObject *list;

    if (!PyErr_Occurred()) {
        return;
    }
    /* Let BaseException-only types (KeyboardInterrupt, SystemExit,
       GeneratorExit) propagate unchanged. */
    if (!PyErr_ExceptionMatches(PyExc_Exception)) {
        return;
    }

    PyErr_Fetch(&type, &value, &tb);
    PyErr_NormalizeException(&type, &value, &tb);
    if (value == NULL) {
        Py_XDECREF(type);
        Py_XDECREF(tb);
        return;
    }
    if (tb != NULL) {
        PyException_SetTraceback(value, tb);
    }
    Py_XDECREF(type);
    Py_XDECREF(tb);

    /* Lazy-allocate the list and append. Attach decides how to chain. */
    list = *storage;
    if (list == NULL) {
        list = PyList_New(0);
        if (list == NULL) {
            /* OOM: drop the capture rather than poison the original error
               indicator state of any concurrent operation. */
            PyErr_Clear();
            Py_DECREF(value);
            return;
        }
        *storage = list;
    }
    if (PyList_Append(list, value) != 0) {
        PyErr_Clear();
    }
    Py_DECREF(value);
}

PYCURL_INTERNAL void
pycurl_attach_callback_cause(PyObject **storage)
{
    PyObject *list = *storage;
    PyObject *cause = NULL;
    PyObject *type = NULL;
    PyObject *value = NULL;
    PyObject *tb = NULL;
    Py_ssize_t n;

    *storage = NULL;
    if (list == NULL) {
        return;
    }
    n = PyList_GET_SIZE(list);
    if (n == 0) {
        Py_DECREF(list);
        return;
    }
    /* No pending error means the callback exceptions were captured but the
       libcurl operation succeeded anyway (e.g. DEBUGFUNCTION whose return
       value is ignored). There is nothing to chain to, so drop them. */
    if (!PyErr_Occurred()) {
        Py_DECREF(list);
        return;
    }

    /* Fetch the pending error first, so any Python call we make below
       (e.g. BaseExceptionGroup constructor) does not see a stale
       indicator. */
    PyErr_Fetch(&type, &value, &tb);
    PyErr_NormalizeException(&type, &value, &tb);
    if (value == NULL) {
        /* Normalization failed; restore whatever we have and drop list. */
        PyErr_Restore(type, value, tb);
        Py_DECREF(list);
        return;
    }

    if (n == 1) {
        cause = PyList_GET_ITEM(list, 0);
        Py_INCREF(cause);
        Py_DECREF(list);
    } else {
#if PY_VERSION_HEX >= 0x030B0000
        /* PEP 654 group; constructor narrows to ExceptionGroup because
           our capture filter rejects BaseException-only types. */
        cause = PyObject_CallFunction(PyExc_BaseExceptionGroup, "sO",
                                      "PycURL callback exceptions", list);
        Py_DECREF(list);
        if (cause == NULL) {
            /* Group construction failed; restore the original error and
               discard the captures. */
            PyErr_Clear();
            PyErr_Restore(type, value, tb);
            return;
        }
#else
        /* Python 3.10: no ExceptionGroup; first-wins. */
        cause = PyList_GET_ITEM(list, 0);
        Py_INCREF(cause);
        Py_DECREF(list);
#endif
    }

    if (tb != NULL) {
        PyException_SetTraceback(value, tb);
    }
    PyException_SetCause(value, cause); /* steals cause */
    PyErr_Restore(type, value, tb);     /* steals all three */
}

PYCURL_INTERNAL void
pycurl_easy_clear_callback_state(struct CurlObject *self)
{
#ifdef HAVE_CURL_MIME
    /* Drain any per-owner mime captures into the easy slot first, then
       clear in one shot. */
    curlmime_collect_callback_exceptions(self->mimepost_obj,
                                         &self->callback_exception);
#endif
    Py_CLEAR(self->callback_exception);
}

PYCURL_INTERNAL void
pycurl_easy_attach_callback_cause(struct CurlObject *self)
{
#ifdef HAVE_CURL_MIME
    /* Pull any captured exception from mime data callbacks into the easy
       handle's slot, then attach as __cause__ in the usual way. */
    curlmime_collect_callback_exceptions(self->mimepost_obj,
                                         &self->callback_exception);
#endif
    pycurl_attach_callback_cause(&self->callback_exception);
}

PYCURL_INTERNAL PyObject *
PyLong_FromCurlSocket(curl_socket_t sockfd)
{
#if defined(WIN32)
    if (sockfd == CURL_SOCKET_BAD) {
        return PyLong_FromLong(-1);
    }
    return PyLong_FromUnsignedLongLong((unsigned long long) sockfd);
#else
    return PyLong_FromLongLong((long long) sockfd);
#endif
}

PYCURL_INTERNAL int
PyLong_AsCurlSocket(PyObject *obj, curl_socket_t *sockfd)
{
#if defined(WIN32)
    const unsigned long long max_socket =
        (unsigned long long) ((curl_socket_t) ~(curl_socket_t) 0);
    long long ll;
    unsigned long long ull;
#else
    long long ll;
#endif

    assert(sockfd != NULL);

#if defined(WIN32)
    ll = PyLong_AsLongLong(obj);
    if (!PyErr_Occurred()) {
        if (ll == -1) {
            *sockfd = CURL_SOCKET_BAD;
            return 0;
        }
        if (ll < 0) {
            PyErr_SetString(PyExc_OverflowError,
                "socket value must be -1 or non-negative");
            return -1;
        }
        if ((unsigned long long) ll > max_socket) {
            PyErr_SetString(PyExc_OverflowError, "socket value is out of range");
            return -1;
        }
        *sockfd = (curl_socket_t) ll;
        return 0;
    }

    if (!PyErr_ExceptionMatches(PyExc_OverflowError)) {
        return -1;
    }
    PyErr_Clear();

    ull = PyLong_AsUnsignedLongLong(obj);
    if (PyErr_Occurred()) {
        return -1;
    }
    if (ull > max_socket) {
        PyErr_SetString(PyExc_OverflowError, "socket value is out of range");
        return -1;
    }
    *sockfd = (curl_socket_t) ull;
    return 0;
#else
    ll = PyLong_AsLongLong(obj);
    if (PyErr_Occurred()) {
        return -1;
    }
    if (ll < (long long) CURL_SOCKET_BAD || ll > INT_MAX) {
        PyErr_SetString(PyExc_OverflowError, "socket value is out of range");
        return -1;
    }
    *sockfd = (curl_socket_t) ll;
    return 0;
#endif
}

static PyObject *
create_error_object(CurlObject *self, int code)
{
    PyObject *s, *v;

    if (strlen(self->error)) {
        s = PyText_FromString_Ignore(self->error);
        if (s == NULL) {
            return NULL;
        }
    } else {
        s = PyText_FromString_Ignore(curl_easy_strerror(code));
        if (s == NULL) {
            return NULL;
        }
    }
    v = Py_BuildValue("(iO)", code, s);
    if (v == NULL) {
        Py_DECREF(s);
        return NULL;
    }
    return v;
}

PYCURL_INTERNAL void
create_and_set_error_object(CurlObject *self, int code)
{
    PyObject *e;

    self->error[sizeof(self->error) - 1] = 0;
    e = create_error_object(self, code);
    if (e != NULL) {
        PyErr_SetObject(ErrorObject, e);
        Py_DECREF(e);
    }
}
