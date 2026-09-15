"""colcon install path for neo_sources -- see ../../setup.py and CLAUDE.md for
why this is separate from the repo-root pip install."""

from setuptools import find_packages, setup

package_name = "neo_sources"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["tests", "tests.*"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="Nekhal James",
    maintainer_email="funkydude450@gmail.com",
    description="Owns which backend -- the Pi hardware or the browser -- is live per stream.",
    license="MIT",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "source_manager = neo_sources.nodes.source_manager:main",
        ],
    },
)
