"""Alias for the preview persistence cutover rehearsal.

See :mod:`slatewatch.switchover_walkthrough` for the full scenario.
"""

from .switchover_walkthrough import PASS_MARK, drill, main, run

__all__ = ['main', 'run', 'drill', 'PASS_MARK']


if __name__ == '__main__':
    main()
