from typing import Iterable


class GlobLike(object):

    def glob(self, path: bytes) -> Iterable[bytes]:  # glob's name, so pylint: disable=g-bad-name
        raise NotImplementedError('Abstract')