import setuptools

with open("README.md", "r") as fh:
    long_description = fh.read()

with open("LXMF/_version.py", "r") as fh:
    exec(fh.read())

setuptools.setup(
    name="lxmf",
    version=__version__,
    author="Mark Qvist",
    author_email="mark@unsigned.io",
    description="Lightweight Extensible Message Format for Reticulum",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/markqvist/lxmf",
    packages=["LXMF", "LXMF.Utilities"],
    license="Reticulum License",
    license_files = ("LICENSE"),
    classifiers=[
        "Programming Language :: Python :: 3",
        "Operating System :: OS Independent",
    ],
    entry_points= {
        'console_scripts': [
            'lxmd=LXMF.Utilities.lxmd:main',
        ]
    },
    install_requires=["rns>=0.9.5"],
    python_requires=">=3.7",
)
