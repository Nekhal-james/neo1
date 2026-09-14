"""colcon's install path for this package -- separate from the repo-root
setup.py, which covers the pip/PYTHONPATH install used by everything that
isn't `ros2 run` (see ../../setup.py and CLAUDE.md).

Landed alongside the real node binding in model_conn/nodes/link_node.py:
colcon skipped this package with COLCON_IGNORE while that node had no rclpy
publisher and was not instantiated by any console_script.

Directory name keeps the hyphen (model-conn) to match the rest of the repo
layout; the importable package and this colcon package are both model_conn,
per package.xml.
"""

from setuptools import find_packages, setup

package_name = "model_conn"

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
    description=(
        "CLI for the off-board Qwen 2.5 host/receiver link: serves the model "
        "(host) and checks the link (receiver)."
    ),
    license="MIT",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "link_node = model_conn.nodes.link_node:main",
        ],
    },
)
