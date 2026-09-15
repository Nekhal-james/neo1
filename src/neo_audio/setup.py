"""colcon install path for neo_audio -- separate from the repo-root setup.py,
which covers the pip/PYTHONPATH install everything that is not  uses
(see ../../setup.py and CLAUDE.md)."""

from setuptools import find_packages, setup

package_name = "neo_audio"

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
    description="The microphone, the speaker, the wake word, and speech to text.",
    license="MIT",
    tests_require=["pytest"],
    entry_points={
        "console_scripts": [
            "mic_hw = neo_audio.nodes.mic_hw:main",
            "speaker_hw = neo_audio.nodes.speaker_hw:main",
            "wake_word = neo_audio.nodes.wake_word:main",
            "asr_router = neo_audio.nodes.asr_router:main",
            "tts = neo_audio.nodes.tts:main",
        ],
    },
)
