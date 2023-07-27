#!/usr/bin/env python3
# encoding: utf-8
"""Reolink NVR/cameras API package."""
import pathlib
from setuptools import find_packages, setup

# The directory containing this file
HERE = pathlib.Path(__file__).parent

# The text of the README file
README = (HERE / "README.md").read_text()

setup(name='reolink_aio',
      version='0.7.5',
      description='Reolink NVR/cameras API package',
      long_description=README,
      long_description_content_type="text/markdown",
      url='https://github.com/starkillerOG/reolink_aio',
      author='starkillerOG',
      author_email='starkiller.og@gmail.com',
      license='MIT',
      packages=find_packages(),
      python_requires='>=3.9',
      install_requires=[
        'ffmpeg',
        'requests',
        'aiohttp',
        'orjson'
        ],
      tests_require=[],
      platforms=['any'],
      zip_safe=False,
      classifiers=[
          "Development Status :: 5 - Production/Stable",
          "Intended Audience :: Developers",
          "License :: OSI Approved :: MIT License",
          "Operating System :: OS Independent",
          "Topic :: Software Development :: Libraries",
          "Topic :: Home Automation",
          "Programming Language :: Python",
          "Programming Language :: Python :: 3.9",
          "Programming Language :: Python :: 3.10",
          "Programming Language :: Python :: 3.11",
          ])
