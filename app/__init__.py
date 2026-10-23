import logging

class _KVLogger(logging.getLoggerClass()):
    """Allow log.info("msg", key=value) style calls."""
    def _log(self, level, msg, args, exc_info=None, extra=None,
             stack_info=False, stacklevel=1, **kw):
        if kw:
            msg = f"{msg} " + " ".join(f"{k}={v}" for k, v in kw.items())
        super()._log(level, msg, args, exc_info=exc_info, extra=extra,
                     stack_info=stack_info, stacklevel=stacklevel + 1)

logging.setLoggerClass(_KVLogger)