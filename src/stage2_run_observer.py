"""Best-effort observation around native Engine operations; never a writer lock."""
from __future__ import annotations

import functools

from stage2_run_state import RunPublisher


def _publish(publisher, method, *args, **kwargs):
    try:
        getattr(publisher, method)(*args, **kwargs)
    except Exception:
        # A full disk or unavailable observer must not abort paid processing.
        pass


def observe_engine_operation(method):
    """Also retained in __wrapped__ for callers holding native locks externally."""
    @functools.wraps(method)
    def observed(self, *args, **kwargs):
        if (method.__name__ == "recover_primary_submission"
                and not kwargs.get("allow_resubmit", args[0] if args else False)):
            return method(self, *args, **kwargs)
        if getattr(self, "_run_publisher", None) is not None:
            return method(self, *args, **kwargs)
        try:
            publisher = RunPublisher(self.dir, mode=method.__name__,
                                     destination=getattr(self, "move_dest", None))
        except Exception:
            return method(self, *args, **kwargs)
        self._run_publisher = publisher
        originals = {}
        result_status = ["operation-ended"]

        def wrap(name, observer):
            original = getattr(self, name, None)
            if not callable(original):
                return
            originals[name] = original
            @functools.wraps(original)
            def callback(*values, **options):
                try:
                    observer(*values, **options)
                except Exception:
                    pass
                return original(*values, **options)
            setattr(self, name, callback)

        def done(stats, outcome, *unused, **options):
            result_status[0] = str(outcome or "operation-ended")
            _publish(publisher, "update", stats=dict(stats),
                     last_activity=result_status[0])

        wrap("set_status", lambda text: _publish(publisher, "update", last_activity=str(text)))
        wrap("set_progress", lambda completed, total: _publish(
            publisher, "update", completed=completed, total=total,
            stats=dict(getattr(self, "stats", {}))))
        wrap("set_preview", lambda image, name: _publish(
            publisher, "update", current_document=str(name)))
        wrap("on_cost", lambda gbp, tokens: _publish(publisher, "update", cost_gbp=gbp))
        wrap("on_done", done)
        error = ""
        try:
            return method(self, *args, **kwargs)
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            _publish(publisher, "finish", outcome=result_status[0], error=error)
            for name, callback in originals.items():
                setattr(self, name, callback)
            self._run_publisher = None
    return observed
