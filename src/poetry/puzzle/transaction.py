from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING
from typing import Any

from poetry.utils.extras import get_extra_package_names


if TYPE_CHECKING:
    from packaging.utils import NormalizedName
    from poetry.core.packages.package import Package

    from poetry.installation.operations.operation import Operation
    from poetry.packages.transitive_package_info import TransitivePackageInfo


class Transaction:
    def __init__(
        self,
        current_packages: list[Package],
        result_packages: list[Package] | dict[Package, TransitivePackageInfo],
        installed_packages: list[Package] | None = None,
        root_package: Package | None = None,
        marker_env: dict[str, Any] | None = None,
        groups: set[str] | None = None,
    ) -> None:
        self._current_packages = current_packages
        self._result_packages = result_packages

        if installed_packages is None:
            installed_packages = []

        self._installed_packages = installed_packages
        self._root_package = root_package
        self._marker_env = marker_env
        self._groups = groups

    def get_solved_packages(self) -> dict[Package, TransitivePackageInfo]:
        assert isinstance(self._result_packages, dict)
        return self._result_packages

    def calculate_operations(
        self,
        with_uninstalls: bool = True,
        synchronize: bool = False,
        *,
        skip_directory: bool = False,
        extras: set[NormalizedName] | None = None,
    ) -> list[Operation]:
        from poetry.installation.operations import Install
        from poetry.installation.operations import Uninstall
        from poetry.installation.operations import Update

        operations: list[Operation] = []

        extra_packages: set[NormalizedName] = set()
        if self._marker_env:
            marker_env_with_extras = self._marker_env.copy()
            if extras is not None:
                marker_env_with_extras["extra"] = extras
        elif extras is not None:
            assert self._root_package is not None
            extra_packages = get_extra_package_names(
                self._result_packages,
                {k: [d.name for d in v] for k, v in self._root_package.extras.items()},
                extras,
            )

        if isinstance(self._result_packages, dict):
            priorities = {
                pkg: info.depth for pkg, info in self._result_packages.items()
            }
        else:
            priorities = defaultdict(int)
        relevant_result_packages: set[NormalizedName] = set()
        uninstalls: set[NormalizedName] = set()
        for result_package in self._result_packages:
            is_unsolicited_extra = False
            if self._marker_env:
                assert self._groups is not None
                assert isinstance(self._result_packages, dict)
                info = self._result_packages[result_package]

                if info.groups & self._groups and info.get_marker(
                    self._groups
                ).validate(marker_env_with_extras):
                    relevant_result_packages.add(result_package.name)
                elif result_package.optional:
                    is_unsolicited_extra = True
                else:
                    continue
            else:
                relevant_result_packages.add(result_package.name)
                is_unsolicited_extra = extras is not None and (
                    result_package.optional
                    and result_package.name not in extra_packages
                )

            installed = False
            for installed_package in self._installed_packages:
                if result_package.name == installed_package.name:
                    installed = True

                    # Extras that were not requested are always uninstalled.
                    if is_unsolicited_extra:
                        uninstalls.add(installed_package.name)
                        operations.append(Uninstall(installed_package))

                    # We have to perform an update if the version or another
                    # attribute of the package has changed (source type, url, ref, ...).
                    elif result_package.version != installed_package.version or (
                        (
                            # This has to be done because installed packages cannot
                            # have type "legacy". If a package with type "legacy"
                            # is installed, the installed package has no source_type.
                            # Thus, if installed_package has no source_type and
                            # the result_package has source_type "legacy" (negation of
                            # the following condition), update must not be performed.
                            # This quirk has the side effect that when switching
                            # from PyPI to legacy (or vice versa),
                            # no update is performed.
                            installed_package.source_type
                            or result_package.source_type != "legacy"
                        )
                        and not result_package.is_same_package_as(installed_package)
                    ):
                        operations.append(
                            Update(
                                installed_package,
                                result_package,
                                priority=priorities[result_package],
                            )
                        )
                    else:
                        operations.append(
                            Install(result_package).skip("Already installed")
                        )

                    break

            if not (
                installed
                or (skip_directory and result_package.source_type == "directory")
            ):
                op = Install(result_package, priority=priorities[result_package])
                if is_unsolicited_extra:
                    op.skip("Not required")
                operations.append(op)

        if with_uninstalls:
            for current_package in self._current_packages:
                found = current_package.name in (relevant_result_packages | uninstalls)

                if not found:
                    for installed_package in self._installed_packages:
                        if installed_package.name == current_package.name:
                            uninstalls.add(installed_package.name)
                            operations.append(Uninstall(installed_package))

            if synchronize:
                # We preserve pip when not managed by poetry, this is done to avoid
                # externally managed virtual environments causing unnecessary removals.
                preserved_package_names = {"pip"} - relevant_result_packages

                for installed_package in self._installed_packages:
                    if installed_package.name in uninstalls:
                        continue

                    if (
                        self._root_package
                        and installed_package.name == self._root_package.name
                    ):
                        continue

                    if installed_package.name in preserved_package_names:
                        continue

                    if installed_package.name not in relevant_result_packages:
                        uninstalls.add(installed_package.name)
                        operations.append(Uninstall(installed_package))

        return sorted(
            operations,
            key=lambda o: (
                -o.priority,
                o.package.name,
                o.package.version,
            ),
        )
