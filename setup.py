from setuptools import setup, find_packages

setup(
    name="simple-sip-client",
    version="0.1.0",
    description="Pure-Python SIP VoIP client library",
    author="simple-sip",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    python_requires=">=3.10",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Topic :: Communications :: Telephony",
    ],
)
