import json
from pathlib import Path
import shutil
import subprocess
from typing import Dict, Iterable, Iterator

from semantic_version import NpmSpec, SimpleSpec, Version

from .dependencies import (
    ClassifierAvailability, Dependency, DependencyClassifier, DependencyResolver, DockerSetup, Package, SemanticVersion
)


class NPMResolver(DependencyResolver):
    @staticmethod
    def from_package_json(package_json_path: str) -> "NPMResolver":
        path: Path = Path(package_json_path)
        if path.is_dir():
            path = path / "package.json"
        if not path.exists():
            raise ValueError(f"Expected a package.json file at {path!s}")
        with open(path, "r") as json_file:
            package = json.load(json_file)
        if "name" not in package:
            raise ValueError(f"Expected \"name\" key in {path!s}")
        if "dependencies" in package:
            dependencies: Dict[str, str] = package["dependencies"]
        else:
            dependencies = {}
        if "version" in package:
            version = package["version"]
        else:
            version = "0"
        version = Version.coerce(version)
        resolver = NPMResolver([
            Package(package["name"], version, source="npm", dependencies=(
                Dependency(package=dep_name, semantic_version=NPMResolver.parse_spec(dep_version))
                for dep_name, dep_version in dependencies.items()
            ))
        ], source=NPMClassifier.default_instance())
        resolver.resolve_unsatisfied()
        return resolver

    @staticmethod
    def parse_spec(spec: str) -> SemanticVersion:
        try:
            return NpmSpec(spec)
        except ValueError:
            pass
        try:
            return SimpleSpec(spec)
        except ValueError:
            pass
        # Sometimes NPM specs have whitespace, which trips up the parser
        no_whitespace = "".join(c for c in spec if c != " ")
        if no_whitespace != spec:
            return NPMResolver.parse_spec(no_whitespace)

    def resolve_missing(self, dependency: Dependency) -> Iterator[Package]:
        """Yields all packages that satisfy the dependency without expanding those packages' dependencies"""
        try:
            output = subprocess.check_output(["npm", "view", "--json", f"{dependency.package}@{dependency.semantic_version!s}", "dependencies"])
        except subprocess.CalledProcessError as e:
            raise ValueError(f"Error running `npm view --json {dependency.package}@{dependency.semantic_version!s} dependencies`: {e!s}")
        if len(output.strip()) == 0:
            # this means the package has no dependencies
            deps = {}
        else:
            try:
                deps = json.loads(output)
            except ValueError as e:
                raise ValueError(
                    f"Error parsing output of `npm view --json {dependency.package}@{dependency.semantic_version!s} dependencies`: {e!s}"
                )
        if isinstance(deps, list):
            # this means that there are multiple dependencies that match the version
            in_data = False
            versions = []
            for line in subprocess.check_output(
                    ["npm", "view", f"{dependency.package}@{dependency.semantic_version!s}", "dependencies"]
            ).splitlines():
                line = line.decode("utf-8").strip()
                if in_data:
                    if line.endswith("}"):
                        in_data = False
                    continue
                elif line.startswith("{"):
                    in_data = True
                else:
                    versions.append(line)
            for pkg_version, dep_dict in zip(versions, deps):
                version = Version.coerce(pkg_version[len(dependency.package)+1:])
                yield Package(name=dependency.package, version=version, source="npm", dependencies=(
                    Dependency(package=dep, semantic_version=NPMResolver.parse_spec(dep_version))
                    for dep, dep_version in dep_dict.items()
                ))
        else:
            try:
                output = subprocess.check_output(
                    ["npm", "view", "--json", f"{dependency.package}@{dependency.semantic_version!s}", "versions"])
            except subprocess.CalledProcessError as e:
                raise ValueError(
                    f"Error running `npm view --json {dependency.package}@{dependency.semantic_version!s} versions`: {e!s}")
            if len(output.strip()) == 0:
                # no available versions!
                return
            try:
                version_list = json.loads(output)
            except ValueError as e:
                raise ValueError(
                    f"Error parsing output of `npm view --json {dependency.package}@{dependency.semantic_version!s} versions`: {e!s}"
                )
            while version_list and isinstance(version_list[0], list):
                # TODO: Figure out why sometimes `npm view` returns a list of lists 🤷
                version_list = version_list[0]
            for version_string in version_list:
                try:
                    version = Version.coerce(version_string)
                except ValueError:
                    continue
                if version in dependency.semantic_version:
                    yield Package(name=dependency.package, version=version, source="npm", dependencies=(
                        Dependency(package=dep, semantic_version=NPMResolver.parse_spec(dep_version))
                        for dep, dep_version in deps.items()
                    ))


class NPMClassifier(DependencyClassifier):
    name = "npm"
    description = "classifies the dependencies of JavaScript packages using `npm`"

    def is_available(self) -> ClassifierAvailability:
        if shutil.which("npm") is None:
            return ClassifierAvailability(False, "`npm` does not appear to be installed! "
                                                 "Make sure it is installed and in the PATH.")
        return ClassifierAvailability(True)

    def can_classify(self, path: str) -> bool:
        return (Path(path) / "package.json").exists()

    def classify(self, path: str, resolvers: Iterable[DependencyResolver] = ()) -> NPMResolver:
        return NPMResolver.from_package_json(path)

    def docker_setup(self) -> DockerSetup:
        return DockerSetup(
            apt_get_packages=["npm"],
            install_package_script="""#!/usr/bin/env bash
npm install $1@$2
""",
            load_package_script="""#!/usr/bin/env bash
node -e "require(\\"$1\\")"
""",
            baseline_script="#!/usr/bin/env node -e \"\"\n"
        )
