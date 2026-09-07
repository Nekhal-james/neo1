from setuptools import find_packages, setup

package_name = "neo_webapp"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["tests", "tests.*"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=[
        "setuptools",
        "fastapi>=0.110",
        "uvicorn[standard]>=0.27",
        "argon2-cffi>=23.1",
        "itsdangerous>=2.1",
        "pyyaml>=6.0",
        "python-multipart>=0.0.9",
    ],
    extras_require={
        # Only needed to mint the local development certificate.
        "dev": ["cryptography>=42.0", "pytest>=8.0", "httpx>=0.27", "psutil>=5.9"],
    },
    zip_safe=True,
    maintainer="Nekhal James",
    maintainer_email="funkydude450@gmail.com",
    description="Neo admin panel: always-on light API plus an on-demand media bridge.",
    license="MIT",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "neo-webapp = neo_webapp.__main__:main",
            "neo-webapp-setup = neo_webapp.scripts.setup_admin:main",
            "neo-webapp-devcert = neo_webapp.scripts.make_dev_cert:main",
        ],
    },
    include_package_data=True,
    package_data={"neo_webapp": ["ui/*.html", "ui/*.css", "ui/*.js"]},
)
