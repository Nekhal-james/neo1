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
    install_requires=["setuptools", "pyyaml>=6.0", "requests>=2.31"],
    extras_require={
        "dev": ["pytest>=8.0"],
    },
    zip_safe=True,
    maintainer="Nekhal James",
    maintainer_email="funkydude450@gmail.com",
    description="CLI for the off-board Qwen 2.5 host/receiver link.",
    license="MIT",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "neo = model_conn.__main__:main",
        ],
    },
)
