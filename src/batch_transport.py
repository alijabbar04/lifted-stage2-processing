"""One-shot Windows-native transport for paid Message Batch creation.

No network action happens at import. There is exactly one Send call and no
transport fallback: a missing response must be reconciled against provider
state before another paid submission is authorized.
"""

from urllib.parse import urlsplit


class BatchTransportError(RuntimeError):
    def __init__(self, message, send_started=False):
        super().__init__(message)
        self.send_started = bool(send_started)


def _load_com():
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        raise BatchTransportError(
            "Windows batch transport is unavailable (pywin32 is missing). "
            "No request was sent; repair or rebuild the app before retrying.") from None
    return pythoncom, win32com.client


def _disable_redirects(request, pythoncom):
    # The installed WinHttpRequest 5.1 typelib exposes indexed Option as
    # DISPATCH_PROPERTYPUT(enum, VARIANT), returning VT_VOID. Resolve its DISPID
    # at runtime rather than depending on a generated makepy cache. Microsoft:
    # https://learn.microsoft.com/en-us/windows/win32/winhttp/iwinhttprequest-option
    # EnableRedirects is enum value 6; no certificate/TLS ignore flags are set.
    dispid = request._oleobj_.GetIDsOfNames("Option")
    request._oleobj_.InvokeTypes(
        dispid, 0, pythoncom.DISPATCH_PROPERTYPUT,
        (pythoncom.VT_VOID, 0),
        ((pythoncom.VT_I4, 1), (pythoncom.VT_VARIANT, 1)), 6, False)


def winhttp_post_batch(url, headers, payload):
    """Return (HTTP status, response text); send exact supplied UTF-8 bytes once.

    COM belongs to the caller's thread, including background UI worker threads.
    HTTPS certificate validation and Windows protocol defaults are unchanged.
    Redirects and automatic Windows credential negotiation are disabled before
    Send. Every timeout is positive and finite; there are no retry/fallback loops.
    """
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "api.anthropic.com"
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.path != "/v1/messages/batches" or parsed.query or parsed.fragment):
        raise BatchTransportError("Refusing an unexpected batch creation endpoint; no request sent")
    if not isinstance(payload, bytes):
        raise BatchTransportError("Batch request must be serialized UTF-8 bytes; no request sent")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError:
        raise BatchTransportError("Batch request was not UTF-8; no request sent") from None
    pythoncom, client = _load_com()
    initialized = False
    request = None
    send_started = False
    phase = "initialization"
    try:
        # CoInitializeEx reports apartment conflicts instead of swallowing them;
        # only a successful initialization is balanced by CoUninitialize.
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        initialized = True
        request = client.Dispatch("WinHttp.WinHttpRequest.5.1")
        request.SetTimeouts(30_000, 30_000, 120_000, 120_000)
        request.Open("POST", url, False)
        _disable_redirects(request, pythoncom)
        request.SetAutoLogonPolicy(2)  # Never negotiate Windows credentials.
        for name, value in headers.items():
            request.SetRequestHeader(str(name), str(value))
        body = client.VARIANT(pythoncom.VT_ARRAY | pythoncom.VT_UI1, payload)
        phase = "upload/response"
        send_started = True
        request.Send(body)
        return int(request.Status), str(request.ResponseText)
    except Exception as exc:
        # COM exception descriptions can contain request or proxy details.
        # Surface only the failure phase and numeric HRESULT, never headers,
        # response bodies, document contents, or the original exception string.
        hresult = getattr(exc, "hresult", None)
        code = f" (HRESULT 0x{hresult & 0xffffffff:08X})" if isinstance(hresult, int) else ""
        outcome = ("The submission outcome must be reconciled before retrying."
                   if send_started else "No request was sent.")
        raise BatchTransportError(
            f"Windows-native batch {phase} failed{code}. {outcome} "
            "No automatic retry or transport fallback was attempted.",
            send_started=send_started) from None
    finally:
        request = None
        if initialized:
            pythoncom.CoUninitialize()
