"""colcon's install path for this package -- separate from the repo-root
setup.py, which covers the pip/PYTHONPATH install used by everything that
isn't `ros2 run` (see ../../setup.py and CLAUDE.md).

Landed alongside the real node binding in intelligence/nodes/dialog_node.py:
colcon skipped this package with COLCON_IGNORE while that node had no rclpy
pub/sub and was not instantiated by any console_script.
"""

from setuptools import find_packages, setup

package_name = "intelligence"

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
    description="Prompts, RAG data, and chat/ASR/TTS for the Neo assistant, reached via model_conn's link.",
    license="MIT",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "dialog_node = intelligence.nodes.dialog_node:main",
        ],
    },
)
