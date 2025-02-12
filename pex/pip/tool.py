# coding=utf-8
# Copyright 2019 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import absolute_import, print_function

import os
import re
import subprocess
import sys
from collections import deque
from tempfile import mkdtemp

from pex import dist_metadata, targets
from pex.auth import PasswordEntry
from pex.common import safe_mkdir, safe_mkdtemp
from pex.compatibility import get_stderr_bytes_buffer, shlex_quote, urlparse
from pex.interpreter import PythonInterpreter
from pex.jobs import Job
from pex.network_configuration import NetworkConfiguration
from pex.pep_376 import Record
from pex.pep_425 import CompatibilityTags
from pex.pip import foreign_platform
from pex.pip.download_observer import DownloadObserver, PatchSet
from pex.pip.log_analyzer import ErrorAnalyzer, ErrorMessage, LogAnalyzer, LogScrapeJob
from pex.pip.tailer import Tailer
from pex.pip.version import PipVersion, PipVersionValue
from pex.platforms import Platform
from pex.resolve.resolver_configuration import ResolverVersion
from pex.targets import LocalInterpreter, Target
from pex.tracer import TRACER
from pex.typing import TYPE_CHECKING
from pex.variables import ENV

if TYPE_CHECKING:
    from typing import (
        Any,
        Callable,
        Dict,
        Iterable,
        Iterator,
        List,
        Mapping,
        Optional,
        Sequence,
        Tuple,
    )

    import attr  # vendor:skip
else:
    from pex.third_party import attr


class PackageIndexConfiguration(object):
    @staticmethod
    def _calculate_args(
        indexes=None,  # type: Optional[Sequence[str]]
        find_links=None,  # type: Optional[Iterable[str]]
        network_configuration=None,  # type: Optional[NetworkConfiguration]
    ):
        # type: (...) -> Iterator[str]

        # N.B.: `--cert` and `--client-cert` are passed via env var to work around:
        #   https://github.com/pypa/pip/issues/5502
        # See `_calculate_env`.

        trusted_hosts = []

        def maybe_trust_insecure_host(url):
            url_info = urlparse.urlparse(url)
            if "http" == url_info.scheme:
                # Implicitly trust explicitly asked for http indexes and find_links repos instead of
                # requiring separate trust configuration.
                trusted_hosts.append(url_info.netloc)
            return url

        # N.B.: We interpret None to mean accept pip index defaults, [] to mean turn off all index
        # use.
        if indexes is not None:
            if len(indexes) == 0:
                yield "--no-index"
            else:
                all_indexes = deque(indexes)
                yield "--index-url"
                yield maybe_trust_insecure_host(all_indexes.popleft())
                if all_indexes:
                    for extra_index in all_indexes:
                        yield "--extra-index-url"
                        yield maybe_trust_insecure_host(extra_index)

        if find_links:
            for find_link_url in find_links:
                yield "--find-links"
                yield maybe_trust_insecure_host(find_link_url)

        for trusted_host in trusted_hosts:
            yield "--trusted-host"
            yield trusted_host

        network_configuration = network_configuration or NetworkConfiguration()

        yield "--retries"
        yield str(network_configuration.retries)

        yield "--timeout"
        yield str(network_configuration.timeout)

    @staticmethod
    def _calculate_env(
        network_configuration,  # type: NetworkConfiguration
        isolated,  # type: bool
    ):
        # type: (...) -> Iterator[Tuple[str, str]]
        if network_configuration.proxy:
            # We use the backdoor of the universality of http(s)_proxy env var support to continue
            # to allow Pip to operate in `--isolated` mode.
            yield "http_proxy", network_configuration.proxy
            yield "https_proxy", network_configuration.proxy

        if network_configuration.cert:
            # We use the backdoor of requests (which is vendored by Pip to handle all network
            # operations) support for REQUESTS_CA_BUNDLE when possible to continue to allow Pip to
            # operate in `--isolated` mode.
            yield (
                ("REQUESTS_CA_BUNDLE" if isolated else "PIP_CERT"),
                os.path.abspath(network_configuration.cert),
            )

        if network_configuration.client_cert:
            assert not isolated
            yield "PIP_CLIENT_CERT", os.path.abspath(network_configuration.client_cert)

    @classmethod
    def create(
        cls,
        pip_version=None,  # type: Optional[PipVersionValue]
        resolver_version=None,  # type: Optional[ResolverVersion.Value]
        indexes=None,  # type: Optional[Sequence[str]]
        find_links=None,  # type: Optional[Iterable[str]]
        network_configuration=None,  # type: Optional[NetworkConfiguration]
        password_entries=(),  # type: Iterable[PasswordEntry]
    ):
        # type: (...) -> PackageIndexConfiguration
        resolver_version = resolver_version or ResolverVersion.default(pip_version)
        network_configuration = network_configuration or NetworkConfiguration()

        # We must pass `--client-cert` via PIP_CLIENT_CERT to work around
        # https://github.com/pypa/pip/issues/5502. We can only do this by breaking Pip `--isolated`
        # mode.
        isolated = not network_configuration.client_cert

        return cls(
            pip_version=pip_version,
            resolver_version=resolver_version,
            network_configuration=network_configuration,
            args=cls._calculate_args(
                indexes=indexes, find_links=find_links, network_configuration=network_configuration
            ),
            env=cls._calculate_env(network_configuration=network_configuration, isolated=isolated),
            isolated=isolated,
            password_entries=password_entries,
        )

    def __init__(
        self,
        resolver_version,  # type: ResolverVersion.Value
        network_configuration,  # type: NetworkConfiguration
        args,  # type: Iterable[str]
        env,  # type: Iterable[Tuple[str, str]]
        isolated,  # type: bool
        password_entries=(),  # type: Iterable[PasswordEntry]
        pip_version=None,  # type: Optional[PipVersionValue]
    ):
        # type: (...) -> None
        self.resolver_version = resolver_version  # type: ResolverVersion.Value
        self.network_configuration = network_configuration  # type: NetworkConfiguration
        self.args = tuple(args)  # type: Iterable[str]
        self.env = dict(env)  # type: Mapping[str, str]
        self.isolated = isolated  # type: bool
        self.password_entries = password_entries  # type: Iterable[PasswordEntry]
        self.pip_version = pip_version  # type: Optional[PipVersionValue]


if TYPE_CHECKING:
    from pex.pip.log_analyzer import ErrorAnalysis


@attr.s
class _Issue9420Analyzer(ErrorAnalyzer):
    # Works around: https://github.com/pypa/pip/issues/9420

    _strip = attr.ib(default=None)  # type: Optional[int]

    def analyze(self, line):
        # type: (str) -> ErrorAnalysis
        # N.B.: Pip --log output looks like:
        # 2021-01-04T16:12:01,119 ERROR: Cannot install pantsbuild-pants==1.24.0.dev2 and wheel==0.33.6 because these package versions have conflicting dependencies.
        # 2021-01-04T16:12:01,119
        # 2021-01-04T16:12:01,119 The conflict is caused by:
        # 2021-01-04T16:12:01,119     The user requested wheel==0.33.6
        # 2021-01-04T16:12:01,119     pantsbuild-pants 1.24.0.dev2 depends on wheel==0.31.1
        # 2021-01-04T16:12:01,119
        # 2021-01-04T16:12:01,119 To fix this you could try to:
        # 2021-01-04T16:12:01,119 1. loosen the range of package versions you've specified
        # 2021-01-04T16:12:01,119 2. remove package versions to allow pip attempt to solve the dependency conflict
        # 2021-01-04T16:12:01,119 ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/en/latest/user_guide/#fixing-conflicting-dependencies
        if not self._strip:
            match = re.match(r"^(?P<timestamp>[^ ]+) ERROR: Cannot install ", line)
            if match:
                self._strip = len(match.group("timestamp"))
        else:
            match = re.match(r"^[^ ]+ ERROR: ResolutionImpossible: ", line)
            if match:
                return self.Complete()
            else:
                return self.Continue(ErrorMessage(line[self._strip :]))
        return self.Continue()


@attr.s(frozen=True)
class PipVenv(object):
    venv_dir = attr.ib()  # type: str
    _execute_args = attr.ib()  # type: Tuple[str, ...]

    def execute_args(self, *args):
        # type: (*str) -> List[str]
        return list(self._execute_args + args)


@attr.s(frozen=True)
class Pip(object):
    _PATCHES_PACKAGE_ENV_VAR_NAME = "_PEX_PIP_RUNTIME_PATCHES_PACKAGE"
    _PATCHES_PACKAGE_NAME = "_pex_pip_patches"

    _pip = attr.ib()  # type: PipVenv
    _pip_cache = attr.ib()  # type: str

    @staticmethod
    def _calculate_resolver_version(package_index_configuration=None):
        # type: (Optional[PackageIndexConfiguration]) -> ResolverVersion.Value
        return (
            package_index_configuration.resolver_version
            if package_index_configuration
            else ResolverVersion.default()
        )

    @classmethod
    def _calculate_resolver_version_args(
        cls,
        interpreter,  # type: PythonInterpreter
        package_index_configuration=None,  # type: Optional[PackageIndexConfiguration]
    ):
        # type: (...) -> Iterator[str]
        resolver_version = cls._calculate_resolver_version(
            package_index_configuration=package_index_configuration
        )
        # N.B.: The pip default resolver depends on the python it is invoked with. For Python 2.7
        # Pip defaults to the legacy resolver and for Python 3 Pip defaults to the 2020 resolver.
        # Further, Pip warns when you do not use the default resolver version for the interpreter
        # in play. To both avoid warnings and set the correct resolver version, we need
        # to only set the resolver version when it's not the default for the interpreter in play:
        if resolver_version == ResolverVersion.PIP_2020 and interpreter.version[0] == 2:
            yield "--use-feature"
            yield "2020-resolver"
        elif resolver_version == ResolverVersion.PIP_LEGACY and interpreter.version[0] == 3:
            yield "--use-deprecated"
            yield "legacy-resolver"

    def _spawn_pip_isolated(
        self,
        args,  # type: Iterable[str]
        package_index_configuration=None,  # type: Optional[PackageIndexConfiguration]
        interpreter=None,  # type: Optional[PythonInterpreter]
        pip_verbosity=0,  # type: int
        extra_env=None,  # type: Optional[Dict[str, str]]
        **popen_kwargs  # type: Any
    ):
        # type: (...) -> Tuple[List[str], subprocess.Popen]
        pip_args = [
            # We vendor the version of pip we want so pip should never check for updates.
            "--disable-pip-version-check",
            # If we want to warn about a version of python we support, we should do it, not pip.
            "--no-python-version-warning",
            # If pip encounters a duplicate file path during its operations we don't want it to
            # prompt and we'd also like to know about this since it should never occur. We leverage
            # the pip global option:
            # --exists-action <action>
            #   Default action when a path already exists: (s)witch, (i)gnore, (w)ipe, (b)ackup,
            #   (a)bort.
            "--exists-action",
            "a",
            # We are not interactive.
            "--no-input",
        ]
        python_interpreter = interpreter or PythonInterpreter.get()
        pip_args.extend(
            self._calculate_resolver_version_args(
                python_interpreter, package_index_configuration=package_index_configuration
            )
        )
        if not package_index_configuration or package_index_configuration.isolated:
            # Don't read PIP_ environment variables or pip configuration files like
            # `~/.config/pip/pip.conf`.
            pip_args.append("--isolated")

        # The max pip verbosity is -vvv and for pex it's -vvvvvvvvv; so we scale down by a factor
        # of 3.
        pip_verbosity = pip_verbosity or (ENV.PEX_VERBOSE // 3)
        if pip_verbosity > 0:
            pip_args.append("-{}".format("v" * pip_verbosity))
        else:
            pip_args.append("-q")

        pip_args.extend(["--cache-dir", self._pip_cache])

        command = pip_args + list(args)

        # N.B.: Package index options in Pep always have the same option names, but they are
        # registered as subcommand-specific, so we must append them here _after_ the pip subcommand
        # specified in `args`.
        if package_index_configuration:
            command.extend(package_index_configuration.args)

        extra_env = extra_env or {}
        if package_index_configuration:
            extra_env.update(package_index_configuration.env)

        # Ensure the pip cache (`http/` and `wheels/` dirs) is housed in the same partition as the
        # temporary directories it creates. This is needed to ensure atomic filesystem operations
        # since Pip relies upon `shutil.move` which is only atomic when `os.rename` can be used.
        # See https://github.com/pantsbuild/pex/issues/1776 for an example of the issues non-atomic
        # moves lead to in the `pip wheel` case.
        pip_tmpdir = os.path.join(self._pip_cache, ".tmp")
        safe_mkdir(pip_tmpdir)
        extra_env.update(TMPDIR=pip_tmpdir)

        with ENV.strip().patch(
            PEX_ROOT=ENV.PEX_ROOT,
            PEX_VERBOSE=str(ENV.PEX_VERBOSE),
            __PEX_UNVENDORED__="1",
            **extra_env
        ) as env:
            # Guard against API calls from environment with ambient PYTHONPATH preventing pip PEX
            # bootstrapping. See: https://github.com/pantsbuild/pex/issues/892
            pythonpath = env.pop("PYTHONPATH", None)
            if pythonpath:
                TRACER.log(
                    "Scrubbed PYTHONPATH={} from the pip PEX environment.".format(pythonpath), V=3
                )

            # Pip has no discernible stdout / stderr discipline with its logging. Pex guarantees
            # stdout will only contain usable (parseable) data and all logging will go to stderr.
            # To uphold the Pex standard, force Pip to comply by re-directing stdout to stderr.
            #
            # See:
            # + https://github.com/pantsbuild/pex/issues/1267
            # + https://github.com/pypa/pip/issues/9420
            if "stdout" not in popen_kwargs:
                popen_kwargs["stdout"] = sys.stderr.fileno()
            popen_kwargs.update(stderr=subprocess.PIPE)

            args = self._pip.execute_args(*command)

            rendered_env = " ".join(
                "{}={}".format(key, shlex_quote(value)) for key, value in env.items()
            )
            rendered_args = " ".join(shlex_quote(s) for s in args)
            TRACER.log("Executing: {} {}".format(rendered_env, rendered_args), V=3)

            return args, subprocess.Popen(args=args, env=env, **popen_kwargs)

    def _spawn_pip_isolated_job(
        self,
        args,  # type: Iterable[str]
        package_index_configuration=None,  # type: Optional[PackageIndexConfiguration]
        interpreter=None,  # type: Optional[PythonInterpreter]
        pip_verbosity=0,  # type: int
        finalizer=None,  # type: Optional[Callable[[int], None]]
        extra_env=None,  # type: Optional[Dict[str, str]]
        **popen_kwargs  # type: Any
    ):
        # type: (...) -> Job
        command, process = self._spawn_pip_isolated(
            args,
            package_index_configuration=package_index_configuration,
            interpreter=interpreter,
            pip_verbosity=pip_verbosity,
            extra_env=extra_env,
            **popen_kwargs
        )
        return Job(command=command, process=process, finalizer=finalizer)

    def spawn_download_distributions(
        self,
        download_dir,  # type: str
        requirements=None,  # type: Optional[Iterable[str]]
        requirement_files=None,  # type: Optional[Iterable[str]]
        constraint_files=None,  # type: Optional[Iterable[str]]
        allow_prereleases=False,  # type: bool
        transitive=True,  # type: bool
        target=None,  # type: Optional[Target]
        package_index_configuration=None,  # type: Optional[PackageIndexConfiguration]
        build=True,  # type: bool
        use_wheel=True,  # type: bool
        prefer_older_binary=False,  # type: bool
        use_pep517=None,  # type: Optional[bool]
        build_isolation=True,  # type: bool
        observer=None,  # type: Optional[DownloadObserver]
        preserve_log=False,  # type: bool
    ):
        # type: (...) -> Job
        target = target or targets.current()

        if not use_wheel:
            if not build:
                raise ValueError(
                    "Cannot both ignore wheels (use_wheel=False) and refrain from building "
                    "distributions (build=False)."
                )
            elif not isinstance(target, LocalInterpreter):
                raise ValueError(
                    "Cannot ignore wheels (use_wheel=False) when resolving for a platform: "
                    "{}".format(target.platform)
                )

        download_cmd = ["download", "--dest", download_dir]
        extra_env = {}  # type: Dict[str, str]

        if not build:
            download_cmd.extend(["--only-binary", ":all:"])

        if not use_wheel:
            download_cmd.extend(["--no-binary", ":all:"])

        if prefer_older_binary:
            download_cmd.append("--prefer-binary")

        if use_pep517 is not None:
            download_cmd.append("--use-pep517" if use_pep517 else "--no-use-pep517")

        if not build_isolation:
            download_cmd.append("--no-build-isolation")
            extra_env.update(PEP517_BACKEND_PATH=os.pathsep.join(sys.path))

        if allow_prereleases:
            download_cmd.append("--pre")

        if not transitive:
            download_cmd.append("--no-deps")

        if requirement_files:
            for requirement_file in requirement_files:
                download_cmd.extend(["--requirement", requirement_file])

        if constraint_files:
            for constraint_file in constraint_files:
                download_cmd.extend(["--constraint", constraint_file])

        if requirements:
            download_cmd.extend(requirements)

        foreign_platform_observer = foreign_platform.patch(target)
        if (
            foreign_platform_observer
            and foreign_platform_observer.patch_set.patches
            and observer
            and observer.patch_set.patches
        ):
            raise ValueError(
                "Can only have one patch for Pip code, but, in addition to patching for a foreign "
                "platform, asked to patch code for {observer}.".format(observer=observer)
            )

        log_analyzers = []  # type: List[LogAnalyzer]
        pex_extra_sys_path = []  # type: List[str]
        for obs in (foreign_platform_observer, observer):
            if obs:
                if obs.analyzer:
                    log_analyzers.append(obs.analyzer)
                extra_env.update(obs.patch_set.env)
                extra_sys_path = obs.patch_set.emit_patches(package=self._PATCHES_PACKAGE_NAME)
                if extra_sys_path:
                    pex_extra_sys_path.append(extra_sys_path)

        if pex_extra_sys_path:
            extra_env["PEX_EXTRA_SYS_PATH"] = os.pathsep.join(pex_extra_sys_path)
            extra_env[self._PATCHES_PACKAGE_ENV_VAR_NAME] = self._PATCHES_PACKAGE_NAME

        # The Pip 2020 resolver hides useful dependency conflict information in stdout interspersed
        # with other information we want to suppress. We jump though some hoops here to get at that
        # information and surface it on stderr. See: https://github.com/pypa/pip/issues/9420.
        if (
            self._calculate_resolver_version(
                package_index_configuration=package_index_configuration
            )
            == ResolverVersion.PIP_2020
        ):
            log_analyzers.append(_Issue9420Analyzer())

        log = None
        popen_kwargs = {}
        finalizer = None
        if log_analyzers:
            prefix = "pex-pip-log."
            log = os.path.join(
                mkdtemp(prefix=prefix) if preserve_log else safe_mkdtemp(prefix=prefix), "pip.log"
            )
            if preserve_log:
                TRACER.log(
                    "Preserving `pip download` log at {log_path}".format(log_path=log),
                    V=ENV.PEX_VERBOSE,
                )

            download_cmd = ["--log", log] + download_cmd
            # N.B.: The `pip -q download ...` command is quiet but
            # `pip -q --log log.txt download ...` leaks download progress bars to stdout. We work
            # around this by sending stdout to the bit bucket.
            popen_kwargs["stdout"] = open(os.devnull, "wb")

            if ENV.PEX_VERBOSE > 0:
                tailer = Tailer.tail(
                    path=log,
                    output=get_stderr_bytes_buffer(),
                    filters=(
                        re.compile(
                            r"^.*(pip is looking at multiple versions of [^\s+] to determine "
                            r"which version is compatible with other requirements\. This could "
                            r"take a while\.).*$"
                        ),
                        re.compile(
                            r"^.*(This is taking longer than usual. You might need to provide "
                            r"the dependency resolver with stricter constraints to reduce "
                            r"runtime\. If you want to abort this run, you can press "
                            r"Ctrl \+ C to do so\. To improve how pip performs, tell us what "
                            r"happened here: https://pip\.pypa\.io/surveys/backtracking).*$"
                        ),
                    ),
                )

                def finalizer(_):
                    # type: (int) -> None
                    tailer.stop()

        elif preserve_log:
            TRACER.log(
                "The `pip download` log is not being utilized, to see more `pip download` "
                "details, re-run with more Pex verbosity (more `-v`s).",
                V=ENV.PEX_VERBOSE,
            )

        command, process = self._spawn_pip_isolated(
            download_cmd,
            package_index_configuration=package_index_configuration,
            interpreter=target.get_interpreter(),
            pip_verbosity=0,
            extra_env=extra_env,
            **popen_kwargs
        )
        if log:
            return LogScrapeJob(
                command, process, log, log_analyzers, preserve_log=preserve_log, finalizer=finalizer
            )
        else:
            return Job(command, process)

    def spawn_build_wheels(
        self,
        distributions,  # type: Iterable[str]
        wheel_dir,  # type: str
        interpreter=None,  # type: Optional[PythonInterpreter]
        package_index_configuration=None,  # type: Optional[PackageIndexConfiguration]
        prefer_older_binary=False,  # type: bool
        use_pep517=None,  # type: Optional[bool]
        build_isolation=True,  # type: bool
        verify=True,  # type: bool
    ):
        # type: (...) -> Job
        wheel_cmd = ["wheel", "--no-deps", "--wheel-dir", wheel_dir]
        extra_env = {}  # type: Dict[str, str]

        # It's not clear if Pip's implementation of PEP-517 builds respects this option for
        # resolving build dependencies, but in case it is we pass it.
        if use_pep517 is not False and prefer_older_binary:
            wheel_cmd.append("--prefer-binary")

        if use_pep517 is not None:
            wheel_cmd.append("--use-pep517" if use_pep517 else "--no-use-pep517")

        if not build_isolation:
            wheel_cmd.append("--no-build-isolation")
            interpreter = interpreter or PythonInterpreter.get()
            extra_env.update(PEP517_BACKEND_PATH=os.pathsep.join(interpreter.sys_path))

        if not verify:
            wheel_cmd.append("--no-verify")

        wheel_cmd.extend(distributions)

        return self._spawn_pip_isolated_job(
            wheel_cmd,
            # If the build leverages PEP-518 it will need to resolve build requirements.
            package_index_configuration=package_index_configuration,
            interpreter=interpreter,
            extra_env=extra_env,
        )

    def spawn_install_wheel(
        self,
        wheel,  # type: str
        install_dir,  # type: str
        compile=False,  # type: bool
        target=None,  # type: Optional[Target]
    ):
        # type: (...) -> Job

        project_name_and_version = dist_metadata.project_name_and_version(wheel)
        assert project_name_and_version is not None, (
            "Should never fail to parse a wheel path into a project name and version, but "
            "failed to parse these from: {wheel}".format(wheel=wheel)
        )

        target = target or targets.current()
        interpreter = target.get_interpreter()
        if target.is_foreign:
            if compile:
                raise ValueError(
                    "Cannot compile bytecode for {} using {} because the wheel has a foreign "
                    "platform.".format(wheel, interpreter)
                )

        install_cmd = [
            "install",
            "--no-deps",
            "--no-index",
            "--only-binary",
            ":all:",
            # In `--prefix` scheme, Pip warns about installed scripts not being on $PATH. We fix
            # this when a PEX is turned into a venv.
            "--no-warn-script-location",
            # In `--prefix` scheme, Pip normally refuses to install a dependency already in the
            # `sys.path` of Pip itself since the requirement is already satisfied. Since `pip`,
            # `setuptools` and `wheel` are always in that `sys.path` (Our `pip.pex` venv PEX), we
            # force installation so that PEXes with dependencies on those projects get them properly
            # installed instead of skipped.
            "--force-reinstall",
            "--ignore-installed",
            # We're potentially installing a wheel for a foreign platform. This is just an
            # unpacking operation though; so we don't actually need to perform it with a target
            # platform compatible interpreter (except for scripts - which we deal with in fixup
            # install below).
            "--ignore-requires-python",
            "--prefix",
            install_dir,
        ]

        # The `--prefix` scheme causes Pip to refuse to install foreign wheels. It assumes those
        # wheels must be compatible with the current venv. Since we just install wheels in
        # individual chroots for later re-assembly on the `sys.path` at runtime or at venv install
        # time, we override this concern by forcing the wheel's tags to be considered compatible
        # with the current Pip install interpreter being used.
        compatible_tags = CompatibilityTags.from_wheel(wheel).extend(
            interpreter.identity.supported_tags
        )
        patch_set = PatchSet.create(foreign_platform.patch_tags(compatible_tags))
        extra_env = dict(patch_set.env)
        extra_sys_path = patch_set.emit_patches(package=self._PATCHES_PACKAGE_NAME)
        if extra_sys_path:
            extra_env["PEX_EXTRA_SYS_PATH"] = extra_sys_path
            extra_env[self._PATCHES_PACKAGE_ENV_VAR_NAME] = self._PATCHES_PACKAGE_NAME
        install_cmd.append("--compile" if compile else "--no-compile")
        install_cmd.append(wheel)

        def fixup_install(returncode):
            if returncode != 0:
                return
            record = Record.from_prefix_install(
                prefix_dir=install_dir,
                project_name=project_name_and_version.project_name,
                version=project_name_and_version.version,
            )
            record.fixup_install(interpreter=interpreter)

        return self._spawn_pip_isolated_job(
            args=install_cmd,
            interpreter=interpreter,
            finalizer=fixup_install,
            extra_env=extra_env,
        )

    def spawn_debug(
        self,
        platform,  # type: Platform
        manylinux=None,  # type: Optional[str]
    ):
        # type: (...) -> Job

        # N.B.: Pip gives fair warning:
        #   WARNING: This command is only meant for debugging. Do not use this with automation for
        #   parsing and getting these details, since the output and options of this command may
        #   change without notice.
        #
        # We suppress the warning by capturing stderr below. The information there will be dumped
        # only if the Pip command fails, which is what we want.

        debug_command = ["debug"]
        debug_command.extend(foreign_platform.iter_platform_args(platform, manylinux=manylinux))
        return self._spawn_pip_isolated_job(
            debug_command, pip_verbosity=1, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
