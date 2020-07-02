import asyncio
import configparser
import datetime
import hashlib
import html
import logging
import os
import sys
import time
from json import dump
from pathlib import Path
from shutil import rmtree
from threading import RLock
from typing import Awaitable, Dict, List, Optional, Set, Tuple, Union
from unittest.mock import Mock
from urllib.parse import unquote, urlparse

from filelock import Timeout
from packaging.utils import canonicalize_name

from . import utils
from .configuration import validate_config_values
from .errors import PackageNotFound
from .filter import LoadedFilters
from .master import Master
from .package import Package
from .storage import Storage, storage_backend_plugins

LOG_PLUGINS = True
logger = logging.getLogger(__name__)


class BandersnatchState:
    def __init__(self, storage_backend: Storage, homedir: Path) -> None:
        self.storage_backend = storage_backend
        self.homedir = self.storage_backend.PATH_BACKEND(homedir)

    @property
    def todolist(self) -> Path:
        return self.storage_backend.PATH_BACKEND(self.homedir) / "todo"

    @property
    def lockfile(self) -> Path:
        return self.storage_backend.PATH_BACKEND(self.homedir) / ".lock"

    @property
    def statusfile(self) -> Path:
        return self.storage_backend.PATH_BACKEND(self.homedir) / "status"

    @property
    def generationfile(self) -> Path:
        return self.storage_backend.PATH_BACKEND(self.homedir) / "generation"

    def reset(self) -> None:
        for path in [self.statusfile, self.todolist]:
            if path.exists():
                path.unlink()

    def clean_todo(self) -> None:
        if self.todolist.exists():
            self.todolist.unlink()

    def validate_todofile(self) -> None:
        # Does a couple of cleanup tasks to ensure consistent data for later
        # processing.
        if self.storage_backend.exists(self.todolist):
            try:
                with self.storage_backend.open_file(self.todolist, text=True) as fh:
                    saved_todo = iter(fh)
                    int(next(saved_todo).strip())
                    for line in saved_todo:
                        _, serial = line.strip().split(maxsplit=1)
                        int(serial)
            except (StopIteration, ValueError, TypeError):
                # The todo list was inconsistent. This may happen if we get
                # killed e.g. by the timeout wrapper. Just remove it - we'll
                # just have to do whatever happened since the last successful
                # sync.
                logger.error("Removing inconsistent todo list.")
                self.storage_backend.delete_file(self.todolist)

    def load_todofile(self) -> Optional[Tuple[int, dict]]:
        self.validate_todofile()

        target_serial = 0  # what is the serial we are trying to reach?
        packages_to_sync = {}
        if self.storage_backend.exists(self.todolist):
            # We started a sync previously and left a todo list as well as the
            # targeted serial. We'll try to keep going through the todo list
            # and then mark the targeted serial as done.
            with self.storage_backend.open_file(self.todolist, text=True) as fh:
                saved_todo = iter(fh)
                target_serial = int(next(saved_todo).strip())
                for line in saved_todo:
                    package, serial = line.strip().split()
                    packages_to_sync[package] = int(serial)
            return (target_serial, packages_to_sync)

    def update_todofile(self, target_serial: int, packages_to_sync: dict) -> None:
        with self.storage_backend.update_safe(
            self.todolist, mode="w+", encoding="utf-8"
        ) as f:
            # First line is the target serial we're working on.
            f.write(f"{target_serial}\n")
            # Consecutive lines are the packages we still have to sync
            todo = [f"{name_} {serial}" for name_, serial in packages_to_sync.items()]
            f.write("\n".join(todo))

    def update_status(self, new_serial: int) -> None:
        self.statusfile.write_text(str(new_serial), encoding="ascii")

    def get_status(self) -> Optional[int]:
        if self.statusfile.exists():
            return int(self.statusfile.read_text(encoding="ascii").strip())

    def get_generation(self) -> int:
        return int(self.generationfile.read_text(encoding="ascii").strip())

    def update_generation(self, generation: int) -> None:
        self.generationfile.write_text(str(generation), encoding="ascii")

    def load_serial(self, flock_timeout: float = 1.0) -> int:
        flock = self.storage_backend.get_lock(str(self.lockfile))
        try:
            logger.debug(f"Acquiring FLock with timeout: {flock_timeout!s}")
            with flock.acquire(timeout=flock_timeout):
                # Simple generation mechanism to support transparent software
                # updates.
                CURRENT_GENERATION = 5  # noqa
                try:
                    generation = self.get_generation()
                except ValueError:
                    logger.info(
                        "Generation file inconsistent. Reinitialising status files."
                    )
                    self.reset()
                    generation = CURRENT_GENERATION
                except OSError:
                    logger.info("Generation file missing. Reinitialising status files.")
                    # This is basically the 'install' generation: anything previous to
                    # release 1.0.2.
                    self.reset()
                    generation = CURRENT_GENERATION
                if generation in [2, 3, 4]:
                    # In generation 2 -> 3 we changed the way we generate simple
                    # page package directory names. Simply run a full update.
                    # Generation 3->4 is intended to counter a data bug on PyPI.
                    # https://bitbucket.org/pypa/bandersnatch/issue/56/setuptools-went-missing
                    # Generation 4->5 is intended to ensure that we have PEP 503
                    # compatible /simple/ URLs generated for everything.
                    self.reset()
                    generation = 5
                if generation != CURRENT_GENERATION:
                    raise RuntimeError(f"Unknown generation {generation} found")
                self.update_generation(CURRENT_GENERATION)
                # Now, actually proceed towards using the status files.
                status = self.get_status()
                if not status:
                    logger.info(
                        f"Status file {self.statusfile} missing. Starting over."
                    )
                    return 0
                return status
        except Timeout:
            logger.error("Flock timed out!")
            raise RuntimeError(
                f"Could not acquire lock on {self.lockfile}. "
                + "Another instance could be running?"
            )

    def init_dirs(self, json_dirs: bool) -> None:
        paths = [
            self.storage_backend.PATH_BACKEND(""),
            self.storage_backend.PATH_BACKEND("web/simple"),
            self.storage_backend.PATH_BACKEND("web/packages"),
            self.storage_backend.PATH_BACKEND("web/local-stats/days"),
        ]
        if json_dirs:
            logger.debug("Adding json directories to bootstrap")
            paths.extend(
                [
                    self.storage_backend.PATH_BACKEND("web/json"),
                    self.storage_backend.PATH_BACKEND("web/pypi"),
                ]
            )
        for path in paths:
            path = self.homedir / path
            if not path.exists():
                logger.info(f"Setting up mirror directory: {path}")
                path.mkdir(parents=True)


class MetadataWriter:
    need_index_sync = True
    # Allow configuring a root_uri to make generated index pages absolute.
    # This is generally not necessary, but was added for the official internal
    # PyPI mirror, which requires serving packages from
    # https://files.pythonhosted.org
    root_uri: Optional[str] = ""

    def __init__(
        self,
        storage_backend: Storage,
        bandersnatch_state: BandersnatchState,
        hash_index: bool = False,
        root_uri: Optional[str] = None,
        save_json: bool = False,
        digest_name: Optional[str] = None,
        flock_timeout: int = 1,
        keep_index_versions: int = 0,
        diff_append_epoch: bool = False,
        diff_full_path: Optional[Union[Path, str]] = None,
        diff_file_list: Optional[List] = None,
    ):
        self.storage_backend = storage_backend
        self.bandersnatch_state = bandersnatch_state
        self.homedir = self.bandersnatch_state.homedir
        self.hash_index = hash_index
        self.root_uri = root_uri or ""
        self.flock_timeout = flock_timeout

        self.keep_index_versions = keep_index_versions

        # Whether or not to mirror PyPI JSON metadata to disk
        self.save_json = save_json
        self.digest_name = digest_name if digest_name else "sha256"

        self.diff_append_epoch = diff_append_epoch
        self.diff_full_path = diff_full_path
        self.diff_file_list = diff_file_list or []

        self._finish_lock = RLock()

        self.bandersnatch_state.init_dirs(self.save_json)

    @property
    def webdir(self) -> Path:
        return self.homedir / "web"

    def _package_simple_directory(self, package: Package) -> Path:
        if self.hash_index:
            return Path(self.webdir / "simple" / package.name[0] / package.name)
        return Path(self.webdir / "simple" / package.name)

    async def cleanup_non_pep_503_paths(self, package: Package) -> None:
        """
        Before 4.0 we use to store backwards compatible named dirs for older pip
        This function checks for them and cleans them up
        """

        def raw_simple_directory() -> Path:
            if self.hash_index:
                return self.webdir / "simple" / package.raw_name[0] / package.raw_name
            return self.webdir / "simple" / package.raw_name

        def normalized_legacy_simple_directory() -> Path:
            normalized_name_legacy = utils.bandersnatch_safe_name(package.raw_name)
            if self.hash_index:
                return (
                    self.webdir
                    / "simple"
                    / normalized_name_legacy[0]
                    / normalized_name_legacy
                )
            return self.webdir / "simple" / normalized_name_legacy

        logger.debug(f"Running Non PEP503 path cleanup for {package.raw_name}")
        for deprecated_dir in (
            raw_simple_directory(),
            normalized_legacy_simple_directory(),
        ):
            # Had to compare path strs as Windows did not match path objects ...
            if str(deprecated_dir) != str(self._package_simple_directory(package)):
                if not deprecated_dir.exists():
                    logger.debug(f"{deprecated_dir} does not exist. Not cleaning up")
                    continue

                logger.info(
                    f"Attempting to cleanup non PEP 503 simple dir: {deprecated_dir}"
                )
                try:
                    rmtree(deprecated_dir)
                except Exception:
                    logger.exception(
                        f"Unable to cleanup non PEP 503 dir {deprecated_dir}"
                    )

    def gen_data_requires_python(self, release: Dict) -> str:
        if "requires_python" in release and release["requires_python"] is not None:
            return f' data-requires-python="{html.escape(release["requires_python"])}"'
        return ""

    def save_json_metadata_for_package(self, package: Package) -> bool:
        """
        Take the JSON metadata we just fetched and save to disk
        """
        json_file = Path(self.webdir / "json" / package.name)
        json_pypi_symlink = Path(self.webdir / "pypi" / package.name / "json")

        try:
            # TODO: Fix this so it works with swift
            with self.storage_backend.rewrite(json_file) as jf:
                dump(package.metadata, jf, indent=4, sort_keys=True)
            self.diff_file_list.append(json_file)
        except Exception as e:
            logger.error(f"Unable to write json to {json_file}: {str(e)} ({type(e)})")
            return False

        symlink_dir = json_pypi_symlink.parent
        symlink_dir.mkdir(exist_ok=True)
        # Lets always ensure symlink is pointing to correct self.json_file
        # In 4.0 we move to normalized name only so want to overwrite older symlinks
        if json_pypi_symlink.exists():
            json_pypi_symlink.unlink()
        json_pypi_symlink.symlink_to(json_file)

        return True

    def generate_simple_page_for_package(self, package: Package) -> str:
        # Generate the header of our simple page.
        simple_page_content = (
            "<!DOCTYPE html>\n"
            "<html>\n"
            "  <head>\n"
            "    <title>Links for {0}</title>\n"
            "  </head>\n"
            "  <body>\n"
            "    <h1>Links for {0}</h1>\n"
        ).format(package.raw_name)

        # Get a list of all of the files.
        release_files = package.release_files
        logger.debug(f"There are {len(release_files)} releases for {package.name}")
        # Lets sort based on the filename rather than the whole URL
        release_files.sort(key=lambda x: x["filename"])

        simple_page_content += "\n".join(
            [
                '    <a href="{}#{}={}"{}>{}</a><br/>'.format(
                    self._file_url_to_local_url(r["url"]),
                    self.digest_name,
                    r["digests"][self.digest_name],
                    self.gen_data_requires_python(r),
                    r["filename"],
                )
                for r in release_files
            ]
        )

        simple_page_content += (
            f"\n  </body>\n</html>\n<!--SERIAL {package.last_serial}-->"
        )

        return simple_page_content

    def write_simple_page(self, package: Package) -> None:
        logger.info(
            f"Storing index page: {package.name} - in {self._package_simple_directory(package)}"
        )
        simple_page_content = self.generate_simple_page_for_package(package)
        if not self._package_simple_directory(package).exists():
            self._package_simple_directory(package).mkdir(parents=True)

        if self.keep_index_versions > 0:
            self._save_simple_page_version(simple_page_content, package)
        else:
            simple_page = self._package_simple_directory(package) / "index.html"
            with self.storage_backend.rewrite(simple_page, "w", encoding="utf-8") as f:
                f.write(simple_page_content)
            self.diff_file_list.append(simple_page)

    def _save_simple_page_version(
        self, simple_page_content: str, package: Package
    ) -> None:
        versions_path = self._prepare_versions_path(package)
        timestamp = utils.make_time_stamp()
        version_file_name = f"index_{package.serial}_{timestamp}.html"
        full_version_path = versions_path / version_file_name
        # TODO: Change based on storage backend
        with self.storage_backend.rewrite(
            full_version_path, "w", encoding="utf-8"
        ) as f:
            f.write(simple_page_content)
        self.diff_file_list.append(full_version_path)

        symlink_path = self._package_simple_directory(package) / "index.html"
        if symlink_path.exists() or symlink_path.is_symlink():
            symlink_path.unlink()

        symlink_path.symlink_to(full_version_path)

    def _prepare_versions_path(self, package: Package) -> Path:
        versions_path = (
            self.storage_backend.PATH_BACKEND(self._package_simple_directory(package))
            / "versions"
        )
        if not versions_path.exists():
            versions_path.mkdir()
        else:
            version_files = list(sorted(versions_path.iterdir()))
            version_files_to_remove = len(version_files) - self.keep_index_versions + 1
            for i in range(version_files_to_remove):
                version_files[i].unlink()

        return versions_path

    def _file_url_to_local_url(self, url: str) -> str:
        parsed = urlparse(url)
        if not parsed.path.startswith("/packages"):
            raise RuntimeError(f"Got invalid download URL: {url}")
        prefix = self.root_uri if self.root_uri else "../.."
        return prefix + parsed.path

    # TODO: This can also return SwiftPath instances now...
    def _file_url_to_local_path(self, url: str) -> Path:
        path = urlparse(url).path
        path = unquote(path)
        if not path.startswith("/packages"):
            raise RuntimeError(f"Got invalid download URL: {url}")
        path = path[1:]
        return self.webdir / path

    # TODO: This can return SwiftPath types now
    def get_simple_dirs(self, simple_dir: Path) -> List[Path]:
        """Return a list of simple index directories that should be searched
        for package indexes when compiling the main index page."""
        if self.hash_index:
            # We are using index page directory hashing, so the directory
            # format is /simple/f/foo/.  We want to return a list of dirs
            # like "simple/f".
            subdirs = [simple_dir / x for x in simple_dir.iterdir() if x.is_dir()]
        else:
            # This is the traditional layout of /simple/foo/.  We should
            # return a single directory, "simple".
            subdirs = [simple_dir]
        return subdirs

    def find_package_indexes_in_dir(self, simple_dir: Path) -> List[str]:
        """Given a directory that contains simple packages indexes, return
        a sorted list of normalized package names.  This presumes every
        directory within is a simple package index directory."""
        simple_path = self.storage_backend.PATH_BACKEND(simple_dir)
        return sorted(
            {
                # Filter out all of the "non" normalized names here
                canonicalize_name(x.name)
                for x in simple_path.iterdir()
                # Package indexes must be in directories, so ignore anything else.
                # This allows us to rely on the storage plugin to check if this is
                # a directory
                if x.is_dir()
            }
        )

    def write_index_page(self) -> None:
        if not self.need_index_sync:
            return
        logger.info("Generating global index page.")
        simple_dir = self.webdir / "simple"
        with self.storage_backend.rewrite(str(simple_dir / "index.html")) as f:
            f.write("<!DOCTYPE html>\n")
            f.write("<html>\n")
            f.write("  <head>\n")
            f.write("    <title>Simple Index</title>\n")
            f.write("  </head>\n")
            f.write("  <body>\n")
            # This will either be the simple dir, or if we are using index
            # directory hashing, a list of subdirs to process.
            for subdir in self.get_simple_dirs(simple_dir):
                for pkg in self.find_package_indexes_in_dir(subdir):
                    # We're really trusty that this is all encoded in UTF-8. :/
                    f.write(f'    <a href="{pkg}/">{pkg}</a><br/>\n')
            f.write("  </body>\n</html>")
        self.diff_file_list.append(simple_dir / "index.html")


class Mirror:

    synced_serial = 0
    target_serial = None  # What is the serial we are trying to reach?
    errors = False
    packages_to_sync: Dict[str, Union[int, str]] = {}

    # Stop soon after meeting an error. Continue without updating the
    # mirror's serial if false.
    stop_on_error = False

    # We are required to leave a 'last changed' timestamp. I'd rather err
    # on the side of giving a timestamp that is too old so we keep track
    # of it when starting to sync.
    now = None

    def __init__(
        self,
        master: Master,
        writer: MetadataWriter,
        bandersnatch_state: BandersnatchState,
        stop_on_error: bool = False,
        workers: int = 3,
    ):
        self.master = master
        self.writer = writer
        self.filters = LoadedFilters(load_all=True)
        self.bandersnatch_state = bandersnatch_state
        self.stop_on_error = stop_on_error
        self.workers = workers
        if self.workers > 10:
            raise ValueError("Downloading with more than 10 workers is not allowed.")

        # Lets record and report back the changes we do each run
        # Format: dict['pkg_name'] = [set(removed), Set[added]
        # Class Instance variable so each package can add their changes
        self.altered_packages: Dict[str, Set[str]] = {}

    async def synchronize(
        self, specific_packages: Optional[List[str]] = None
    ) -> Dict[str, Set[str]]:
        logger.info(f"Syncing with {self.master.url}.")
        self.now = datetime.datetime.utcnow()
        # Lets ensure we get a new dict each run
        # - others importing may not reset this like our main.py
        self.altered_packages = {}

        if specific_packages is None:
            # Changelog-based synchronization
            await self.determine_packages_to_sync()
            await self.sync_packages()
            self.writer.write_index_page()
            self.finalize_sync()
        else:
            # Synchronize specific packages. This method doesn't update the statusfile
            # Pass serial number 0 to bypass the stale serial check in Package class
            SERIAL_DONT_CARE = 0
            self.packages_to_sync = {
                utils.bandersnatch_safe_name(name): SERIAL_DONT_CARE
                for name in specific_packages
            }
            await self.sync_packages()
            self.writer.write_index_page()

        return self.altered_packages

    def _filter_packages(self) -> None:
        """
        Run the package filtering plugins and remove any packages from the
        packages_to_sync that match any filters.
        - Logging of action will be done within the check_match methods
        """
        global LOG_PLUGINS

        filter_plugins = self.filters.filter_project_plugins()
        if not filter_plugins:
            if LOG_PLUGINS:
                logger.info("No project filters are enabled. Skipping filtering")
                LOG_PLUGINS = False
            return

        # Make a copy of self.packages_to_sync keys
        # as we may delete packages during iteration
        packages = list(self.packages_to_sync.keys())
        for package_name in packages:
            if not all(
                plugin.filter({"info": {"name": package_name}})
                for plugin in filter_plugins
                if plugin
            ):
                if package_name not in self.packages_to_sync:
                    logger.debug(f"{package_name} not found in packages to sync")
                else:
                    del self.packages_to_sync[package_name]

    async def determine_packages_to_sync(self) -> None:
        """
        Update the self.packages_to_sync to contain packages that need to be
        synced.
        """
        raise NotImplementedError()

    async def package_syncer(self, idx: int) -> None:
        logger.debug(f"Package syncer {idx} started for duty")
        while True:
            try:
                package = self.package_queue.get_nowait()
                await package.update_metadata(self.master, attempts=3)
                await self.process_package(package)
            except asyncio.QueueEmpty:
                logger.debug(f"Package syncer {idx} emptied queue")
                break
            except PackageNotFound:
                return
            except Exception:
                logger.exception(
                    f"Error syncing package: {package.name}@{package.serial}"
                )
                self.errors = True

            if self.errors and self.stop_on_error:
                logger.error("Exiting early after error.")
                sys.exit(1)


    async def process_package(self, package: Package) -> None:
        raise NotImplementedError()

    async def sync_release_files_for_package(self, package) -> None:
        """ Purge + download files returning files removed + added """
        downloaded_files = set()
        deferred_exception = None
        for release_file in package.release_files:
            url = release_file["url"]
            path = self.writer._file_url_to_local_path(release_file["url"])
            sha256sum = release_file["digests"]["sha256"]
            # Avoid downloading again if we have the file and it matches the hash.
            if path.exists():
                existing_hash = self.writer.storage_backend.get_hash(str(path))
                if existing_hash == sha256sum:
                    return None
                else:
                    logger.info(
                        f"Checksum mismatch with local file {path}: expected {sha256sum} "
                        + f"got {existing_hash}, will re-download."
                    )
                    path.unlink()

            logger.info(f"Downloading: {url}")
            try:
                downloaded_file = await self.download_file(url, path, sha256sum)
                if downloaded_file:
                    downloaded_files.add(
                        str(
                            downloaded_file.relative_to(self.bandersnatch_state.homedir)
                        )
                    )
            except Exception as e:
                logger.exception(
                    "Continuing to next file after error downloading: "
                    f"{release_file['url']}"
                )
                if not deferred_exception:  # keep first exception
                    deferred_exception = e
        if deferred_exception:
            raise deferred_exception  # raise the exception after trying all files

        self.altered_packages[package.name] = downloaded_files

    # TODO: This can also return SwiftPath instances now...
    async def download_file(
        self, url: str, path: Path, sha256sum: str, chunk_size: int = 64 * 1024
    ) -> Optional[Path]:
        dirname = path.parent
        if not dirname.exists():
            dirname.mkdir(parents=True)

        # Even more special handling for the serial of package files here:
        # We do not need to track a serial for package files
        # as PyPI generally only allows a file to be uploaded once
        # and then maybe deleted. Re-uploading (and thus changing the hash)
        # is only allowed in extremely rare cases with intervention from the
        # PyPI admins.
        r_generator = self.master.get(url, required_serial=None)
        response = await r_generator.asend(None)

        checksum = hashlib.sha256()

        with self.writer.storage_backend.rewrite(path, "wb") as f:
            while True:
                chunk = await response.content.read(chunk_size)
                if not chunk:
                    break
                checksum.update(chunk)
                f.write(chunk)

            existing_hash = checksum.hexdigest()
            if existing_hash != sha256sum:
                # Bad case: the file we got does not match the expected
                # checksum. Even if this should be the rare case of a
                # re-upload this will fix itself in a later run.
                raise ValueError(
                    f"Inconsistent file. {url} has hash {existing_hash} "
                    + f"instead of {sha256sum}."
                )

        return path

    async def sync_packages(self) -> None:
        self.package_queue: asyncio.Queue = asyncio.Queue()
        # Sorting the packages alphabetically makes it more predictable:
        # easier to debug and easier to follow in the logs.
        for name in sorted(self.packages_to_sync):
            serial = int(self.packages_to_sync[name])
            await self.package_queue.put(Package(name, serial=serial))

        sync_coros: List[Awaitable] = [
            self.package_syncer(idx) for idx in range(self.workers)
        ]
        try:
            await asyncio.gather(*sync_coros)
        except KeyboardInterrupt:
            # Setting self.errors to True to ensure we don't save Serial
            # and thus save to disk that we've had a successful sync
            self.errors = True
            logger.info(
                "Cancelling, all downloads are forcibly stopped, data may be "
                + "corrupted. Serial will not be saved to disk. "
                + "Next sync will start from previous serial"
            )

    def finalize_sync(self) -> None:
        return None


class BandersnatchMirror(Mirror):

    def __init__(self, *args, cleanup, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.cleanup = cleanup

        self.synced_serial = self.bandersnatch_state.load_serial(
            self.writer.flock_timeout
        )

    async def determine_packages_to_sync(self) -> None:
        """
        Update the self.packages_to_sync to contain packages that need to be
        synced.
        """
        # In case we don't find any changes we will stay on the currently
        # synced serial.
        self.target_serial = self.synced_serial
        self.packages_to_sync = {}
        logger.info(f"Current mirror serial: {self.synced_serial}")

        todo_state = self.bandersnatch_state.load_todofile()
        if todo_state:
            logger.info("Resuming interrupted sync from local todo list.")
            (self.target_serial, self.packages_to_sync) = todo_state
        elif not self.synced_serial:
            logger.info("Syncing all packages.")
            # First get the current serial, then start to sync. This makes us
            # more defensive in case something changes on the server between
            # those two calls.
            all_packages = await self.master.all_packages()
            self.packages_to_sync.update(all_packages)
            self.target_serial = max(
                [self.synced_serial] + [int(v) for v in self.packages_to_sync.values()]
            )
        else:
            logger.info("Syncing based on changelog.")
            changed_packages = await self.master.changed_packages(self.synced_serial)
            self.packages_to_sync.update(changed_packages)
            self.target_serial = max(
                [self.synced_serial] + [int(v) for v in self.packages_to_sync.values()]
            )
            # We can avoid writing the main index page if we don't have
            # anything todo at all during a changelog-based sync.
            self.writer.need_index_sync = bool(self.packages_to_sync)

        self._filter_packages()
        logger.info(f"Trying to reach serial: {self.target_serial}")
        pkg_count = len(self.packages_to_sync)
        logger.info(f"{pkg_count} packages to sync.")

    async def process_package(self, package: Package) -> None:
        # Don't save anything if our metadata filters all fail.
        if not package._filter_metadata(self.filters.filter_metadata_plugins()):
            return

        # save the metadata before filtering releases
        if self.writer.save_json:
            loop = asyncio.get_event_loop()
            json_saved = await loop.run_in_executor(
                None, self.writer.save_json_metadata_for_package, package
            )
            assert json_saved

        package._filter_all_releases_files(
            self.filters.filter_release_file_plugins()
        )
        package._filter_all_releases(self.filters.filter_release_plugins())

        await self.sync_release_files_for_package(package)
        self.writer.write_simple_page(package)
        # XMLRPC PyPI Endpoint stores raw_name so we need to provide it
        with self.writer._finish_lock:
            del self.packages_to_sync[package.raw_name]
            self.bandersnatch_state.update_todofile(
                self.target_serial, self.packages_to_sync
            )

        # Cleanup old legacy non PEP 503 Directories created for the Simple API
        if self.cleanup:
            # Cleanup non normalized name directory
            await self.writer.cleanup_non_pep_503_paths(package)

    async def finalize_sync(self) -> None:
        if self.errors:
            return
        self.synced_serial = int(self.target_serial) if self.target_serial else 0
        self.bandersnatch_state.clean_todo()
        logger.info(f"New mirror serial: {self.synced_serial}")

        if not self.now:
            logger.error(
                "strftime did not return a valid time - Not updating last modified"
            )
            return

        with self.writer.storage_backend.rewrite(
            str(self.bandersnatch_state.homedir / "web" / "last-modified")
        ) as f:
            f.write(self.now.strftime("%Y%m%dT%H:%M:%S\n"))
        self.bandersnatch_state.update_status(self.synced_serial)


async def mirror(
    config: configparser.ConfigParser, specific_packages: Optional[List[str]] = None
) -> int:

    config_values = validate_config_values(config)

    storage_plugin = next(
        iter(
            storage_backend_plugins(
                config_values.storage_backend_name, config=config, clear_cache=True
            )
        )
    )

    diff_file = storage_plugin.PATH_BACKEND(config_values.diff_file_path)
    diff_full_path: Union[Path, str]
    if diff_file:
        diff_file.parent.mkdir(exist_ok=True, parents=True)
        if config_values.diff_append_epoch:
            diff_full_path = diff_file.with_name(f"{diff_file.name}-{int(time.time())}")
        else:
            diff_full_path = diff_file
    else:
        diff_full_path = ""

    if diff_full_path:
        if isinstance(diff_full_path, str):
            diff_full_path = storage_plugin.PATH_BACKEND(diff_full_path)
        if diff_full_path.is_file():
            diff_full_path.unlink()
        elif diff_full_path.is_dir():
            diff_full_path = diff_full_path / "mirrored-files"

    mirror_url = config.get("mirror", "master")
    timeout = config.getfloat("mirror", "timeout")
    global_timeout = config.getfloat("mirror", "global-timeout", fallback=None)
    storage_backend = config_values.storage_backend_name
    homedir = Path(config.get("mirror", "directory"))

    if storage_backend:
        storage_backend = next(iter(storage_backend_plugins(storage_backend)))
    else:
        storage_backend = next(iter(storage_backend_plugins()))

    bandersnatch_state = BandersnatchState(storage_backend, homedir)

    writer = MetadataWriter(
        storage_backend=storage_backend,
        bandersnatch_state=bandersnatch_state,
        hash_index=config.getboolean("mirror", "hash-index"),
        root_uri=config_values.root_uri,
        save_json=config_values.save_json,
        keep_index_versions=config.getint("mirror", "keep_index_versions", fallback=0),
        digest_name=config_values.digest_name,
        diff_append_epoch=config_values.diff_append_epoch,
        diff_full_path=diff_full_path if diff_full_path else None,
    )

    # Always reference those classes here with the fully qualified name to
    # allow them being patched by mock libraries!
    async with Master(mirror_url, timeout, global_timeout) as master:
        mirror = BandersnatchMirror(
            master,
            writer,
            bandersnatch_state,
            stop_on_error=config.getboolean("mirror", "stop-on-error"),
            workers=config.getint("mirror", "workers"),
            cleanup=config_values.cleanup,
        )

        # TODO: Remove this terrible hack and async mock the code correctly
        # This works around "TypeError: object
        # MagicMock can't be used in 'await' expression"
        changed_packages: Dict[str, Set[str]] = {}
        if not isinstance(mirror, Mock):  # type: ignore
            changed_packages = await mirror.synchronize(specific_packages)

    logger.info(f"{len(changed_packages)} packages had changes")
    for package_name, changes in changed_packages.items():
        for change in changes:
            writer.diff_file_list.append(writer.homedir / change)
        loggable_changes = [str(chg) for chg in writer.diff_file_list]
        logger.debug(f"{package_name} added: {loggable_changes}")

    if writer.diff_full_path:
        logger.info(f"Writing diff file to {writer.diff_full_path}")
        diff_text = f"{os.linesep}".join(
            [str(chg.absolute()) for chg in writer.diff_file_list]
        )
        diff_file = writer.storage_backend.PATH_BACKEND(writer.diff_full_path)
        diff_file.write_text(diff_text)

    return 0
