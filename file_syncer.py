import glob
import logging
import os
import stat
import time
from types import TracebackType
from typing import cast, List, Tuple, Callable, Iterable, Optional, Type, Union, Sequence

from adb_file_system import AdbFileSystem
from glob_like import GlobLike
from os_like import OSLike


class FileSyncer(object):
    """File synchronizer."""

    def __init__(self, adb: AdbFileSystem, local_path: bytes, remote_path: bytes, excludes: bytes,
                 local_to_remote: bool, remote_to_local: bool,
                 preserve_times: bool, delete_missing: bool,
                 allow_overwrite: bool, allow_replace: bool, copy_links: bool,
                 dry_run: bool, *, time_range: Union[Sequence[int], None]) -> None:
        self.local = local_path
        self.remote = remote_path
        self.excludes = excludes
        self.adb = adb
        self.local_to_remote = local_to_remote
        self.remote_to_local = remote_to_local
        self.preserve_times = preserve_times
        self.delete_missing = delete_missing
        self.allow_overwrite = allow_overwrite
        self.allow_replace = allow_replace
        self.copy_links = copy_links
        self.dry_run = dry_run
        self.num_bytes = 0
        self.time_range = time_range
        self.start_time = time.time()

    # Attributes filled in later.
    local_only = None  # type: List[Tuple[bytes, os.stat_result]]
    both = None  # type: List[Tuple[bytes, os.stat_result, os.stat_result]]
    remote_only = None  # type: List[Tuple[bytes, os.stat_result]]
    src_to_dst = None  # type: Tuple[bool, bool]
    dst_to_src = None  # type: Tuple[bool, bool]
    src_only = None  # type: Tuple[List[Tuple[bytes, os.stat_result]], List[Tuple[bytes, os.stat_result]]]
    dst_only = None  # type: Tuple[List[Tuple[bytes, os.stat_result]], List[Tuple[bytes, os.stat_result]]]
    src = None  # type: Tuple[bytes, bytes]
    dst = None  # type: Tuple[bytes, bytes]
    dst_fs = None  # type: Tuple[OSLike, OSLike]
    push = None  # type: Tuple[str, str]
    copy = None  # type: Tuple[Callable[[bytes, bytes], None], Callable[[bytes, bytes], None]]

    def IsWorking(self) -> bool:
        """Tests the adb connection."""
        return self.adb.IsWorking()

    def ScanAndDiff(self) -> None:
        """Scans the local and remote locations and identifies differences."""
        logging.info('Scanning and diffing...')
        locallist = BuildFileList(
            cast(OSLike, os), self.local, self.copy_links, b'', self.excludes, time_range=self.time_range)
        remotelist = BuildFileList(self.adb, self.remote, self.copy_links, b'',
                                   self.excludes, time_range=self.time_range)
        self.local_only, self.both, self.remote_only = DiffLists(
            locallist, remotelist)
        if not self.local_only and not self.both and not self.remote_only:
            logging.warning('No files seen. User error?')
        self.src_to_dst = (self.local_to_remote, self.remote_to_local)
        self.dst_to_src = (self.remote_to_local, self.local_to_remote)
        self.src_only = (self.local_only, self.remote_only)
        self.dst_only = (self.remote_only, self.local_only)
        self.src = (self.local, self.remote)
        self.dst = (self.remote, self.local)
        self.dst_fs = (self.adb, cast(OSLike, os))
        self.push = ('Push', 'Pull')
        self.copy = (self.adb.Push, self.adb.Pull)

    def PerformDeletions(self) -> None:
        """Perform all deleting necessary for the file sync operation."""
        if not self.delete_missing:
            return
        for i in [0, 1]:
            if self.src_to_dst[i] and not self.dst_to_src[i]:
                if not self.src_only[i] and not self.both:
                    logging.error('Cowardly refusing to delete everything.')
                else:
                    for name, s in reversed(self.dst_only[i]):
                        dst_name = self.dst[i] + name
                        logging.info('%s-Delete: %r', self.push[i], dst_name)
                        if stat.S_ISDIR(s.st_mode):
                            if not self.dry_run:
                                self.dst_fs[i].rmdir(dst_name)
                        else:
                            if not self.dry_run:
                                self.dst_fs[i].unlink(dst_name)
                    del self.dst_only[i][:]

    def PerformOverwrites(self) -> None:
        """Delete files/directories that are in the way for overwriting."""
        src_only_prepend = (
            [], []
        )  # type: Tuple[List[Tuple[bytes, os.stat_result]], List[Tuple[bytes, os.stat_result]]]
        for name, localstat, remotestat in self.both:
            if stat.S_ISDIR(localstat.st_mode) and stat.S_ISDIR(remotestat.st_mode):
                # A dir is a dir is a dir.
                continue
            elif stat.S_ISDIR(localstat.st_mode) or stat.S_ISDIR(remotestat.st_mode):
                # Dir vs file? Nothing to do here yet.
                pass
            else:
                # File vs file? Compare sizes.
                if localstat.st_size == remotestat.st_size:
                    continue
            l2r = self.local_to_remote
            r2l = self.remote_to_local
            if l2r and r2l:
                # Truncate times to full minutes, as Android's "ls" only outputs minute
                # accuracy.
                localminute = int(localstat.st_mtime / 60)
                remoteminute = int(remotestat.st_mtime / 60)
                if localminute > remoteminute:
                    r2l = False
                elif localminute < remoteminute:
                    l2r = False
            if l2r and r2l:
                logging.warning('Unresolvable: %r', name)
                continue
            if l2r:
                i = 0  # Local to remote operation.
                src_stat = localstat
                dst_stat = remotestat
            else:
                i = 1  # Remote to local operation.
                src_stat = remotestat
                dst_stat = localstat
            dst_name = self.dst[i] + name
            logging.info('%s-Delete-Conflicting: %r', self.push[i], dst_name)
            if stat.S_ISDIR(localstat.st_mode) or stat.S_ISDIR(remotestat.st_mode):
                if not self.allow_replace:
                    logging.info('Would have to replace to do this. '
                                 'Use --force to allow this.')
                    continue
            if not self.allow_overwrite:
                logging.info('Would have to overwrite to do this, '
                             'which --no-clobber forbids.')
                continue
            if stat.S_ISDIR(dst_stat.st_mode):
                kill_files = [
                    x for x in self.dst_only[i] if x[0][:len(name) + 1] == name + b'/'
                ]
                self.dst_only[i][:] = [
                    x for x in self.dst_only[i] if x[0][:len(name) + 1] != name + b'/'
                ]
                for l, s in reversed(kill_files):
                    if stat.S_ISDIR(s.st_mode):
                        if not self.dry_run:
                            self.dst_fs[i].rmdir(self.dst[i] + l)
                    else:
                        if not self.dry_run:
                            self.dst_fs[i].unlink(self.dst[i] + l)
                if not self.dry_run:
                    self.dst_fs[i].rmdir(dst_name)
            elif stat.S_ISDIR(src_stat.st_mode):
                if not self.dry_run:
                    self.dst_fs[i].unlink(dst_name)
            else:
                if not self.dry_run:
                    self.dst_fs[i].unlink(dst_name)
            src_only_prepend[i].append((name, src_stat))
        for i in [0, 1]:
            self.src_only[i][:0] = src_only_prepend[i]

    def PerformCopies(self) -> None:
        """Perform all copying necessary for the file sync operation."""
        for i in [0, 1]:
            if self.src_to_dst[i]:
                for name, s in self.src_only[i]:
                    src_name = self.src[i] + name
                    dst_name = self.dst[i] + name
                    logging.info('%s: %r', self.push[i], src_name.decode("utf-8", "replace")
                                 + " -> " + dst_name.decode("utf-8", "replace"))
                    if stat.S_ISDIR(s.st_mode):
                        if not self.dry_run:
                            self.dst_fs[i].makedirs(dst_name)
                    else:
                        with DeleteInterruptedFile(self.dry_run, self.dst_fs[i], dst_name):
                            if not self.dry_run:
                                self.copy[i](src_name, dst_name)
                            if stat.S_ISREG(s.st_mode):
                                self.num_bytes += s.st_size
                    if not self.dry_run:
                        if self.preserve_times:
                            logging.info('%s-Times: accessed %s, modified %s', self.push[i],
                                         time.asctime(time.localtime(s.st_atime)),
                                         time.asctime(time.localtime(s.st_mtime)))
                            self.dst_fs[i].utime(dst_name, (s.st_atime, s.st_mtime))

    def TimeReport(self) -> None:
        """Report time and amount of data transferred."""
        if self.dry_run:
            logging.info('Total: %d bytes', self.num_bytes)
        else:
            end_time = time.time()
            dt = end_time - self.start_time
            rate = self.num_bytes / 1024.0 / dt
            logging.info('Total: %d KB/s (%d bytes in %.3fs)', rate, self.num_bytes,
                         dt)


def BuildFileList(
        fs: OSLike, path: bytes, follow_links: bool, prefix: bytes, excludes: str, exlist=None, *, time_range
) -> Iterable[Tuple[bytes, os.stat_result]]:
    """Builds a file list.

    Args:
      fs: File system provider (can be os or AdbFileSystem()).
      path: Initial path.
      follow_links: Whether to follow symlinks while iterating. May recurse
        endlessly.
      prefix: Path prefix for output file names.

    Yields:
      File names from path (prefixed by prefix).
      Directories are yielded before their contents.
    """
    if exlist is None:
        exlist = []
    try:
        if follow_links:
            statresult = fs.stat(path)
        else:
            statresult = fs.lstat(path)
    except OSError:
        return
    if stat.S_ISDIR(statresult.st_mode):
        yield prefix, statresult
        try:
            files = fs.listdir(path)
        except OSError:
            return
        for x in excludes:
            exlist.extend(glob.glob(path + b"/" + x.encode()))
            try:
                exlist.extend(fs.glob(path + b"/" + x.encode()))
            except:
                pass
        for n in files:
            if n == b'.' or n == b'..' or ((path + b'/' + n) in exlist) or (n.decode() in excludes):
                continue
            else:
                for t in BuildFileList(fs, path + b'/' + n, follow_links,
                                       prefix + b'/' + n, excludes, exlist, time_range=time_range):
                    if t not in exlist:
                        yield t
    elif stat.S_ISREG(statresult.st_mode):
        if within_time_range(statresult, time_range):
            yield prefix, statresult
    elif stat.S_ISLNK(statresult.st_mode) and not follow_links:
        if within_time_range(statresult, time_range):
            yield prefix, statresult
    else:
        logging.info('Unsupported file: %r.', path)


def within_time_range(statresult, time_range):
    return time_range is None or time_range[0] <= statresult.st_mtime <= time_range[1]


def DiffLists(a: Iterable[Tuple[bytes, os.stat_result]],
              b: Iterable[Tuple[bytes, os.stat_result]]
              ) -> Tuple[List[Tuple[bytes, os.stat_result]], List[
    Tuple[bytes, os.stat_result, os
        .stat_result]], List[Tuple[bytes, os.stat_result]]]:
    """Compares two lists.

    Args:
      a: the first list.
      b: the second list.

    Returns:
      a_only: the items from list a.
      both: the items from both list, with the remaining tuple items combined.
      b_only: the items from list b.
    """
    a_only = []  # type: List[Tuple[bytes, os.stat_result]]
    b_only = []  # type: List[Tuple[bytes, os.stat_result]]
    both = []  # type: List[Tuple[bytes, os.stat_result, os.stat_result]]

    a_revlist = sorted(a)
    a_revlist.reverse()
    b_revlist = sorted(b)
    b_revlist.reverse()

    while True:
        if not a_revlist:
            b_only.extend(reversed(b_revlist))
            break
        if not b_revlist:
            a_only.extend(reversed(a_revlist))
            break
        a_item = a_revlist[len(a_revlist) - 1]
        b_item = b_revlist[len(b_revlist) - 1]
        if a_item[0] == b_item[0]:
            both.append((a_item[0], a_item[1], b_item[1]))
            a_revlist.pop()
            b_revlist.pop()
        elif a_item[0] < b_item[0]:
            a_only.append(a_item)
            a_revlist.pop()
        elif a_item[0] > b_item[0]:
            b_only.append(b_item)
            b_revlist.pop()
        else:
            raise

    return a_only, both, b_only


def ExpandWildcards(globber: GlobLike, path: bytes) -> Iterable[bytes]:
    if path.find(b'?') == -1 and path.find(b'*') == -1 and path.find(b'[') == -1:
        return [path]
    return globber.glob(path)


def FixPath(src: bytes, dst: bytes) -> Tuple[bytes, bytes]:
    # rsync-like path munging to make remote specifications shorter.
    append = b''
    pos = src.rfind(b'/')
    if pos >= 0:
        if src.endswith(b'/'):
            # Final slash: copy to the destination "as is".
            pass
        else:
            # No final slash: destination name == source name.
            append = src[pos:]
    else:
        # No slash at all - use same name at destination.
        append = b'/' + src
    # Append the destination file name if any.
    # BUT: do not append "." or ".." components!
    if append != b'/.' and append != b'/..':
        dst += append
    return (src, dst)


class DeleteInterruptedFile(object):

    def __init__(self, dry_run: bool, fs: OSLike, name: bytes) -> None:
        """Sets up interrupt protection.

        Usage:
          with DeleteInterruptedFile(False, fs, name):
            DoSomething()

          If DoSomething() should get interrupted, the file 'name' will be deleted.
          The exception otherwise will be passed on.

        Args:
          dry_run: If true, we don't actually delete.
          fs: File system object.
          name: File name to delete.

        Returns:
          An object for use by 'with'.
        """
        self.dry_run = dry_run
        self.fs = fs
        self.name = name

    def __enter__(self) -> None:
        pass

    def __exit__(self, exc_type: Optional[Type[BaseException]],
                 exc_val: Optional[Exception],
                 exc_tb: Optional[TracebackType]) -> bool:
        if exc_type is not None:
            logging.info('Interrupted-%s-Delete: %r',
                         'Pull' if self.fs == os else 'Push', self.name)
            if not self.dry_run:
                self.fs.unlink(self.name)
        return False
