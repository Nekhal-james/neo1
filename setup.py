"""The one way to pip-install this repo.

Explicit static packages/package_dir, not setuptools' `packages.find` across
multiple `src/*` roots: that dynamic-discovery path depends on setuptools/pip
version behaviour that isn't consistent everywhere, and it silently produced
an editable install missing neo_webapp on at least one real machine. A plain
setup.py with a fixed package list has worked unambiguously since forever,
which is what this repo actually needs -- three source trees, not many.

Packages under src/ keep their own package.xml (ROS metadata for a future
colcon build) but have no setup.py of their own, so this file is the only
install path -- see ../README.md.
"""

from setuptools import setup

setup(
    name="neo",
    version="0.1.0",
    description=(
        "Neo campus receptionist robot: admin panel, perception, and the "
        "off-board model connection CLI."
    ),
    license="MIT",
    maintainer="Nekhal James",
    maintainer_email="funkydude450@gmail.com",
    python_requires=">=3.10",
    packages=[
        "model_conn",
        "model_conn.nodes",
        "neo_webapp",
        "neo_webapp.api",
        "neo_webapp.bridge",
        "neo_webapp.media",
        "neo_webapp.scripts",
        "neo_perception",
        "neo_perception.nodes",
        "neo_perception.scripts",
        "intelligence",
        "intelligence.nodes",
        "neo_motion",
        "neo_motion.nodes",
        "neo_emotion",
        "neo_emotion.nodes",
    ],
    package_dir={
        "model_conn": "src/model-conn/model_conn",
        "neo_webapp": "src/neo_webapp/neo_webapp",
        "neo_perception": "src/neo_perception/neo_perception",
        "intelligence": "src/intelligence/intelligence",
        "neo_motion": "src/neo_motion/neo_motion",
        "neo_emotion": "src/neo_emotion/neo_emotion",
    },
    package_data={
        "neo_webapp": ["ui/*.html", "ui/*.css", "ui/*.js"],
        "intelligence": ["prompts/*.txt"],
    },
    include_package_data=True,
    install_requires=[
        # model_conn
        "pyyaml>=6.0",
        "requests>=2.31",
        # neo_webapp
        "fastapi>=0.110",
        "uvicorn[standard]>=0.27",
        "argon2-cffi>=23.1",
        "itsdangerous>=2.1",
        "python-multipart>=0.0.9",
        # intelligence's chat.py -- pure requests, no extra needed
    ],
    extras_require={
        "dev": [
            "pytest>=8.0",
            "pytest-asyncio>=0.23",
            "httpx>=0.27",
            "psutil>=5.9",
            "cryptography>=42.0",
        ],
        # neo_perception's tracking/gestures/engagement core is pure Python;
        # only the real YOLO detector backend needs this.
        "detector": ["ultralytics>=8.1", "opencv-python-headless>=4.9", "numpy>=1.24"],
        # intelligence's chat.py works with none of this; only real ASR/TTS need it.
        "voice": ["vosk>=0.3.45", "piper-tts>=1.2"],
    },
    entry_points={
        "console_scripts": [
            "neo = model_conn.__main__:main",
            "neo-perception-bench = neo_perception.scripts.bench:main",
        ],
    },
    zip_safe=False,
)
