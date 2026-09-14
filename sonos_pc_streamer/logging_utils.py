import builtins
import time

_print = builtins.print


def timestamped_print(*args, **kwargs):
    _print(time.strftime("[%H:%M:%S]"), *args, **kwargs)
