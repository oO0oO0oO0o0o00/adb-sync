import logging
import os
import re
import stat
import subprocess
import time
from typing import List, Tuple, Iterable, Dict

from glob_like import GlobLike
from my_stdout import Stdout
from os_like import OSLike


class AdbFileSystem(GlobLike, OSLike):
    """Mimics os's file interface but uses the adb utility."""

    def __init__(self, adb: List[bytes]) -> None:
        self.stat_cache = {}  # type: Dict[bytes, os.stat_result]
        self.adb = adb

    # Regarding parsing stat results, we only care for the following fields:
    # - st_size
    # - st_mtime
    # - st_mode (but only about S_ISDIR and S_ISREG properties)
    # Therefore, we only capture parts of 'ls -l' output that we actually use.
    # The other fields will be filled with dummy values.
    LS_TO_STAT_RE = re.compile(
        br"""^
                               (?:
                                 (?P<S_IFREG> -) |
                                 (?P<S_IFBLK> b) |
                                 (?P<S_IFCHR> c) |
                                 (?P<S_IFDIR> d) |
                                 (?P<S_IFLNK> l) |
                                 (?P<S_IFIFO> p) |
                                 (?P<S_IFSOCK> s))
                               [-r][-w][-xsS]
                               [-r][-w][-xsS]
                               [-r][-w][-xtT]  # Mode string.
                               [ ]+
                               (?:
                                  [0-9]+  # number of hard links
                                  [ ]+
                                  )?
                               [^ ]+  # User name/ID.
                               [ ]+
                               [^ ]+  # Group name/ID.
                               [ ]+
                               (?(S_IFBLK) [^ ]+[ ]+[^ ]+[ ]+)  # Device numbers.
                               (?(S_IFCHR) [^ ]+[ ]+[^ ]+[ ]+)  # Device numbers.
                               (?(S_IFDIR) [0-9]+ [ ]+)?        # directory Size.
                               (?(S_IFREG)
                                 (?P<st_size> [0-9]+)           # Size.
                                 [ ]+)
                               (?P<st_mtime>
                                 [0-9]{4}-[0-9]{2}-[0-9]{2}     # Date.
                                 [ ]
                                 [0-9]{2}:[0-9]{2})             # Time.
                               [ ]
                               # Don't capture filename for symlinks (ambiguous).
                               (?(S_IFLNK) .* | (?P<filename> .*))
                               $""", re.DOTALL | re.VERBOSE)

    def LsToStat(self, line: bytes) -> Tuple[os.stat_result, bytes]:
        """Convert a line from 'ls -l' output to a stat result.

        Args:
          line: Output line of 'ls -l' on Android.

        Returns:
          os.stat_result for the line.

        Raises:
          OSError: if the given string is not a 'ls -l' output line (but maybe an
          error message instead).
        """

        match = self.LS_TO_STAT_RE.match(line)
        if match is None:
            logging.error('Could not parse %r.', line)
            raise OSError('Unparseable ls -al result.')
        groups = match.groupdict()

        # Get the values we're interested in.
        st_mode = (  # 0755
                stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH
                | stat.S_IXOTH)
        if groups['S_IFREG']:
            st_mode |= stat.S_IFREG
        if groups['S_IFBLK']:
            st_mode |= stat.S_IFBLK
        if groups['S_IFCHR']:
            st_mode |= stat.S_IFCHR
        if groups['S_IFDIR']:
            st_mode |= stat.S_IFDIR
        if groups['S_IFIFO']:
            st_mode |= stat.S_IFIFO
        if groups['S_IFLNK']:
            st_mode |= stat.S_IFLNK
        if groups['S_IFSOCK']:
            st_mode |= stat.S_IFSOCK
        st_size = None if groups['st_size'] is None else int(groups['st_size'])
        st_mtime = int(
            time.mktime(
                time.strptime(
                    match.group('st_mtime').decode('ascii'), '%Y-%m-%d %H:%M')))

        # Fill the rest with dummy values.
        st_ino = 1
        st_rdev = 0
        st_nlink = 1
        st_uid = -2  # Nobody.
        st_gid = -2  # Nobody.
        st_atime = st_ctime = st_mtime

        stbuf = os.stat_result((st_mode, st_ino, st_rdev, st_nlink, st_uid, st_gid,
                                st_size, st_atime, st_mtime, st_ctime))
        filename = groups['filename']
        return stbuf, filename

    def QuoteArgument(self, arg: bytes) -> bytes:
        # Quotes an argument for use by adb shell.
        # Usually, arguments in 'adb shell' use are put in double quotes by adb,
        # but not in any way escaped.
        arg = arg.replace(b'\\', b'\\\\')
        arg = arg.replace(b'"', b'\\"')
        arg = arg.replace(b'$', b'\\$')
        arg = arg.replace(b'`', b'\\`')
        arg = b'"' + arg + b'"'
        return arg

    def IsWorking(self) -> bool:
        """Tests the adb connection."""
        # This string should contain all possible evil, but no percent signs.
        # Note this code uses 'date' and not 'echo', as date just calls strftime
        # while echo does its own backslash escape handling additionally to the
        # shell's. Too bad printf "%s\n" is not available.
        test_strings = [
            b'(', b'(;  #`ls`$PATH\'"(\\\\\\\\){};!\xc0\xaf\xff\xc2\xbf'
        ]
        # windows compatible
        if os.name == 'nt':
            s = '(;  #`ls`$PATH\'"(\\\\\\\\){};!\xc0\xaf\xff\xc2\xbf'
            test_strings = [
                b'(', s.encode('utf-8')
            ]
        for test_string in test_strings:
            good = False
            with Stdout(self.adb +
                        [b'shell',
                         b'date +%s' % (self.QuoteArgument(test_string),)]) as stdout:
                for line in stdout:
                    line = line.rstrip(b'\r\n')
                    if line == test_string:
                        good = True
            if not good:
                return False
        return True

    def listdir(self, path: bytes) -> Iterable[bytes]:  # os's name, so pylint: disable=g-bad-name
        """List the contents of a directory, caching them for later lstat calls."""
        with Stdout(self.adb +
                    [b'shell',
                     b'ls -al %s' % (self.QuoteArgument(path + b'/'),)]) as stdout:
            for line in stdout:
                if line.startswith(b'total '):
                    continue
                line = line.rstrip(b'\r\n')
                try:
                    statdata, filename = self.LsToStat(line)
                except OSError:
                    continue
                if filename is None:
                    logging.error('Could not parse %r.', line)
                else:
                    self.stat_cache[path + b'/' + filename] = statdata
                    yield filename

    def lstat(self, path: bytes) -> os.stat_result:  # os's name, so pylint: disable=g-bad-name
        """Stat a file."""
        if path in self.stat_cache:
            return self.stat_cache[path]
        return self._stat_lstat(path, b'ls -ald %s')

    def stat(self, path: bytes) -> os.stat_result:  # os's name, so pylint: disable=g-bad-name
        """Stat a file."""
        if path in self.stat_cache and not stat.S_ISLNK(
                self.stat_cache[path].st_mode):
            return self.stat_cache[path]
        return self._stat_lstat(path, b'ls -aldL %s')

    def _stat_lstat(self, path: bytes, ls_command: bytes):
        """Stat or lstat a file."""
        with Stdout(
                self.adb +
                [b'shell', ls_command % (self.QuoteArgument(path),)]) as stdout:
            for line in stdout:
                if line.startswith(b'total '):
                    continue
                line = line.rstrip(b'\r\n')
                statdata, _ = self.LsToStat(line)
                self.stat_cache[path] = statdata
                return statdata
        raise OSError('No such file or directory')

    def unlink(self, path: bytes) -> None:  # os's name, so pylint: disable=g-bad-name
        """Delete a file."""
        if subprocess.call(
                self.adb + [b'shell', b'rm %s' % (self.QuoteArgument(path),)]) != 0:
            raise OSError('unlink failed')

    def rmdir(self, path: bytes) -> None:  # os's name, so pylint: disable=g-bad-name
        """Delete a directory."""
        if subprocess.call(
                self.adb +
                [b'shell', b'rmdir %s' % (self.QuoteArgument(path),)]) != 0:
            raise OSError('rmdir failed')

    def makedirs(self, path: bytes) -> None:  # os's name, so pylint: disable=g-bad-name
        """Create a directory."""
        if subprocess.call(
                self.adb +
                [b'shell', b'mkdir -p %s' % (self.QuoteArgument(path),)]) != 0:
            raise OSError('mkdir failed')

    def utime(self, path: bytes, times: Tuple[float, float]) -> None:
        """Set the time of a file to a specified unix time."""
        atime, mtime = times
        timestr = time.strftime('%Y%m%d%H%M.%S',
                                time.localtime(mtime)).encode('ascii')
        if subprocess.call(
                self.adb +
                [b'shell',
                 b'touch -mt %s %s' % (timestr, self.QuoteArgument(path))]) != 0:
            raise OSError('touch failed')
        timestr = time.strftime('%Y%m%d%H%M.%S',
                                time.localtime(atime)).encode('ascii')
        if subprocess.call(
                self.adb +
                [b'shell',
                 b'touch -at %s %s' % (timestr, self.QuoteArgument(path))]) != 0:
            raise OSError('touch failed')

    def glob(self, path: bytes) -> Iterable[bytes]:  # glob's name, so pylint: disable=g-bad-name
        with Stdout(
                self.adb +
                [b'shell', b'for p in %s; do echo "$p"; done' % (path,)]) as stdout:
            for line in stdout:
                yield line.rstrip(b'\r\n')

    def Push(self, src: bytes, dst: bytes) -> None:
        """Push a file from the local file system to the Android device."""
        if subprocess.call(self.adb + [b'push', src, dst]) != 0:
            raise OSError('push failed')

    def Pull(self, src: bytes, dst: bytes) -> None:
        """Pull a file from the Android device to the local file system."""
        if subprocess.call(self.adb + [b'pull', src, dst]) != 0:
            raise OSError('pull failed')
