"""colcon's install path for this package -- separate from the repo-root
setup.py, which covers the pip/PYTHONPATH install used by everything that
isn't `ros2 run` (see ../../setup.py and CLAUDE.md).

Landed alongside the real node binding in nodes/perception_node.py: colcon
skipped this package with COLCON_IGNORE while that node raised
NotImplementedError, because a package with no way to run its executable had
nothing for `ros2 run` to find.
"""

from setuptools import find_packages, setup

package_name = "neo_perception"

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
    description="Person detection, pose-based gesture recognition, and gaze engagement.",
    license="MIT",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "perception_node = neo_perception.nodes.perception_node:main",
        ],
    },
)
