import subprocess
from types import TracebackType
from typing import List, IO, Optional, Type


class Stdout(object):

    def __init__(self, args: List[bytes]) -> None:
        """Closes the process's stdout when done.

        Usage:
          with Stdout(...) as stdout:
            DoSomething(stdout)

        Args:
          args: Which program to run.

        Returns:
          An object for use by 'with'.
        """
        self.popen = subprocess.Popen(args, stdout=subprocess.PIPE)

    def __enter__(self) -> IO:
        return self.popen.stdout

    def __exit__(self, exc_type: Optional[Type[BaseException]],
                 exc_val: Optional[Exception],
                 exc_tb: Optional[TracebackType]) -> bool:
        self.popen.stdout.close()
        if self.popen.wait() != 0:
            raise OSError('Subprocess exited with nonzero status.')
        return False