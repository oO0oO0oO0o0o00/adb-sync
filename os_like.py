import os
from typing import Iterable, Tuple


class OSLike(object):

    def listdir(self, path: bytes) -> Iterable[bytes]:  # os's name, so pylint: disable=g-bad-name
        raise NotImplementedError('Abstract')

    def lstat(self, path: bytes) -> os.stat_result:  # os's name, so pylint: disable=g-bad-name
        raise NotImplementedError('Abstract')

    def stat(self, path: bytes) -> os.stat_result:  # os's name, so pylint: disable=g-bad-name
        raise NotImplementedError('Abstract')

    def unlink(self, path: bytes) -> None:  # os's name, so pylint: disable=g-bad-name
        raise NotImplementedError('Abstract')

    def rmdir(self, path: bytes) -> None:  # os's name, so pylint: disable=g-bad-name
        raise NotImplementedError('Abstract')

    def makedirs(self, path: bytes) -> None:  # os's name, so pylint: disable=g-bad-name
        raise NotImplementedError('Abstract')

    def utime(self, path: bytes, times: Tuple[float, float]) -> None:  # os's name, so pylint: disable=g-bad-name
        raise NotImplementedError('Abstract')