from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="tgmesh-bridge",
    version="0.2.0",
    author="Dmitriy Lyalyuev",
    author_email="dmitriy@workflowy.com",
    description="A bridge between Meshtastic and Telegram",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/DmitriyLyalyuev/tgmesh-bridge",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: POSIX :: Linux",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.12",
    ],
    python_requires=">=3.12",
    install_requires=[
        "meshtastic",
        "python-telegram-bot",
        "py-staticmaps",
        "Pillow",
        "httpx",
    ],
    entry_points={
        "console_scripts": [
            "tgmesh-bridge=tgmesh_bridge:main",
        ],
    },
)
