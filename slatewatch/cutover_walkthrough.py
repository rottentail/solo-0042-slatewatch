"""Alias for the preview storage cutover drill.

See :mod:`slatewatch.switchover_walkthrough`.
"""

from .switchover_walkthrough import PASS_MARK, drill, main, run

__all__ = ['main', 'run', 'drill', 'PASS_MARK']


if __name__ == '__main__':
    main()
