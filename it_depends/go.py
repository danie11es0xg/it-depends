from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from logging import getLogger
import os
from pathlib import Path
import re
from subprocess import check_call, check_output, DEVNULL, CalledProcessError
from tempfile import TemporaryDirectory
from typing import Iterable, Iterator, List, Optional, Tuple, Union
from urllib import request
from urllib.error import HTTPError, URLError

from semantic_version import Version
from semantic_version.base import BaseSpec, Range, SimpleSpec

from .dependencies import (
    Dependency, DependencyClassifier, DependencyResolver, SourcePackage, SourceRepository, Package, PackageCache,
    SemanticVersion
)
from . import vcs

log = getLogger(__file__)

GITHUB_URL_MATCH = re.compile(r"\s*https?://(www\.)?github.com/([^/]+)/(.+?)(\.git)?\s*", re.IGNORECASE)
REQUIRE_LINE_REGEX = r"\s*([^\s]+)\s+([^\s]+)\s*(//\s*indirect\s*)?"
REQUIRE_LINE_MATCH = re.compile(REQUIRE_LINE_REGEX)
REQUIRE_MATCH = re.compile(fr"\s*require\s+{REQUIRE_LINE_REGEX}")
REQUIRE_BLOCK_MATCH = re.compile(r"\s*require\s+\(\s*")
MODULE_MATCH = re.compile(r"\s*module\s+([^\s]+)\s*")

GOPATH: Optional[str] = os.environ.get("GOPATH", None)


@dataclass(frozen=True, unsafe_hash=True)
class MetaImport:
    prefix: str
    vcs: str
    repo_root: str


class MetadataParser(HTMLParser):
    in_meta: bool = False
    metadata: List[MetaImport] = []

    def error(self, message):
        pass

    def handle_starttag(self, tag, attrs):
        if tag == "meta":
            attrs = dict(attrs)
            if attrs.get("name", "") == "go-import":
                fields = attrs.get("content", "").split(" ")
                if len(fields) == 3:
                    self.metadata.append(MetaImport(*fields))


def git_commit(path: Optional[str] = None) -> Optional[str]:
    try:
        return check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            stderr=DEVNULL
        ).decode("utf-8")
    except CalledProcessError:
        return None


class GoVersion:
    def __init__(self, go_version_string: str):
        self.version_string: str = go_version_string.strip()
        self.build: bool = False  # This is to appease semantic_version.base.SimpleSpec

    def __eq__(self, other):
        return isinstance(other, GoVersion) and self.version_string == other.version_string

    def __hash__(self):
        return hash(self.version_string)

    def __str__(self):
        return self.version_string


@BaseSpec.register_syntax
class GoSpec(SimpleSpec):
    SYNTAX = 'go'

    class Parser(SimpleSpec.Parser):
        @classmethod
        def parse(cls, expression):
            return Range(operator=Range.OP_EQ, target=GoVersion(expression))

    def __contains__(self, item):
        return item == self.clause.target


class GoModule:
    def __init__(self, name: str, dependencies: Iterable[Tuple[str, str]] = ()):
        self.name: str = name
        self.dependencies: List[Tuple[str, str]] = list(dependencies)

    @staticmethod
    def tag_to_git_hash(tag: str) -> str:
        segments = tag.split("-")
        if len(segments) == 3:
            return segments[-1]
        else:
            return tag

    @staticmethod
    def parse_mod(mod_content: Union[str, bytes]) -> "GoModule":
        if isinstance(mod_content, bytes):
            mod_content = mod_content.decode("utf-8")
        in_require = False
        dependencies = []
        name = None
        for line in mod_content.split("\n"):
            if not in_require:
                m = REQUIRE_MATCH.match(line)
                if m:
                    dependencies.append((m.group(1), m.group(2)))
                else:
                    if name is None:
                        m = MODULE_MATCH.match(line)
                        if m:
                            name = m.group(1)
                            continue
                    in_require = bool(REQUIRE_BLOCK_MATCH.match(line))
            elif line.strip() == ")":
                in_require = False
            else:
                m = REQUIRE_LINE_MATCH.match(line)
                if m:
                    dependencies.append((m.group(1), m.group(2)))
        if name is None:
            raise ValueError("Missing `module` line in go mod specification")
        return GoModule(name, dependencies)

    @staticmethod
    def from_github(github_org: str, github_repo: str, tag: str):
        github_url = f"https://raw.githubusercontent.com/{github_org}/{github_repo}/{tag}/go.mod"
        try:
            with request.urlopen(github_url) as response:
                return GoModule.parse_mod(response.read())
        except HTTPError as e:
            if e.code == 404:
                # If there is no `go.mod`, it likely means the package has no dependencies:
                return GoModule(f"github.com/{github_org}/{github_repo}")
            raise

    @staticmethod
    def from_git(import_path: str, git_url: str, tag: str):
        m = GITHUB_URL_MATCH.fullmatch(git_url)
        if m:
            return GoModule.from_github(m.group(2), m.group(3), tag)
        log.info(f"Attempting to clone {git_url}")
        with TemporaryDirectory() as tempdir:
            check_call(["git", "init"], cwd=tempdir, stderr=DEVNULL, stdout=DEVNULL)
            check_call(["git", "remote", "add", "origin", git_url], cwd=tempdir, stderr=DEVNULL, stdout=DEVNULL)
            git_hash = GoModule.tag_to_git_hash(tag)
            env = {
                "GIT_TERMINAL_PROMPT": "0"
            }
            if os.environ.get("GIT_SSH", "") == "" and os.environ.get("GIT_SSH_COMMAND", "") == "":
                # disable any ssh connection pooling by git
                env["GIT_SSH_COMMAND"] = "ssh -o ControlMaster=no"
            try:
                check_call(["git", "fetch", "--depth", "1", "origin", git_hash], cwd=tempdir, stderr=DEVNULL,
                           stdout=DEVNULL, env=env)
            except CalledProcessError:
                # not all git servers support `git fetch --depth 1` on a hash
                try:
                    check_call(["git", "fetch", "origin"], cwd=tempdir, stderr=DEVNULL, stdout=DEVNULL, env=env)
                    check_call(["git", "checkout", git_hash], cwd=tempdir, stderr=DEVNULL, stdout=DEVNULL, env=env)
                except CalledProcessError:
                    log.error(f"Could not clone {git_url} for {import_path!r}")
                    return GoModule(import_path)
            go_mod_path = Path(tempdir) / "go.mod"
            if not go_mod_path.exists():
                # the package likely doesn't have any dependencies
                return GoModule(import_path)
            with open(Path(tempdir) / "go.mod", "r") as f:
                return GoModule.parse_mod(f.read())

    @staticmethod
    def url_for_import_path(import_path: str) -> str:
        """
        returns a partially-populated URL for the given Go import path.

        The URL leaves the Scheme field blank so that web.Get will try any scheme
        allowed by the selected security mode.
        """
        slash = import_path.find("/")
        if slash == -1:
            raise vcs.VCSResolutionError("import path does not contain a slash")
        host, path = import_path[:slash], import_path[slash:]
        if "." not in host:
            raise vcs.VCSResolutionError("import path does not begin with hostname")
        if not path.startswith("/"):
            path = f"/{path}"
        return f"https://{host}{path}?go-get=1"

    @staticmethod
    def meta_imports_for_prefix(import_prefix: str) -> Tuple[str, List[MetaImport]]:
        url = GoModule.url_for_import_path(import_prefix)
        with request.urlopen(url) as req:
            return url, GoModule.parse_meta_go_imports(req.read().decode("utf-8"))

    @staticmethod
    def match_go_import(imports: Iterable[MetaImport], import_path: str) -> MetaImport:
        match: Optional[MetaImport] = None
        for i, m in enumerate(imports):
            if not import_path.startswith(m.prefix):
                continue
            elif match is not None:
                if match.vcs == "mod" and m.vcs != "mod":
                    break
                raise ValueError(f"Multiple meta tags match import path {import_path!r}")
            match = m
        if match is None:
            raise ValueError(f"Unable to match import path {import_path!r}")
        return match

    @staticmethod
    def parse_meta_go_imports(metadata: str) -> List[MetaImport]:
        parser = MetadataParser()
        parser.feed(metadata)
        return parser.metadata

    @staticmethod
    def repo_root_for_import_dynamic(import_path: str) -> vcs.Repository:
        url = GoModule.url_for_import_path(import_path)
        try:
            imports = GoModule.parse_meta_go_imports(request.urlopen(url).read().decode("utf-8"))
        except (HTTPError, URLError):
            raise ValueError(f"Could not download metadata from {url} for import {import_path!s}")
        meta_import = GoModule.match_go_import(imports, import_path)
        if meta_import.prefix != import_path:
            new_url, imports = GoModule.meta_imports_for_prefix(meta_import.prefix)
            meta_import2 = GoModule.match_go_import(imports, import_path)
            if meta_import != meta_import2:
                raise ValueError(f"{url} and {new_url} disagree about go-import for {meta_import.prefix!r}")
        # validateRepoRoot(meta_import.RepoRoot)
        if meta_import.vcs == "mod":
            the_vcs = vcs.VCS_MOD
        else:
            the_vcs = vcs.vcs_by_cmd(meta_import.vcs)  # type: ignore
            if the_vcs is None:
                raise ValueError(f"{url}: unknown VCS {meta_import.vcs!r}")
        vcs.check_go_vcs(the_vcs, meta_import.prefix)
        return vcs.Repository(repo=meta_import.repo_root, root=meta_import.prefix, is_custom=True, vcs=the_vcs)

    @staticmethod
    def repo_root_for_import_path(import_path: str) -> vcs.Repository:
        try:
            return vcs.resolve(import_path)
        except vcs.VCSResolutionError:
            pass
        return GoModule.repo_root_for_import_dynamic(import_path)

    @staticmethod
    def from_import(import_path: str, tag: str) -> "GoModule":
        try:
            repo = GoModule.repo_root_for_import_path(import_path)
        except ValueError as e:
            log.warning(str(e))
            return GoModule(import_path)
        if repo.vcs.name == "Git":
            return GoModule.from_git(import_path, repo.repo, tag)
        else:
            raise NotImplementedError(f"TODO: add support for VCS type {repo.vcs.name}")

    @staticmethod
    def load(name_or_url: str, tag: str = "master") -> "GoModule":
        if not name_or_url.startswith("http://") and not name_or_url.startswith("https://"):
            return GoModule.from_import(name_or_url, tag)
        else:
            return GoModule.from_git(name_or_url, name_or_url, tag)


class GoResolver(DependencyResolver):
    def __init__(self, cache: Optional[PackageCache] = None):
        super().__init__(source=GoClassifier(), cache=cache)

    def resolve_missing(self, dependency: Dependency, from_package: Optional[Package] = None) -> Iterator[Package]:
        assert isinstance(dependency.semantic_version, GoSpec)
        version_string = str(dependency.semantic_version)
        module = GoModule.from_import(dependency.package, version_string)
        yield Package(
            name=module.name,
            version=GoVersion(version_string),  # type: ignore
            source=self.source,
            dependencies=[
                Dependency(package=package, semantic_version=GoSpec(version), source=self.source)
                for package, version in module.dependencies
            ]
        )


class GoClassifier(DependencyClassifier):
    name = "go"
    description = "classifies the dependencies of JavaScript packages using `npm`"

    @classmethod
    def parse_spec(cls, spec: str) -> SemanticVersion:
        return GoSpec(spec)

    @classmethod
    def parse_version(cls, version_string: str) -> Version:
        return GoVersion(version_string)  # type: ignore

    def can_classify(self, repo: SourceRepository) -> bool:
        return (repo.path / "go.mod").exists()

    def classify(self, repo: SourceRepository, cache: Optional[PackageCache] = None):
        with open(repo.path / "go.mod") as f:
            module = GoModule.parse_mod(f.read())
        git_hash = git_commit(str(repo.path))
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        version = f"v0.0.0-{timestamp}-"
        if git_hash is None:
            version = f"{version}????"
        else:
            version = f"{version}{git_hash}"
        repo.add(SourcePackage(
            name=module.name,
            version=GoVersion(version),  # type: ignore
            source_path=repo.path,
            source=self,
            dependencies=[
                Dependency(package=package, semantic_version=GoSpec(version), source=self)
                for package, version in module.dependencies
            ]
        ))
        GoResolver(cache=cache).resolve_unsatisfied(repo)
