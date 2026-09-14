"""colcon's install path for this package -- separate from the repo-root
setup.py, which covers the pip/PYTHONPATH install used by everything that
isn't `ros2 run` (see ../../setup.py and CLAUDE.md).

Landed alongside the real node bindings in nodes/servo_driver.py and
nodes/head_behavior.py: colcon skipped this package with COLCON_IGNORE while
those raised NotImplementedError, because a package with no way to run its
executables had nothing for `ros2 run` to find.
"""

from setuptools import find_packages, setup

package_name = "neo_motion"

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
    description="Head arbitration and the servo driver: the only path to the servos.",
    license="MIT",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "servo_driver = neo_motion.nodes.servo_driver:main",
            "head_behavior = neo_motion.nodes.head_behavior:main",
        ],
    },
)
