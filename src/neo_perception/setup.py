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
    extras_require={
        # The core (tracking, gestures, engagement, gaze) is pure Python and needs
        # none of this. Only the real detector backend does.
        "detector": ["ultralytics>=8.1", "opencv-python-headless>=4.9", "numpy>=1.24"],
        "dev": ["pytest>=8.0"],
    },
    zip_safe=True,
    maintainer="Nekhal James",
    maintainer_email="funkydude450@gmail.com",
    description="Person detection, pose-based gestures, and gaze engagement for Neo.",
    license="MIT",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "neo-perception-bench = neo_perception.scripts.bench:main",
            "perception_node = neo_perception.nodes.perception_node:main",
        ],
    },
)
